#!/usr/bin/env python3
"""geofence_nas.py - always-on location reminders, runs in a container on the NAS.

One process, two jobs:
  * Poll loop: every POLL_SECONDS it asks Dawarich (same box) for recent GPS points,
    checks every armed geofence, and sends the reminder straight to Telegram.
    A geofence is marked fired only after Telegram answers ok. One shot, never re-armed.
  * Small HTTP API (bearer token) so the machine running Hermes can add / list / remove reminders.

Fail silent: Dawarich down, Telegram down, phone not reporting, bad registry -> no message,
try again next cycle. Stdlib only. Secrets come from the environment and are never logged.

Docs: README.md and SKILL.md in this repo.
"""
import hmac
import html
import json
import math
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "nas-1.1"


def env(name, default=""):
    return (os.environ.get(name) or default).strip()


DATA = Path(env("DATA_DIR", "/data"))
REGISTRY = DATA / "geofences.json"
LOG_FILE = DATA / "geofence.log"
STATUS_FILE = DATA / "status.json"

DAWARICH_URL = env("DAWARICH_URL", "http://127.0.0.1:3000").rstrip("/")
DAWARICH_KEY = env("DAWARICH_API_KEY")
TG_TOKEN = env("TELEGRAM_BOT_TOKEN")
TG_CHAT = env("TELEGRAM_CHAT_ID")
TG_BASE = env("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
API_TOKEN = env("GEOFENCE_API_TOKEN")
API_PORT = int(env("GEOFENCE_API_PORT", "8377"))
API_BIND = env("GEOFENCE_API_BIND", "0.0.0.0")
POLL_SECONDS = max(20, int(env("POLL_SECONDS", "300")))
WINDOW_MIN = int(env("WINDOW_MIN", "30"))               # a point must be this fresh to fire
STALE_CAP_MIN = int(env("STALE_CAP_MIN", "180"))        # the phone's last known position counts this long
PRE_MIN = int(env("PRE_MIN", "90"))                     # "already there": arrived up to this long before it was set
SETTLE_MIN = int(env("SETTLE_MIN", "20"))               # ...and not seen anywhere else for this long
FRESHNESS_SECONDS = int(env("FRESHNESS_SECONDS", "3600"))  # how often to note the phone's newest point
ALERT_SILENT_HOURS = float(env("ALERT_PHONE_SILENT_HOURS", "0"))  # 0 = never message about a silent phone
HTTP_TIMEOUT = float(env("HTTP_TIMEOUT", "15"))
MAX_MAYBE_SENT = 2   # sends that timed out after the request left (could have been delivered)
DEFAULT_DAYS = 7

LOCK = threading.RLock()
WAKE = threading.Event()
STATE = {"version": VERSION, "started_at": None, "polls": 0, "sent": 0, "last_poll_at": None, "last_poll": "never",
         "dawarich": "unknown", "newest_point_at": None, "freshness_checked": 0.0, "alerted_silent": False}


class Down(Exception):
    pass


# ---------- helpers ----------
def now_local():
    return datetime.now().astimezone()


def iso(dt):
    return dt.isoformat(timespec="seconds")


def parse_ts(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and re.fullmatch(r"\d{9,13}(\.\d+)?", v.strip())):
            n = float(v)
            if n > 1e11:
                n /= 1000.0
            return datetime.fromtimestamp(n, tz=timezone.utc)
        s = str(v).strip().replace("Z", "+00:00")
        s = re.sub(r"(\.\d{6})\d+", r"\1", s)
        s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(a))


def one_line(s):
    return re.sub(r"\s+", " ", str(s)).strip()


def scrub(s):
    s = str(s)
    for secret in (TG_TOKEN, DAWARICH_KEY, API_TOKEN):
        if secret:
            s = s.replace(secret, "***")
    return s


def log(msg):
    line = "%s %s" % (iso(now_local()), scrub(one_line(msg)))
    print(line, flush=True)
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 512_000:
            LOG_FILE.write_text("".join(LOG_FILE.read_text(errors="replace").splitlines(True)[-1500:]))
        with LOG_FILE.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def atomic_write(path, text):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + "_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------- registry (only this process touches it; a corrupt file is never overwritten) ----------
def load():
    if REGISTRY.exists() and REGISTRY.read_text().strip():
        items = json.loads(REGISTRY.read_text())
        if not isinstance(items, list):
            raise ValueError("registry is not a JSON list")
        return items
    return []


def save(items):
    atomic_write(REGISTRY, json.dumps(items, indent=2, ensure_ascii=False) + "\n")


