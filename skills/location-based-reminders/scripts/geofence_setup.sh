#!/bin/bash
# Location-Based Reminders: one-time setup + live checks. Safe to re-run.
# First run, from the skill folder:  bash <skill folder>/scripts/geofence_setup.sh
# That copies the poller, this script and the NAS files into ~/.hermes, so later runs use the paths below.
#   bash ~/.hermes/scripts/geofence_setup.sh           Dawarich URL + key, test steps 1-2, delivery target, create the cron job
#   bash ~/.hermes/scripts/geofence_setup.sh down      test step 6 (Dawarich-down behavior) + final cron check
#   bash ~/.hermes/scripts/geofence_setup.sh rekey     re-enter the Dawarich API key
#   bash ~/.hermes/scripts/geofence_setup.sh diag      health check: lag, reporting cadence, endpoint behavior
#   bash ~/.hermes/scripts/geofence_setup.sh nas       move polling + sending to the NAS (asks for the NAS sudo password once)
#   bash ~/.hermes/scripts/geofence_setup.sh nas-off   roll back to this machine (local registry + Hermes cron poller)
# Everything is logged to ~/.hermes/logs/geofence_setup.log. The key is never printed or logged.
set -u
H="$HOME/.hermes"
P="$H/scripts/geofence_poll.py"
ENVF="$H/secrets/dawarich.env"
LOG="$H/logs/geofence_setup.log"
PY="$H/hermes-agent/venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
HERMES="$(command -v hermes || echo "$HOME/.local/bin/hermes")"
JOB_NAME="geofence-poller"
CONF="$H/geofence.conf"   # optional, not secret: DELIVER, NAS_SSH_HOST, NAS_API_URL, NAS_REMOTE_DIR, NAS_DAWARICH_URL
[ -f "$CONF" ] && . "$CONF"
DELIVER="${GEOFENCE_DELIVER:-${DELIVER:-}}"   # Hermes --deliver target, e.g. telegram:123456789
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
MODE="${1:-setup}"
mkdir -p "$H/logs" "$H/secrets"; chmod 700 "$H/secrets"
TMPD="$(mktemp -d)"; trap 'rm -rf "$TMPD"' EXIT
say(){ echo "$@" | tee -a "$LOG"; }
run(){ "$@" 2>&1 | tee -a "$LOG"; }
say ""; say "===== geofence_setup $MODE  $(date '+%Y-%m-%d %H:%M:%S %Z') ====="
say "python: $("$PY" --version 2>&1)  hermes: $HERMES"

save_conf(){   # save_conf KEY VALUE  -> ~/.hermes/geofence.conf
  touch "$CONF"; grep -v "^$1=" "$CONF" > "$CONF.tmp" 2>/dev/null; printf "%s='%s'\n" "$1" "$2" >> "$CONF.tmp"; mv "$CONF.tmp" "$CONF"
}

ask_deliver(){
  echo
  echo "Where should Hermes deliver reminders? Use the Hermes --deliver format, e.g. telegram:123456789 (your Telegram chat id)."
  read -r -p "Deliver to: " DELIVER
  DELIVER="$(printf '%s' "$DELIVER" | tr -d '[:space:]')"
  [ -n "$DELIVER" ] || { say "STOP: no delivery target, nothing created. Run me again."; exit 0; }
  save_conf DELIVER "$DELIVER"; say "DELIVER saved to ~/.hermes/geofence.conf"
}

