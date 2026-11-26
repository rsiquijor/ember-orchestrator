#!/usr/bin/env bash
# Live runner for the Ember Orchestrator on the Metatron Cube.
# Sources the API key from the Cube's env file at runtime (the key is
# never copied into this repo) and the public meter rates from
# ember-rates.env, then runs the given Python command.
#
#   ./run_live.sh run_smoke.py
#   ./run_live.sh -m unittest discover -s tests -v
set -euo pipefail
cd "$(dirname "$0")"
if [ -f /root/metatron/ember.env ]; then
  set -a; . /root/metatron/ember.env; set +a
fi
if [ -f ./ember-rates.env ]; then
  set -a; . ./ember-rates.env; set +a
fi
exec python3 "$@"
