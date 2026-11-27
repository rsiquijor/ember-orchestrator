#!/usr/bin/env python3
"""Generate the synthetic server log for the Document Intelligence demo.

Deterministic (seeded): ~600 lines of normal INFO traffic with exactly
5 planted anomalies. Writes fixtures/sample-server.log and
fixtures/sample-server.groundtruth.json (the 5 anomalies with their
1-based line numbers) so Ember's extraction can be verified honestly.

    python3 fixtures/make_log.py
"""
import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "sample-server.log")
GT_PATH = os.path.join(HERE, "sample-server.groundtruth.json")

rng = random.Random(20261126)

SERVICES = ["api-gateway", "auth-svc", "billing", "worker-3", "cache",
            "scheduler", "ingest", "notifier"]
PATHS = ["/api/orders", "/api/users/me", "/api/invoices", "/health",
         "/api/reports/daily", "/api/assets", "/api/search"]
IPS = ["10.0.4.12", "10.0.4.18", "10.0.7.31", "10.0.7.44", "10.0.9.2"]


def normal_line(ts: str) -> str:
    svc = rng.choice(SERVICES)
    kind = rng.random()
    if kind < 0.55:
        return (f"{ts} INFO  {svc} GET {rng.choice(PATHS)} "
                f"{rng.choice([200, 200, 200, 201, 204])} "
                f"{rng.randint(3, 180)}ms ip={rng.choice(IPS)}")
    if kind < 0.7:
        return (f"{ts} INFO  {svc} job batch-{rng.randint(1000, 9999)} "
                f"completed rows={rng.randint(10, 900)} "
                f"in {rng.randint(1, 40)}s")
    if kind < 0.82:
        return (f"{ts} INFO  cache hit ratio={rng.randint(88, 99)}% "
                f"keys={rng.randint(4000, 9000)}")
    if kind < 0.92:
        return (f"{ts} DEBUG {svc} heartbeat ok "
                f"queue_depth={rng.randint(0, 12)}")
    return (f"{ts} INFO  scheduler tick ok "
            f"next_run=+{rng.randint(1, 15)}m")


def build() -> tuple[list[str], list[dict]]:
    lines: list[str] = []
    anomalies: list[dict] = []

    def ts(minute: int, sec: int) -> str:
        return f"2026-11-25T{minute // 60:02d}:{minute % 60:02d}:{sec:02d}Z"

    minute = 0
    total = 620
    # Plant anomalies at fixed line positions (1-based, after insert).
    plant_at = {118: "disk", 237: "auth", 352: "latency",
                461: "crash", 548: "cert"}
    while len(lines) < total:
        nxt = len(lines) + 1
        t = ts(minute, rng.randint(0, 59))
        minute += rng.randint(0, 1)
        if nxt in plant_at:
            kind = plant_at[nxt]
            if kind == "disk":
                lines.append(
                    f"{t} ERROR ingest write failed: No space left on "
                    f"device (/data volume 100% full) file=batch-7712")
                anomalies.append({
                    "line": nxt, "kind": "disk-full",
                    "detail": "ERROR ingest write failed: No space left "
                              "on device (/data volume 100% full)"})
            elif kind == "auth":
                for i in range(6):
                    lines.append(
                        f"{t} WARN  auth-svc FAILED LOGIN user=admin "
                        f"ip=203.0.113.77 attempt={i + 1}")
                anomalies.append({
                    "line": nxt, "kind": "auth-failure-burst",
                    "detail": "6 consecutive FAILED LOGIN for user=admin "
                              "from 203.0.113.77 (lines "
                              f"{nxt}-{nxt + 5})"})
            elif kind == "latency":
                lines.append(
                    f"{t} WARN  api-gateway p99 latency 8420ms "
                    f"(baseline ~120ms) path=/api/reports/daily")
                anomalies.append({
                    "line": nxt, "kind": "latency-spike",
                    "detail": "p99 latency 8420ms vs ~120ms baseline on "
                              "/api/reports/daily"})
            elif kind == "crash":
                lines.append(
                    f"{t} ERROR worker-3 process exited code=137 "
                    f"(OOM killed) — supervisor restarting")
                anomalies.append({
                    "line": nxt, "kind": "crash-oom",
                    "detail": "worker-3 exited code=137 (OOM killed), "
                              "supervisor restarting"})
            elif kind == "cert":
                lines.append(
                    f"{t} WARN  api-gateway TLS certificate for "
                    f"api.internal expires in 2 days (2026-11-27)")
                anomalies.append({
                    "line": nxt, "kind": "cert-expiry",
                    "detail": "TLS certificate for api.internal expires "
                              "in 2 days (2026-11-27)"})
            continue
        lines.append(normal_line(t))
    return lines, anomalies


def main() -> int:
    lines, anomalies = build()
    with open(LOG_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")
    with open(GT_PATH, "w") as f:
        json.dump({"log": "sample-server.log", "lines": len(lines),
                   "anomalies": anomalies}, f, indent=1)
    print(f"wrote {LOG_PATH} ({len(lines)} lines)")
    print(f"wrote {GT_PATH} ({len(anomalies)} planted anomalies)")
    for a in anomalies:
        print(f"  line {a['line']}: {a['kind']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
