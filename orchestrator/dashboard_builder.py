"""Dashboard Builder for the Ember Orchestrator (Nov 27, 2026).

Rom: "Ember designs the dashboard, I approve, Metatron renders it."
Ember authors a dashboard spec (title, KPI cards, charts) against
the live data profile of one to three allowlisted Cube sources;
this module validates the spec deterministically (mirroring the
Dashboard Generator's validate_spec rules) and creates it through
the generator's deterministic create path. The generator's Axion
design step (POST /api/design) is NEVER used — Ember is the only
AI in the path, and Ember-authored specs carry insight=False and
chat=False so no Cube-side model runs at create, refresh, or
render time either.

Spec shape (the generator's, DESIGN_SYS): {title, subtitle,
kpis: [{label, field, format}], charts: [{type, title, from,
label_field, value_field, status_field}], table: {title, from,
columns} | None, thresholds: [{label, field, op, value}]}.
Field/list/column names come from the live profile
(POST /api/profile) — single-endpoint dashboards use unprefixed
names (host_disk_pct); multi-endpoint dashboards prefix every
name with its endpoint (capacity_host_disk_pct,
finops_costs_today_usd, list capacity_components).
"""
from __future__ import annotations

import json
import re
import urllib.request

# Ember-facing dashboard sources: id -> (label, generator endpoint).
# Ids ARE the Dashboard Generator's CUBE_SOURCES keys. Deliberately
# absent: dashboards-status (self-referential) and aiops-health
# (too thin to chart). There is no SecOps source in the generator.
SOURCES = {
    "watcher-overview": "Watcher / AIOps overview",
    "capacity": "Capacity (Watcher-fed)",
    "predictions": "Failure Predictions",
    "finops": "FinOps Summary",
    "leases": "Lease & Acquisition Tracker",
    "sentiment": "ITSM Sentiment (Cerebellum)",
    "finclose": "FinClose Month-End (DEMO)",
    "dotkrc-status": "DOT-KRC bid board status",
    "agents-status": "Metatron Agents status",
}

FORMATS = {"int", "money", "pct", "ratio", "text"}
CHART_TYPES = {"bar", "line", "share", "status-list",
               "echarts-line", "echarts-bar"}
NUMERIC_CHART_TYPES = {"bar", "line", "share",
                       "echarts-line", "echarts-bar"}
THRESHOLD_OPS = {">", ">=", "<", "<=", "==", "!="}
REFRESH_FREQUENCIES = ("Hourly", "Daily 6 AM", "Daily 6 PM",
                       "Weekly Mon 6 AM")
MAX_ENDPOINTS = 3
MAX_KPIS = 8
MAX_CHARTS = 4
MAX_THRESHOLDS = 8

PUBLIC_BASE = "https://dashboards.metatroncube.online"


def slugify(title: str) -> str:
    """Mirror of the generator's slugify (result slug is assigned
    server-side with collision suffixes; this is for fixtures and
    display only)."""
    s = re.sub(r"[^a-z0-9]+", "-", (title or "dashboard").lower())
    return s.strip("-")[:48] or "dashboard"


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(",", "").replace("$", "")
                         .replace("%", ""))
        except ValueError:
            return None
    return None


def _post_json(url: str, body: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "ember-orchestrator/dashboard-builder"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}


def source_payload(endpoints: list[str]) -> dict:
    """The generator source dict for 1..3 endpoints."""
    if len(endpoints) == 1:
        return {"kind": "cube", "endpoint": endpoints[0]}
    return {"kind": "cube", "endpoints": list(endpoints)}


def fetch_profile(endpoints: list[str], base: str,
                  timeout: int = 60) -> dict:
    """Live data profile for the endpoints (deterministic fetch,
    $0 — no model). Returns {facts: [...], lists: [...]}."""
    out = _post_json(base.rstrip("/") + "/api/profile",
                     {"source": source_payload(endpoints)},
                     timeout=timeout)
    profile = out.get("profile")
    if not isinstance(profile, dict):
        raise ValueError("dashboard profile fetch returned no profile")
    return profile


