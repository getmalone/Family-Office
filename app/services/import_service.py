"""
Generic importer for opening accounts and positions from CSV, JSON, or XML.

Lets a new user populate an empty database from a brokerage/spreadsheet export
instead of entering everything by hand. Positions are imported as *opening tax
lots* (an acquisition Transaction + a TaxLot), the same snapshot approach used by
the private broker-specific importers.

All three formats share the same field names. Only ``account`` and ``quantity``
are mandatory; everything else is optional:

    account, account_type, institution, symbol, name, asset_class,
    quantity, cost_basis_total, cost_per_share, acquired, price

Accepted shapes:
  • CSV  — header row + one position per line.
  • JSON — a list of position objects; or {"positions": [...]}; or a nested
           {"accounts": [{"account": "...", "positions": [...]}]} form.
  • XML  — <positions><position>…</position></positions> (field per child element
           or attribute); or a nested <accounts><account><positions>… form.
"""

from __future__ import annotations

import csv
import io
import json
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.models.account import Account, AccountTypeEnum
from app.models.asset import Asset, AssetClassEnum, AssetPrice
from app.models.tax_lot import TaxLot
from app.models.transaction import Transaction, TransactionTypeEnum
from app.services.market_data import STABLE_VALUE_SYMBOLS

_NON_TAXABLE = {
    AccountTypeEnum.IRA_TRADITIONAL,
    AccountTypeEnum.IRA_ROTH,
    AccountTypeEnum.FOUR01K,
    AccountTypeEnum.HSA,
    AccountTypeEnum.FIVE29,
}

# Element/key names that wrap a list of positions, or a list of accounts.
_POSITION_KEYS = ("positions", "holdings", "lots", "rows", "records", "data")
_ACCOUNT_KEYS = ("accounts",)
# Account-level fields propagated onto each child position when flattening.
_ACCOUNT_FIELDS = ("account", "account_name", "name", "account_type", "institution")


# ── value parsing ───────────────────────────────────────────────────────────

def _dec(value) -> Decimal | None:
    if value is None:
        return None
    v = str(value).strip().replace("$", "").replace(",", "").replace("%", "")
    if not v or v in ("--", "N/A"):
        return None
    try:
        return Decimal(v)
    except InvalidOperation:
        return None


def _parse_date(value) -> date:
    if value:
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
            try:
                return datetime.strptime(str(value).strip(), fmt).date()
            except ValueError:
                continue
    return date.today()


def _enum(enum_cls, value, default):
    if value:
        v = str(value).strip().lower()
        for member in enum_cls:
            if member.value == v:
                return member
    return default


def _norm(d: dict) -> dict[str, str]:
    """Lowercase keys, stringify+strip values."""
    return {
        (k or "").strip().lower(): ("" if v is None else str(v).strip())
        for k, v in d.items()
    }


# ── format parsers → list of normalized row dicts ───────────────────────────

def parse_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    return [_norm(row) for row in reader]


def _flatten_accounts(items: list) -> list[dict]:
    """Turn a list of account objects (each holding positions) into flat rows.

    Operates on the raw structure (does not stringify) so nested position lists
    survive; only leaf account fields are propagated onto each position row.
    """
    rows: list[dict] = []
    for acct in items:
        if not isinstance(acct, dict):
            continue
        lower = {k.lower(): v for k, v in acct.items()}
        positions = None
        for key in _POSITION_KEYS:
            if isinstance(lower.get(key), list):
                positions = lower[key]
                break
        # Account-level scalar fields propagated to each child position.
        base = _norm({k: v for k, v in lower.items()
                      if k in _ACCOUNT_FIELDS and not isinstance(v, (list, dict))})
        if "name" in base and "account" not in base:
            base["account"] = base.pop("name")
        if positions is None:
            rows.append(_norm({k: v for k, v in lower.items()
                               if not isinstance(v, (list, dict))}))
            continue
        for pos in positions:
            if isinstance(pos, dict):
                rows.append({**base, **_norm(pos)})
    return rows


