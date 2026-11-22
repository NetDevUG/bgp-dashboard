#!/usr/bin/env bash
# Runs gobgpd and pipes its RIB updates into MongoDB.
# If either process dies the container exits, so Docker restarts it cleanly.
set -euo pipefail

CONF="${GOBGPD_CONF:-/etc/gobgp/gobgpd.conf}"
MONITOR_ARGS="${GOBGP_MONITOR_ARGS:---current}"

gobgpd -f "$CONF" ${GOBGPD_OPTS:-} &
GOBGPD_PID=$!
trap 'kill "$GOBGPD_PID" 2>/dev/null || true' EXIT

# Wait until the gRPC API of gobgpd answers.
for _ in $(seq 1 30); do
    kill -0 "$GOBGPD_PID" 2>/dev/null || { echo "gobgpd failed to start" >&2; exit 1; }
    gobgp global >/dev/null 2>&1 && break
    sleep 1
done
gobgp global >/dev/null

# shellcheck disable=SC2086
gobgp monitor global rib $MONITOR_ARGS -j | python /app/gobgp_to_mongo.py &

# Exit as soon as gobgpd or the monitor pipeline terminates.
wait -n
echo "gobgpd or collector terminated, exiting" >&2
exit 1
