"""
Run the Family Office server: ``python -m app``.

Honors KFO_HOST / KFO_PORT (see app.config). To make the app reachable from
other devices on your network, set KFO_HOST=0.0.0.0 — and set KFO_ACCESS_CODE
first so the app is gated behind a passcode.
"""

import uvicorn

from app.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
