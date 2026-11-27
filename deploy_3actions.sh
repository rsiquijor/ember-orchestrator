#!/bin/bash
# Deploy the 3-actions NEXT STEPS restructure to the Vast box.
# Run ON the box after the changed files have been scp'd into
# /tmp/ember-3actions/. Backs up current files, installs, runs the
# test suite, restarts the service by port, verifies one listener.
set -e
cd /opt/cube/ember-orchestrator
TS=.bak-nov26-3actions
for f in serve.py demo_page.html orchestrator/orchestrator.py tests/test_nextsteps.py; do
  cp "$f" "$f$TS"
done
cp /tmp/ember-3actions/serve.py serve.py
cp /tmp/ember-3actions/demo_page.html demo_page.html
cp /tmp/ember-3actions/orchestrator.py orchestrator/orchestrator.py
cp /tmp/ember-3actions/test_nextsteps.py tests/test_nextsteps.py
echo "--- files installed, running tests ---"
cd /opt/cube/ember-orchestrator && python3 -m unittest discover -s tests 2>&1 | tail -4
echo "--- restarting service ---"
PID=$(ss -tlnp | grep ":8023" | grep -oE "pid=[0-9]+" | head -1 | cut -d= -f2)
if [ -n "$PID" ]; then kill "$PID"; sleep 2; fi
(setsid bash /root/metatron/start-ember.sh </dev/null >/dev/null 2>&1 &)
sleep 3
echo "--- listeners on 8023 ---"
ss -tlnp | grep ":8023" || echo "NONE"
curl -s -o /dev/null -w "local page HTTP %{http_code}\n" http://127.0.0.1:8023/