def profile_dataset(profile: dict) -> dict:
    """Profile -> {facts: {name: {type, value}}, lists: {name:
    {columns, rows, count}}} shaped like the generator's dataset,
    so the validator can mirror validate_spec exactly."""
    facts = {}
    for f in profile.get("facts") or []:
        if isinstance(f, dict) and f.get("name"):
            facts[str(f["name"])] = {"type": f.get("type", "text"),
                                     "value": f.get("value")}
    lists = {}
    for lst in profile.get("lists") or []:
        if isinstance(lst, dict) and lst.get("name"):
            lists[str(lst["name"])] = {
                "columns": list(lst.get("columns") or []),
                "rows": list(lst.get("sample") or []),
                "count": lst.get("count", 0)}
    return {"facts": facts, "lists": lists}


def normalize_spec(spec: dict, ds: dict) -> dict:
    """Fill omissions with obvious defaults (mirror of the
    generator's normalize_spec): chart label/value/status fields
    and table columns."""
    lists = ds["lists"]
    for c in spec.get("charts", []):
        lst = lists.get(c.get("from", ""))
        if not lst:
            continue
        cols = lst["columns"]
        if not c.get("label_field") and cols:
            c["label_field"] = cols[0]
        if c.get("type") in NUMERIC_CHART_TYPES \
                and not c.get("value_field"):
            for col in cols:
                if any(_num(r.get(col)) is not None
                       for r in lst["rows"][:20]):
                    c["value_field"] = col
                    break
        if c.get("type") == "status-list" and not c.get("status_field"):
            for col in cols:
                if col != c.get("label_field"):
                    c["status_field"] = col
                    break
    tbl = spec.get("table")
    if isinstance(tbl, dict) and not tbl.get("columns"):
        lst = lists.get(tbl.get("from", ""))
        if lst:
            tbl["columns"] = lst["columns"][:6]
    return spec


