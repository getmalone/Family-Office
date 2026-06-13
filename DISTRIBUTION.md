# Distributing Family Office

How to build the installable bundle and hand it to a user, plus the OS security
prompts they'll see and how to remove them.

## Build the bundle

```bash
./.venv/bin/python scripts/build_release.py
```

This produces `dist/family-office-<version>/` and `dist/family-office-<version>.zip`.
The bundle contains the app, the per-OS double-click launchers, the dependency
lockfile, and the install guide. It **excludes** the real database, the
`private/` import scripts, any `.env`, the virtualenv, and git history — the
build aborts if any of those slip in.

Send the user the `.zip`. They unzip it and follow `INSTALL.md`.

## What happens on the user's machine
- First launch installs a private copy of `uv` + Python 3.12 and the exact locked
  dependencies (needs internet once). Nothing else on their system is touched.
- They choose a master password. It encrypts the database (SQLCipher / AES-256)
  and, if the app is ever exposed on a network, gates the web login. It cannot be
  recovered — losing it means losing access to the data.
- All dependencies install from prebuilt **wheels** for macOS (Intel + Apple
  Silicon), Windows, and Linux — no compiler required.

## OS security prompts (unsigned bundle)
The launchers are scripts, not signed apps, so the OS will warn the first time.

**macOS (Gatekeeper).** Right-click `family-office-macos.command` → **Open** →
**Open**. Once per machine. (Newer macOS may also need
*System Settings → Privacy & Security → Open Anyway*.)

**Windows (SmartScreen).** "Windows protected your PC" → **More info** →
**Run anyway**.

### Removing the prompts entirely (optional, requires paid certs)
- **macOS:** sign with a Developer ID and notarize:
  `codesign --deep --sign "Developer ID Application: …" <bundle>` then
  `xcrun notarytool submit … && xcrun stapler staple …`. Requires an Apple
  Developer account ($99/yr). Cleanest path is to wrap the launcher in a `.app`
  first (e.g. Platypus) and sign that.
- **Windows:** sign the `.bat`/an `.exe` wrapper with `signtool` using an
  Authenticode / EV code-signing certificate. EV certs clear SmartScreen
  reputation immediately.

These are only needed to avoid the one-time prompt; the app runs fine without them.

## Offline / air-gapped installs
First run needs internet to fetch Python and the wheels. For a machine with no
internet, pre-download per-OS wheels (`uv export` + `pip download` against the
lock on a matching platform) and ship them alongside the bundle for an offline
`uv sync --offline`. Ask if you need this packaged.

## Updates
To ship a new version, rebuild and send the new `.zip`. The user replaces the app
folder but keeps their `data/` folder (their encrypted database and backups).
Tell them to copy `data/` from the old folder into the new one before launching.
```
```

> Reminder: the database is the system of record (double-entry books, tax lots).
> Encourage regular use of **Settings → Download encrypted backup**.
