"""python -m server entry point."""
from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="ds-yue-webui server")
    parser.add_argument("--config", default=None, help="config.yaml path (default: config.yaml next to the repo)")
    parser.add_argument("--host", default=None, help="bind address (overrides config.yaml)")
    parser.add_argument("--port", type=int, default=None, help="port (overrides config.yaml)")
    args = parser.parse_args()

    import uvicorn

    from .app import create_app
    from .config import Settings

    settings = Settings.from_sources(args.config)
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
