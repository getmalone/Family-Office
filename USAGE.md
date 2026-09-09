# Family Office — User Guide

Everything you need to install the app, get your financial data in, and use it
day to day. The app runs entirely on your own computer; your data lives in an
**encrypted database** on this machine and never leaves it (except live market
prices and, if you enable it, AI requests).

---

## 1. Install & first run

First, unzip the bundle you were given to a permanent location (e.g. your home
folder or Applications) and keep the whole folder together. Then **double-click
the launcher for your computer:**

| Computer | Launcher |
|----------|----------|
| Mac      | `family-office-macos.command` |
| Windows  | `family-office-windows.bat` |
| Linux    | `family-office-linux.sh` |

What happens next:

- A terminal window opens and sets everything up automatically (it installs a
  private copy of Python — nothing else on your computer is touched). The first
  run takes a few minutes and needs an internet connection; later runs start in
  seconds.
- You're asked to **create a password**. This password encrypts your data.
  **Write it down somewhere safe — it cannot be recovered.** Lose it and the data
  can't be opened.
- The app then opens in your web browser. Keep the terminal window open while you
  use it; closing it stops the app.

**Mac — "unidentified developer" warning:** right-click
`family-office-macos.command` → **Open** → **Open** (once per machine).
**Windows — "Windows protected your PC":** **More info** → **Run anyway**.

**Everyday use:** double-click the same launcher, type your password, and the app
opens. To stop it, close the terminal window.

**Install it like an app (optional):** in the browser, use your browser's
**Install** / **Add to Home Screen** option to get a standalone window and icon.
It also works offline for pages you've already opened.

---

## 2. Getting your data in (ingestion)

A brand-new database is empty. There are two ways to populate it.

### A) Bulk import from a file — CSV, JSON, or XML

Go to **Settings** (gear icon, top right) → **Import accounts & positions**,
choose your file, and click **Import**. The format is detected automatically
from the file. Each row/record becomes an *opening position* (an account, a
security, and a starting tax lot).

**Fields** (same names in all three formats). Only `account` and `quantity` are
required:

| Field | Meaning |
|-------|---------|
| `account` | Account name (created if new). **Required.** |
| `quantity` | Number of shares/units. **Required.** |
| `account_type` | `brokerage`, `ira_traditional`, `ira_roth`, `401k`, `trust`, `bank_checking`, `bank_savings`, `real_estate`, `private_equity`, `crypto`, `hsa`, `529`, `other` (default `brokerage`). Retirement types are auto-marked non-taxable. |
| `institution` | Custodian/bank name (optional). |
| `symbol` | Ticker (omit for private/illiquid holdings). |
| `name` | Security name (defaults to the symbol). |
| `asset_class` | `us_equity`, `intl_equity`, `fixed_income`, `real_estate`, `private_equity`, `crypto`, `cash`, `commodity`, `alternative`, … (default `us_equity`). |
| `cost_basis_total` | Total cost basis for the lot (preferred), **or**… |
| `cost_per_share` | …per-unit cost basis. |
| `acquired` | Acquisition date `YYYY-MM-DD` (default: today). |
| `price` | Current price per unit (stored as today's quote). |

**CSV example**

```csv
account,account_type,symbol,name,asset_class,quantity,cost_basis_total,acquired,price
My Brokerage,brokerage,VOO,Vanguard S&P 500 ETF,us_equity,100,45000,2023-03-15,520.50
My Roth IRA,ira_roth,AAPL,Apple Inc.,us_equity,50,8000,2022-06-01,195.20
```

**JSON example** (a flat list also works; nested by account shown here)

```json
{
  "accounts": [
    { "account": "My Brokerage", "account_type": "brokerage",
      "positions": [
        { "symbol": "VOO", "quantity": 100, "cost_basis_total": 45000, "price": 520.50 },
        { "symbol": "BND", "quantity": 200, "cost_per_share": 72 }
      ] }
  ]
}
```

**XML example** (fields as attributes or child elements; nesting by account also
supported)

```xml
<positions>
  <position account="My Brokerage" symbol="VOO" quantity="100"
            cost_basis_total="45000" price="520.50"/>
</positions>
```

**Notes**
- Each file is the **current snapshot** of the accounts it names. Re-uploading an
  updated export replaces those accounts' imported positions instead of adding a
  second copy, so your totals stay right. Accounts not in the file are untouched,
  as are hand-entered transactions and any lot you've already sold from.
- Uploading positions for an account you had deactivated **brings it back**.
  Otherwise the rows would land in a hidden account and never show up in
  holdings or AUM, which looks like the import created the securities but not
  the account.
- Rows missing an account or a valid quantity are skipped and reported in the
  result message.
- Imports are recorded as **opening balances**, not as buy/sell trades.
- Blank rows and blank columns above or around the data are ignored, so a raw
  spreadsheet export usually imports as-is.
- Most broker exports can be saved as CSV; column names and values are matched
  loosely (`ticker`→`symbol`, `shares`→`quantity`, `401(k)`→`401k`,
  `international_equity`→`intl_equity`), so light cleanup is usually all that's
  needed.

**Already got inflated numbers?** Versions before v0.1.21 added a second copy of
every holding on each re-upload. If an account's value looks too high, open
**Settings** — when stacked positions are detected you'll see a *Duplicate
positions found* panel listing the affected accounts and what would be removed.
It keeps each account's most recent upload and deletes the ones it replaced, so
you don't have to re-upload anything (which matters when your figures have moved
on since that export). The database is backed up first, and positions with sale
history are always kept.

