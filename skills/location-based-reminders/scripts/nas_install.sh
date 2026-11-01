#!/bin/sh
# Runs as root on the NAS (geofence_setup.sh nas copies it there as install.sh). Starts or restarts the geofence container.
set -u
cd "$(dirname "$0")" || exit 1
DOCKER=/usr/local/bin/docker
[ -x "$DOCKER" ] || DOCKER="$(command -v docker 2>/dev/null)"
[ -n "$DOCKER" ] || { echo "INSTALL FAIL: docker not found"; exit 1; }
chown -R root:root . 2>/dev/null
chmod 700 . ; chmod 600 geofence.env ; mkdir -p data
PORT="$(sed -n 's/^GEOFENCE_API_PORT=//p' geofence.env)"; PORT="${PORT:-8377}"
if ! "$DOCKER" ps --format '{{.Names}}' | grep -qx geofence; then
  if netstat -tln 2>/dev/null | grep -q "[:.]$PORT[[:space:]]"; then
    echo "INSTALL FAIL: port $PORT is already in use on the NAS"; exit 1
  fi
fi
if "$DOCKER" compose version >/dev/null 2>&1; then
  "$DOCKER" compose up -d --force-recreate || { echo "INSTALL FAIL: docker compose up failed"; exit 1; }
elif command -v docker-compose >/dev/null 2>&1; then
  docker-compose up -d --force-recreate || { echo "INSTALL FAIL: docker-compose up failed"; exit 1; }
else
  echo "INSTALL FAIL: docker compose not found"; exit 1
fi
sleep 6
"$DOCKER" ps --filter name=geofence --format 'container: {{.Names}} | {{.Status}}'
"$DOCKER" logs --tail 4 geofence 2>&1
echo "INSTALL OK"