install_files(){   # first run from the skill folder: copy everything into ~/.hermes
  local SK; SK="$(cd "$SRC/.." && pwd -P)"
  mkdir -p "$H/scripts" "$H/geofence-nas"
  [ "$SK/scripts" = "$(cd "$H/scripts" && pwd -P)" ] && return 0
  [ -f "$SK/scripts/geofence_poll.py" ] || return 0
  cp "$SK/scripts/geofence_poll.py" "$SK/scripts/geofence_setup.sh" "$H/scripts/"
  chmod 755 "$H/scripts/geofence_poll.py" "$H/scripts/geofence_setup.sh"
  [ -f "$SK/scripts/geofence_nas.py" ] && cp "$SK/scripts/geofence_nas.py" "$H/geofence-nas/geofence_nas.py"
  [ -f "$SK/scripts/nas_install.sh" ] && cp "$SK/scripts/nas_install.sh" "$H/geofence-nas/install.sh"
  [ -f "$SK/templates/docker-compose.yml" ] && cp "$SK/templates/docker-compose.yml" "$H/geofence-nas/docker-compose.yml"
  say "INSTALL copied geofence_poll.py, geofence_setup.sh and the NAS files into ~/.hermes"
}

job_id(){ "$PY" - "$H/cron/jobs.json" "$JOB_NAME" <<'PY'
import json, sys
try:
    print(next(j["id"] for j in json.load(open(sys.argv[1])).get("jobs", []) if j.get("name") == sys.argv[2]))
except Exception:
    pass
PY
}

enter_key(){
  local CUR U
  CUR="$(sed -n 's/^DAWARICH_URL=//p' "$ENVF" 2>/dev/null | head -1)"; CUR="${CUR:-http://localhost:3000}"
  echo
  read -r -p "Dawarich URL, as this machine reaches it [$CUR]: " U
  U="$(printf '%s' "${U:-$CUR}" | tr -d '[:space:]')"
  echo "Paste your Dawarich API key and press Enter. Nothing will show while you paste. That's normal."
  read -r -s -p "Dawarich API key: " K; echo
  K="$(printf '%s' "$K" | tr -d '[:space:]')"
  if [ -z "$K" ]; then say "KEY_ENTRY empty, nothing saved. Run me again."; exit 0; fi
  ( umask 077; printf 'DAWARICH_URL=%s\nDAWARICH_API_KEY=%s\n' "$U" "$K" > "$ENVF" ); chmod 600 "$ENVF"
  say "KEY_ENTRY saved to ~/.hermes/secrets/dawarich.env (url $U, key ${#K} chars, chmod 600)"; unset K
}

install_files   # no-op when this copy already lives in ~/.hermes/scripts

if [ "$MODE" = "rekey" ]; then enter_key; MODE="setup"; fi

if [ "$MODE" = "setup" ]; then
  grep -q '^DAWARICH_API_KEY=.' "$ENVF" 2>/dev/null || enter_key
  say "PERMS $(ls -l "$ENVF" | cut -c1-10) dawarich.env"

  say "--- STEP 1: selftest"
  OUT="$("$PY" "$P" --selftest 2>&1)"; say "$OUT"
  case "$OUT" in
    *"SELFTEST OK"*) ;;
    *http_401*|*http_403*) say "STOP: Dawarich rejected the key. Run: bash ~/.hermes/scripts/geofence_setup.sh rekey"; exit 0;;
    *) say "STOP: can't reach Dawarich. Check the URL (rekey changes it) and your network or VPN, then run me again."; exit 0;;
  esac

  say "--- LIVE PROBE (lag + endpoint behavior)"
  run "$PY" "$P" --whereami
  run "$PY" "$P" --probe here,15

  say "--- STEP 2: far geofence dry-run (Antarctica, throwaway registry)"
  export GEOFENCE_REGISTRY="$TMPD/geofences_test.json"
  "$PY" "$P" --add --name "Far test" --lat -77.846 --lon 166.676 --radius 5 --task "far test" >/dev/null 2>&1
  OUT="$("$PY" "$P" --dry-run 2>&1)"; say "$OUT"; unset GEOFENCE_REGISTRY
  case "$OUT" in *"NO_FIRE far-test"*) say "STEP 2 PASS";; *) say "STOP: step 2 did not return NO_FIRE"; exit 0;; esac

  say "--- CRON JOB"
  JID="$(job_id)"
  if [ -n "$JID" ]; then
    say "cron job already exists: $JID (not creating another)"
  else
    [ -n "$DELIVER" ] || ask_deliver
    PROMPT='You are the geofence reminder relay. The Script Output above comes from geofence_poll.py and is authoritative. If it contains a block between the lines MESSAGE_BEGIN and MESSAGE_END, your entire final response must be exactly the lines inside that block, copied verbatim, one per line, with nothing added, removed, translated or reworded. No greeting, no commentary. If there is no such block, or the script reported an error, respond with exactly [SILENT] and nothing else. Never run commands, never edit files, never call tools.'
    run "$HERMES" cron create '*/10 * * * *' "$PROMPT" --name "$JOB_NAME" --deliver "$DELIVER" --script geofence_poll.py
    JID="$(job_id)"
  fi
  say "JOB_ID=${JID:-MISSING}"
  run "$HERMES" cron status
  run "$HERMES" cron list
  say "===== setup phase finished. ====="
