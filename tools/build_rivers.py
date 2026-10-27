"""Build rivers/<lat>_<lon>.json tiles: Thai river lines (OpenStreetMap) with flood risk per stretch (GloFAS via Open-Meteo).

River geometry is refreshed weekly (rivers-geom.json); flow risk is recomputed on every run.
Runs in GitHub Actions; standard library only.
"""
import json, math, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta

BBOX = (5.5, 97.3, 20.6, 105.7)          # south, west, north, east (Thailand)
GEOM_FILE, TILE_DIR = "rivers-geom.json", "rivers"
SAMPLE_KM = 8.0
UA = {"User-Agent": "fiowerwool001-weather-app (github.com/fiowerwool001/weather-app)"}


def http(url, data=None, tries=4, timeout=300):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            print("retry", k + 1, url[:80], e, file=sys.stderr)
            time.sleep(20 * (k + 1))
    raise RuntimeError("failed: " + url[:80])


def km(a, b):
    m = math.cos(math.radians((a[0] + b[0]) / 2))
    return 6371 * math.hypot(math.radians(b[0] - a[0]), math.radians(b[1] - a[1]) * m)


def simplify(pts, tol):
    """Douglas-Peucker in degrees."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        (ay, ax), (by, bx) = pts[i], pts[j]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy or 1e-12
        best, bi = 0, -1
        for k in range(i + 1, j):
            py, px = pts[k]
            t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L2))
            d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > best:
                best, bi = d, k
        if bi > 0 and best > tol * tol:
            keep[bi] = True
            stack += [(i, bi), (bi, j)]
    return [p for p, k in zip(pts, keep) if k]


def load_geometry():
    if os.path.exists(GEOM_FILE):
        g = json.load(open(GEOM_FILE))
        if time.time() - g.get("at", 0) < 7 * 86400:
            return g["ways"]
    s, w, n, e = BBOX
    q = f'[out:json][timeout:600][maxsize:1073741824];way["waterway"="river"]({s},{w},{n},{e});out tags geom;'
    j = None
    for host in ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"):
        try:
            j = http(host, data=urllib.parse.urlencode({"data": q}).encode(), tries=2, timeout=900)
            break
        except Exception as e:  # noqa: BLE001
            print("overpass failed", host, e, file=sys.stderr)
    if j is None:
        raise RuntimeError("overpass unavailable")
    ways = []
    for el in j.get("elements", []):
        geo = el.get("geometry") or []
        pts = [(round(p["lat"], 4), round(p["lon"], 4)) for p in geo]
        pts = simplify(pts, 0.0015)
        if len(pts) < 2:
            continue
        tags = el.get("tags", {})
        name = tags.get("name:th") or tags.get("name") or ""
        ways.append({"n": name, "c": [[round(a, 3), round(b, 3)] for a, b in pts]})
    json.dump({"at": time.time(), "ways": ways}, open(GEOM_FILE, "w"), ensure_ascii=False, separators=(",", ":"))
    print("ways", len(ways))
    return ways


def main():
    ways = load_geometry()
    # sample points along each river, one per GloFAS cell (0.05 deg)
    cells = {}
    for w in ways:
        c = w["c"]
        cum = [0.0]
        for i in range(1, len(c)):
            cum.append(cum[-1] + km(c[i - 1], c[i]))
        L = cum[-1]
        n = max(1, round(L / SAMPLE_KM))
        w["cum"], w["step"], w["keys"] = cum, L / n, []
        for k in range(n):
            at = (k + 0.5) * L / n
            i = 1
            while i < len(c) - 1 and cum[i] < at:
                i += 1
            f = (at - cum[i - 1]) / max(1e-9, cum[i] - cum[i - 1])
            lat = c[i - 1][0] + (c[i][0] - c[i - 1][0]) * f
            lon = c[i - 1][1] + (c[i][1] - c[i - 1][1]) * f
            key = (round(lat * 20) / 20, round(lon * 20) / 20)
            cells.setdefault(key, None)
            w["keys"].append(key)
    keys = list(cells)
    print("cells", len(keys))
    today = datetime.now(timezone.utc).date().isoformat()
    for b in range(0, len(keys), 100):
        part = keys[b:b + 100]
        url = "https://flood-api.open-meteo.com/v1/flood?" + urllib.parse.urlencode({
            "latitude": ",".join(f"{k[0]:.3f}" for k in part), "longitude": ",".join(f"{k[1]:.3f}" for k in part),
            "daily": "river_discharge,river_discharge_max", "past_days": 60, "forecast_days": 15})
        j = http(url)
        if isinstance(j, dict):
            j = [j]
        for key, loc in zip(part, j):
            d = (loc or {}).get("daily") or {}
            q, qm, t = d.get("river_discharge"), d.get("river_discharge_max"), d.get("time")
            if not q or not t:
                continue
            qm = qm or q
            ti = t.index(today) if today in t else 60
            past = sorted(v for v in q[:ti] if v is not None)
            if not past:
                continue
            med = past[len(past) // 2]
            peak, pat = 0.0, ti
            for i in range(ti, min(len(qm), ti + 14)):
                if (qm[i] or 0) > peak:
                    peak, pat = qm[i] or 0, i
            cells[key] = (round(med, 1), round(peak, 1), round(q[ti] or 0, 1), t[pat])
        time.sleep(12)  # stay well under the per-minute limit
    out = []
    for idx, w in enumerate(ways):
        vals = [cells[k] if cells[k] and cells[k][0] >= 3 else None for k in w["keys"]]
        filled = []
        for i, v in enumerate(vals):  # borrow the nearest good reading on the same river
            if v is None:
                for d in range(1, len(vals)):
                    if i - d >= 0 and vals[i - d]:
                        v = vals[i - d]; break
                    if i + d < len(vals) and vals[i + d]:
                        v = vals[i + d]; break
            filled.append(v)
        # stretches: [first vertex index, med, peak, now, peakDay]
        st, cur = [], -1
        for i in range(len(w["c"])):
            k = min(len(filled) - 1, int(w["cum"][i] / w["step"]))
            if k != cur:
                v = filled[k]
                st.append([i] + (list(v) if v else []))
                cur = k
        lats = [p[0] for p in w["c"]]; lons = [p[1] for p in w["c"]]
        out.append({"id": idx, "n": w["n"], "b": [min(lats), min(lons), max(lats), max(lons)], "c": w["c"], "s": st})
    # one file per 1x1 degree tile so the app downloads only the area on screen
    at = datetime.now(timezone(timedelta(hours=7))).isoformat(timespec="minutes")
    tiles = {}
    for w in out:
        b = w["b"]
        for la in range(math.floor(b[0]), math.floor(b[2]) + 1):
            for lo in range(math.floor(b[1]), math.floor(b[3]) + 1):
                tiles.setdefault(f"{la}_{lo}", []).append(w)
    os.makedirs(TILE_DIR, exist_ok=True)
    for f in os.listdir(TILE_DIR):
        os.remove(os.path.join(TILE_DIR, f))
    for key, ws in tiles.items():
        json.dump({"at": at, "ways": ws}, open(os.path.join(TILE_DIR, key + ".json"), "w"), ensure_ascii=False, separators=(",", ":"))
    json.dump({"at": at, "tiles": sorted(tiles)}, open(os.path.join(TILE_DIR, "index.json"), "w"), separators=(",", ":"))
    print("tiles", len(tiles))

if __name__ == "__main__":
    main()
