"""Command line entry point: ``python -m isochrone {fetch,build,serve}``."""
from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m isochrone", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch", help="download raw datasets")
    p_fetch.add_argument("--only", help="comma-separated subset of: lta,speeds,osm,hdb,boundary,busrouter")
    p_fetch.add_argument("--force", action="store_true", help="re-download cached large files")

    sub.add_parser("build", help="build the walk/drive/transit networks from raw data")

    p_serve = sub.add_parser("serve", help="run the web app")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)

    args = parser.parse_args()
    if args.cmd == "fetch":
        from .fetch import fetch
        fetch(args.only.split(",") if args.only else None, force=args.force)
    elif args.cmd == "build":
        from .build import build
        build()
    elif args.cmd == "serve":
        import uvicorn
        uvicorn.run("isochrone.server:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
