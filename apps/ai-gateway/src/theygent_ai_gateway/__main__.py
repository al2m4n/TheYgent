"""Run the gateway: ``python -m theygent_ai_gateway`` / ``theygent-ai-gateway``."""

from __future__ import annotations

import os
import sys

import uvicorn

from theygent_ai_gateway.app import create_app
from theygent_ai_gateway.settings import SettingsError, load_settings


def main() -> None:
    try:
        settings = load_settings(os.environ)
    except SettingsError as exc:
        sys.exit(f"theygent-ai-gateway: {exc}")
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
