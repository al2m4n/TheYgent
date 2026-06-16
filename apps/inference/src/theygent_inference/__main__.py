"""Run the inference plane: ``python -m theygent_inference`` / ``theygent-inference``."""

from __future__ import annotations

import os

import uvicorn

from theygent_inference.app import create_app


def main() -> None:
    app = create_app(max_resident=int(os.environ.get("THEYGENT_MAX_RESIDENT", "2")))
    uvicorn.run(
        app,
        host=os.environ.get("THEYGENT_INFERENCE_HOST", "127.0.0.1"),
        port=int(os.environ.get("THEYGENT_INFERENCE_PORT", "8081")),
    )


if __name__ == "__main__":
    main()
