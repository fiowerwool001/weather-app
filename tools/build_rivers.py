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


def overpass_box(s, w, n, e):
    q = f'[out:json][timeout:180];way["waterway"="river"]({s},{w},{n},{e});out tags geom;'
    for host in ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
                 "https://maps.mail.ru/osm/tools/overpass/api/interpreter"):
        try:
            return http(host, data=urllib.parse.urlencode({"data": q}).encode(), tries=2, timeout=240)
        except Exception as e:  # noqa: BLE001
            print("overpass failed", host, e, file=sys.stderr)
    return None


def load_geometry():
    """River lines, fetched as 2x2 degree boxes (the whole country in one query times out). Resumable; refreshed weekly."""
    g = json.load(open(GEOM_FILE)) if os.path.exists(GEOM_FILE) else {}
    if not g or time.time() - g.get("at", 0) > 7 * 86400:
        g = {"at": time.time(), "done": [], "ways": {}}
    S, W, N, E = BBOX
    boxes = [(la, lo) for la in range(math.floor(S), math.ceil(N), 2) for lo in range(math.floor(W), math.ceil(E), 2)]
    boxes.sort(key=region_order)
    start = time.time()
    for la, lo in boxes:
        key = f"{la}_{lo}"
        if key in g["done"]:
            continue
        if time.time() - start > 25 * 60:
            print("geometry time budget used; the rest next run"); break
        j = overpass_box(la, lo, la + 2, lo + 2)
        if j is None:
            continue
        for el in j.get("elements", []):
            pts = [(round(p["lat"], 4), round(p["lon"], 4)) for p in el.get("geometry") or []]
            pts = simplify(pts, 0.0015)
            if len(pts) < 2:
                continue
            tags = el.get("tags", {})
            g["ways"][str(el["id"])] = {"n": tags.get("name:th") or tags.get("name") or "", "c": [[round(a, 3), round(b, 3)] for a, b in pts]}
        g["done"].append(key)
        print("box", key, "ways so far", len(g["ways"]))
        json.dump(g, open(GEOM_FILE, "w"), ensure_ascii=False, separators=(",", ":"))
        time.sleep(5)
    print("boxes done", len(g["done"]), "of", len(boxes))
    return list(g["ways"].values()), g["done"]


def region_order(p):
    """South first, then the other regions going north (Ram's request); boxes outside Thailand's land last."""
    la, lo = p
    south = la < 11 and lo <= 101
    thai = lo <= 103 or 13 <= la < 19
    return (0 if south else 1 if thai else 2, la, lo)

FLOW_FILE = "rivers-flow.json"
MED_BUDGET = 1200      # new cells per run for the 30-day normal (keeps each run under the hourly API limit)


def flood_batch(part, params):
    url = "https://flood-api.open-meteo.com/v1/flood?" + urllib.parse.urlencode(dict(
        latitude=",".join(f"{k[0]:.3f}" for k in part), longitude=",".join(f"{k[1]:.3f}" for k in part), **params))
    for k, wait in enumerate((0, 70, 150, 300)):
        time.sleep(wait)
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=120) as r:
                j = json.load(r)
            return j if isinstance(j, list) else [j]
        except Exception as e:  # noqa: BLE001
            print("flood retry", k + 1, e, file=sys.stderr)
    return None


def load_flow(keys):
    """Two passes, both resumable: the normal flow (30-day median, refreshed monthly) and the 14-day peak forecast (daily)."""
    flow = json.load(open(FLOW_FILE)) if os.path.exists(FLOW_FILE) else {}
    now = time.time()
    kid = lambda k: f"{k[0]:.2f},{k[1]:.2f}"
    keys = sorted(keys, key=lambda k: region_order((k[0], k[1])))
    need_med = [k for k in keys if kid(k) not in flow or now - flow[kid(k)].get("mat", 0) > 30 * 86400][:MED_BUDGET]
    print("normal-flow cells this run", len(need_med))
    for b in range(0, len(need_med), 50):
        part = need_med[b:b + 50]
        j = flood_batch(part, {"daily": "river_discharge", "past_days": 30, "forecast_days": 1})
        if j is None:
            print("stopping normal-flow pass early (API busy)"); break
        for k, loc in zip(part, j):
            q = [v for v in ((loc or {}).get("daily") or {}).get("river_discharge") or [] if v is not None]
            e = flow.setdefault(kid(k), {})
            e["mat"] = now
            e["med"] = round(sorted(q)[len(q) // 2], 1) if q else None
        time.sleep(15)
    json.dump(flow, open(FLOW_FILE, "w"), separators=(",", ":"))
    # peak forecast only for cells that sit on a real river
    river = [k for k in keys if (flow.get(kid(k)) or {}).get("med") is not None and flow[kid(k)]["med"] >= 3]
    need_pk = [k for k in river if now - flow[kid(k)].get("pat", 0) > 18 * 3600]
    print("river cells", len(river), "peak forecast this run", len(need_pk))
    for b in range(0, len(need_pk), 100):
        part = need_pk[b:b + 100]
        j = flood_batch(part, {"daily": "river_discharge,river_discharge_max", "forecast_days": 14})
        if j is None:
            print("stopping peak pass early (API busy)"); break
        for k, loc in zip(part, j):
            d = (loc or {}).get("daily") or {}
            q, qm, t = d.get("river_discharge") or [], d.get("river_discharge_max") or [], d.get("time") or []
            if not t:
                continue
            qm = [x if x is not None else 0 for x in (qm or q)]
            i = max(range(len(qm)), key=lambda n: qm[n])
            e = flow[kid(k)]
            e.update(pat=now, peak=round(qm[i], 1), now=round(q[0] or 0, 1) if q else 0, day=t[i])
        time.sleep(12)
    json.dump(flow, open(FLOW_FILE, "w"), separators=(",", ":"))
    return flow


def main():
    ways, boxes_done = load_geometry()
    # sample points along each river, one per GloFAS cell (0.05 deg)
    cells = {}
    for w in ways:
        c = w["c"]
        cum = [0.0]
        for i in range(1, len(c)):
            cum.append(cum[-1] + km(c[i - 1], c[i]))
        L = cum[-1]
        n = max(1, round(L / SAMPLE_KM))
        w["cum"], w["step"], w["keys"] = cum, max(L / n, 1e-6), []  # some OSM ways have zero length
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
    flow = load_flow(keys)
    # every stretch keeps the fields it has: [first vertex index, med, peak, now, peakDay]
    for key in keys:
        f = flow.get(f"{key[0]:.2f},{key[1]:.2f}")
        if f and f.get("med") is not None and f["med"] >= 3 and f.get("peak") is not None:
            cells[key] = (f["med"], f["peak"], f["now"], f["day"])
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
    json.dump({"at": at, "tiles": sorted(tiles), "boxes": sorted(boxes_done)}, open(os.path.join(TILE_DIR, "index.json"), "w"), separators=(",", ":"))
    print("tiles", len(tiles))

if __name__ == "__main__":
    main()
