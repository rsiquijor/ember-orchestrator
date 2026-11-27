"""Deterministic file export (Rom, Nov 26, 2026).

Ember proposes `export_file` (format + source only); a human
approves; this module generates the file with plain deterministic
code — no model runs, $0. Formats: csv, xlsx (Excel), json, txt,
docx (Word). Sources: the most recent filed intake extraction
(invoice / document / photo / audio transcript / video), the triage
board, the most recent document-analysis report, and the previous
run's structured tool results.

Storage note: file_intake stringifies list items (line_items dicts
become repr strings like "{'description': ..., 'qty': 2}"). The
exporter recovers those deterministically with ast.literal_eval —
literals only, never eval — so line items export as proper columns.
Anything unparseable is exported as the raw string; nothing is
invented.

openpyxl / python-docx: imported normally; on the Cube they live in
an app-local vendor dir (pip install --target vendor, Nov 26, 2026)
so the shared sys-venv that STT/TTS depend on stays untouched.
"""
from __future__ import annotations

import ast
import csv
import io
import json
import os
import re
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXPORT_FORMATS = ("csv", "xlsx", "json", "txt", "docx")
EXPORT_SOURCES = ("intake", "board", "document", "last_run", "plc",
                  "medical", "legal")

CONTENT_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": ("application/vnd.openxmlformats-officedocument."
             "spreadsheetml.sheet"),
    "json": "application/json",
    "txt": "text/plain; charset=utf-8",
    "docx": ("application/vnd.openxmlformats-officedocument."
             "wordprocessingml.document"),
}

# Long free-text fields render as their own section in txt/docx and
# as a (truncated) field value in csv/xlsx cells; JSON keeps all.
LONG_TEXT_FIELDS = {"transcript"}
CELL_CAP = 1000          # csv/xlsx cell truncation point
LINE_ITEM_COLUMNS = ("description", "qty", "unit_price", "amount")
BOARD_COLUMNS = ("ticket_id", "category", "priority", "route",
                 "draft_reply", "filed_at", "run_tag", "status")
FINDING_COLUMNS = ("line", "severity", "detail")


def _load_lib(name: str):
    """Import a library, falling back to the app-local vendor dir."""
    try:
        return __import__(name)
    except ImportError:
        vendor = os.path.join(ROOT, "vendor")
        if os.path.isdir(vendor) and vendor not in sys.path:
            sys.path.insert(0, vendor)
        return __import__(name)


# ------------------------------------------------------------- validation
def validate_export_params(params: dict) -> dict:
    """Allowlist check for Ember's export_file params. Returns the
    clean {format, source} or raises ValueError with a clear reason."""
    if not isinstance(params, dict):
        raise ValueError("export_file needs a params object")
    unknown = set(params) - {"format", "source"}
    if unknown:
        raise ValueError(
            f"unknown export_file parameter(s): {sorted(unknown)}")
    fmt = str(params.get("format") or "").strip().lower()
    if fmt not in EXPORT_FORMATS:
        raise ValueError(
            f"format must be one of {list(EXPORT_FORMATS)}, got {fmt!r}")
    source = str(params.get("source") or "").strip().lower()
    if source not in EXPORT_SOURCES:
        raise ValueError(
            f"source must be one of {list(EXPORT_SOURCES)}, "
            f"got {source!r}")
    return {"format": fmt, "source": source}