def validate_dashboard_params(params: dict, profile: dict) -> dict:
    """Validate Ember's create_dashboard params against the live
    profile. Returns the clean params dict; raises ValueError
    with a teaching message (every error names the fix and the
    valid choices) so a repair retry can succeed."""
    params = params or {}
    title = str(params.get("title") or "").strip()
    if not (3 <= len(title) <= 80):
        raise ValueError("dashboard title must be 3-80 characters "
                         "(e.g. 'Capacity & Costs')")
    endpoints = params.get("endpoints")
    if isinstance(endpoints, str):
        endpoints = [endpoints]
    if not isinstance(endpoints, list) or not endpoints:
        raise ValueError(
            "endpoints must list 1-3 data sources from: "
            + ", ".join(sorted(SOURCES)))
    endpoints = [str(e).strip() for e in endpoints]
    if len(endpoints) > MAX_ENDPOINTS:
        raise ValueError(f"at most {MAX_ENDPOINTS} endpoints per "
                         "dashboard")
    seen = set()
    for e in endpoints:
        if e not in SOURCES:
            raise ValueError(
                f"unknown dashboard source {e!r} — use one of: "
                + ", ".join(sorted(SOURCES)))
        if e in seen:
            raise ValueError(f"duplicate endpoint {e!r}")
        seen.add(e)
    refresh_frequency = params.get("refresh_frequency")
    if refresh_frequency is not None:
        refresh_frequency = str(refresh_frequency).strip()
        if refresh_frequency not in REFRESH_FREQUENCIES:
            raise ValueError(
                "refresh_frequency must be one of: "
                + ", ".join(REFRESH_FREQUENCIES)
                + " (or omit it for manual refresh)")

    ds = profile_dataset(profile)
    facts, lists = ds["facts"], ds["lists"]
    spec = {
        "title": title,
        "subtitle": str(params.get("subtitle") or "").strip()[:200],
        "kpis": params.get("kpis") or [],
        "charts": params.get("charts") or [],
        "table": params.get("table"),
        "thresholds": params.get("thresholds") or [],
    }
    normalize_spec(spec, ds)

    errs = []
    kpis = spec["kpis"]
    if not isinstance(kpis, list):
        errs.append("kpis must be a list of {label, field, format}")
        kpis = []
    if len(kpis) > MAX_KPIS:
        errs.append(f"at most {MAX_KPIS} KPI cards")
    for k in kpis:
        if not isinstance(k, dict):
            errs.append("every KPI must be an object {label, field, "
                        "format}")
            continue
        if k.get("field") not in facts:
            errs.append(
                f"KPI field not in the data: {k.get('field')} — "
                "use a fact name from the profile exactly")
        if k.get("format", "int") not in FORMATS:
            errs.append(f"KPI format not allowed: {k.get('format')} "
                        "— use int, money, pct, ratio, or text")
        if not str(k.get("label") or "").strip():
            errs.append("every KPI needs a label")
    charts = spec["charts"]
    if not isinstance(charts, list):
        errs.append("charts must be a list")
        charts = []
    if len(charts) > MAX_CHARTS:
        errs.append(f"at most {MAX_CHARTS} charts")
    for c in charts:
        if not isinstance(c, dict):
            errs.append("every chart must be an object {type, title, "
                        "from, label_field, value_field}")
            continue
        if c.get("type") not in CHART_TYPES:
            errs.append(f"chart type not allowed: {c.get('type')} — "
                        "use bar, line, share, status-list, "
                        "echarts-line, or echarts-bar")
            continue
        lst = lists.get(c.get("from", ""))
        if lst is None:
            errs.append(f"chart source list not in the data: "
                        f"{c.get('from')} — use a list name from "
                        "the profile exactly")
            continue
        if c.get("label_field") not in lst["columns"]:
            errs.append(f"chart label_field not a column: "
                        f"{c.get('label_field')}")
        if c["type"] in NUMERIC_CHART_TYPES:
            vf = c.get("value_field")
            if vf not in lst["columns"]:
                errs.append(f"chart value_field not a column: {vf}")
            elif not any(_num(r.get(vf)) is not None
                         for r in lst["rows"][:20]):
                errs.append(f"chart value_field is not numeric: {vf}")
        if c["type"] == "status-list" and c.get("status_field") \
                and c["status_field"] not in lst["columns"]:
            errs.append(f"status_field not a column: "
                        f"{c['status_field']}")
    tbl = spec["table"]
    if tbl is not None:
        if not isinstance(tbl, dict):
            errs.append("table must be an object {title, from, "
                        "columns} or omitted")
        else:
            lst = lists.get(tbl.get("from", ""))
            if lst is None:
                errs.append(f"table source list not in the data: "
                            f"{tbl.get('from')}")
            else:
                for col in tbl.get("columns", []):
                    if col not in lst["columns"]:
                        errs.append(f"table column not in the data: "
                                    f"{col}")
                if len(tbl.get("columns", [])) > 8:
                    errs.append("at most 8 table columns")
                if not tbl.get("columns"):
                    errs.append("a table needs at least one column")
    thresholds = spec["thresholds"]
    if not isinstance(thresholds, list):
        errs.append("thresholds must be a list")
        thresholds = []
    if len(thresholds) > MAX_THRESHOLDS:
        errs.append(f"at most {MAX_THRESHOLDS} thresholds")
    for t in thresholds:
        if not isinstance(t, dict):
            errs.append("every threshold must be an object {label, "
                        "field, op, value}")
            continue
        if t.get("field") not in facts:
            errs.append(f"threshold field not in the data: "
                        f"{t.get('field')}")
        if t.get("op") not in THRESHOLD_OPS:
            errs.append(f"threshold op not allowed: {t.get('op')}")
        if _num(t.get("value")) is None:
            errs.append("every threshold needs a numeric value")
        if not str(t.get("label") or "").strip():
            errs.append("every threshold needs a label")
    if not kpis and not charts and not (tbl and isinstance(tbl, dict)
                                         and tbl.get("columns")):
        errs.append("the design is empty: add at least one KPI card, "
                    "chart, or table with columns")
    if errs:
        raise ValueError("; ".join(errs))

    clean_kpis = [{"label": str(k["label"]).strip()[:80],
                   "field": k["field"],
                   "format": k.get("format", "int")} for k in kpis]
    clean_charts = []
    for c in charts:
        cc = {"type": c["type"],
              "title": str(c.get("title") or "").strip()[:120],
              "from": c.get("from"),
              "label_field": c.get("label_field")}
        if c.get("value_field"):
            cc["value_field"] = c["value_field"]
        if c.get("status_field"):
            cc["status_field"] = c["status_field"]
        clean_charts.append(cc)
    clean_table = None
    if isinstance(tbl, dict) and tbl.get("columns"):
        clean_table = {"title": str(tbl.get("title") or "Records")[:120],
                       "from": tbl.get("from"),
                       "columns": list(tbl["columns"])[:8]}
    clean_thresholds = [
        {"label": str(t["label"]).strip()[:80], "field": t["field"],
         "op": t["op"], "value": _num(t["value"])} for t in thresholds]
    return {
        "title": title,
        "subtitle": spec["subtitle"],
        "endpoints": endpoints,
        "source_labels": [SOURCES[e] for e in endpoints],
        "kpis": clean_kpis,
        "charts": clean_charts,
        "table": clean_table,
        "thresholds": clean_thresholds,
        "refresh_frequency": refresh_frequency,
    }


