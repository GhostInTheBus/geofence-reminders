#!/usr/bin/env python3
"""geofence_poll.py - location-based reminders poller (Hermes cron `script` hook).

Run with NO arguments only from the Hermes cron job "geofence-poller".
By hand, use --dry-run (no writes) or one of the helper flags.

Contract with the Hermes scheduler (cron/scheduler.py, v0.10.0):
  * stdout is injected into the cron prompt.
  * If the LAST stdout line is {"wakeAgent": false} the LLM is skipped entirely
    and nothing is delivered. Every quiet tick ends that way.
  * A non-zero exit or a timeout makes Hermes tell the user. So: always exit 0.
  * The agent's final response is what gets delivered, AFTER the agent exits.
    So the agent cannot "confirm send, then mark fired". The poller claims a
    fire attempt itself and confirms delivery on the next tick from Hermes'
    own job status (cron/jobs.json + cron/output/<job_id>/).

Docs: README.md and SKILL.md in this repo.
"""
import argparse
import fcntl
import json
import math
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
REGISTRY = Path(os.environ.get("GEOFENCE_REGISTRY") or HERMES_HOME / "geofences.json")
ENV_FILE = Path(os.environ.get("DAWARICH_ENV_FILE") or HERMES_HOME / "secrets" / "dawarich.env")
LOG_FILE = HERMES_HOME / "logs" / "geofence_poll.log"
JOBS_FILE = HERMES_HOME / "cron" / "jobs.json"
CRON_OUTPUT = HERMES_HOME / "cron" / "output"

JOB_NAME = "geofence-poller"
DEFAULT_URL = "http://localhost:3000"   # set DAWARICH_URL in ~/.hermes/secrets/dawarich.env
WINDOW_MIN = 30        # presence evidence must be this fresh to fire
STALE_CAP_MIN = 180    # the phone's LAST KNOWN position still counts for this long (it only logs while moving)
PRE_MIN = 90           # "already there": arrived up to this long before the reminder was set
SETTLE_MIN = 20        # ...and has not been seen anywhere else for this long (covers late batch uploads)
MAX_ATTEMPTS = 2       # max FIRING emissions per geofence, ever
HTTP_TIMEOUT = 15      # seconds per request
RUN_BUDGET_S = 50      # whole poll run (Hermes kills the script at 120 s)
DEFAULT_DAYS = 7
FEED_STALE_H = 24      # warn once on Telegram when the phone's newest Dawarich point is older than this
                       # (a parked phone logs nothing, often all night, so a much lower value false-alarms each morning)
FEED_STATE = HERMES_HOME / "geofence_feed_state.json"
GATE_OFF = '{"wakeAgent": false}'
UA = "hermes-geofence-reminders/1.0 (+https://github.com/GhostInTheBus/geofence-reminders)"


class Down(Exception):
    """Dawarich unreachable / unusable. Always handled silently."""


# ---------- small helpers ----------
def now_local():
    return datetime.now().astimezone()