# ------------------------------------------------------------- store
class ExportStore:
    """Generated files + manifest under board/exports/.

    Files are named by unguessable uuid; the manifest maps id ->
    {id, filename, format, source, created_at, size_bytes}. The
    human-friendly filename lives only in the manifest + download
    header, never on disk."""

    def __init__(self, dir_path: str | None = None):
        self.dir = dir_path or os.environ.get(
            "EMBER_EXPORTS_DIR", os.path.join(ROOT, "board", "exports"))
        self.manifest_path = os.path.join(self.dir, "manifest.json")

    def _read_manifest(self) -> list[dict]:
        if not os.path.exists(self.manifest_path):
            return []
        with open(self.manifest_path) as f:
            data = json.load(f)
        return data.get("exports", []) if isinstance(data, dict) else []

    def _write_manifest(self, exports: list[dict]) -> None:
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.manifest_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"exports": exports}, f, indent=1)
        os.replace(tmp, self.manifest_path)

    def add(self, filename: str, fmt: str, source: str,
            payload: bytes) -> dict:
        os.makedirs(self.dir, exist_ok=True)
        export_id = uuid.uuid4().hex
        path = os.path.join(self.dir, f"{export_id}.{fmt}")
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(payload)
        os.replace(tmp, path)
        entry = {
            "id": export_id,
            "filename": filename,
            "format": fmt,
            "source": source,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime()),
            "size_bytes": len(payload),
        }
        exports = self._read_manifest()
        exports.append(entry)
        self._write_manifest(exports)
        return entry

    def list(self) -> list[dict]:
        return self._read_manifest()

    def get(self, export_id: str) -> dict | None:
        for e in self._read_manifest():
            if e.get("id") == export_id:
                return e
        return None

    def read(self, export_id: str) -> tuple[dict, bytes] | None:
        entry = self.get(export_id)
        if entry is None:
            return None
        path = os.path.join(self.dir, f"{export_id}.{entry['format']}")
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            return entry, f.read()

    def remove(self, export_id: str) -> bool:
        entry = self.get(export_id)
        if entry is None:
            return False
        path = os.path.join(self.dir, f"{export_id}.{entry['format']}")
        try:
            os.unlink(path)
        except OSError:
            pass
        self._write_manifest(
            [e for e in self._read_manifest() if e.get("id") != export_id])
        return True


# ------------------------------------------------------------- data prep
def _recover(value):
    """Recover a stored value: repr'd dicts/lists become real objects
    (ast.literal_eval — literals only). Anything else passes through."""
    if isinstance(value, str) and value[:1] in ("{", "["):
        try:
            parsed = ast.literal_eval(value)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            return value
        if isinstance(parsed, (dict, list)):
            return parsed
    return value


def _recover_fields(fields: dict) -> dict:
    out = {}
    for k, v in (fields or {}).items():
        if isinstance(v, list):
            out[k] = [_recover(x) for x in v]
        elif isinstance(v, dict):
            out[k] = {kk: _recover(vv) for kk, vv in v.items()}
        else:
            out[k] = _recover(v)
    return out


def _scalar_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _table_from_items(items: list, preferred: tuple) -> dict:
    """Rows of dicts (or plain strings) -> {columns, rows}."""
    dicts = [i for i in items if isinstance(i, dict)]
    if dicts and len(dicts) == len(items):
        columns = [c for c in preferred
                   if any(c in d for d in dicts)]
        for d in dicts:
            for k in d:
                if k not in columns:
                    columns.append(k)
        rows = [[_scalar_text(d.get(c)) for c in columns] for d in dicts]
        return {"columns": columns, "rows": rows}
    return {"columns": ["item"],
            "rows": [[_scalar_text(i)] for i in items]}


def intake_export_data(record: dict) -> dict:
    """Most recent filed intake extraction -> normalized export data."""
    fields = _recover_fields(record.get("fields") or {})
    kind = str(record.get("kind") or "other")
    field_rows = [("kind", kind)]
    if record.get("filed_at"):
        field_rows.append(("filed_at", str(record["filed_at"])))
    if record.get("status"):
        field_rows.append(("status", str(record["status"])))
    sections = []
    table = None
    for key, value in fields.items():
        if key == "line_items" and isinstance(value, list) and value:
            table = _table_from_items(value, LINE_ITEM_COLUMNS)
            continue
        if key in LONG_TEXT_FIELDS and isinstance(value, str) and value:
            sections.append((key.capitalize(), value))
            field_rows.append((key, value))
            continue
        field_rows.append((key, _scalar_text(value)))
    notes = str(record.get("notes") or "")
    if notes:
        field_rows.append(("notes", notes))
        sections.append(("Notes", notes))
    title = f"Intake extraction — {kind}"
    if fields.get("invoice_number"):
        title = f"Invoice {fields['invoice_number']}"
    return {
        "title": title,
        "field_rows": field_rows,
        "table": table,
        "table_title": "Line items" if table else None,
        "sections": sections,
        "json_payload": {"kind": kind, "fields": fields, "notes": notes,
                         "filed_at": record.get("filed_at"),
                         "status": record.get("status")},
        "invoice_number": fields.get("invoice_number"),
        "kind": kind,
    }


