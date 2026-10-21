#!/usr/bin/env python3
"""
45th Weather Squadron Operations Dashboard - data collector.

Run by GitHub Actions every ~5 minutes. Fetches the Merlin lightning, WINDS and
field mill (LPLWS) APIs, plus METARs and TAFs from aviationweather.gov, and writes <out>/data/latest.json, which the dashboard
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
    "avwx":  "https://aviationweather.gov",
    "nhc":   "https://www.nhc.noaa.gov",
    "iem":   "https://mesonet.agron.iastate.edu",
    "ll":    "https://ll.thespacedevs.com",
}
STATIONS = os.environ.get("WX_STATIONS", "KXMR,KTTS,KCOF,KMLB")
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
    keep = ("reading_datetime", "tower", "hgt", "lat", "lon", "avg_dir_1min", "avg_spd_1min", "peak_spd_1min", "peak_spd_10min", "tmp")
    rows = [{k: r.get(k) for k in keep} for r in rows
            if r.get("avg_spd_1min") is not None or r.get("peak_spd_1min") is not None or r.get("tmp") is not None]
    return {"rows": rows, "window_start": start.isoformat()}


def site_of(r):
    """Kennedy (KSC) or Cape (CCSFS), using the same location rules as the dashboard."""
    lat, lon = r.get("lat"), r.get("lon")
    if not lat or not lon or lat < 28.375:
        return "OTHER"
    return "CCSFS" if lon > -80.612 and lat < 28.595 else "KSC"


def winds_max(now, hist_rows, prev):
    """Highest 1-min sustained and peak per minute, per site and sensor height, kept for 12 hours."""
    points = {r["t"]: {k: v for k, v in r["h"].items() if "|" in k} for r in prev.get("rows", []) if "t" in r}
    fresh = {}
    for r in hist_rows:
        t = (r.get("reading_datetime") or "")[:16]
        if not t:
            continue
        h = site_of(r) + "|" + (str(round(r["hgt"])) if r.get("hgt") is not None else "?")
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


def temps_min(now, hist_rows, prev):
    """Coldest temperature per 5 minutes, per site and sensor height, kept for 24 hours."""
    points = {r["t"]: r["s"] for r in prev.get("rows", []) if "t" in r}
    for r in hist_rows:
        t, v = (r.get("reading_datetime") or "")[:16], r.get("tmp")
        if not t or v is None or v >= 999 or v <= -100:
            continue
        b = t[:14] + "%02d" % (int(t[14:16]) // 5 * 5) + ":00Z"
        k = site_of(r) + "|" + (str(round(r["hgt"])) if r.get("hgt") is not None else "?")
        e = points.setdefault(b, {})
        if k not in e or v < e[k][0]:
            e[k] = [v, r.get("tower")]
    cut = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:00Z")
    return {"rows": [{"t": t, "s": s} for t, s in sorted(points.items()) if t >= cut]}


def metar(now):
    return {"rows": as_list(get(API["avwx"], "/api/data/metar", f"ids={STATIONS}&format=json&hours=3"))}


def taf(now):
    return {"rows": as_list(get(API["avwx"], "/api/data/taf", f"ids={STATIONS}&format=json"))}


HIST_HOURS = 24


def _minute(t):
    return int(datetime.strptime(t[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=UTC).timestamp() // 60)


def _wv(v):
    return None if v is None or v >= 999 else round(v, 1)


def winds_24h(now, recent_rows, prev):
    """Per-sensor 1-min winds for the last 24 hours, written to data/winds_hist.json for the tower trend.
    Each run adds the last hour; missing hours (first run, or after a gap) are filled from /winds/range."""
    t0p = prev.get("t0", 0)
    hist = {k: {t0p + r[0]: r[1:] for r in rows} for k, rows in prev.get("s", {}).items()}
    now_m = int(now.timestamp() // 60)
    start_m = now_m - HIST_HOURS * 60

    def add(rows):
        for r in rows:
            t, h = r.get("reading_datetime"), r.get("hgt")
            if not t or not r.get("tower"):
                continue
            a, p, p10, d = _wv(r.get("avg_spd_1min")), _wv(r.get("peak_spd_1min")), _wv(r.get("peak_spd_10min")), _wv(r.get("avg_dir_1min"))
            if a is None and p is None:
                continue
            k = str(r["tower"]).strip() + ("@%d" % round(h) if h is not None else "")
            hist.setdefault(k, {})[_minute(t)] = [a, p, p10, d]

    add(recent_rows)
    # fill hours we don't have yet, newest first, at most 24 requests per run
    have = set()
    for m in hist.values():
        have.update(x // 60 for x in m)
    end = now - timedelta(minutes=WIND_HIST_MIN)
    for _ in range(HIST_HOURS):
        a = end - timedelta(hours=1)
        if int(a.timestamp() // 60) < start_m - 60:
            break
        if int(a.timestamp() // 3600) not in have or int(end.timestamp() // 3600) not in have:
            try:
                add(as_list(get(API["winds"], "/winds/range", f"date>={stamp(a)}&date<={stamp(end)}&sort=asc")))
            except Exception as e:
                print(f"winds history {stamp(a)}: {e}", file=sys.stderr)
                break
        end = a
    out = {}
    for k, m in hist.items():
        rows = [[t - start_m] + v for t, v in sorted(m.items()) if t >= start_m]
        if rows:
            out[k] = rows
    return {"t0": start_m, "generated_at": now.isoformat(), "s": out}


def previous_hist():
    base = os.environ.get("PAGES_URL", "").rstrip("/")
    if not base:
        return {}
    try:
        r = requests.get(base + "/data/winds_hist.json", timeout=30, headers=HEADERS)
        return r.json() if r.ok else {}
    except Exception:
        return {}


def nhc_storms(now):
    d = get(API["nhc"], "/CurrentStorms.json")
    return {"rows": [s for s in (d or {}).get("activeStorms", []) if str(s.get("id", "")).lower().startswith("al")]}


def raob(now):
    """Latest Cape Canaveral (XMR) sounding from the Iowa Environmental Mesonet."""
    t = now.replace(minute=0, second=0, microsecond=0, hour=12 if now.hour >= 12 else 0)
    for _ in range(4):
        d = get(API["iem"], "/json/raob.py", f"station=KXMR&ts={t.strftime('%Y%m%d%H%M')}")
        p = (d or {}).get("profiles", [])
        if p and len(p[0].get("profile", [])) > 5:
            return {"rows": p[:1]}
        t -= timedelta(hours=12)
    raise RuntimeError("No recent XMR sounding")


LAUNCH_EVERY_MIN = 30   # the free Launch Library allows about 15 requests an hour


def launches(now, prev):
    last = prev.get("fetched_at")
    if last and prev.get("status") == "ok" and now - datetime.fromisoformat(last) < timedelta(minutes=LAUNCH_EVERY_MIN):
        return {"rows": prev.get("rows", []), "kept": True}
    d = get(API["ll"], "/2.2.0/launch/upcoming/", "location__ids=12,27&limit=12")
    return {"rows": (d or {}).get("results", [])}


def efm(now):
    rows = as_list(get(API["efm"], "/lplws_1min/latest"))
    if not rows:
        raise RuntimeError("No field mill readings returned")
    return {"rows": rows}


FEEDS = {"c2g": lambda n: lightning("c2g", n), "c2c": lambda n: lightning("c2c", n),
         "winds": winds, "winds_hist": winds_hist, "efm": efm, "metar": metar, "taf": taf, "nhc_storms": nhc_storms, "raob": raob}
OPTIONAL = ("metar", "taf", "nhc_storms", "raob", "launches")


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
    try:
        lp = old.get("launches", {})
        r = launches(now, lp)
        kept = r.pop("kept", False)
        result["feeds"]["launches"] = {**r, "status": "ok", "fetched_at": lp.get("fetched_at") if kept else now.isoformat(), "error": None}
    except Exception as e:
        prev = old.get("launches", {})
        result["feeds"]["launches"] = {**prev, "rows": prev.get("rows", []), "status": "error", "error": str(e)[:300], "failed_at": now.isoformat()}
    try:
        hist = result["feeds"]["winds_hist"]["rows"] if result["feeds"]["winds_hist"]["status"] == "ok" else []
        result["feeds"]["temps_min"] = {**temps_min(now, hist, old.get("temps_min", {})), "status": "ok" if hist else "error",
                                        "fetched_at": now.isoformat(), "error": None if hist else "No recent tower readings"}
    except Exception as e:
        result["feeds"]["temps_min"] = {**old.get("temps_min", {"rows": []}), "status": "error", "error": str(e)[:300]}
    os.makedirs(os.path.join(out, "data"), exist_ok=True)
    try:
        recent = result["feeds"]["winds_hist"]["rows"] if result["feeds"]["winds_hist"]["status"] == "ok" else []
        h = winds_24h(now, recent, previous_hist())
        hp = os.path.join(out, "data", "winds_hist.json")
        with open(hp + ".tmp", "w") as f:
            json.dump(h, f, separators=(",", ":"), allow_nan=False)
        os.replace(hp + ".tmp", hp)
        print(f"winds_hist.json: {len(h['s'])} sensors, {sum(len(v) for v in h['s'].values())} readings")
    except Exception as e:
        print(f"winds history: {e}", file=sys.stderr)
    path = os.path.join(out, "data", "latest.json")
    with open(path + ".tmp", "w") as f:
        json.dump(result, f, separators=(",", ":"), allow_nan=False)
    os.replace(path + ".tmp", path)
    print(json.dumps({k: {"status": v["status"], "rows": len(v["rows"])} for k, v in result["feeds"].items()}))
    if all(v["status"] == "error" for k, v in result["feeds"].items() if k not in OPTIONAL):
        sys.exit("Every feed failed")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "site")
