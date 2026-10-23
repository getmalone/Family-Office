"""
Read brokerage-account screenshots with Claude and turn them into import rows.

Flow: the user uploads every screenshot for ONE account → ``extract_positions``
sends them to Claude in a single request and gets back structured JSON (the
account plus its positions and cash) → the user reviews/edits the rows → the
confirmed rows go through ``import_service.import_rows``, so the usual
snapshot-replace rule applies (the account's previously imported positions are
replaced).

Privacy: the images are held in memory for the one API call and never written
to disk or the database. Claude is asked for only the last 4 digits of an
account number, and anything longer is masked again here before it is shown or
stored. The screenshots do leave the machine (they go to Anthropic's API), so
the upload page says so.
"""

from __future__ import annotations

import base64
import json
import os
import re
from decimal import Decimal, InvalidOperation

ALLOWED_MEDIA_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 5 * 1024 * 1024          # API per-image limit

ACCOUNT_TYPES = ["brokerage", "ira_traditional", "ira_roth", "401k", "hsa", "529", "trust",
                 "bank_checking", "bank_savings", "crypto", "other"]
ASSET_CLASSES = ["us_equity", "intl_equity", "fixed_income", "real_estate", "commodity",
                 "crypto", "cash", "alternative", "other"]

_NUM_OR_NULL = {"type": ["number", "null"]}
_STR_OR_NULL = {"type": ["string", "null"]}

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "account": {
            "type": "object",
            "properties": {
                "institution": _STR_OR_NULL,
                "account_name": _STR_OR_NULL,
                "account_type": {"type": "string", "enum": ACCOUNT_TYPES + ["unknown"]},
                "account_last4": _STR_OR_NULL,
                "as_of_date": _STR_OR_NULL,
            },
            "required": ["institution", "account_name", "account_type", "account_last4", "as_of_date"],
            "additionalProperties": False,
        },
        "positions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": _STR_OR_NULL,
                    "name": {"type": "string"},
                    "asset_class": {"type": "string", "enum": ASSET_CLASSES},
                    "quantity": _NUM_OR_NULL,
                    "price": _NUM_OR_NULL,
                    "market_value": _NUM_OR_NULL,
                    "cost_basis_total": _NUM_OR_NULL,
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "note": _STR_OR_NULL,
                },
                "required": ["symbol", "name", "asset_class", "quantity", "price", "market_value",
                             "cost_basis_total", "confidence", "note"],
                "additionalProperties": False,
            },
        },
        "cash_balance": _NUM_OR_NULL,
        "notes": _STR_OR_NULL,
    },
    "required": ["account", "positions", "cash_balance", "notes"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You extract investment holdings from screenshots of a brokerage, retirement or bank account so they can be imported into the account owner's own portfolio tracker. Accuracy matters more than completeness: these numbers become the owner's books.

All screenshots in a request show the SAME account (for example several pages or scroll positions of one positions list). Combine them into one list: a holding that appears in more than one screenshot (overlapping scroll) is listed once.

For each holding, copy what is shown:
- symbol: the ticker or fund symbol exactly as shown; null if none is shown (CUSIP-only or unlisted funds go in name).
- name: the security description.
- quantity (shares/units), price (per share), market_value (current value), cost_basis_total (total cost basis): plain numbers without $ or commas; negative only if shown negative; null when not visible. Never compute a value that isn't shown.
- asset_class: your best classification from the name/symbol (money market and sweep funds are cash).
- confidence: "low" when digits are blurry, cut off, or you are unsure which row a number belongs to; "medium" when legible but ambiguous; else "high". Explain anything low or medium in note.

Put the account's cash / sweep / core position in cash_balance (not in positions) unless the screen lists it as a money-market fund with a ticker, in which case list it as a position with asset_class "cash".

Account details: institution (e.g. "Fidelity"), account_name as displayed, account_type from the listed values ("unknown" if unclear), as_of_date as YYYY-MM-DD if shown. For account_last4 give ONLY the last 4 digits of the account number; never return more digits than that.

Skip subtotal, total, pending-activity and header rows. Use notes for anything the owner should know (for example "the last row is cut off — upload the next page")."""


class ScreenshotImportError(Exception):
    """A user-facing problem reading the screenshots."""


def _api_key() -> str:
    from app.config import settings
    return settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")


def _model() -> str:
    from app.config import settings
    return settings.screenshot_model


def _import_anthropic():
    try:
        import anthropic
    except ImportError as exc:
        raise ScreenshotImportError(
            "The app's Python packages look damaged, so Claude can't be reached. Quit the app "
            "and relaunch it — the launcher repairs them automatically (or run "
            "'uv sync --frozen --reinstall' in the app folder).") from exc
    return anthropic


def validate_images(files: list[tuple[bytes, str]]) -> None:
    if not files:
        raise ScreenshotImportError("Choose at least one screenshot.")
    if len(files) > MAX_IMAGES:
        raise ScreenshotImportError(f"Upload at most {MAX_IMAGES} screenshots per account.")
    for data, media_type in files:
        if media_type not in ALLOWED_MEDIA_TYPES:
            raise ScreenshotImportError("Screenshots must be PNG, JPEG, WebP or GIF images.")
        if len(data) > MAX_IMAGE_BYTES:
            raise ScreenshotImportError("Each screenshot must be under 5 MB.")


def extract_positions(files: list[tuple[bytes, str]], client=None) -> dict:
    """Send one account's screenshots to Claude; return normalized extraction
    (see ``normalize``). ``client`` is injectable for tests."""
    validate_images(files)
    if client is None:
        key = _api_key()
        if not key:
            raise ScreenshotImportError(
                "Add your Anthropic API key in Settings (the same key the AI assistant uses) "
                "to read screenshots.")
        anthropic = _import_anthropic()
        client = anthropic.Anthropic(api_key=key, timeout=300.0, max_retries=2)

    content: list[dict] = []
    for i, (data, media_type) in enumerate(files, start=1):
        content.append({"type": "text", "text": f"Screenshot {i} of {len(files)}:"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": media_type,
            "data": base64.standard_b64encode(data).decode("ascii")}})
    content.append({"type": "text", "text": "Extract this account's holdings."})

    anthropic = _import_anthropic()
    try:
        response = client.messages.create(
            model=_model(),
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": "high",
                           "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}},
            # If a safety classifier declines, re-run on Anthropic's recommended
            # fallback model server-side instead of failing the import.
            extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
            extra_body={"fallbacks": "default"},
        )
    except anthropic.AuthenticationError:
        raise ScreenshotImportError("The Anthropic API key was rejected — check it in Settings.")
    except anthropic.RateLimitError:
        raise ScreenshotImportError("Anthropic's API is rate-limiting requests — try again in a minute.")
    except anthropic.BadRequestError as exc:
        raise ScreenshotImportError(f"The screenshots couldn't be processed: {exc.message}")
    except anthropic.APIStatusError as exc:
        raise ScreenshotImportError(f"Anthropic's API returned an error ({exc.status_code}) — try again.")
    except anthropic.APIConnectionError:
        raise ScreenshotImportError("Couldn't reach Anthropic's API — check the internet connection.")

    if response.stop_reason == "refusal":
        raise ScreenshotImportError("Claude declined to read these screenshots.")
    if response.stop_reason == "max_tokens":
        raise ScreenshotImportError("Too many holdings for one pass — split the screenshots into two uploads.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return normalize(json.loads(text))
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ScreenshotImportError("Claude's answer couldn't be read — try again.") from exc


# ── Normalisation (pure; tested without the API) ─────────────────────────────

def _num(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v).replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        return None


def mask_account_number(value: str | None) -> str | None:
    """Keep only the last 4 digits of anything that looks like an account number."""
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    return digits[-4:] if digits else None


def normalize(data: dict) -> dict:
    """Clean Claude's JSON into review rows: mask the account number, fill a
    missing quantity/price from market value, turn cash into a CASH row,
    de-duplicate, and flag rows that need a human look."""
    acct = data.get("account") or {}
    last4 = mask_account_number(acct.get("account_last4"))
    account = {
        "institution": (acct.get("institution") or "").strip() or None,
        "account_name": (acct.get("account_name") or "").strip() or None,
        "account_type": acct.get("account_type") if acct.get("account_type") in ACCOUNT_TYPES else "brokerage",
        "account_type_known": acct.get("account_type") in ACCOUNT_TYPES,
        "account_last4": last4,
        "as_of_date": acct.get("as_of_date"),
    }
    # The account name must never carry a full account number either.
    if account["account_name"]:
        account["account_name"] = re.sub(r"\d{5,}", lambda m: "…" + m.group(0)[-4:], account["account_name"])

    rows: list[dict] = []
    seen: dict[str, dict] = {}
    for p in data.get("positions") or []:
        qty, price, mv, cost = (_num(p.get(k)) for k in ("quantity", "price", "market_value", "cost_basis_total"))
        symbol = (p.get("symbol") or "").strip().upper() or None
        is_cash = p.get("asset_class") == "cash" and symbol in (None, "CASH")
        if qty is None and mv is not None and price:
            qty = (mv / price).quantize(Decimal("0.0001"))
        if price is None and mv is not None and qty:
            price = (mv / qty).quantize(Decimal("0.0001"))
        if is_cash:
            symbol = "CASH"
            if qty is None and mv is not None:
                qty, price = mv, Decimal("1")
        issues = []
        if p.get("confidence") in ("low", "medium"):
            issues.append(p.get("note") or f"{p.get('confidence')} confidence")
        if qty is None or qty <= 0:
            issues.append("quantity missing")
        if qty and price and mv and abs(qty * price - mv) > max(Decimal("1"), mv * Decimal("0.01")):
            issues.append("quantity × price doesn't match market value")
        row = {
            "symbol": symbol, "name": (p.get("name") or symbol or "Unnamed holding").strip(),
            "asset_class": p.get("asset_class") if p.get("asset_class") in ASSET_CLASSES else "other",
            "quantity": qty, "price": price, "market_value": mv, "cost_basis_total": cost,
            "issues": issues,
        }
        key = (symbol or row["name"]).lower()
        if key in seen:
            prev = seen[key]
            if prev["quantity"] != qty:
                prev["issues"].append("listed twice with different quantities — check")
            continue
        seen[key] = row
        rows.append(row)

    cash = _num(data.get("cash_balance"))
    if cash and cash > 0 and "cash" not in seen:
        rows.append({"symbol": "CASH", "name": "Cash", "asset_class": "cash", "quantity": cash,
                     "price": Decimal("1"), "market_value": cash, "cost_basis_total": cash,
                     "issues": []})

    return {"account": account, "rows": rows, "notes": data.get("notes")}


def default_account_name(account: dict) -> str:
    parts = [account.get("institution") or "", account.get("account_name") or ""]
    name = " ".join(p for p in parts if p).strip() or "Imported account"
    if account.get("account_last4") and account["account_last4"] not in name:
        name += f" …{account['account_last4']}"
    return name


def to_import_rows(account_name: str, account_type: str, institution: str | None,
                   rows: list[dict]) -> list[dict]:
    """Confirmed review rows → the dicts ``import_service.import_rows`` takes."""
    out = []
    for r in rows:
        qty = _num(r.get("quantity"))
        if qty is None or qty <= 0:
            continue
        cash = (r.get("asset_class") == "cash") or (str(r.get("symbol") or "").upper() == "CASH")
        out.append({
            "account": account_name, "account_type": account_type, "institution": institution,
            "symbol": "CASH" if cash and not r.get("symbol") else (r.get("symbol") or None),
            "name": r.get("name") or r.get("symbol"),
            "asset_class": "cash" if cash else r.get("asset_class"),
            "quantity": str(qty),
            "price": str(_num(r.get("price"))) if _num(r.get("price")) is not None else None,
            "cost_basis_total": (str(_num(r.get("cost_basis_total")))
                                 if _num(r.get("cost_basis_total")) is not None else None),
        })
    return out