def board_export_data(tickets: list) -> dict:
    table = _table_from_items(list(tickets), BOARD_COLUMNS)
    return {
        "title": "Triage board tickets",
        "field_rows": [("tickets", str(len(tickets)))],
        "table": table,
        "table_title": "Tickets",
        "sections": [],
        "json_payload": {"tickets": list(tickets)},
    }


def document_export_data(report: dict) -> dict:
    findings = report.get("findings") or []
    table = (_table_from_items(findings, FINDING_COLUMNS)
             if findings else None)
    field_rows = [("title", str(report.get("title") or ""))]
    if report.get("filed_at"):
        field_rows.append(("filed_at", str(report["filed_at"])))
    if report.get("status"):
        field_rows.append(("status", str(report["status"])))
    summary = str(report.get("summary") or "")
    if summary:
        field_rows.append(("summary", summary))
    sections = []
    if summary:
        sections.append(("Summary", summary))
    if findings:
        lines = []
        for f in findings:
            ref = f"line {f['line']}" if f.get("line") else "—"
            lines.append(f"- {ref} [{f.get('severity', 'info')}] "
                         f"{f.get('detail', '')}")
        sections.append(("Findings", "\n".join(lines)))
    return {
        "title": str(report.get("title") or "Document analysis report"),
        "field_rows": field_rows,
        "table": table,
        "table_title": "Findings" if table else None,
        "sections": sections,
        "json_payload": {"title": report.get("title"),
                         "summary": report.get("summary"),
                         "findings": findings,
                         "filed_at": report.get("filed_at"),
                         "status": report.get("status")},
    }


COVERAGE_COLUMNS = ("instruction", "status", "target", "note")


def plc_export_data(conversion: dict) -> dict:
    """Most recent PLC conversion: fields + coverage table +
    converted code as a full-text section (TXT/DOCX/JSON carry
    it whole; CSV/XLSX cells truncate with a note)."""
    coverage = conversion.get("coverage") or []
    table = (_table_from_items(coverage, COVERAGE_COLUMNS)
             if coverage else None)
    stats = conversion.get("stats") or {}
    field_rows = [
        ("program_name", str(conversion.get("program_name") or "")),
        ("direction", str(conversion.get("direction_label") or "")),
        ("status", str(conversion.get("status") or "draft")),
        ("source_instructions",
         str(stats.get("source_instructions", ""))),
        ("mapped", str(stats.get("mapped", ""))),
        ("unsupported_flagged",
         ", ".join(stats.get("flagged") or []) or "none"),
    ]
    if conversion.get("rfc_ref"):
        field_rows.append(("change_record",
                           str(conversion["rfc_ref"])))
    sections = [
        ("Converted code (DRAFT — qualified-engineer review "
         "required; not safety-certified; do not deploy)",
         str(conversion.get("converted_code") or "")),
        ("Source code", str(conversion.get("source_code") or "")),
    ]
    if conversion.get("notes"):
        sections.append(("Conversion notes",
                         str(conversion["notes"])))
    return {
        "title": ("PLC conversion — "
                  + str(conversion.get("program_name") or "program")),
        "field_rows": field_rows,
        "table": table,
        "table_title": "Instruction coverage" if table else None,
        "sections": sections,
        "json_payload": {
            "conversion_id": conversion.get("id"),
            "program_name": conversion.get("program_name"),
            "direction": conversion.get("direction"),
            "status": conversion.get("status"),
            "stats": stats,
            "coverage": coverage,
            "converted_code": conversion.get("converted_code"),
            "source_code": conversion.get("source_code"),
            "notes": conversion.get("notes"),
        },
    }


