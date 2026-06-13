"""Single source of truth for the application version.

Prefers pyproject.toml (always present in our source/git/zip distribution and
always current with the code) and falls back to installed package metadata.
"""

import tomllib
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _from_pyproject() -> str | None:
    try:
        with open(_PYPROJECT, "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except Exception:
        return None


def _from_metadata() -> str | None:
    try:
        return _pkg_version("family-office")
    except PackageNotFoundError:
        return None


__version__ = _from_pyproject() or _from_metadata() or "0.0.0+dev"


def get_app_version() -> str:
    return __version__
