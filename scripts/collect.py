#!/usr/bin/env python3
"""
45th Weather Squadron Operations Dashboard - data collector.

Run by GitHub Actions every ~5 minutes. Fetches the Merlin lightning, WINDS and
field mill (LPLWS) APIs and writes <out>/data/latest.json, which the dashboard
reads from the same site. If a feed fails, the last good data from the live
site is kept and the feed is marked with its error.

    python scripts/collect.py site          # writes site/data/latest.json
"""
import json, os, sys, time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import requests

UTC = timezone.utc
API = {
    "c2g":   "https://codiapi-lightningc2g.rad.spaceforce.mil",
    "c2c":   "https://codiapi-lightningc2c.rad.spaceforce.mil",
    "winds": "https://codiapi-winds1.rad.spaceforce.mil",
    "efm":   "https://codiapi-lplws1.rad.spaceforce.mil",
}
API.update(json.loads(os.environ.get("WX_API_OVERRIDE", "{}")))   # for testing only
BOX = {"lat": (26.0, 31.0), "lon": (-84.0, -77.0)}                 # lightning area kept (Florida and waters)
LIGHTNING_MIN, WIND_HIST_MIN = 60, 60
HEADERS = {"User-Agent": "45WS-Ops-Dashboard/1.0", "Accept": "application/json"}


def stamp(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S")


def get(base, path, query=""):
    """GET with retries. `query` is written the way the API guides show it (date>=...&lat<=...)."""
    url = base + path + ("?" + quote(query, safe="=&:.-T,") if query else "")
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=40)
            if r.status_code >= 500 or r.status_code == 429:
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            return data
        except (requests.RequestException, ValueError) as e:
            if attempt == 2:
                raise RuntimeError(str(e)[:200])
            time.sleep(2 ** attempt)


def as_list(data):
    if isinstance(data, list):
        return data
    raise RuntimeError("Expected a list of observations")


def lightning(kind, now):
    start = now - timedelta(minutes=LIGHTNING_MIN)

    def window(a, b):
        q = (f"date>={stamp(a)}&date<={stamp(b)}&lat>={BOX['lat'][0]}&lat<={BOX['lat'][1]}"
             f"&lon>={BOX['lon'][0]}&lon<={BOX['lon'][1]}&sort=asc")
        try:
            return as_list(get(API[kind], f"/lightning_{kind}/range", q))
        except RuntimeError as e:   # too many rows: split the window in half
            if ("100" in str(e) and "000" in str(e) or "too many" in str(e).lower()) and (b - a) > timedelta(seconds=30):
                mid = a + (b - a) / 2
                return window(a, mid) + window(mid, b)
            raise

    keep = ("id", "event_datetime", "lat_decimal", "lon_decimal", "signal_strength", "sensor_ids")
    rows = {r.get("id", i): {k: r.get(k) for k in keep} for i, r in enumerate(window(start, now))}
    return {"rows": list(rows.values()), "window_start": start.isoformat()}


def winds(now):
    rows = as_list(get(API["winds"], "/winds/latest"))
    if not rows:
        raise RuntimeError("No tower readings returned")
    return {"rows": rows}


def winds_hist(now):
    start = now - timedelta(minutes=WIND_HIST_MIN)
    rows = as_list(get(API["winds"], "/winds/range", f"date>={stamp(start)}&date<={stamp(now)}&sort=asc"))
    keep = ("reading_datetime", "tower", "hgt", "lat", "lon", "avg_dir_1min", "avg_spd_1min", "peak_spd_1min", "peak_spd_10min")
    rows = [{k: r.get(k) for k in keep} for r in rows if r.get("avg_spd_1min") is not None or r.get("peak_spd_1min") is not None]
    return {"rows": rows, "window_start": start.isoformat()}


def winds_max(now, hist_rows, prev):
    """Highest 1-min sustained and peak per minute and per sensor height, kept for 12 hours."""
    points = {r["t"]: r["h"] for r in prev.get("rows", []) if "t" in r}
    fresh = {}
    for r in hist_rows:
        t = (r.get("reading_datetime") or "")[:16]
        if not t:
            continue
        h = str(round(r["hgt"])) if r.get("hgt") is not None else "?"
        e = fresh.setdefault(t + ":00Z", {}).setdefault(h, [None, None, "", ""])
        a, p = r.get("avg_spd_1min"), r.get("peak_spd_1min")
        if a is not None and a < 999 and (e[0] is None or a > e[0]):
            e[0], e[2] = a, r.get("tower")
        if p is not None and p < 999 and (e[1] is None or p > e[1]):
            e[1], e[3] = p, r.get("tower")
    points.update(fresh)
    cut = (now - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:00Z")
    rows = [{"t": t, "h": h} for t, h in sorted(points.items()) if t >= cut]
    return {"rows": rows}


def efm(now):
    rows = as_list(get(API["efm"], "/lplws_1min/latest"))
    if not rows:
        raise RuntimeError("No field mill readings returned")
    return {"rows": rows}


FEEDS = {"c2g": lambda n: lightning("c2g", n), "c2c": lambda n: lightning("c2c", n),
         "winds": winds, "winds_hist": winds_hist, "efm": efm}


def previous():
    """Last good data from the live site, used when a feed fails this run."""
    base = os.environ.get("PAGES_URL", "").rstrip("/")
    if not base:
        return {}
    try:
        r = requests.get(base + "/data/latest.json", timeout=20, headers=HEADERS)
        return r.json() if r.ok else {}
    except Exception:
        return {}


def main(out):
    now = datetime.now(UTC).replace(microsecond=0)
    old = previous().get("feeds", {})
    result = {"generated_at": now.isoformat(), "collector": "GitHub Actions", "feeds": {}}
    for name, fn in FEEDS.items():
        try:
            result["feeds"][name] = {**fn(now), "status": "ok", "fetched_at": now.isoformat(), "error": None}
        except Exception as e:
            prev = old.get(name, {})
            result["feeds"][name] = {**prev, "rows": prev.get("rows", []), "status": "error",
                                     "error": str(e)[:300], "failed_at": now.isoformat()}
            print(f"{name}: {e}", file=sys.stderr)
    try:
        hist = result["feeds"]["winds_hist"]["rows"] if result["feeds"]["winds_hist"]["status"] == "ok" else []
        result["feeds"]["winds_max"] = {**winds_max(now, hist, old.get("winds_max", {})), "status": "ok" if hist else "error",
                                        "fetched_at": now.isoformat(), "error": None if hist else "No recent tower readings"}
    except Exception as e:
        result["feeds"]["winds_max"] = {**old.get("winds_max", {"rows": []}), "status": "error", "error": str(e)[:300]}
    os.makedirs(os.path.join(out, "data"), exist_ok=True)
    path = os.path.join(out, "data", "latest.json")
    with open(path + ".tmp", "w") as f:
        json.dump(result, f, separators=(",", ":"), allow_nan=False)
    os.replace(path + ".tmp", path)
    print(json.dumps({k: {"status": v["status"], "rows": len(v["rows"])} for k, v in result["feeds"].items()}))
    if all(v["status"] == "error" for v in result["feeds"].values()):
        sys.exit("Every feed failed")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "site")