PASSAGE_COLUMNS = ("chapter", "section", "pages")


def medical_export_data(result: dict) -> dict:
    """Most recent medical-manual answer: question fields +
    citation table + retrieved passages as full-text sections
    (TXT/DOCX/JSON carry them whole; CSV/XLSX cells truncate
    with a note). Demo reference only — not medical advice."""
    passages = result.get("passages") or []
    table = (_table_from_items(passages, PASSAGE_COLUMNS)
             if passages else None)
    field_rows = [
        ("question", str(result.get("question") or "")),
        ("manual", str(result.get("manual") or "")),
        ("covered", "yes" if result.get("covered") else
         "no — the manual does not cover this question"),
        ("passages", str(len(passages))),
        ("reference_tokens_approx",
         str(result.get("reference_tokens_approx", ""))),
        ("note", "Demo reference only — not medical advice; "
                 "consult a qualified professional."),
    ]
    sections = []
    for p in passages:
        head = str(p.get("chapter") or "")
        if p.get("section"):
            head += f" — {p['section']}"
        if p.get("pages"):
            head += f", {p['pages']}"
        sections.append((head or "Passage", str(p.get("text") or "")))
    return {
        "title": "Medical manual answer",
        "field_rows": field_rows,
        "table": table,
        "table_title": "Citations" if table else None,
        "sections": sections,
        "json_payload": {
            "question": result.get("question"),
            "manual": result.get("manual"),
            "covered": result.get("covered"),
            "citations": result.get("citations"),
            "passages": passages,
            "reference_tokens_approx":
                result.get("reference_tokens_approx"),
        },
    }


LEGAL_PASSAGE_COLUMNS = ("group", "citation", "heading")


def legal_export_data(result: dict) -> dict:
    """Most recent paralegal answer: question fields +
    citation table + retrieved passages as full-text sections
    (TXT/DOCX/JSON carry them whole; CSV/XLSX cells truncate
    with a note). Legal information only — not legal advice."""
    passages = result.get("passages") or []
    table = (_table_from_items(passages, LEGAL_PASSAGE_COLUMNS)
             if passages else None)
    field_rows = [
        ("question", str(result.get("question") or "")),
        ("corpus", str(result.get("corpus") or "")),
        ("covered", "yes" if result.get("covered") else
         "no — these sources do not cover this question"),
        ("passages", str(len(passages))),
        ("reference_tokens_approx",
         str(result.get("reference_tokens_approx", ""))),
        ("note", "Legal information only — not legal advice; "
                 "consult a licensed attorney."),
    ]
    sections = []
    for p in passages:
        head = str(p.get("citation") or "")
        if p.get("heading"):
            head += f" — {p['heading']}"
        if p.get("group"):
            head += f" ({p['group']})"
        sections.append((head or "Passage", str(p.get("text") or "")))
    return {
        "title": "Paralegal answer",
        "field_rows": field_rows,
        "table": table,
        "table_title": "Citations" if table else None,
        "sections": sections,
        "json_payload": {
            "question": result.get("question"),
            "corpus": result.get("corpus"),
            "covered": result.get("covered"),
            "citations": result.get("citations"),
            "passages": passages,
            "reference_tokens_approx":
                result.get("reference_tokens_approx"),
        },
    }