fi

if [ "$MODE" = "nas-off" ]; then
  if [ -f "$H/secrets/geofence_nas.env" ]; then
    mv "$H/secrets/geofence_nas.env" "$H/secrets/geofence_nas.env.off"
    say "ROLLBACK done: back to the local registry + Hermes cron poller. The NAS container is still running."
    say "To stop it too, as root on the NAS: cd ${NAS_REMOTE_DIR:-/volume1/docker/geofence} && docker compose down"
  else
    say "Already in local mode (no geofence_nas.env)."
  fi
  run "$PY" "$P" --list
fi

if [ "$MODE" = "nas" ]; then
  NASDIR="$H/geofence-nas"
  REMOTE="${NAS_REMOTE_DIR:-/volume1/docker/geofence}"
  NAS_HOST="${NAS_SSH_HOST:-nas}"
  NAS_URL="${NAS_API_URL:-}"
  if [ -z "$NAS_URL" ]; then
    read -r -p "URL this machine uses to reach the NAS service, e.g. http://192.168.1.20:8377: " NAS_URL
    NAS_URL="$(printf '%s' "$NAS_URL" | tr -d '[:space:]')"
    [ -n "$NAS_URL" ] || { say "STOP: no NAS URL."; exit 0; }
    save_conf NAS_API_URL "$NAS_URL"
  fi
  [ -n "$DELIVER" ] || ask_deliver
  case "$DELIVER" in
    telegram:*) CHAT_ID="${DELIVER#telegram:}"; CHAT_ID="${CHAT_ID%%:*}";;
    *) say "STOP: NAS mode sends through the Telegram Bot API itself, so DELIVER must be telegram:<chat id> (now: $DELIVER)."; exit 0;;
  esac
  NASENV="$H/secrets/geofence_nas.env"; PEND="$NASENV.pending"

  say "--- NAS deploy: preflight"
  for f in geofence_nas.py docker-compose.yml install.sh; do
    [ -f "$NASDIR/$f" ] || { say "STOP: missing $NASDIR/$f (run the setup from the skill folder once to copy it)"; exit 0; }
  done
  grep -q '^DAWARICH_API_KEY=.' "$ENVF" 2>/dev/null || { say "STOP: no Dawarich key yet. Run: bash ~/.hermes/scripts/geofence_setup.sh"; exit 0; }
  if ! ssh -o BatchMode=yes -o ConnectTimeout=12 "$NAS_HOST" true 2>/dev/null; then
    say "STOP: 'ssh $NAS_HOST' did not connect on its own. It needs the '$NAS_HOST' alias with its key, and a network path to the NAS (LAN or VPN)."
    say "If you are logged in remotely, run this in a local terminal instead (the ssh key may live in the local keychain)."
    exit 0
  fi
  say "ssh $NAS_HOST: ok"

  say "--- building the bundle (secrets are copied file to file, never shown)"
  B="$TMPD/bundle"; mkdir -p "$B"
  cp "$NASDIR/geofence_nas.py" "$NASDIR/docker-compose.yml" "$NASDIR/install.sh" "$B/"
  OUT="$("$PY" - "$H" "$B/geofence.env" "$PEND" "$NAS_URL" "$CHAT_ID" "${NAS_DAWARICH_URL:-http://127.0.0.1:3000}" <<'PY'
import os, re, secrets, sys
H, out_nas, out_local, url, chat, dw = sys.argv[1:7]
def env(path):
    v = {}
    try:
        for l in open(path, encoding="utf-8", errors="replace"):
            m = re.match(r'\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*)$', l)
            if m:
                v[m.group(1)] = m.group(2).strip().strip("'\"")
    except OSError:
        pass
    return v
tok = env(os.path.join(H, ".env")).get("TELEGRAM_BOT_TOKEN", "")
key = env(os.path.join(H, "secrets", "dawarich.env")).get("DAWARICH_API_KEY", "")
if not re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,}", tok):
    print("BUNDLE FAIL: no usable TELEGRAM_BOT_TOKEN in ~/.hermes/.env"); sys.exit(0)
