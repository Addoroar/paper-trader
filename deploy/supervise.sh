#!/usr/bin/env bash
# Keep papertrader running: restart it whenever it dies, with capped backoff.
#
#   nohup deploy/supervise.sh config.yaml > logs/supervisor.log 2>&1 &
#
# Stops when the bot exits cleanly (code 0, e.g. after Ctrl+C / SIGTERM) or
# with code 2 (bad config / another instance running). Any other exit -
# a crash, kill -9, OOM - triggers a restart; the bot recovers on its own.
set -u
CONFIG="${1:-config.yaml}"
PY="${PYTHON:-$(dirname "$0")/../.venv/bin/python}"
[ -x "$PY" ] || PY=python3
delay=5
child=0
trap 'echo "supervisor: stopping"; [ "$child" -gt 0 ] && kill -TERM "$child" && wait "$child"; exit 0' INT TERM

while true; do
  start=$(date +%s)
  echo "$(date -Is) supervisor: starting papertrader ($CONFIG)"
  "$PY" -m papertrader run --config "$CONFIG" &
  child=$!
  wait "$child"
  code=$?
  child=0
  echo "$(date -Is) supervisor: papertrader exited with code $code"
  if [ "$code" -eq 0 ] || [ "$code" -eq 2 ]; then
    exit "$code"
  fi
  # reset backoff after a healthy run of 10+ minutes
  if [ $(( $(date +%s) - start )) -gt 600 ]; then delay=5; fi
  echo "$(date -Is) supervisor: restarting in ${delay}s"
  sleep "$delay"
  delay=$(( delay * 2 > 300 ? 300 : delay * 2 ))
done