def last_run_export_data(trace: list) -> dict:
    """Previous run's structured tool results flattened to rows."""
    rows = []
    for entry in trace or []:
        tool = str(entry.get("tool") or "")
        result = entry.get("result")
        if isinstance(result, dict):
            for k, v in result.items():
                rows.append((f"{tool}.{k}", _scalar_text(v)))
        else:
            rows.append((tool, _scalar_text(result)))
    return {
        "title": "Last run results",
        "field_rows": rows,
        "table": None,
        "table_title": None,
        "sections": [],
        "json_payload": {"trace": [
            {"tool": e.get("tool"), "arguments": e.get("arguments"),
             "result": e.get("result")} for e in (trace or [])]},
    }


# ------------------------------------------------------------- filenames
def _safe_filename(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name)).strip("-.")
    return name[:80] or "ember-export"


def export_filename(source: str, fmt: str, data: dict) -> str:
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    if source == "intake" and data.get("invoice_number"):
        base = f"invoice-{data['invoice_number']}"
    elif source == "intake" and data.get("kind") in ("audio_note",
                                                      "video"):
        base = f"transcript-{ts}"
    elif source == "intake":
        base = f"intake-{data.get('kind') or 'record'}-{ts}"
    elif source == "board":
        base = f"triage-board-{ts}"
    elif source == "document":
        base = f"document-report-{ts}"
    elif source == "last_run":
        base = f"last-run-{ts}"
    elif source == "plc":
        base = f"plc-conversion-{ts}"
    elif source == "medical":
        base = f"medical-answer-{ts}"
    elif source == "legal":
        base = f"legal-answer-{ts}"
    else:
        base = f"ember-export-{ts}"
    return f"{_safe_filename(base)}.{fmt}"


# ------------------------------------------------------------- generators
def _cell(value: str) -> str:
    """Cell-safe text: truncate huge values (noted, never silent)."""
    text = _scalar_text(value)
    if len(text) > CELL_CAP:
        return text[:CELL_CAP] + "… [truncated — full text in TXT/DOCX/JSON export]"
    return text


def _number(value: str, allow_int: bool = False):
    """Numeric-looking strings -> numbers for spreadsheet cells.
    Field values keep plain integers as text (ids like work orders);
    table cells may convert clean integers (quantities)."""
    text = str(value).strip()
    cleaned = text.replace(",", "")
    if re.fullmatch(r"-?\d+\.\d+", cleaned):
        return float(cleaned)
    if allow_int and re.fullmatch(r"-?\d+", cleaned):
        if len(cleaned.lstrip("-")) > 1 and cleaned.lstrip("-").startswith("0"):
            return text
        return int(cleaned)
    return text


def to_csv_bytes(data: dict) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    field_rows = data.get("field_rows") or []
    table = data.get("table")
    if field_rows:
        writer.writerow(["Field", "Value"])
        for k, v in field_rows:
            writer.writerow([k, _cell(v)])
    if table:
        if field_rows:
            writer.writerow([])
        writer.writerow(table["columns"])
        for row in table["rows"]:
            writer.writerow([_cell(c) for c in row])
    return buf.getvalue().encode("utf-8")


def to_json_bytes(data: dict) -> bytes:
    return (json.dumps(data["json_payload"], indent=1) + "\n").encode()


def to_xlsx_bytes(data: dict) -> bytes:
    openpyxl = _load_lib("openpyxl")
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Export"
    bold = Font(bold=True)
    field_rows = data.get("field_rows") or []
    table = data.get("table")
    if field_rows:
        ws.append(["Field", "Value"])
        for cell in ws[1]:
            cell.font = bold
        for k, v in field_rows:
            ws.append([k, _number(_cell(v))])
        ws.column_dimensions["A"].width = 24
        ws.column_dimensions["B"].width = 60
    if table:
        target = ws if not field_rows else wb.create_sheet(
            (data.get("table_title") or "Table")[:31])
        target.append(list(table["columns"]))
        for cell in target[1]:
            cell.font = bold
        for row in table["rows"]:
            target.append([_number(_cell(c), allow_int=True)
                           for c in row])
        for idx in range(1, len(table["columns"]) + 1):
            target.column_dimensions[
                openpyxl.utils.get_column_letter(idx)].width = 22
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _txt_table(table: dict) -> str:
    columns = list(table["columns"])
    rows = [[_cell(c) for c in row] for row in table["rows"]]
    widths = []
    for i, col in enumerate(columns):
        widths.append(min(48, max([len(col)] +
                                  [len(r[i]) for r in rows] or [0])))

    def fmt(row):
        cells = []
        for i, c in enumerate(row):
            text = c if len(c) <= widths[i] else c[:widths[i] - 1] + "…"
            cells.append(text.ljust(widths[i]))
        return "  ".join(cells).rstrip()

    lines = [fmt(columns), fmt(["-" * w for w in widths])]
    lines += [fmt(r) for r in rows]
    return "\n".join(lines)