def parse_ts(v):
    """epoch (s or ms) or ISO-8601 string -> aware datetime, else None."""
    if v is None or isinstance(v, bool):
        return None
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and re.fullmatch(r"\d{9,13}(\.\d+)?", v.strip())):
            n = float(v)
            if n > 1e11:
                n /= 1000.0
            return datetime.fromtimestamp(n, tz=timezone.utc)
        s = str(v).strip().replace("Z", "+00:00")
        s = re.sub(r"(\.\d{6})\d+", r"\1", s)          # trim nanoseconds
        s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)  # +0000 -> +00:00
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def ago(dt, now):
    m = max(0, int((now - dt).total_seconds() // 60))
    if m < 90:
        return "%dm ago" % m
    if m < 2880:
        return "%dh ago" % (m // 60)
    return "%dd ago" % (m // 1440)


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(a))


def one_line(s):
    return re.sub(r"\s+", " ", str(s)).strip()


def log(msg):
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 512_000:
            LOG_FILE.write_text("".join(LOG_FILE.read_text(errors="replace").splitlines(True)[-1500:]))
        with LOG_FILE.open("a") as f:
            f.write("%s %s\n" % (now_local().isoformat(timespec="seconds"), msg))
    except Exception:
        pass


# ---------- Dawarich client ----------
def read_env_file(path):
    vals = {}
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.replace("export ", "").strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return vals


def load_creds():
    """-> (api_key or None, base_url). Never prints the key."""
    vals = read_env_file(ENV_FILE)
    key = os.environ.get("DAWARICH_API_KEY") or vals.get("DAWARICH_API_KEY")
    url = os.environ.get("DAWARICH_URL") or vals.get("DAWARICH_URL") or DEFAULT_URL
    return (key or None), url.rstrip("/")


_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # private or VPN address: never via a proxy


def api_get(path, params, creds):
    key, base = creds
    if not key:
        raise Down("no_api_key")
    req = urllib.request.Request(
        base + path + ("?" + urllib.parse.urlencode(params) if params else ""),
        headers={"Authorization": "Bearer " + key, "Accept": "application/json", "User-Agent": UA},
    )
    try:
        with _DIRECT.open(req, timeout=HTTP_TIMEOUT) as r:
            if r.status != 200:
                raise Down("http_%s" % r.status)
            return json.loads(r.read().decode("utf-8", "replace"))
    except Down:
        raise
    except urllib.error.HTTPError as e:
        raise Down("http_%s" % e.code)
    except (json.JSONDecodeError, UnicodeError):
        raise Down("bad_json")
    except Exception as e:  # timeout, refused, no route (VPN off), DNS...
        raise Down("unreachable_%s" % type(getattr(e, "reason", e)).__name__)


def point_latlon(p):
    lat = p.get("latitude", p.get("lat"))
    lon = p.get("longitude", p.get("lon", p.get("lng")))
    if (lat is None or lon is None) and isinstance(p.get("lonlat"), str):
        m = re.search(r"\(\s*(-?[\d.]+)\s+(-?[\d.]+)\s*\)", p["lonlat"])
        if m:
            lon, lat = m.group(1), m.group(2)
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


def recent_points(creds, now, minutes, limit=1000):
    """Newest-first GPS points from the last `minutes`. One call per poll run."""
    start = (now - timedelta(minutes=minutes)).astimezone(timezone.utc)
    data = api_get("/api/v1/points", {
        "start_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_at": (now + timedelta(minutes=5)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "order": "desc", "per_page": limit, "page": 1,
    }, creds)
    rows = data if isinstance(data, list) else (data.get("points") or data.get("data") or [])
    out = []
    for p in rows:
        if not isinstance(p, dict):
            continue
        ll, ts = point_latlon(p), parse_ts(p.get("timestamp") or p.get("recorded_at") or p.get("created_at"))
        if ll and ts:
            out.append((ts, ll[0], ll[1]))
    out.sort(reverse=True)
    return out


def locations_evidence(creds, e, now):
    """Spec source: /api/v1/locations. -> (latest_ts or None, dist_km or None)."""
    data = api_get("/api/v1/locations", {
        "lat": e["lat"], "lon": e["lon"], "radius": e["radius_km"],
        "radius_override": int(float(e["radius_km"]) * 1000),
        "date_from": (now - timedelta(days=2)).strftime("%Y-%m-%d"),
    }, creds)
    locs = data.get("locations") if isinstance(data, dict) else data
    best = (None, None)
    for loc in locs or []:
        if not isinstance(loc, dict):
            continue
        cands = [(parse_ts(loc.get("last_visit")), None)]
        for v in loc.get("visits") or []:
            if not isinstance(v, dict):
                continue
            det = v.get("visit_details") or {}
            d = v.get("distance_meters")
            for raw in (v.get("date"), v.get("timestamp"), det.get("end_time"), det.get("start_time")):
                cands.append((parse_ts(raw), d))
        for ts, d in cands:
            if ts and ts <= now + timedelta(minutes=10) and (best[0] is None or ts > best[0] or (ts == best[0] and best[1] is None)):
                best = (ts, (float(d) / 1000.0) if isinstance(d, (int, float)) else None)
    return best


# ---------- NAS mode ----------
# When ~/.hermes/secrets/geofence_nas.env exists, the always-on service on the NAS (geofence_nas.py)
# owns the registry, does the polling and sends Telegram itself. The helpers below just call its API,
# and the Hermes cron run turns into a health monitor. Rename that file and everything falls back to
# the local registry + Hermes cron poller.
NAS_ENV = Path(os.environ.get("GEOFENCE_NAS_ENV") or HERMES_HOME / "secrets" / "geofence_nas.env")


def nas_cfg():
    vals = read_env_file(NAS_ENV)
    url, tok = vals.get("GEOFENCE_API_URL"), vals.get("GEOFENCE_API_TOKEN")
    return (url.rstrip("/"), tok) if url and tok else None


def nas_call(path, body=None):
    """-> (http_status, json). Raises Down only when the NAS cannot be reached at all."""
    url, tok = nas_cfg()
    req = urllib.request.Request(url + path, data=(json.dumps(body).encode() if body is not None else None),
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    try:
        with _DIRECT.open(req, timeout=HTTP_TIMEOUT) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        raise Down("nas_unreachable_%s" % type(getattr(e, "reason", e)).__name__)


def _mins(m):
    return "unknown" if m is None else ("%dm" % m if m < 90 else ("%dh" % (m // 60) if m < 2880 else "%dd" % (m // 1440)))


def nas_health_line():
    st, h = nas_call("/health")
    if st != 200 or not h.get("ok"):
        raise Down("nas_http_%s" % st)
    return h, "%s | checks every %ss | last check %ss ago (%s) | dawarich=%s | telegram=%s | armed=%d | phone last reported %s ago" % (
        h.get("version"), h.get("poll_seconds"), h.get("last_poll_age_s"), h.get("last_poll"), h.get("dawarich"),
        "yes" if h.get("telegram") else "NO", h.get("active", 0), _mins(h.get("newest_point_age_min")))


def nas_monitor(dry_run):
    """Cron path in NAS mode: never fires anything, only records whether the NAS service is alive."""
    try:
        h, line = nas_health_line()
        print("NAS_MONITOR ok | " + line)
        log("nas%s ok armed=%d dawarich=%s phone_age=%s last_check=%ss" % (
            " dry" if dry_run else "", h.get("active", 0), h.get("dawarich"), _mins(h.get("newest_point_age_min")), h.get("last_poll_age_s")))
    except Down as d:
        print("NAS_MONITOR down %s" % d)
        log("nas%s DOWN %s" % (" dry" if dry_run else "", d))
    print(GATE_OFF)


def nas_add(a):
    name, task = one_line(a.name or ""), one_line(a.task or "")
    if not name or not task or a.lat is None or a.lon is None:
        print("ADD_ERROR need --name --lat --lon --task")
        return
    st, r = nas_call("/add", {"name": name, "task_text": task, "lat": a.lat, "lon": a.lon,
                              "radius_km": a.radius or 2.0, "days": a.days or DEFAULT_DAYS})
    if st != 200 or not r.get("ok"):
        print("ADD_ERROR %s" % (r.get("error") or "nas_http_%s" % st))
        return
    e = r["entry"]
    if r.get("duplicate"):
        print("ALREADY_WATCHING %s" % e["id"])
        return
    exp = parse_ts(e.get("expires_at"))
    print("✅ Watching %s, %g km, expires %s. Ping lands a few minutes after you arrive." % (
        e["name"], float(e["radius_km"]), exp.astimezone().strftime("%b %-d") if exp else "in 7 days"))
    print("id=%s" % e["id"])


def nas_list():
    st, r = nas_call("/list")
    if st != 200:
        print("LIST_ERROR %s" % (r.get("error") or "nas_http_%s" % st))
        return
    if not r.get("entries"):
        print("EMPTY")
    for e in r.get("entries", []):
        stt = ("fired:%s" % e.get("fire_status", "yes")) if e.get("fired_at") else "active"
        exp = parse_ts(e.get("expires_at"))
        print("%s | %s | %g km | %s | expires %s | %s" % (
            e.get("id"), e.get("name"), float(e.get("radius_km", 0)), stt,
            exp.astimezone().strftime("%b %-d %H:%M") if exp else "?", one_line(e.get("task_text", ""))))


def nas_simple(path, gid, done):
    st, r = nas_call(path, {"id": gid} if gid else {})
    if st == 404:
        print("NOT_FOUND %s" % gid)
    elif st != 200 or not r.get("ok"):
        print("ERROR %s" % (r.get("error") or "nas_http_%s" % st))
    elif r.get("already_fired"):
        print("ALREADY_FIRED %s | %s" % (gid, r["already_fired"]))
    else:
        print(done % (r.get("id") or gid))


# ---------- registry ----------
class Registry:
    """geofences.json with an flock and atomic writes. A corrupt file is never overwritten."""

    def __enter__(self):
        REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        self._lock = open(str(REGISTRY) + ".lock", "w")
        fcntl.flock(self._lock, fcntl.LOCK_EX)
        if REGISTRY.exists() and REGISTRY.read_text().strip():
            self.items = json.loads(REGISTRY.read_text())
            if not isinstance(self.items, list):
                raise ValueError("registry is not a JSON list")
        else:
            self.items = []
        return self

    def save(self):
        fd, tmp = tempfile.mkstemp(dir=str(REGISTRY.parent), prefix=".geofences_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.items, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, REGISTRY)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def __exit__(self, *a):
        try:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            self._lock.close()
        except Exception:
            pass
        return False


def message_line(e):
    return "📍 *%s* — %s" % (one_line(e.get("name", e["id"])), one_line(e.get("task_text", "")))


# ---------- delivery confirmation (reads Hermes' own cron bookkeeping) ----------
def delivery_confirmed(e):
    """True = Hermes delivered the reminder; False = it did not (or we can't tell)."""
    try:
        attempt = parse_ts(e.get("last_attempt_at"))
        job = next(j for j in json.loads(JOBS_FILE.read_text()).get("jobs", []) if j.get("name") == JOB_NAME)
        last_run = parse_ts(job.get("last_run_at"))
        if not attempt or not last_run or last_run < attempt:
            return False                      # no cron run finished since we claimed the attempt
        if job.get("last_status") != "ok" or job.get("last_delivery_error"):
            return False
        norm = lambda s: one_line(s).casefold()
        needles = [n for n in (norm(e.get("task_text", "")), norm(e.get("name", ""))) if n]
        # Ticks are sequential, so the newest output doc is the run that carried our FIRING.
        docs = list((CRON_OUTPUT / job["id"]).glob("*.md"))
        if not docs:
            return True                       # status says ok and there is nothing to cross-check
        body = max(docs, key=lambda d: d.stat().st_mtime).read_text(errors="replace")
        if "## Response" not in body:
            return False                      # agent was skipped or produced nothing
        resp = norm(body.rsplit("## Response", 1)[1])
        return "[silent]" not in resp and any(n in resp for n in needles)
    except Exception:
        return False


# ---------- phone feed watchdog ----------
def feed_check(creds, now, dry_run):
    """Warn ONCE (as a MESSAGE block line) when the phone has not reported to Dawarich for FEED_STALE_H hours.
    Re-arms itself when fresh points show up again. Dawarich unreachable = silent (can't tell feed from network).
    -> (log_lines, message_lines)"""
    try:
        st = json.loads(FEED_STATE.read_text()) if FEED_STATE.exists() else {}
        if not isinstance(st, dict):
            st = {}
    except Exception:
        st = {}

    def save(d):
        if not dry_run:
            try:
                FEED_STATE.write_text(json.dumps(d) + "\n")
            except OSError:
                pass

    try:
        pts = recent_points(creds, now, 7 * 24 * 60, limit=1)
    except Down as d:
        return ["FEED_CHECK skipped (%s)" % d], []
    newest = pts[0][0] if pts else None
    stale = newest is None or (now - newest) > timedelta(hours=FEED_STALE_H)
    seen = ago(newest, now) if newest else "none in 7d"
    if not stale:
        if st.get("warned_at"):
            save({"warned_at": None, "recovered_at": now.isoformat(timespec="seconds")})
            return ["FEED_OK recovered newest_point=%s" % seen], []
        return ["FEED_OK newest_point=%s" % seen], []
    if st.get("warned_at"):
        return ["FEED_STALE already_warned=%s newest_point=%s" % (st["warned_at"], seen)], []
    save({"warned_at": now.isoformat(timespec="seconds"), "newest_point": newest.isoformat(timespec="seconds") if newest else None})
    when = newest.astimezone().strftime("%a %b %-d %H:%M") if newest else "never (no points in 7 days)"
    msg = "⚠️ Location feed is stale: your phone last reported to Dawarich %s (%s). Location reminders can't fire until it's back. Check the tracker app (and your VPN, if you use one) on the phone." % (seen, when)
    return ["FEED_STALE WARNING newest_point=%s" % seen], [msg]


# ---------- poll ----------
def poll(dry_run):
    t0 = time.monotonic()
    now = now_local()
    lines, firing, changed = [], [], False
    n_no = 0
    down = None
    tag = "DRY_RUN " if dry_run else ""
    creds = load_creds()
    feed_lines, feed_msgs = feed_check(creds, now, dry_run)
    lines += feed_lines
    with Registry() as reg:
        # 1) GC expired entries (the only unconditional write)
        keep = []
        for e in reg.items:
            exp = parse_ts(e.get("expires_at"))
            if exp and exp < now:
                lines.append("%sEXPIRED %s | removed" % (tag, e.get("id")))
                changed = True
            else:
                keep.append(e)
        if not dry_run:
            reg.items = keep

        # 2) settle fire attempts claimed on an earlier tick
        active = []
        for e in keep:
            if e.get("fired_at"):
                continue
            if int(e.get("fire_attempts") or 0) > 0:
                if delivery_confirmed(e):
                    lines.append("%sCONFIRMED %s | delivered" % (tag, e["id"]))
                    if not dry_run:
                        e["fired_at"], e["fire_status"] = e.get("last_attempt_at"), "delivered"
                        changed = True
                elif int(e["fire_attempts"]) >= MAX_ATTEMPTS:
                    lines.append("%sGAVE_UP %s | unconfirmed after %d attempts, closed" % (tag, e["id"], MAX_ATTEMPTS))
                    if not dry_run:
                        e["fired_at"], e["fire_status"] = now.isoformat(timespec="seconds"), "unconfirmed"
                        changed = True
                else:
                    firing.append((e, "retry"))
                continue
            active.append(e)

        if not active and not firing:
            if changed and not dry_run:
                reg.save()
            lines.append("NO_ACTIVE_GEOFENCES")
            if feed_msgs and not dry_run:
                lines += ["MESSAGE_BEGIN"] + feed_msgs + ["MESSAGE_END"]
            else:
                if feed_msgs:
                    lines += ["DRY_RUN would send:"] + ["  " + m for m in feed_msgs]
                lines.append(GATE_OFF)
            print("\n".join(lines))
            log("poll%s no_active feed=%s" % (" dry" if dry_run else "", "WARN" if feed_msgs else feed_lines[0].split()[0]))
            return

        # 3) one Dawarich call (/points) covers every active geofence.
        #    If this Dawarich build has no /points, fall back to the per-geofence
        #    /locations search from the original spec.
        pts, per_fence = [], False
        last = None            # last known position: the phone's newest point, up to STALE_CAP_MIN old
        if active:
            try:
                pts = recent_points(creds, now, WINDOW_MIN + 5)
                last = pts[0] if pts else (recent_points(creds, now, STALE_CAP_MIN, limit=1) or [None])[0]
            except Down as d:
                if str(d) == "http_404":
                    per_fence = True
                else:
                    down = str(d)
        for i, e in enumerate(active):
            if down or time.monotonic() - t0 > RUN_BUDGET_S:
                break
            created = parse_ts(e.get("created_at")) or now
            hit = (now, 0.0) if e.get("force") else None   # --test-ping entries skip the location check
            if per_fence and not hit:
                try:
                    if i:
                        time.sleep(1)
                    ts, dist = locations_evidence(creds, e, now)
                    hit = (ts, dist if dist is not None else 0.0) if ts else None
                except Down as d:
                    down = str(d)
                    break
            for ts, lat, lon in ([] if hit else pts):   # newest first
                dist = haversine_km(float(e["lat"]), float(e["lon"]), lat, lon)
                if dist <= float(e["radius_km"]):
                    hit = (ts, dist)
                    break
            how = None
            if hit and (now - hit[0]) <= timedelta(minutes=WINDOW_MIN) and hit[0] >= created:
                how = "radius_hit=%.1fkm" % hit[1]
            elif last and not per_fence:
                # Last known position. The tracker only logs while moving, so if the newest point is inside
                # the radius the user is most likely still there, even when that point is older than the window
                # (late upload) or older than the reminder itself (they arrived before it was set).
                ld, age = haversine_km(float(e["lat"]), float(e["lon"]), last[1], last[2]), now - last[0]
                if ld <= float(e["radius_km"]) and age <= timedelta(minutes=STALE_CAP_MIN) and (
                        last[0] >= created or (last[0] >= created - timedelta(minutes=PRE_MIN) and age >= timedelta(minutes=SETTLE_MIN))):
                    how = "still_there=%.1fkm last_seen=%s" % (ld, ago(last[0], now).replace(" ago", ""))
            if how:
                firing.append((e, how))
            else:
                n_no += 1
                if not hit:
                    if pts or per_fence:
                        why = "not_in_radius (last %dm)" % (WINDOW_MIN + 5)
                    elif not last:
                        why = "no_recent_points (phone not reporting)"
                    elif haversine_km(float(e["lat"]), float(e["lon"]), last[1], last[2]) <= float(e["radius_km"]):
                        why = "here_since_long_before_reminder (%s)" % ago(last[0], now)
                    else:
                        why = "last_seen_elsewhere (%s)" % ago(last[0], now)
                elif hit[0] < created:
                    why = "visit_predates_reminder (%s)" % ago(hit[0], now)
                else:
                    why = "stale_last_visit (%s)" % ago(hit[0], now)
                lines.append("%sNO_FIRE %s | reason:%s" % (tag, e["id"], why))

        # 4) claim the attempts BEFORE announcing them (fail closed: no claim, no fire)
        if firing and not dry_run:
            for e, _ in firing:
                e["fire_attempts"] = int(e.get("fire_attempts") or 0) + 1
                e["last_attempt_at"] = now.isoformat(timespec="seconds")
            changed = True
        if changed and not dry_run:
            reg.save()

    for e, how in firing:
        lines.append("%sFIRING %s | %s | task: %s" % (tag, e["id"], how, one_line(e.get("task_text", ""))))
    if down:
        lines.append("DAWARICH_DOWN %s" % down)
    lines.append("=== %d FIRING, %d NO_FIRE ===" % (len(firing), n_no))
    outgoing = [message_line(e) for e, _ in firing] + feed_msgs
    if outgoing and not dry_run:
        lines += ["MESSAGE_BEGIN"] + outgoing + ["MESSAGE_END"]
    else:
        if outgoing:
            lines += ["DRY_RUN would send:"] + ["  " + m for m in outgoing]
        lines.append(GATE_OFF)
    print("\n".join(lines))
    log("poll%s firing=%d no_fire=%d down=%s pts=%d newest_point=%s ms=%d" % (
        " dry" if dry_run else "", len(firing), n_no, down or "-", len(pts),
        ago(last[0], now).replace(" ago", "") if last else "none", (time.monotonic() - t0) * 1000))


# ---------- helper commands ----------
def selftest():
    if nas_cfg():
        try:
            print("NAS OK | " + nas_health_line()[1])
        except Down as d:
            print("NAS FAIL | %s" % d)
    creds = load_creds()
    try:
        stats = api_get("/api/v1/stats", None, creds)
        size = len(stats) if isinstance(stats, (dict, list)) else 0
        print("SELFTEST OK | key+network+API fine | stats fields=%d" % size)
    except Down as d:
        print("SELFTEST FAIL | %s" % d)
        return
    try:
        now = now_local()
        pts = recent_points(creds, now, 7 * 24 * 60, limit=1)
        if pts:
            print("LAG newest_point_age=%s (%s)" % (ago(pts[0][0], now), pts[0][0].astimezone().strftime("%Y-%m-%d %H:%M %Z")))
        else:
            print("LAG no points in the last 7 days (phone not reporting?)")
    except Down as d:
        print("LAG unknown | points endpoint: %s" % d)


def whereami():
    try:
        now = now_local()
        pts = recent_points(load_creds(), now, 7 * 24 * 60, limit=1)
        print("WHEREAMI %.5f,%.5f | %s" % (pts[0][1], pts[0][2], ago(pts[0][0], now)) if pts else "WHEREAMI unknown | no points in 7 days")
    except Down as d:
        print("DAWARICH_DOWN %s" % d)


def _stats(vals):
    v = sorted(vals)
    return "median=%.1f p90=%.1f max=%.1f" % (v[len(v) // 2], v[min(len(v) - 1, int(len(v) * 0.9))], v[-1]) if v else "n/a"


def probe(spec):
    """Diagnostics for LAT,LON,RADIUS_KM: reporting cadence, ingest lag, endpoint behavior. Never prints the key."""
    parts = [x.strip() for x in spec.split(",")]
    creds, now = load_creds(), now_local()
    if parts[0].lower() == "here":   # here[,KM]: probe around the phone's last known position
        try:
            pts = recent_points(creds, now, 7 * 24 * 60, limit=1)
        except Down as d:
            print("PROBE points: DOWN %s" % d)
            return
        if not pts:
            print("PROBE no points in the last 7 days. Pass LAT,LON,KM instead.")
            return
        lat, lon, rad = pts[0][1], pts[0][2], float(parts[1]) if len(parts) > 1 else 15.0
    else:
        lat, lon, rad = [float(x) for x in parts]
    utc = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        t = time.monotonic()
        raw = api_get("/api/v1/points", {"start_at": utc(now - timedelta(days=7)), "end_at": utc(now + timedelta(minutes=5)),
                                         "order": "desc", "per_page": 1000, "page": 1}, creds)
        ms = (time.monotonic() - t) * 1000
        rows = [r for r in (raw if isinstance(raw, list) else []) if isinstance(r, dict)]
        print("PROBE points 7d: type=%s n=%d ms=%d" % (type(raw).__name__, len(rows), ms))
        if not rows:
            raw = api_get("/api/v1/points", {"end_at": utc(now + timedelta(minutes=5)), "order": "desc", "per_page": 5}, creds)
            rows = [r for r in (raw if isinstance(raw, list) else []) if isinstance(r, dict)]
            print("PROBE points all-time newest: n=%d" % len(rows))
        if rows:
            print("PROBE point keys=%s" % sorted(rows[0].keys()))
            hints = {k: rows[0].get(k) for k in ("tracker_id", "topic", "trigger", "connection", "battery", "battery_status", "import_id", "accuracy", "velocity") if k in rows[0]}
            print("PROBE newest point hints=%s" % json.dumps(hints, default=str))
            tss = [parse_ts(r.get("timestamp")) for r in rows]
            ok = [x for x in tss if x]
            print("PROBE point ts: parsed=%d/%d latlon_parsed=%d newest=%s oldest_in_page=%s imported=%d" % (
                len(ok), len(rows), sum(1 for r in rows if point_latlon(r)), ago(max(ok), now) if ok else "-", ago(min(ok), now) if ok else "-",
                sum(1 for r in rows if r.get("import_id"))))
            lags = [(c - t0).total_seconds() / 60 for c, t0 in ((parse_ts(r.get("created_at")), parse_ts(r.get("timestamp"))) for r in rows) if c and t0]
            print("PROBE ingest lag minutes (created_at - timestamp): n=%d %s" % (len(lags), _stats(lags)))
            srt = sorted(ok, reverse=True)
            print("PROBE gap minutes between points: %s" % _stats([(srt[i] - srt[i + 1]).total_seconds() / 60 for i in range(len(srt) - 1)]))
            trail = sorted(((parse_ts(r.get("timestamp")), point_latlon(r), r.get("velocity")) for r in rows if parse_ts(r.get("timestamp")) and point_latlon(r)), key=lambda z: z[0])
            for (t1, p1, v1), (t2, p2, _) in zip(trail, trail[1:]):
                if (t2 - t1).total_seconds() > 1800:
                    print("PROBE gap %s -> %s (%dm): speed_before=%s m/s, jump=%.1f km" % (
                        t1.astimezone().strftime("%m-%d %H:%M"), t2.astimezone().strftime("%m-%d %H:%M"),
                        (t2 - t1).total_seconds() // 60, str(v1)[:5], haversine_km(p1[0], p1[1], p2[0], p2[1])))
            days = {}
            for x in ok:
                d = x.astimezone().strftime("%m-%d")
                days[d] = days.get(d, 0) + 1
            print("PROBE points per day: %s" % " ".join("%s:%d" % kv for kv in sorted(days.items())))
            inside = [x for r, x in zip(rows, tss) if x and point_latlon(r) and haversine_km(lat, lon, *point_latlon(r)) <= rad]
            print("PROBE inside %gkm of target: n=%d newest=%s" % (rad, len(inside), ago(max(inside), now) if inside else "-"))
    except Down as d:
        print("PROBE points: DOWN %s" % d)
    try:
        slim = api_get("/api/v1/points", {"start_at": utc(now - timedelta(days=7)), "end_at": utc(now + timedelta(minutes=5)),
                                          "order": "desc", "per_page": 3, "slim": "true"}, creds)
        srows = [r for r in (slim if isinstance(slim, list) else []) if isinstance(r, dict)]
        print("PROBE slim=true: n=%d keys=%s parsed=%d" % (len(srows), sorted(srows[0].keys()) if srows else [],
                                                            sum(1 for r in srows if point_latlon(r) and parse_ts(r.get("timestamp")))))
    except Down as d:
        print("PROBE slim=true: DOWN %s" % d)
    for label, extra in (("radius(km) only", {"radius": rad}), ("radius_override(m) only", {"radius_override": int(rad * 1000)}),
                         ("radius=0.3km only", {"radius": 0.3}), ("no radius param", {})):
        try:
            time.sleep(1)
            t = time.monotonic()
            data = api_get("/api/v1/locations", dict({"lat": lat, "lon": lon}, **extra), creds)
            ms = (time.monotonic() - t) * 1000
            locs = data.get("locations") if isinstance(data, dict) else data
            visits = [v for l in (locs or []) if isinstance(l, dict) for v in (l.get("visits") or []) if isinstance(v, dict)]
            dists = [v.get("distance_meters") for v in visits if isinstance(v.get("distance_meters"), (int, float))]
            latest = [parse_ts(l.get("last_visit")) for l in (locs or []) if isinstance(l, dict)]
            latest = max([x for x in latest if x], default=None)
            print("PROBE locations %s: ms=%d locs=%d visits=%d max_dist_m=%s last_visit=%s meta=%s" % (
                label, ms, len(locs or []), len(visits), int(max(dists)) if dists else "-", ago(latest, now) if latest else "-",
                json.dumps((data.get("search_metadata") if isinstance(data, dict) else None), default=str)[:200]))
        except Down as d:
            print("PROBE locations %s: DOWN %s" % (label, d))


def mark_fired(gid):
    with Registry() as reg:
        for e in reg.items:
            if e.get("id") == gid:
                if e.get("fired_at"):
                    print("ALREADY_FIRED %s | %s" % (gid, e["fired_at"]))
                    return
                e["fired_at"], e["fire_status"] = now_local().isoformat(timespec="seconds"), "manual"
                reg.save()
                print("MARKED %s" % gid)
                return
    print("NOT_FOUND %s" % gid)


def add(a):
    name, task = one_line(a.name or ""), one_line(a.task or "")
    if not name or not task or a.lat is None or a.lon is None:
        print("ADD_ERROR need --name --lat --lon --task")
        return
    if not (-90 <= a.lat <= 90 and -180 <= a.lon <= 180) or (a.lat == 0 and a.lon == 0):
        print("ADD_ERROR coordinates out of range")
        return
    radius = a.radius if a.radius else 2.0
    if not (0.1 <= radius <= 50):
        print("ADD_ERROR radius must be 0.1 to 50 km")
        return
    now = now_local()
    with Registry() as reg:
        for e in reg.items:
            if not e.get("fired_at") and one_line(e.get("name", "")).casefold() == name.casefold() \
                    and one_line(e.get("task_text", "")).casefold() == task.casefold():
                print("ALREADY_WATCHING %s" % e["id"])
                return
        base = "%s-%s" % (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32] or "place", now.strftime("%Y-%m-%d"))
        gid, n = base, 2
        while any(e.get("id") == gid for e in reg.items):
            gid, n = "%s-%d" % (base, n), n + 1
        exp = now + timedelta(days=a.days or DEFAULT_DAYS)
        reg.items.append({
            "id": gid, "name": name, "lat": round(a.lat, 6), "lon": round(a.lon, 6), "radius_km": radius,
            "task_text": task, "created_at": now.isoformat(timespec="seconds"),
            "fired_at": None, "expires_at": exp.isoformat(timespec="seconds"),
        })
        reg.save()
    print("✅ Watching %s, %g km, expires %s. Ping lands a few minutes after you arrive." % (name, radius, exp.strftime("%b %-d")))
    print("id=%s" % gid)


def test_ping():
    """One-shot delivery check: fires on the next cron tick wherever the phone is."""
    now = now_local()
    with Registry() as reg:
        gid = "test-ping-%s" % now.strftime("%Y%m%d-%H%M%S")
        reg.items.append({"id": gid, "name": "Test ping", "lat": 0.0, "lon": 0.0, "radius_km": 1.0, "force": True,
                          "task_text": "TEST: ignore. Location reminders can reach you.",
                          "created_at": now.isoformat(timespec="seconds"), "fired_at": None,
                          "expires_at": (now + timedelta(hours=2)).isoformat(timespec="seconds")})
        reg.save()
    print("TEST_PING queued | fires on the next 10 min tick | id=%s" % gid)


def migrate_local():
    """Cutover helper: move reminders that are still armed in the local registry over to the NAS."""
    moved, now = 0, now_local()
    with Registry() as reg:
        for e in reg.items:
            if e.get("fired_at") or e.get("force"):
                continue
            exp = parse_ts(e.get("expires_at")) or (now + timedelta(days=DEFAULT_DAYS))
            if exp <= now:
                continue
            st, r = nas_call("/add", {"name": e.get("name"), "task_text": e.get("task_text"), "lat": e.get("lat"), "lon": e.get("lon"),
                                      "radius_km": e.get("radius_km"), "days": max(1, math.ceil((exp - now).total_seconds() / 86400))})
            if st == 200 and r.get("ok"):
                e["fired_at"], e["fire_status"] = now.isoformat(timespec="seconds"), "migrated_to_nas"
                moved += 1
            else:
                print("MIGRATE_SKIPPED %s | %s" % (e.get("id"), r.get("error") or "nas_http_%s" % st))
        if moved:
            reg.save()
    print("MIGRATED %d armed reminder(s) to the NAS" % moved)


def list_entries():
    now = now_local()
    with Registry() as reg:
        if not reg.items:
            print("EMPTY")
        for e in reg.items:
            st = ("fired:%s" % e.get("fire_status", "yes")) if e.get("fired_at") else ("pending" if e.get("fire_attempts") else "active")
            exp = parse_ts(e.get("expires_at"))
            print("%s | %s | %g km | %s | expires %s | %s" % (
                e.get("id"), e.get("name"), float(e.get("radius_km", 0)), st,
                exp.astimezone().strftime("%b %-d %H:%M") if exp else "?", one_line(e.get("task_text", ""))))


def remove(gid):
    with Registry() as reg:
        n = len(reg.items)
        reg.items = [e for e in reg.items if e.get("id") != gid]
        if len(reg.items) < n:
            reg.save()
            print("REMOVED %s" % gid)
        else:
            print("NOT_FOUND %s" % gid)


def geocode(q, near=None):
    """Nominatim lookup for intake. --near LAT,LON (or "here") limits results to ~40 km around that point."""
    try:
        params, origin = {"q": q, "format": "jsonv2", "limit": 5}, None
        if near:
            if near.strip().lower() == "here":
                pts = recent_points(load_creds(), now_local(), 7 * 24 * 60, limit=1)
                if not pts:
                    print("GEOCODE_ERROR no current location (phone not reporting). Ask which city.")
                    return
                origin = (pts[0][1], pts[0][2])
            else:
                origin = tuple(float(x) for x in near.split(","))
            params.update({"viewbox": "%f,%f,%f,%f" % (origin[1] - 0.45, origin[0] + 0.4, origin[1] + 0.45, origin[0] - 0.4), "bounded": 1})
        req = urllib.request.Request("https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params),
                                     headers={"User-Agent": UA, "Accept-Language": "en"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            rows = json.loads(r.read().decode("utf-8", "replace"))
        if origin:
            rows.sort(key=lambda x: haversine_km(origin[0], origin[1], float(x["lat"]), float(x["lon"])))
        if not rows:
            print("GEOCODE none")
        for x in rows[:3]:
            away = " | %.1f km away" % haversine_km(origin[0], origin[1], float(x["lat"]), float(x["lon"])) if origin else ""
            print("GEOCODE %.5f,%.5f | %s/%s | %s%s" % (float(x["lat"]), float(x["lon"]), x.get("category", "?"), x.get("type", "?"), x.get("display_name", ""), away))
    except Down as d:
        print("DAWARICH_DOWN %s" % d)
    except Exception as e:
        print("GEOCODE_ERROR %s" % type(e).__name__)


def main():
    ap = argparse.ArgumentParser(description="Location-based reminders poller for Hermes cron.")
    ap.add_argument("--selftest", action="store_true", help="check key + network + API, report location lag")
    ap.add_argument("--dry-run", action="store_true", help="show what a poll would do; no writes")
    ap.add_argument("--mark-fired", metavar="ID", help="manually close a geofence (it never fires again)")
    ap.add_argument("--add", action="store_true", help="add a reminder (needs --name --lat --lon --task)")
    ap.add_argument("--name"); ap.add_argument("--task")
    ap.add_argument("--lat", type=float); ap.add_argument("--lon", type=float)
    ap.add_argument("--radius", type=float, help="km (default 2)")
    ap.add_argument("--days", type=int, help="expiry in days (default 7)")
    ap.add_argument("--list", action="store_true"); ap.add_argument("--remove", metavar="ID")
    ap.add_argument("--geocode", metavar="QUERY"); ap.add_argument("--near", metavar="LAT,LON|here")
    ap.add_argument("--whereami", action="store_true")
    ap.add_argument("--test-ping", action="store_true", help="queue a one-shot test message for the next tick")
    ap.add_argument("--probe", metavar="LAT,LON,KM|here,KM", help="diagnostics for both Dawarich sources")
    ap.add_argument("--nas-health", action="store_true", help="one line: is the NAS service alive")
    ap.add_argument("--migrate-local", action="store_true", help="cutover: copy still-armed local reminders to the NAS")
    a = ap.parse_args()
    nas = nas_cfg() is not None
    if a.nas_health:
        try:
            print(("NAS OK | " + nas_health_line()[1]) if nas else "NAS OFF | no geofence_nas.env, running in local mode")
        except Down as d:
            print("NAS FAIL | %s" % d)
        return
    if nas and (a.add or a.list or a.remove or a.mark_fired or a.test_ping or a.migrate_local):
        try:
            if a.migrate_local: migrate_local()
            elif a.add: nas_add(a)
            elif a.list: nas_list()
            elif a.remove: nas_simple("/remove", a.remove, "REMOVED %s")
            elif a.mark_fired: nas_simple("/mark-fired", a.mark_fired, "MARKED %s")
            else: nas_simple("/test-ping", None, "TEST_PING sent to the NAS | arrives in a few seconds | id=%s")
        except Down as d:
            print("NAS_DOWN %s | nothing was changed" % d)
        return
    if a.selftest: selftest()
    elif a.mark_fired: mark_fired(a.mark_fired)
    elif a.add: add(a)
    elif a.list: list_entries()
    elif a.remove: remove(a.remove)
    elif a.geocode: geocode(a.geocode, a.near)
    elif a.whereami: whereami()
    elif a.test_ping: test_ping()
    elif a.probe: probe(a.probe)
    elif nas: nas_monitor(a.dry_run)
    else: poll(a.dry_run)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:   # never a non-zero exit, never a traceback: Hermes would message the user
        print("POLLER_ERROR %s" % type(exc).__name__)
        print(GATE_OFF)
        log("error %s: %s" % (type(exc).__name__, one_line(exc)[:200]))
    sys.exit(0)