**Managing accounts** (Portfolio → Manage Accounts) shows when each account was
added and last updated, and how many positions it holds. Deactivating an account
(⊘) hides it from holdings, AUM, and tax views without deleting anything, and
inactive accounts can be brought back with the ✓ button. To get rid of an old
account for good, deactivate it first, then use the 🗑 button — it deletes the
account with every position and trade in it, cannot be undone, and takes a
database backup first.

### B) Enter data by hand

Prefer to type it in? Use the app's screens directly:
- **Portfolio → Accounts → Manage Accounts** to add accounts.
- **Portfolio → Manage Assets** to add securities.
- **Portfolio → New Trade** to record buys/sells (these post to the books).

### Live prices

Public tickers are priced automatically from the market when you're online and
cached locally. Cash and private holdings keep the price you provide.

---

## 3. Using the app

The top navigation has the main sections:

- **Dashboard** — total assets, unrealized/realized gains, allocation, and
  anything waiting for your approval.
- **Portfolio** — holdings by account, buy/sell, per-account cards, history
  charts, and top movers.
- **Tax** — tax-loss-harvest candidates, year-to-date realized gains/losses,
  cost-basis method comparison (FIFO/HIFO/LIFO), and Schedule D.
- **Accounting** — the double-entry journal, trial balance, and financial
  statements. Every trade auto-posts a balanced journal entry.
- **Reports** — generated reports and exports.
- **Estate** — gift tracking against annual/lifetime exclusions, GRAT
  simulation, and the ownership tree.
- **Analysis** — risk metrics, allocation drift, the Morning Brief, and
  **Monte Carlo** retirement projections (see below).
- **Approvals** — the queue of high-value actions (large trades, gifts, tax
  elections) that need your sign-off before they execute.

### Monte Carlo + Social Security

Under **Analysis → Monte Carlo** you can project your portfolio through
accumulation and retirement:

- **Retirement Timeline & Social Security:** enter your **current age**,
  **retirement age**, **SSA claiming age** (62–70), and **estimated monthly SSA
  benefit at full retirement age**. The tool adjusts the benefit for the claiming
  age (62 ≈ 70%, 67 = 100%, 70 ≈ 124%) and treats it as income from that age on —
  so the portfolio funds 100% of spending during the pre-SSA *bridge* years and
  only covers the gap afterward.
- **Spending pattern:** constant, or the "Spending Smile" (Go-Go / Slow-Go /
  No-Go step-downs).
- Compare your current allocation against saved profiles, and read the survival
  rate and bridge summary on each result card.
- **Max sustainable spending:** below the chart, set a target survival rate
  (default 85%) and click *Calculate* to see the highest annual/monthly spending
  that keeps the portfolio's survival at that target — using the same scenario
  (ages, Social Security, contributions, distribution years, spending pattern).

### AI assistant (optional)

The chat bubble (bottom-right) answers questions about your portfolio, taxes, and
estate. It needs an Anthropic API key — add it under **Settings → AI Assistant**
(stored encrypted; no files to edit). The rest of the app works without it.

### Privacy mode

Click **Hide** (top right) to mask every dollar amount behind dots — useful on
screen shares. Click again to reveal.

---

## 4. Settings, backups & moving computers

**Settings** (gear icon) lets you:
- Set your **Anthropic API key** and model.
- **Download an encrypted backup** — a timestamped copy of your database. Keep
  copies somewhere safe; they can only be opened with your password.
- **Import** accounts/positions (section 2).

**Back up regularly.** Your database is the system of record (holdings, tax lots,
books). Use **Settings → Download encrypted backup** and store the file safely.

**Move to a new computer:** install the app there (section 1), then copy your
`data/` folder from the old install into the new one before launching, and use
the same password.

---

## 5. Troubleshooting

- **"Incorrect password":** the password must match the one used when the
  database was first created. There is no recovery — restore from a backup if
  needed.
- **First run is slow / fails:** it downloads Python and dependencies — make sure
  you're online. Re-run the launcher to resume.
- **Mac/Windows blocked the launcher:** see the unidentified-developer /
  SmartScreen steps in section 1.
- **AI assistant says no key:** add your Anthropic API key in Settings.
- **Use from your phone/tablet on the same Wi-Fi:** an administrator can start it
  in network mode (`KFO_HOST=0.0.0.0`), which requires a login with your
  password; otherwise the app stays private to this computer.
