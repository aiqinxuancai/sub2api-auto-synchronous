"""Start Flow: python web_app.py --host 127.0.0.1 --port 8787."""

import argparse
from pathlib import Path

import uvicorn

from relaydesk.web import create_app
from sync_sub2api import state_lock


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Flow · Sub2API 同步工作台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--data-dir", type=Path, default=root / "data" / "web")
    parser.add_argument("--no-scheduler", action="store_true")
    options = parser.parse_args()
    with state_lock(options.data_dir / "web-server"):
        uvicorn.run(create_app(options.data_dir, start_scheduler=not options.no_scheduler),
                    host=options.host, port=options.port, access_log=False)


if __name__ == "__main__":
    main()
