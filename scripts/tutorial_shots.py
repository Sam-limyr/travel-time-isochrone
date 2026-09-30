"""Take the screenshots on web/tutorial.html from a running app (needs Edge or Chrome).

Usage: python scripts/tutorial_shots.py [http://127.0.0.1:8000/]
Writes web/tutorial/*.jpg. Uses the Chrome DevTools protocol, like scripts/screenshot.py.
"""
import base64
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket

from screenshot import find_browser  # noqa: E402  (scripts/screenshot.py)

OUT = Path(__file__).resolve().parent.parent / "web" / "tutorial"
APP = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/").rstrip("/") + "/"
LOOK = "style=bands&max=60&bw=10&pal=plasma&res=med&mode=transit&band=am_peak&wait=avg&walk=4.8&use=brv&ov=m"
W, H = 1280, 800


class Page:
    def __init__(self, port: int = 9351):
        self.profile = tempfile.mkdtemp(prefix="tutorial-shots-")
        self.proc = subprocess.Popen([find_browser(), "--headless=new", f"--remote-debugging-port={port}",
                                      f"--user-data-dir={self.profile}", "--blink-settings=preferredColorScheme=1",
                                      "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--hide-scrollbars",
                                      f"--window-size={W},{H}", "about:blank"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                break
            except OSError:
                time.sleep(0.2)
        self.ws = websocket.create_connection(next(t for t in tabs if t["type"] == "page")["webSocketDebuggerUrl"],
                                              timeout=120, suppress_origin=True)
        self.ids = iter(range(1, 10**6))
        self.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "light"}])

    def call(self, method, **params):
        i = next(self.ids)
        self.ws.send(json.dumps({"id": i, "method": method, "params": params}))
        while True:
            r = json.loads(self.ws.recv())
            if r.get("id") == i:
                return r.get("result", {})

    def js(self, expr):
        return self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True).get("result", {}).get("value")

    def wait(self, expr, timeout=120):
        for _ in range(int(timeout / 0.25)):
            if self.js(f"(() => {{ try {{ return !!({expr}); }} catch (e) {{ return false; }} }})()"):
                return
            time.sleep(0.25)
        raise TimeoutError(expr)

    def open(self, hash_):
        self.call("Page.navigate", url="about:blank")
        time.sleep(0.3)
        self.call("Page.navigate", url=APP + "#" + hash_ + "&" + LOOK)
        self.wait("typeof map !== 'undefined' && map.loaded() && !!grid")
        self.settle()

    def settle(self):
        self.wait("map.loaded() && map.areTilesLoaded()")
        time.sleep(1.2)

    def shot(self, name, clip=None):
        params = {"format": "jpeg", "quality": 80}
        if clip:
            params["clip"] = {**clip, "scale": 1}
        data = self.call("Page.captureScreenshot", **params)["data"]
        (OUT / name).write_bytes(base64.b64decode(data))
        print(f"{name}: {(OUT / name).stat().st_size / 1000:.0f} kB")

    def close(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)
        shutil.rmtree(self.profile, ignore_errors=True)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    p = Page()
    try:
        bishan = "o=1.35080,103.84840&v=1.3350,103.8400,11.20"
        # 1: click the map
        p.open(f"dir=from&{bishan}")
        p.js("document.querySelector('#panel-toggle').click()")  # fold the panel away: all map
        time.sleep(0.6)
        p.shot("1-click.jpg")
        # 2: right-click for a route
        p.open(f"dir=from&{bishan}&d=1.28400,103.85150")
        p.wait("!!lastRoute")
        p.settle()
        p.shot("2-route.jpg")
        # 3: several places
        p.open("dir=to&pl=1.28400,103.85150,5,Work%2520(you);1.29950,103.78750,5,Work%2520(partner);"
               "1.28160,103.86360,2,Weekend%2520park&v=1.3300,103.8200,11.20")
        p.wait("placeParts.length === 3")
        p.settle()
        p.shot("3-places.jpg")
        # 4: a profile
        p.open(f"dir=profile&kp=cbd&{bishan}")
        p.wait("!!spotScores && document.querySelector('#key-total').textContent.includes('min')")
        p.settle()
        p.shot("4-profile.jpg")
        # 5: hover for exact minutes
        p.open(f"dir=from&{bishan}")
        p.js("document.querySelector('#panel-toggle').click()")
        time.sleep(0.4)
        x, y = 700, 470
        p.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
        p.wait("!document.querySelector('#tooltip').hidden")
        time.sleep(0.4)
        p.shot("5-hover.jpg", {"x": x - 260, "y": y - 150, "width": 560, "height": 300})
        # 6: settings, with Advanced open
        p.open(f"dir=from&{bishan}")
        p.js("document.querySelector('#advanced').open = true;"
             "document.querySelector('#panel-body').scrollTop = document.querySelector('#band-group').closest('section').offsetTop - 60;")
        time.sleep(0.6)
        p.shot("6-settings.jpg", {"x": 0, "y": 0, "width": 380, "height": p.js("innerHeight")})
    finally:
        p.close()


if __name__ == "__main__":
    main()
