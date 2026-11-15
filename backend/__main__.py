"""Start the Inside the Box backend and web UI: ``python -m backend`` → http://127.0.0.1:8000/"""
from __future__ import annotations

import argparse
import logging

import uvicorn

from . import create_app
from .config import Config
from .pipeline import fixture_titles


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Inside the Box backend: job API + web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--mode", choices=("fixtures", "live"), help="overrides ITB_PIPELINE_MODE (default fixtures)")
    p.add_argument("--jobs-dir", help="overrides ITB_JOBS_DIR")
    p.add_argument("--fixtures", help="JSON file with extra fixture games (overrides ITB_FIXTURES)")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = Config.from_env(mode=a.mode, jobs_dir=a.jobs_dir, fixtures_file=a.fixtures)
    print(f"Inside the Box backend, {config.mode} mode: http://{a.host}:{a.port}/")
    if config.mode == "fixtures":
        print("  No paid API calls. Games with fixtures: " + ", ".join(fixture_titles(config)))
    elif not Config.api_key():
        print("  WARNING: ANTHROPIC_API_KEY is not set; jobs will be refused until it is.")
    uvicorn.run(create_app(config), host=a.host, port=a.port, log_level="info")


if __name__ == "__main__":
    main()
