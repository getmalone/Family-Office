"""Let the web app ask the desktop launcher to restart it (after an update).

The launcher (launchers/_run.py) registers a callback that stops the server;
it then re-syncs dependencies and starts again on the same port, reusing the
password already entered. When the app runs any other way (e.g. a developer's
``uvicorn --reload``) nothing is registered and the user restarts by hand.
"""

from __future__ import annotations

import threading
from typing import Callable

_restart_hook: Callable[[], None] | None = None


def register(hook: Callable[[], None]) -> None:
    global _restart_hook
    _restart_hook = hook


def can_restart() -> bool:
    return _restart_hook is not None


def request_restart(delay: float = 1.5) -> bool:
    """Schedule a restart shortly (so the current response can finish).
    Returns False when no launcher is managing the process."""
    hook = _restart_hook
    if hook is None:
        return False
    threading.Timer(delay, hook).start()
    return True
