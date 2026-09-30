"""Take the README's screenshots from a running app (needs Edge or Chrome).

Usage: python scripts/readme_shots.py [http://127.0.0.1:8000/]
Writes docs/screenshots/*.jpg: one per view, in the dark theme, with the whole island in a
1872 x 924 window. Uses the Chrome DevTools protocol, like scripts/tutorial_shots.py.
"""
import sys
import time
from pathlib import Path

from tutorial_shots import Page  # noqa: E402  (scripts/tutorial_shots.py)

OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
APP = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/").rstrip("/") + "/"
W, H = 1872, 924
LOOK = "style=bands&pal=plasma&res=high&wait=avg&walk=4.8&use=brv&ov=m&v=1.3593,103.8020,11.33"
QUALITY = 90


def shoot(p: Page, name: str) -> None:
    # fold the map's attribution away, as panning does (the README credits the sources)
    p.js("document.querySelector('.maplibregl-ctrl-attrib')?.classList.remove('maplibregl-compact-show')")
    time.sleep(0.2)
    p.shot(name, quality=QUALITY)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    p = Page(port=9352, app=APP, look=LOOK, out=OUT, size=(W, H), scheme="dark")
    try:
        p.call("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=1, mobile=False)
        # 1: from Raffles Place by public transport
        p.open("dir=from&o=1.28400,103.85150&mode=transit&band=am_peak&max=50&bw=10&kp=general")
        shoot(p, "1-from-one-point.jpg")
        # 2: to Jurong East, Punggol Coast and Tampines, weighted 2 : 1 : 2
        p.open("dir=to&pl=1.33400,103.74700,2,;1.41700,103.90980,1,;1.35170,103.94920,2,"
               "&mode=transit&band=am_peak&max=60&bw=10&kp=general")
        p.wait("placeParts.length === 3")
        p.settle()
        shoot(p, "2-several-places.jpg")
        # 3: to a profile, with the pin at Springleaf and the pointer on it
        pin = (1.40010, 103.81860)
        p.open(f"dir=profile&kp=business_parks&o={pin[0]:.5f},{pin[1]:.5f}&mode=transit&band=am_peak&max=60&bw=10")
        p.wait("!!spotScores && document.querySelector('#key-total').textContent.includes('min')")
        p.settle()
        x, y = p.js(f"(() => {{ const q = map.project([{pin[1]}, {pin[0]}]); return [q.x, q.y]; }})()")
        p.call("Input.dispatchMouseEvent", type="mouseMoved", x=x + 3, y=y + 3)
        p.wait("!document.querySelector('#tooltip').hidden")
        time.sleep(0.4)
        shoot(p, "3-profile.jpg")
        # 4: from Jurong East by car, PM peak
        p.open("dir=from&o=1.33370,103.74740&mode=car&band=pm_peak&park=2&max=40&bw=5&kp=business_parks")
        shoot(p, "4-car.jpg")
    finally:
        p.close()


if __name__ == "__main__":
    main()
