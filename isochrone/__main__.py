"""Command line entry point: ``isochrone {fetch,build,serve}`` (or ``python -m isochrone ...``)."""
from __future__ import annotations

import argparse
import sys


def main() -> None:
    # Windows consoles default to a legacy code page; never crash on a stray non-ASCII name.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(prog="isochrone", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch", help="download raw datasets")
    p_fetch.add_argument("--only", help="comma-separated subset of: lta,speeds,osm,hdb,boundary,busrouter,barriers "
                                        "(barriers is copied from ../public-dataset-research/hdb-resale-analysis)")
    p_fetch.add_argument("--force", action="store_true", help="re-download cached large files")

    p_build = sub.add_parser("build", help="build the walk/drive/transit networks from raw data")
    p_build.add_argument("--if-needed", action="store_true",
                         help="only if there is no build yet, or it is in an older format")

    p_serve = sub.add_parser("serve", help="run the web app")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--open", action="store_true", help="open the app in the default browser")

    args = parser.parse_args()
    if args.cmd == "fetch":
        from .fetch import fetch
        fetch(args.only.split(",") if args.only else None, force=args.force)
    elif args.cmd == "build":
        from .build import build, build_is_current
        if args.if_needed and build_is_current():
            return
        if args.if_needed:
            print("Building the networks (about two minutes, once) ...", flush=True)
        build()
    elif args.cmd == "serve":
        import threading
        import webbrowser

        import uvicorn
        if args.open:
            threading.Timer(2.0, webbrowser.open, [f"http://{args.host}:{args.port}/"]).start()
        uvicorn.run("isochrone.server:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