def parse_json(text: str) -> list[dict]:
    data = json.loads(text)
    if isinstance(data, list):
        # Could be positions, or account objects containing positions.
        if data and isinstance(data[0], dict) and any(
            k in {kk.lower() for kk in data[0]} for k in _POSITION_KEYS
        ):
            return _flatten_accounts(data)
        return [_norm(r) for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        lower = {k.lower(): v for k, v in data.items()}
        for key in _ACCOUNT_KEYS:
            if key in lower and isinstance(lower[key], list):
                return _flatten_accounts(lower[key])
        for key in _POSITION_KEYS:
            if key in lower and isinstance(lower[key], list):
                return [_norm(r) for r in lower[key] if isinstance(r, dict)]
        return [_norm(data)]
    return []


def _xml_element_to_dict(el: ET.Element) -> dict:
    """Combine an element's attributes and simple child elements into a dict."""
    out = dict(el.attrib)
    for child in el:
        if len(child) == 0:  # leaf node → field
            out[child.tag] = child.text
    return out


def parse_xml(text: str) -> list[dict]:
    # Block DTDs / entity definitions (billion-laughs / XXE) before parsing.
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise ValueError("XML with a DTD or entity definitions is not allowed")
    root = ET.fromstring(text)

    def tag(el):
        return el.tag.split("}")[-1].lower()  # strip namespace

    # Nested accounts form: <account> elements that contain position children.
    account_els = [e for e in root.iter() if tag(e) in ("account",)]
    nested = []
    for ae in account_els:
        children = [c for c in ae if tag(c) in ("positions", "holdings", "lots")]
        pos_parents = children or [ae]
        positions = [c for parent in pos_parents for c in parent
                     if tag(c) in ("position", "holding", "lot")]
        if positions:
            base = _norm(_xml_element_to_dict(ae))
            if "name" in base and "account" not in base:
                base["account"] = base.pop("name")
            for p in positions:
                nested.append({**base, **_norm(_xml_element_to_dict(p))})
    if nested:
        return nested

    # Flat form: any repeated position-like element anywhere.
    flat = [e for e in root.iter() if tag(e) in ("position", "holding", "lot", "row", "record")]
    return [_norm(_xml_element_to_dict(e)) for e in flat]


def detect_format(raw: str, filename: str | None = None) -> str:
    if filename:
        ext = filename.rsplit(".", 1)[-1].lower()
        if ext in ("csv", "json", "xml"):
            return ext
    stripped = raw.lstrip()
    if stripped.startswith(("{", "[")):
        return "json"
    if stripped.startswith("<"):
        return "xml"
    return "csv"


# ── core import ─────────────────────────────────────────────────────────────

def _get(row: dict, *keys: str) -> str | None:
    for k in keys:
        if row.get(k) not in (None, ""):
            return row[k]
    return None


def import_rows(session: Session, rows: list[dict]) -> dict:
    """Import a list of normalized row dicts. Returns a summary."""
    accounts: dict[str, Account] = {}
    assets: dict[str, Asset] = {}
    new_accounts = new_assets = positions = 0
    errors: list[str] = []
    today = date.today()

    for i, row in enumerate(rows, start=1):
        acct_name = _get(row, "account", "account_name")
        qty = _dec(_get(row, "quantity", "qty", "shares", "units"))
        if not acct_name:
            errors.append(f"row {i}: missing account")
            continue
        if qty is None or qty <= 0:
            errors.append(f"row {i}: missing/invalid quantity")
            continue

        key = acct_name.lower()
        account = accounts.get(key)
        if account is None:
            account = session.query(Account).filter(Account.name.ilike(acct_name)).first()
            if account is None:
                acct_type = _enum(AccountTypeEnum, _get(row, "account_type"), AccountTypeEnum.BROKERAGE)
                account = Account(
                    name=acct_name,
                    account_type=acct_type,
                    institution=_get(row, "institution"),
                    is_taxable=acct_type not in _NON_TAXABLE,
                    is_active=True,
                )
                session.add(account)
                session.flush()
                new_accounts += 1
            accounts[key] = account

        symbol = _get(row, "symbol", "ticker")
        name = _get(row, "name", "security", "description") or symbol or "Unnamed holding"
        akey = (symbol or name).lower()
        asset = assets.get(akey)
        if asset is None:
            q = session.query(Asset)
            asset = q.filter(Asset.symbol.ilike(symbol)).first() if symbol else q.filter(Asset.name.ilike(name)).first()
            if asset is None:
                asset = Asset(
                    symbol=symbol.upper() if symbol else None,
                    name=name,
                    asset_class=_enum(AssetClassEnum, _get(row, "asset_class"), AssetClassEnum.US_EQUITY),
                    is_publicly_traded=bool(symbol),
                )
                session.add(asset)
                session.flush()
                new_assets += 1
            assets[akey] = asset

        price = _dec(_get(row, "price", "current_price"))
        # Stable-value holdings (cash, money markets) are always $1.00 — never
        # take a stray price from the import file for these.
        if symbol and symbol.upper() in STABLE_VALUE_SYMBOLS:
            price = Decimal("1")
        cost_total = _dec(_get(row, "cost_basis_total", "cost_basis", "total_cost"))
        cost_per = _dec(_get(row, "cost_per_share", "unit_cost"))
        if cost_per is None:
            cost_per = (cost_total / qty) if cost_total is not None else (price or Decimal("0"))
        if cost_total is None:
            cost_total = cost_per * qty
        acquired = _parse_date(_get(row, "acquired", "acquisition_date", "date"))

        txn = Transaction(
            account_id=account.id,
            asset_id=asset.id,
            transaction_type=TransactionTypeEnum.BUY,
            transaction_date=acquired,
            quantity=qty,
            price_per_unit=cost_per,
            total_amount=cost_total,
            notes="Imported opening position",
        )
        session.add(txn)
        session.flush()

        session.add(TaxLot(
            account_id=account.id,
            asset_id=asset.id,
            acquisition_date=acquired,
            acquisition_transaction_id=txn.id,
            original_quantity=qty,
            remaining_quantity=qty,
            cost_basis_per_unit=cost_per,
            original_cost_basis_per_unit=cost_per,
            cost_basis_method="fifo",
            is_closed=False,
        ))

        if price is not None and asset.id:
            existing = (
                session.query(AssetPrice)
                .filter(AssetPrice.asset_id == asset.id, AssetPrice.price_date == today)
                .first()
            )
            if existing:
                existing.close_price = price
            else:
                session.add(AssetPrice(asset_id=asset.id, price_date=today,
                                       close_price=price, source="import"))
        positions += 1

    session.flush()
    return {"accounts": new_accounts, "assets": new_assets, "positions": positions, "errors": errors}


def import_positions_data(session: Session, raw: str, filename: str | None = None) -> dict:
    """Detect the format (CSV/JSON/XML) and import. Raises on unparseable input."""
    fmt = detect_format(raw, filename)
    if fmt == "json":
        rows = parse_json(raw)
    elif fmt == "xml":
        rows = parse_xml(raw)
    else:
        rows = parse_csv(raw)
    result = import_rows(session, rows)
    result["format"] = fmt
    return result


def import_positions_csv(session: Session, text: str) -> dict:
    """Backwards-compatible CSV entry point."""
    return import_rows(session, parse_csv(text))
