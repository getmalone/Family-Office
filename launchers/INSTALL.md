# Installing Family Office

A private wealth-management app that runs entirely on your own computer. Your
financial data is stored in an **encrypted database** on this machine and never
leaves it (except live market prices and, if configured, AI requests).

## First-time setup (a few minutes, needs internet)

Pick the launcher for your computer and **double-click it**:

| Your computer | Double-click |
|---------------|--------------|
| **Mac**       | `family-office-macos.command` |
| **Windows**   | `family-office-windows.bat` |
| **Linux**     | `family-office-linux.sh` |

A terminal window opens and sets things up automatically (it installs a private
copy of Python — nothing else on your computer is affected). The first run takes
a few minutes; later runs start in seconds.

You'll be asked to **create a password**. This password encrypts your data.
**Write it down somewhere safe — it cannot be recovered.** If you lose it, the
data cannot be opened.

When setup finishes, the app opens in your web browser. Keep the terminal window
open while you use it; closing it stops the app.

### Mac: "cannot be opened because it is from an unidentified developer"
Right-click `family-office-macos.command` → **Open** → **Open**. You only need to
do this once: the app clears the download flag from its folder on every launch,
and updates leave unchanged launchers untouched, so macOS won't ask again after
an update.

## Everyday use
Double-click the same launcher, type your password, and the app opens. To stop
it, close the terminal window.

## Updates
Use **Settings → Check for updates**. The app updates itself in the same folder,
restarts on its own, and reloads the page. You don't need to re-enter your
password or move any files.

## Where your data is kept
Your encrypted database, its backups and your `.env` settings live in a data
folder outside the app: `~/Library/Application Support/FamilyOffice` on a Mac,
`%APPDATA%\FamilyOffice` on Windows, and `~/.local/share/FamilyOffice` on Linux.
Set `KFO_DATA_DIR` to use another location. Installs from before v0.1.29 are moved
there automatically on first launch.

## Optional
- **AI assistant:** to enable the built-in assistant, add your Anthropic API key
  in **Settings**, or to the `.env` file in your data folder (`KFO_ANTHROPIC_API_KEY=...`). The rest of the app works
  without it.
- **Use from your phone/tablet on the same network:** see `scripts/serve.sh`
  (sets `KFO_HOST=0.0.0.0`); a network login is required automatically in that mode.
