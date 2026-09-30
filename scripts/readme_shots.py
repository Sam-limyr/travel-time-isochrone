"""Take the README's screenshots from a running app (needs Edge or Chrome).

Usage: python scripts/readme_shots.py [http://127.0.0.1:8000/]
Writes docs/screenshots/*.jpg in the dark theme and a 1872 x 924 window: one per view with the
whole island, then a bus route closer in. Uses the Chrome DevTools protocol, like
scripts/tutorial_shots.py.
"""
import sys
import time
from pathlib import Path

from tutorial_shots import Page  # noqa: E402  (scripts/tutorial_shots.py)

OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
APP = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/").rstrip("/") + "/"
W, H = 1872, 924
LOOK = "style=bands&pal=plasma&res=high&wait=avg&walk=4.8&use=brv"
ISLAND = "ov=m&v=1.3593,103.8020,11.33"  # MRT lines, and all of the island
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
        p.open("dir=from&o=1.28400,103.85150&mode=transit&band=am_peak&max=50&bw=10&kp=general&" + ISLAND)
        shoot(p, "1-from-one-point.jpg")
        # 2: to Jurong East, Punggol Coast and Tampines, weighted 2 : 1 : 2, with the trips from
        # Newton drawn (as a right-click there does)
        p.open("dir=to&pl=1.33400,103.74700,2,;1.41700,103.90980,1,;1.35170,103.94920,2,&t=1.31291,103.83801"
               "&mode=transit&band=am_peak&max=60&bw=10&kp=general&" + ISLAND)
        p.wait("placeParts.length === 3 && !!lastTrips && !document.querySelector('#route-section').hidden")
        p.settle()
        shoot(p, "2-several-places.jpg")
        # 3: to a profile, with the pin at Springleaf and the pointer on it
        pin = (1.40010, 103.81860)
        p.open(f"dir=profile&kp=business_parks&o={pin[0]:.5f},{pin[1]:.5f}&mode=transit&band=am_peak"
               f"&max=60&bw=10&{ISLAND}")
        p.wait("!!spotScores && document.querySelector('#key-total').textContent.includes('min')")
        p.settle()
        x, y = p.js(f"(() => {{ const q = map.project([{pin[1]}, {pin[0]}]); return [q.x, q.y]; }})()")
        p.call("Input.dispatchMouseEvent", type="mouseMoved", x=x + 3, y=y + 3)
        p.wait("!document.querySelector('#tooltip').hidden")
        time.sleep(0.4)
        shoot(p, "3-profile.jpg")
        # 4: from Jurong East by car, PM peak
        p.open("dir=from&o=1.33370,103.74740&mode=car&band=pm_peak&park=2&max=40&bw=5&kp=business_parks&" + ISLAND)
        shoot(p, "4-car.jpg")
        # 5: bus routes shown, from Serangoon North (2 km from the nearest station) to Ang Mo Kio on
        # bus 73, closer in
        p.open("dir=from&o=1.37380,103.87300&d=1.36993,103.84958&mode=transit&band=am_peak&max=40&bw=10"
               "&kp=general&ov=mb&v=1.3680,103.8494,12.70")
        p.wait("!!lastRoute")
        p.settle()
        shoot(p, "5-bus-route.jpg")
    finally:
        p.close()


if __name__ == "__main__":
    main()
