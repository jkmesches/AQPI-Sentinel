"""Entrypoint. Loads .env, configures logging, runs uvicorn with the app."""
from __future__ import annotations
import logging
from pathlib import Path

from dotenv import load_dotenv

# .env lives at the project root (one level above backend/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def main() -> None:
    # Import config AFTER dotenv so env vars are visible.
    from .config import SETTINGS

    logging.basicConfig(
        level=SETTINGS.log_level,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )
    # Importing this triggers the @register side-effects across backend/checks.
    from . import checks as _all_checks  # noqa: F401

    import uvicorn
    from .api.app import create_app

    uvicorn.run(
        create_app(),
        host=SETTINGS.api_host,
        port=SETTINGS.api_port,
        log_level=SETTINGS.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