if not key:
    print("BUNDLE FAIL: no DAWARICH_API_KEY in ~/.hermes/secrets/dawarich.env"); sys.exit(0)
api = ""
for pth in (os.path.join(H, "secrets", "geofence_nas.env"), os.path.join(H, "secrets", "geofence_nas.env.off"), out_local):
    api = api or env(pth).get("GEOFENCE_API_TOKEN", "")
reused = bool(api)
api = api or secrets.token_urlsafe(32)
m = re.search(r":(\d+)$", url)
def w(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, text.encode()); os.close(fd); os.chmod(path, 0o600)
w(out_nas, "DAWARICH_URL=%s\nDAWARICH_API_KEY=%s\nTELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\n"
           "GEOFENCE_API_TOKEN=%s\nGEOFENCE_API_PORT=%s\nPOLL_SECONDS=300\nWINDOW_MIN=30\nALERT_PHONE_SILENT_HOURS=0\n" % (dw, key, tok, chat, api, m.group(1) if m else "8377"))
w(out_local, "GEOFENCE_API_URL=%s\nGEOFENCE_API_TOKEN=%s\n" % (url, api))
print("BUNDLE OK | dawarich key: yes | telegram token: yes | api token: %s" % ("reused" if reused else "new"))
PY
)"
  say "$OUT"
  case "$OUT" in *"BUNDLE OK"*) ;; *) say "STOP: could not build the bundle"; exit 0;; esac
  COPYFILE_DISABLE=1 tar -czf "$TMPD/b.tgz" -C "$B" . 2>/dev/null
  RD="$(ssh -o ConnectTimeout=12 "$NAS_HOST" 'umask 077; d=$(mktemp -d /tmp/gf.XXXXXX 2>/dev/null) || { d=/tmp/gf.$$; mkdir -m 700 "$d"; }; cat > "$d/b.tgz" && echo "$d"' < "$TMPD/b.tgz" 2>/dev/null | tail -1)"
  case "$RD" in /tmp/gf.*) say "bundle pushed to the NAS";; *) say "STOP: pushing the bundle to the NAS failed"; exit 0;; esac

  echo
  echo ">>> The NAS now asks for your sudo password. Type it at the prompt and press Enter."
  echo ">>> Nothing shows while you type. It is not stored or logged."
  ssh -t "$NAS_HOST" "sudo sh -c 'mkdir -p $REMOTE && tar xzf $RD/b.tgz -C $REMOTE && rm -rf $RD && sh $REMOTE/install.sh'"
  say "install step exit code: $?"
  ssh -o BatchMode=yes -o ConnectTimeout=12 "$NAS_HOST" "rm -rf $RD" 2>/dev/null

  say "--- checking the NAS service from this machine"
  OK=""; OUT=""
  for i in $(seq 1 "${NAS_VERIFY_TRIES:-10}"); do
    OUT="$(GEOFENCE_NAS_ENV="$PEND" "$PY" "$P" --nas-health 2>&1)"
    case "$OUT" in "NAS OK"*"dawarich=ok"*"telegram=yes"*) OK=1; break;; esac
    sleep 6
  done
  say "$OUT"
  if [ -z "$OK" ]; then
    case "$OUT" in
      "NAS OK"*) say "STOP: the NAS service is up but not healthy yet (see dawarich= and telegram= above). NOT switching over. This machine keeps polling as before.";;
      *) say "STOP: this machine cannot reach the NAS service at $NAS_URL. NOT switching over. This machine keeps polling as before.";;
    esac
    say "===== nas phase finished (not switched). ====="
    exit 0
  fi
  mv "$PEND" "$NASENV"; chmod 600 "$NASENV"
  say "CUTOVER done: the NAS now checks your location every 5 minutes and sends Telegram itself."
  run "$PY" "$P" --migrate-local
  say "--- test ping (you should get one Telegram message within seconds)"
  run "$PY" "$P" --test-ping
  sleep 8
  run "$PY" "$P" --list
  run "$PY" "$P" --nas-health
  say "--- Hermes gateway service on this machine (read only)"
  "$HERMES" gateway status 2>&1 | head -15 | tee -a "$LOG"
  say "===== nas phase finished. ====="