def to_txt_bytes(data: dict) -> bytes:
    lines = [data.get("title") or "Export",
             "=" * len(data.get("title") or "Export"), ""]
    section_keys = {h.lower() for h, _ in data.get("sections") or []}
    for k, v in data.get("field_rows") or []:
        if k.lower() in section_keys:
            continue  # rendered as a section below, not duplicated
        lines.append(f"{k}: {_scalar_text(v)}")
    for heading, text in data.get("sections") or []:
        lines += ["", heading, "-" * len(heading), text]
    table = data.get("table")
    if table:
        heading = data.get("table_title") or "Table"
        lines += ["", heading, "-" * len(heading), _txt_table(table)]
    lines += ["", "Generated by Metatron Cube · Ember Orchestrator "
              "(deterministic export — no model ran)."]
    return ("\n".join(lines) + "\n").encode("utf-8")


def to_docx_bytes(data: dict) -> bytes:
    docx = _load_lib("docx")
    doc = docx.Document()
    doc.add_heading(data.get("title") or "Export", level=1)
    section_keys = {h.lower() for h, _ in data.get("sections") or []}
    rows = [(k, v) for k, v in (data.get("field_rows") or [])
            if k.lower() not in section_keys]
    if rows:
        table = doc.add_table(rows=0, cols=2)
        table.style = "Light Grid Accent 1"
        for k, v in rows:
            cells = table.add_row().cells
            cells[0].text = str(k)
            for run in cells[0].paragraphs[0].runs:
                run.bold = True
            cells[1].text = _scalar_text(v)
    for heading, text in data.get("sections") or []:
        doc.add_heading(heading, level=2)
        for para in str(text).split("\n"):
            doc.add_paragraph(para)
    table = data.get("table")
    if table:
        doc.add_heading(data.get("table_title") or "Table", level=2)
        t = doc.add_table(rows=1, cols=len(table["columns"]))
        t.style = "Light Grid Accent 1"
        for i, col in enumerate(table["columns"]):
            cell = t.rows[0].cells[i]
            cell.text = str(col)
            for run in cell.paragraphs[0].runs:
                run.bold = True
        for row in table["rows"]:
            cells = t.add_row().cells
            for i, value in enumerate(row):
                cells[i].text = _scalar_text(value)
    doc.add_paragraph(
        "Generated by Metatron Cube · Ember Orchestrator "
        "(deterministic export — no model ran).")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


GENERATORS = {
    "csv": to_csv_bytes,
    "xlsx": to_xlsx_bytes,
    "json": to_json_bytes,
    "txt": to_txt_bytes,
    "docx": to_docx_bytes,
}


def generate_export(store: ExportStore, fmt: str, source: str,
                    data: dict) -> dict:
    """Generate + store one export file. Returns the tool result."""
    payload = GENERATORS[fmt](data)
    filename = export_filename(source, fmt, data)
    entry = store.add(filename, fmt, source, payload)
    return {
        "exported": True,
        "export_id": entry["id"],
        "filename": entry["filename"],
        "format": fmt,
        "source": source,
        "size_bytes": entry["size_bytes"],
        "download_path": f"/api/exports/{entry['id']}",
        "note": "Deterministic export generated ($0 — no model ran). "
                "Download it from the card in the stage.",
    }
