"""Screenshot the running app once the map has finished loading (dev tool).

Usage: python scripts/screenshot.py URL OUT.png [--dark] [--width 1440 --height 900] [--eval JS]

Drives headless Edge, Chrome or Chromium (Windows, macOS or Linux) over the DevTools protocol and waits
until MapLibre reports that all tiles and sources are loaded and no request is
in flight, which a plain ``--screenshot`` run cannot do. Needs websocket-client.
"""
import argparse
import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request

import websocket

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]
BROWSER_COMMANDS = ["msedge", "google-chrome", "chromium", "chromium-browser", "chrome"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("out")
    ap.add_argument("--dark", action="store_true")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--eval", default="", help="JavaScript to run once the map is idle (then wait again)")
    ap.add_argument("--port", type=int, default=9333)
    args = ap.parse_args()

    browser = next((b for b in BROWSERS if os.path.exists(b)), None) or next(
        (shutil.which(c) for c in BROWSER_COMMANDS if shutil.which(c)), None)
    if not browser:
        raise SystemExit("No Chromium-based browser found (Edge, Chrome or Chromium).")
    profile = tempfile.mkdtemp(prefix="isochrone-shot-")
    proc = subprocess.Popen([
        browser, "--headless=new", f"--remote-debugging-port={args.port}", f"--user-data-dir={profile}",
        "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--hide-scrollbars",
        f"--window-size={args.width},{args.height}",
        f"--blink-settings=preferredColorScheme={0 if args.dark else 1}", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{args.port}/json"))
                break
            except OSError:
                time.sleep(0.2)
        page = next(t for t in tabs if t["type"] == "page")
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=60, suppress_origin=True)
        counter = iter(range(1, 10 ** 6))

        def call(method, **params):
            msg_id = next(counter)
            ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
            while True:
                reply = json.loads(ws.recv())
                if reply.get("id") == msg_id:
                    if "error" in reply:
                        raise RuntimeError(reply["error"])
                    return reply.get("result", {})

        def evaluate(expr):
            res = call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
            return res.get("result", {}).get("value")

        def wait_idle(timeout=45):
            ready = ("typeof map !== 'undefined' && map && map.loaded() && map.areTilesLoaded()"
                     " && (typeof reqSeq === 'undefined' || document.getElementById('status').hidden)")
            deadline = time.time() + timeout
            stable = 0
            while time.time() < deadline:
                stable = stable + 1 if evaluate(ready) else 0
                if stable >= 4:
                    return True
                time.sleep(0.25)
            return False

        call("Page.enable")
        call("Page.navigate", url=args.url)
        ok = wait_idle()
        if args.eval:
            evaluate(args.eval)
            time.sleep(0.5)
            ok = wait_idle() and ok
        time.sleep(0.5)
        shot = call("Page.captureScreenshot", format="png")
        with open(args.out, "wb") as fh:
            fh.write(base64.b64decode(shot["data"]))
        errors = evaluate("document.getElementById('status').hidden ? '' : document.getElementById('status').textContent")
        print(f"saved {args.out} (map idle: {ok}){' status: ' + errors if errors else ''}")
        ws.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    main()
