"""Screenshot import: Claude extraction (with a fake API client — tests stay
offline), normalisation/masking, and the upload → review → import flow."""

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.account import Account
from app.models.tax_lot import TaxLot
from app.services import screenshot_import as si

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64

EXTRACTED = {
    "account": {"institution": "Fidelity", "account_name": "Individual 123456789",
                "account_type": "brokerage", "account_last4": "Z12-3456789", "as_of_date": "2026-10-20"},
    "positions": [
        {"symbol": "VOO", "name": "Vanguard S&P 500 ETF", "asset_class": "us_equity", "quantity": 100,
         "price": 550.0, "market_value": 55000.0, "cost_basis_total": 41000.0, "confidence": "high", "note": None},
        {"symbol": "voo", "name": "Vanguard S&P 500 ETF", "asset_class": "us_equity", "quantity": 100,
         "price": 550.0, "market_value": 55000.0, "cost_basis_total": 41000.0, "confidence": "high", "note": None},
        {"symbol": "BND", "name": "Vanguard Total Bond", "asset_class": "fixed_income", "quantity": None,
         "price": 72.5, "market_value": 7250.0, "cost_basis_total": None, "confidence": "low",
         "note": "quantity column cut off"},
        {"symbol": "AAPL", "name": "Apple", "asset_class": "us_equity", "quantity": 10, "price": 200.0,
         "market_value": 2500.0, "cost_basis_total": 1500.0, "confidence": "high", "note": None},
    ],
    "cash_balance": 1234.56,
    "notes": "Last row may be cut off.",
}


class FakeClient:
    def __init__(self, payload=EXTRACTED, stop_reason="end_turn"):
        self.payload, self.stop_reason, self.calls = payload, stop_reason, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="text", text=json.dumps(self.payload))])


def test_request_shape_and_masking():
    client = FakeClient()
    out = si.extract_positions([(PNG, "image/png"), (PNG, "image/png")], client=client)
    kw = client.calls[0]
    images = [b for b in kw["messages"][0]["content"] if b["type"] == "image"]
    assert len(images) == 2 and images[0]["source"]["media_type"] == "image/png"
    assert kw["output_config"]["format"]["schema"] == si.EXTRACTION_SCHEMA
    assert kw["extra_body"] == {"fallbacks": "default"}
    assert out["account"]["account_last4"] == "6789"              # never more than 4 digits
    assert "123456789" not in out["account"]["account_name"]


def test_normalize_dedupes_fills_and_flags():
    rows = {r["symbol"]: r for r in si.normalize(EXTRACTED)["rows"]}
    assert list(rows) == ["VOO", "BND", "AAPL", "CASH"]          # duplicate VOO merged
    assert rows["BND"]["quantity"] == Decimal("100.0000")        # 7250 / 72.5
    assert rows["BND"]["issues"]                                 # low confidence flagged
    assert any("doesn't match" in i for i in rows["AAPL"]["issues"])   # 10 × 200 ≠ 2,500
    assert rows["CASH"]["quantity"] == Decimal("1234.56") and rows["CASH"]["asset_class"] == "cash"


def test_refusal_and_bad_input_are_user_errors():
    with pytest.raises(si.ScreenshotImportError):
        si.extract_positions([(PNG, "image/png")], client=FakeClient(stop_reason="refusal"))
    with pytest.raises(si.ScreenshotImportError):
        si.extract_positions([(b"x", "application/pdf")], client=FakeClient())
    with pytest.raises(si.ScreenshotImportError):
        si.extract_positions([], client=FakeClient())


def test_missing_api_key_message(monkeypatch):
    monkeypatch.setattr(si, "_api_key", lambda: "")
    with pytest.raises(si.ScreenshotImportError, match="API key"):
        si.extract_positions([(PNG, "image/png")])


def test_upload_review_and_import(test_client, engine, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setattr(si, "extract_positions", lambda images: si.normalize(EXTRACTED))
    r = test_client.post("/settings/import-screenshots",
                         files=[("files", ("a.png", PNG, "image/png"))])
    assert r.status_code == 200
    assert "Review screenshot import" in r.text and "quantity column cut off" in r.text
    assert "123456789" not in r.text

    form = {"target": "new", "account_name": "Fidelity Individual …6789",
            "account_type": "brokerage", "institution": "Fidelity",
            "include": ["0", "1", "3"],                             # skip AAPL (index 2)
            "symbol": ["VOO", "BND", "AAPL", "CASH"], "name": ["VOO", "BND", "Apple", "Cash"],
            "asset_class": ["us_equity", "fixed_income", "us_equity", "cash"],
            "quantity": ["100", "100", "10", "1234.56"], "price": ["550", "72.5", "200", "1"],
            "cost_basis_total": ["41000", "", "1500", "1234.56"]}
    r = test_client.post("/settings/import-screenshots/confirm", data=form, follow_redirects=False)
    assert r.status_code == 303 and "Imported+3+positions" in r.headers["location"]

    s = sessionmaker(bind=engine)()
    acct = s.query(Account).filter_by(name="Fidelity Individual …6789").one()
    lots = s.query(TaxLot).filter_by(account_id=acct.id).all()
    assert len(lots) == 3 and acct.is_taxable


def test_damaged_sdk_install_gives_a_plain_message(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def broken(name, *a, **kw):
        if name == "anthropic":
            raise ImportError("cannot import name 'epydoc' from partially initialized module")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(si, "_api_key", lambda: "sk-test")
    monkeypatch.setattr(builtins, "__import__", broken)
    with pytest.raises(si.ScreenshotImportError, match="relaunch"):
        si.extract_positions([(PNG, "image/png")])