# ---------- Dawarich ----------
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def dawarich_get(path, params):
    if not DAWARICH_KEY:
        raise Down("no_api_key")
    req = urllib.request.Request(DAWARICH_URL + path + "?" + urllib.parse.urlencode(params),
                                 headers={"Authorization": "Bearer " + DAWARICH_KEY, "Accept": "application/json"})
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
    except Exception as e:
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


def recent_points(now, minutes, limit=1000):
    utc = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = dawarich_get("/api/v1/points", {"start_at": utc(now - timedelta(minutes=minutes)),
                                           "end_at": utc(now + timedelta(minutes=5)),
                                           "order": "desc", "per_page": limit, "page": 1})
    rows = data if isinstance(data, list) else (data.get("points") or data.get("data") or [])
    out = []
    for p in rows:
        if isinstance(p, dict):
            ll, ts = point_latlon(p), parse_ts(p.get("timestamp") or p.get("recorded_at") or p.get("created_at"))
            if ll and ts:
                out.append((ts, ll[0], ll[1]))
    out.sort(reverse=True)
    return out


# ---------- Telegram ----------
def telegram_send(text):
    """-> 'ok' (delivered) | 'failed' (certainly not delivered) | 'maybe' (timed out after the request left)."""
    if not TG_TOKEN or not TG_CHAT:
        return "failed"
    body = json.dumps({"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}).encode()
    req = urllib.request.Request("%s/bot%s/sendMessage" % (TG_BASE, TG_TOKEN), data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return "ok" if json.loads(r.read().decode("utf-8", "replace")).get("ok") else "failed"
    except urllib.error.HTTPError:
        return "failed"          # Telegram answered with an error, nothing was delivered
    except urllib.error.URLError:
        return "failed"          # never connected (DNS, refused, connect timeout)
    except Exception:
        return "maybe"           # request went out, no answer came back


def message_for(e):
    return "📍 <b>%s</b> — %s" % (html.escape(one_line(e.get("name", e.get("id", "")))), html.escape(one_line(e.get("task_text", ""))))


# ---------- poll ----------
def note_newest(ts):
    if ts and (STATE["newest_point_at"] is None or ts > STATE["newest_point_at"]):
        STATE["newest_point_at"] = ts
        STATE["alerted_silent"] = False


def poll_once():
    now = now_local()
    STATE["polls"] += 1
    STATE["last_poll_at"] = now
    with LOCK:
        try:
            items = load()
        except Exception as e:
            STATE["last_poll"] = "registry_error"
            log("poll registry_error %s" % type(e).__name__)
            return
        keep = [e for e in items if not ((parse_ts(e.get("expires_at")) or now) < now)]
        if len(keep) != len(items):
            save(keep)
            log("gc removed %d expired" % (len(items) - len(keep)))
        active = [dict(e) for e in keep if not e.get("fired_at")]

    pts, last = [], None       # last = the phone's last known position (newest point overall)
    fresh_due = time.time() - STATE["freshness_checked"] >= FRESHNESS_SECONDS
    try:
        if active:
            pts = recent_points(now, WINDOW_MIN + 5)
            if pts:
                last = pts[0]
                note_newest(pts[0][0])
        if fresh_due or (active and not pts):
            one = recent_points(now, 7 * 24 * 60, limit=1)
            STATE["freshness_checked"] = time.time()
            if one:
                last = last or one[0]
                note_newest(one[0][0])
        if active or fresh_due:
            STATE["dawarich"] = "ok"
    except Down as d:
        STATE["dawarich"] = "down:%s" % d
        STATE["last_poll"] = "dawarich_down"
        log("poll dawarich_down %s active=%d" % (d, len(active)))
        return

    firing, n_no = [], 0
    for e in active:
        try:
            created = parse_ts(e.get("created_at")) or now
            hit = (now, 0.0) if e.get("force") else None
            for ts, lat, lon in ([] if hit else pts):      # newest first
                dist = haversine_km(float(e["lat"]), float(e["lon"]), lat, lon)
                if dist <= float(e["radius_km"]):
                    hit = (ts, dist)
                    break
            if hit and (now - hit[0]) <= timedelta(minutes=WINDOW_MIN) and hit[0] >= created:
                firing.append((e, hit[1]))
                continue
            # Last known position: the tracker only logs while moving, so a newest point inside the radius
            # means the user is most likely still there, even if it is older than the window (late upload) or
            # older than the reminder (they arrived before it was set).
            if last and not e.get("force"):
                ld, age = haversine_km(float(e["lat"]), float(e["lon"]), last[1], last[2]), now - last[0]
                if ld <= float(e["radius_km"]) and age <= timedelta(minutes=STALE_CAP_MIN) and (
                        last[0] >= created or (last[0] >= created - timedelta(minutes=PRE_MIN) and age >= timedelta(minutes=SETTLE_MIN))):
                    firing.append((e, ld))
                    continue
            n_no += 1
        except Exception:
            n_no += 1                                      # one bad entry never blocks the others

    sent = 0
    for e, dist in firing:
        res = telegram_send(message_for(e))
        with LOCK:
            try:
                items = load()
            except Exception:
                break
            cur = next((x for x in items if x.get("id") == e.get("id")), None)
            if cur is None or cur.get("fired_at"):
                continue
            if res == "ok":
                cur["fired_at"], cur["fire_status"] = iso(now_local()), "delivered"
                sent += 1
                STATE["sent"] += 1
            elif res == "maybe":
                cur["maybe_sent"] = int(cur.get("maybe_sent") or 0) + 1
                if cur["maybe_sent"] >= MAX_MAYBE_SENT:
                    cur["fired_at"], cur["fire_status"] = iso(now_local()), "unconfirmed"
            else:
                cur["send_failures"] = int(cur.get("send_failures") or 0) + 1
            cur["last_attempt_at"] = iso(now_local())
            save(items)
        log("fire %s radius_hit=%.1fkm telegram=%s" % (e.get("id"), dist, res))

    age = STATE["newest_point_at"]
    age_min = int((now - age).total_seconds() // 60) if age else None
    if ALERT_SILENT_HOURS > 0 and age_min is not None and age_min > ALERT_SILENT_HOURS * 60 and not STATE["alerted_silent"]:
        if telegram_send("⚠️ Location reminders: your phone has not reported its location for %d hours." % (age_min // 60)) == "ok":
            STATE["alerted_silent"] = True
    STATE["last_poll"] = "active=%d firing=%d sent=%d no_fire=%d pts=%d" % (len(active), len(firing), sent, n_no, len(pts))
    if active or STATE["polls"] % max(1, 3600 // POLL_SECONDS) == 1:   # quiet idle polls: one line an hour
        log("poll %s phone_age=%s" % (STATE["last_poll"], "%dm" % age_min if age_min is not None else "unknown"))
    try:
        atomic_write(STATUS_FILE, json.dumps(health(), indent=2) + "\n")
    except Exception:
        pass


def poll_loop():
    while True:
        try:
            poll_once()
        except Exception as e:
            STATE["last_poll"] = "error"
            log("poll error %s" % type(e).__name__)
        WAKE.wait(POLL_SECONDS)
        WAKE.clear()


def health():
    now = now_local()
    with LOCK:
        try:
            items = load()
            reg = "ok"
        except Exception:
            items, reg = [], "error"
    np, lp = STATE["newest_point_at"], STATE["last_poll_at"]
    return {"ok": True, "version": VERSION, "now": iso(now), "uptime_min": int((now - STATE["started_at"]).total_seconds() // 60),
            "poll_seconds": POLL_SECONDS, "window_min": WINDOW_MIN, "polls": STATE["polls"], "sent": STATE["sent"],
            "last_poll_at": iso(lp) if lp else None, "last_poll_age_s": int((now - lp).total_seconds()) if lp else None,
            "last_poll": STATE["last_poll"], "dawarich": STATE["dawarich"], "telegram": bool(TG_TOKEN and TG_CHAT),
            "registry": reg, "total": len(items), "active": sum(1 for e in items if not e.get("fired_at")),
            "newest_point_age_min": int((now - np).total_seconds() // 60) if np else None}


# ---------- API ----------
def api_add(b):
    name, task = one_line(b.get("name") or ""), one_line(b.get("task_text") or b.get("task") or "")
    try:
        lat, lon = float(b.get("lat")), float(b.get("lon"))
        radius = float(b.get("radius_km") or b.get("radius") or 2.0)
        days = int(b.get("days") or DEFAULT_DAYS)
    except (TypeError, ValueError):
        return 400, {"ok": False, "error": "lat, lon, radius_km and days must be numbers"}
    if not name or not task:
        return 400, {"ok": False, "error": "name and task_text are required"}
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return 400, {"ok": False, "error": "coordinates out of range"}
    if not (0.1 <= radius <= 50) or not (1 <= days <= 90):
        return 400, {"ok": False, "error": "radius must be 0.1 to 50 km, days 1 to 90"}
    now = now_local()
    with LOCK:
        items = load()
        for e in items:
            if not e.get("fired_at") and one_line(e.get("name", "")).casefold() == name.casefold() \
                    and one_line(e.get("task_text", "")).casefold() == task.casefold():
                return 200, {"ok": True, "duplicate": True, "entry": e}
        base = "%s-%s" % (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32] or "place", now.strftime("%Y-%m-%d"))
        gid, n = base, 2
        while any(e.get("id") == gid for e in items):
            gid, n = "%s-%d" % (base, n), n + 1
        entry = {"id": gid, "name": name, "lat": round(lat, 6), "lon": round(lon, 6), "radius_km": radius, "task_text": task,
                 "created_at": iso(now), "fired_at": None, "expires_at": iso(now + timedelta(days=days))}
        items.append(entry)
        save(items)
    log("api add %s radius=%gkm" % (gid, radius))
    return 200, {"ok": True, "entry": entry}


def api_post(path, b):
    if path == "/add":
        return api_add(b)
    if path == "/poll":
        WAKE.set()
        return 200, {"ok": True}
    if path == "/test-ping":
        now = now_local()
        with LOCK:
            items = load()
            gid = "test-ping-%s" % now.strftime("%Y%m%d-%H%M%S")
            items.append({"id": gid, "name": "Test ping", "lat": 0.0, "lon": 0.0, "radius_km": 1.0, "force": True,
                          "task_text": "TEST: ignore. Location reminders can reach you.", "created_at": iso(now),
                          "fired_at": None, "expires_at": iso(now + timedelta(hours=2))})
            save(items)
        WAKE.set()
        log("api test-ping %s" % gid)
        return 200, {"ok": True, "id": gid}
    if path in ("/remove", "/mark-fired"):
        gid = str(b.get("id") or "")
        with LOCK:
            items = load()
            cur = next((e for e in items if e.get("id") == gid), None)
            if cur is None:
                return 404, {"ok": False, "error": "not_found"}
            if path == "/remove":
                items = [e for e in items if e.get("id") != gid]
            elif cur.get("fired_at"):
                return 200, {"ok": True, "already_fired": cur["fired_at"]}
            else:
                cur["fired_at"], cur["fire_status"] = iso(now_local()), "manual"
            save(items)
        log("api %s %s" % (path[1:], gid))
        return 200, {"ok": True, "id": gid}
    return 404, {"ok": False, "error": "unknown_path"}


class Handler(BaseHTTPRequestHandler):
    server_version, sys_version, protocol_version = "geofence", "", "HTTP/1.1"

    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def authed(self):
        got = self.headers.get("Authorization", "")
        got = got[7:] if got.startswith("Bearer ") else ""
        if API_TOKEN and hmac.compare_digest(got.encode(), API_TOKEN.encode()):
            return True
        self.reply(401, {"ok": False, "error": "unauthorized"})
        return False

    def do_GET(self):
        if not self.authed():
            return
        try:
            if self.path == "/health":
                return self.reply(200, health())
            if self.path == "/list":
                with LOCK:
                    return self.reply(200, {"ok": True, "entries": load()})
            self.reply(404, {"ok": False, "error": "unknown_path"})
        except Exception as e:
            self.reply(500, {"ok": False, "error": "registry_error" if isinstance(e, (ValueError, OSError)) else "server_error"})

    def do_POST(self):
        if not self.authed():
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 16384:
                return self.reply(413, {"ok": False, "error": "too_large"})
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, UnicodeError):
            return self.reply(400, {"ok": False, "error": "bad_json"})
        try:
            self.reply(*api_post(self.path, body))
        except Exception as e:
            self.reply(500, {"ok": False, "error": "registry_error" if isinstance(e, (ValueError, OSError)) else "server_error"})


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    STATE["started_at"] = now_local()
    if not REGISTRY.exists():
        save([])
    log("start %s poll=%ds window=%dm dawarich_key=%s telegram=%s api=%s:%d api_token=%s" % (
        VERSION, POLL_SECONDS, WINDOW_MIN, "yes" if DAWARICH_KEY else "MISSING", "yes" if (TG_TOKEN and TG_CHAT) else "MISSING",
        API_BIND, API_PORT, "yes" if API_TOKEN else "MISSING (API refuses everything)"))
    threading.Thread(target=poll_loop, daemon=True).start()
    ThreadingHTTPServer((API_BIND, API_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