def build_service_spec(clean: dict) -> dict:
    """The generator spec for an Ember-designed dashboard.
    insight/chat are False: no Cube-side model ever runs for an
    Ember dashboard (eligibility); designed_by marks provenance."""
    return {
        "title": clean["title"],
        "subtitle": clean.get("subtitle") or "",
        "kpis": clean["kpis"],
        "charts": clean["charts"],
        "table": clean.get("table"),
        "thresholds": clean.get("thresholds") or [],
        "insight": False,
        "chat": False,
        "designed_by": "ember",
    }


def create_dashboard(clean: dict, base: str,
                     timeout: int = 180) -> dict:
    """Create via the generator's deterministic /api/create."""
    refresh = {"mode": "manual"}
    if clean.get("refresh_frequency"):
        refresh = {"mode": "rpa-schedule",
                   "frequency": clean["refresh_frequency"]}
    out = _post_json(base.rstrip("/") + "/api/create", {
        "spec": build_service_spec(clean),
        "source": source_payload(clean["endpoints"]),
        "refresh": refresh,
    }, timeout=timeout)
    if not out.get("ok"):
        errs = out.get("errors") or [out.get("error") or "create failed"]
        raise ValueError("dashboard create refused: "
                         + "; ".join(str(e) for e in errs))
    entry = out.get("dashboard") or {}
    slug = entry.get("slug") or slugify(clean["title"])
    result = {
        "slug": slug,
        "title": entry.get("title") or clean["title"],
        "url": PUBLIC_BASE + "/d/" + slug,
        "endpoints": clean["endpoints"],
        "source_labels": clean["source_labels"],
        "kpi_count": len(clean["kpis"]),
        "chart_count": len(clean["charts"]),
        "has_table": bool(clean.get("table")),
        "threshold_count": len(clean.get("thresholds") or []),
        "refresh_mode": entry.get("refresh_mode", "manual"),
        "refresh_flow_id": entry.get("flow_id"),
        "designed_by": "ember",
        "note": ("Created and rendered by the Metatron Dashboard "
                 "Generator from Ember's spec — live at the URL. "
                 "No Cube-side model was involved."),
    }
    if entry.get("flow_id"):
        result["note"] += (" A DISABLED refresh flow was created "
                           "in Metatron RPA; it will not run until "
                           "a human enables it.")
    return result


def delete_dashboard(slug: str, base: str,
                     timeout: int = 120) -> dict:
    """Delete via the generator's /api/delete (idempotent)."""
    out = _post_json(base.rstrip("/") + "/api/delete",
                     {"slug": slug}, timeout=timeout)
    if not out.get("ok"):
        raise ValueError("dashboard delete refused: "
                         + str(out.get("error") or "unknown error"))
    return out


def dashboard_page_ok(slug: str, base: str,
                      timeout: int = 30) -> bool:
    """Deterministic verification read: the rendered page serves."""
    req = urllib.request.Request(
        base.rstrip("/") + "/d/" + slug,
        headers={"User-Agent": "ember-orchestrator/dashboard-builder"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False