fi

if [ "$MODE" = "diag" ]; then
  say "--- DIAG: selftest, current position age, reporting cadence, endpoint behavior"
  run "$PY" "$P" --nas-health
  run "$PY" "$P" --selftest
  run "$PY" "$P" --whereami
  run "$PY" "$P" --probe here,15
  say "--- registry + cron"
  run "$PY" "$P" --list
  say "JOB_ID=$(job_id)"
  say "===== diag finished. ====="
fi

if [ "$MODE" = "down" ]; then
  say "--- STEP 6: Dawarich-down behavior (wrong key in a throwaway env + registry)"
  printf 'DAWARICH_URL=%s\nDAWARICH_API_KEY=wrong-key-for-down-test\n' "$(sed -n 's/^DAWARICH_URL=//p' "$ENVF" | head -1)" > "$TMPD/bad.env"
  export GEOFENCE_REGISTRY="$TMPD/geofences_test.json"
  "$PY" "$P" --add --name "Down test" --lat -77.846 --lon 166.676 --radius 15 --task "down test" >/dev/null 2>&1
  OUT="$(DAWARICH_ENV_FILE="$TMPD/bad.env" "$PY" "$P" --dry-run 2>&1; echo "exit_code=$?")"; say "$OUT"
  OUT2="$(DAWARICH_URL="http://127.0.0.1:9" "$PY" "$P" --dry-run 2>&1; echo "exit_code=$?")"; say "$OUT2"
  unset GEOFENCE_REGISTRY
  case "$OUT" in *DAWARICH_DOWN*'{"wakeAgent": false}'*"exit_code=0"*) say "STEP 6 PASS (bad key: quiet, exit 0)";; *) say "STEP 6 FAIL";; esac
  case "$OUT2" in *DAWARICH_DOWN*"exit_code=0"*) say "STEP 6b PASS (unreachable: quiet, exit 0)";; *) say "STEP 6b FAIL";; esac
  say "--- real poller still healthy with the real key"
  run "$PY" "$P" --selftest
  run "$PY" "$P" --dry-run
  run "$PY" "$P" --probe here,15
  say "--- intake helpers (live geocoder)"
  run "$PY" "$P" --geocode "Union Station, Denver CO"
  sleep 2
  run "$PY" "$P" --geocode "post office" --near here
  say "--- CRON"
  say "JOB_ID=$(job_id)"
  run "$HERMES" cron list
  say "last poller log lines:"; tail -8 "$H/logs/geofence_poll.log" | tee -a "$LOG"
  say "===== down phase finished. ====="
fi
