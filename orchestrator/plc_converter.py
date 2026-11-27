"""Universal PLC Code Converter for the Ember Orchestrator
(Nov 27, 2026).

Rom: "incorporate the Universal PLC Code Converter as a feature,
function, and real demo... upload as many PLC code manuals to
train Long context parsing to automatically change from one PLC
system and program to another. The human approval route is
automated, then Ember executes the code conversion. Then change
management approval uses the Metatron RPA for approval and
acceptance."

How it fits the MAC pattern (Ember proposes, a human approves,
deterministic code executes): Ember authors the converted code
AND a per-instruction coverage report at propose time, with the
vendor instruction-reference manuals injected as long context
by deterministic file reads (no KB search, no Cube-side model —
Ember is the only AI). This module validates the proposal
deterministically BEFORE it reaches the approval screen:

  - every instruction mnemonic parsed out of the source program
    must appear in the coverage report as mapped or flagged
    unsupported (unsupported instructions are FLAGGED, never
    silently replaced);
  - the converted code must not contain source-platform-only
    mnemonics (a Siemens output still calling CPT/LIM/MSG is not
    a conversion);
  - mapped entries naming a distinctive target instruction
    must actually use it in the converted code.

Converted code is DRAFT, labelled demo, and never auto-deployed
to any PLC. A qualified engineer reviews it before any real
use; the RPA change-approval flow (change record + approval
waitpoint) is the acceptance gate.

Manuals corpus: plc_manuals/*.txt (extracted instruction
reference text). File naming: ab-*.txt (Allen-Bradley),
siemens-*.txt (Siemens). load_manual_context() assembles the
long-context block for a direction within a character budget.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DIRECTIONS = ("ab_to_siemens", "siemens_to_ab")
DIRECTION_LABELS = {
    "ab_to_siemens": "Allen-Bradley → Siemens",
    "siemens_to_ab": "Siemens → Allen-Bradley",
}
SOURCE_PLATFORM = {"ab_to_siemens": "ab", "siemens_to_ab": "siemens"}
TARGET_PLATFORM = {"ab_to_siemens": "siemens", "siemens_to_ab": "ab"}
PLATFORM_LABELS = {"ab": "Allen-Bradley (Studio 5000)",
                   "siemens": "Siemens (S7-1200/1500 SCL)"}

# Long-context budget for the manuals block. ~720K chars ≈ 180K
# tokens — a genuine long-context call that stays under the
# 200K-token pricing tier ($0.10/1M input).
MANUAL_CHAR_BUDGET = 720_000

# ------------------------------------------------------ instruction sets
# Mnemonic names only (facts from the vendor instruction
# references) — used by the parser and the coverage validator.
AB_INSTRUCTIONS = {
    # bit
    "XIC", "XIO", "OTE", "OTL", "OTU", "OSR", "OSF", "ONS",
    # timer / counter
    "TON", "TOF", "RTO", "CTU", "CTD", "RES",
    # compare
    "CMP", "EQU", "NEQ", "GRT", "GEQ", "LES", "LEQ", "LIM", "MEQ",
    # compute / math
    "CPT", "ADD", "SUB", "MUL", "DIV", "MOD", "SQR", "NEG", "ABS",
    "CLR", "AND", "OR", "XOR", "NOT", "MAX", "MIN", "AVE", "STD",
    "SIN", "COS", "TAN", "ASN", "ACS", "ATN", "LN", "LOG", "XPY",
    "DEG", "RAD", "TRN",
    # move / logical
    "MOV", "MVM", "COP", "CPS", "FLL", "BTD", "SWPB", "DTOS",
    "STOD", "TOD", "FRD", "ENC", "DEC", "SRT",
    # program control
    "JMP", "LBL", "JSR", "RET", "SBR", "FOR", "TND", "MCR",
    # special
    "MSG", "GSV", "SSV", "PID", "FAL", "FSC", "UID", "UIE",
    "SCP", "SCL",
}
SIEMENS_INSTRUCTIONS = {
    # timers / counters
    "TON", "TOF", "TP", "TONR", "CTU", "CTD", "CTUD",
    # math
    "ADD", "SUB", "MUL", "DIV", "MOD", "ABS", "MIN", "MAX",
    "LIMIT", "NORM_X", "SCALE_X", "SQR", "SQRT", "LN", "EXP",
    "SIN", "COS", "TAN", "ASIN", "ACOS", "ATAN", "FRAC", "TRUNC",
    "ROUND", "CEIL", "FLOOR",
    # move / word logic
    "MOVE", "BLKMOV", "FILL", "SWAP", "SEL", "MUX", "DEMUX",
    "SHL", "SHR", "ROL", "ROR", "ENCO", "DECO", "AND", "OR",
    "XOR", "NOT", "NEG", "INV",
    # compare (LAD forms)
    "EQ", "NE", "GT", "GE", "LT", "LE", "IN_RANGE", "OUT_RANGE",
    # bit logic
    "SR", "RS", "R_TRIG", "F_TRIG", "SET_BF", "RESET_BF",
    # communications / system
    "GET", "PUT", "TSEND", "TRCV", "TCON", "TDISCON", "WRREC",
    "RDREC", "GETIO", "SETIO",
    # control / misc
    "PID_COMPACT", "CALCULATE", "CONV", "SCALE", "UNSCALE",
}
PLATFORM_INSTRUCTIONS = {"ab": AB_INSTRUCTIONS,
                         "siemens": SIEMENS_INSTRUCTIONS}
# Mnemonics that exist on ONE platform only — their presence in
# converted output means the "conversion" still speaks the
# source language.
AB_ONLY = AB_INSTRUCTIONS - SIEMENS_INSTRUCTIONS
SIEMENS_ONLY = SIEMENS_INSTRUCTIONS - AB_INSTRUCTIONS
PLATFORM_ONLY = {"ab": AB_ONLY, "siemens": SIEMENS_ONLY}

_KEYWORDS = {
    "IF", "THEN", "ELSE", "ELSIF", "END_IF", "FOR", "TO", "DO",
    "END_FOR", "WHILE", "END_WHILE", "REPEAT", "UNTIL",
    "END_REPEAT", "CASE", "OF", "END_CASE", "RETURN", "EXIT",
    "CONTINUE", "VAR", "VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT",
    "VAR_TEMP", "VAR_STAT", "END_VAR", "FUNCTION_BLOCK",
    "END_FUNCTION_BLOCK", "FUNCTION", "END_FUNCTION", "PROGRAM",
    "END_PROGRAM", "TRUE", "FALSE", "AND", "OR", "XOR", "NOT",
    "MOD", "BOOL", "INT", "DINT", "REAL", "LREAL", "SINT",
    "USINT", "UINT", "UDINT", "BYTE", "WORD", "DWORD", "TIME",
    "TIMER", "COUNTER", "STRING", "ARRAY", "STRUCT", "END_STRUCT",
    "BEGIN", "END", "REGION", "END_REGION", "CONST", "TYPE",
    "END_TYPE", "AT", "WITH", "BY", "STEP", "TRANSITION",
}
_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_LADDER_RE = re.compile(
    r"\b(XIC|XIO|OTE|OTL|OTU|OSR|OSF|ONS|RES|JMP|LBL|JSR|RET|"
    r"TND|MCR|SR|RS)\b")


def _strip_comments(code: str) -> str:
    code = re.sub(r"\(\*.*?\*\)", " ", code, flags=re.S)
    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.S)
    code = re.sub(r"//[^\n]*", " ", code)
    code = re.sub(r"'[^']*'", "''", code)
    code = re.sub(r'"[^"]*"', '""', code)
    return code


def parse_source_instructions(code: str, platform: str) -> list[str]:
    """Distinct instruction mnemonics in a source program.

    Candidates: call-form tokens (`NAME(`) that are known
    instructions of the source platform, any ALL-CAPS call token
    (unrecognized — still requires a coverage entry so nothing
    is silently dropped), and ladder bit mnemonics. Keywords,
    type names, and comments are excluded. Instance calls with
    mixed-case names (#RunDelay(...)) are user function-block
    instances, not mnemonics — not candidates."""
    known = PLATFORM_INSTRUCTIONS[platform]
    text = _strip_comments(str(code or ""))
    found: list[str] = []
    seen: set[str] = set()

    def add(tok: str):
        up = tok.upper()
        if up not in seen and up not in _KEYWORDS:
            seen.add(up)
            found.append(up)

    for m in _CALL_RE.finditer(text):
        tok = m.group(1)
        up = tok.upper()
        if up in _KEYWORDS:
            continue
        if up in known:
            add(up)
        elif tok == up and len(up) >= 2 and not up.isdigit():
            # ALL-CAPS call, not a known instruction: still an
            # instruction candidate — coverage must account for it.
            add(up)
    for m in _LADDER_RE.finditer(text):
        add(m.group(1))
    return found


def _contains_token(code: str, mnemonic: str) -> bool:
    return re.search(r"\b" + re.escape(mnemonic) + r"\b",
                     _strip_comments(code), flags=re.I) is not None


_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _mnemonic_tokens(text: str) -> list[str]:
    """Mnemonic-like tokens in a coverage instruction/target field.

    Ember may write 'SCALE_X (scale value)' or 'NORM_X/SCALE_X';
    the coverage check works on the mnemonic tokens, not the
    prose around them. A token qualifies when it is ALL-CAPS in
    the original (mnemonics are written that way) and len >= 2."""
    out: list[str] = []
    for m in _WORD_RE.finditer(str(text or "")):
        tok = m.group(0)
        if tok == tok.upper() and len(tok) >= 2 and tok not in out:
            out.append(tok)
    return out


def validate_conversion(params: dict) -> dict:
    """Validate Ember's convert_plc_code params deterministically.

    Returns the clean params dict (with coverage stats); raises
    ValueError with a teaching message naming the fix."""
    params = params or {}
    direction = str(params.get("direction") or "").strip().lower()
    if direction not in DIRECTIONS:
        raise ValueError(
            "direction must be 'ab_to_siemens' or 'siemens_to_ab'")
    source_code = str(params.get("source_code") or "")
    converted_code = str(params.get("converted_code") or "")
    program_name = str(params.get("program_name")
                       or "Converted program").strip()[:80]
    if len(source_code.strip()) < 20:
        raise ValueError(
            "source_code is missing or too short — include the "
            "full source program text")
    if len(converted_code.strip()) < 30:
        raise ValueError(
            "converted_code is missing or too short — include "
            "the full converted program")
    # Parenthesis balance is checked on comment/string-stripped
    # code: parens inside comments or string literals are not
    # code structure.
    stripped = _strip_comments(converted_code)
    if stripped.count("(") != stripped.count(")"):
        raise ValueError(
            "converted_code has unbalanced parentheses — fix "
            "the code and propose again")
    if ";" not in converted_code and ":=" not in converted_code:
        raise ValueError(
            "converted_code contains no statements (no ';' or "
            "':=') — include real program statements")

    src_platform = SOURCE_PLATFORM[direction]
    tgt_platform = TARGET_PLATFORM[direction]
    source_instructions = parse_source_instructions(
        source_code, src_platform)
    if not source_instructions:
        raise ValueError(
            "no recognizable "
            + PLATFORM_LABELS[src_platform]
            + " instructions found in source_code — check the "
              "direction and include real program code")

    coverage = params.get("coverage")
    if not isinstance(coverage, list) or not coverage:
        raise ValueError(
            "coverage must be a list accounting for EVERY source "
            "instruction: [{instruction, status: mapped|"
            "unsupported, target, note}]. Source instructions "
            "found: " + ", ".join(source_instructions))
    clean_coverage = []
    covered: dict[str, dict] = {}
    for entry in coverage:
        if not isinstance(entry, dict):
            raise ValueError(
                "every coverage entry must be an object "
                "{instruction, status, target, note}")
        raw_inst = str(entry.get("instruction") or "").strip()
        status = str(entry.get("status") or "").strip().lower()
        if not raw_inst:
            raise ValueError(
                "every coverage entry needs an instruction name")
        # The instruction field may carry prose ('SCALE_X (scale
        # value)') or combine mnemonics ('NORM_X/SCALE_X'). Work
        # on the mnemonic tokens; the stored name is the primary
        # token (first one that is a real source instruction).
        tokens = _mnemonic_tokens(raw_inst)
        primary = next(
            (t for t in tokens if t in source_instructions),
            tokens[0] if tokens else raw_inst.upper()[:80])
        if status not in ("mapped", "unsupported"):
            raise ValueError(
                f"coverage status for {primary} must be 'mapped' "
                "or 'unsupported' — unsupported instructions are "
                "flagged, never silently replaced")
        item = {
            "instruction": primary,
            "status": status,
            "target": str(entry.get("target") or "").strip()[:80],
            "note": str(entry.get("note") or "").strip()[:300],
        }
        clean_coverage.append(item)
        for tok in tokens:
            if tok in source_instructions:
                covered[tok] = item
        covered.setdefault(primary, item)
    missing = [i for i in source_instructions if i not in covered]
    if missing:
        raise ValueError(
            "coverage is incomplete — these source instructions "
            "are not accounted for: " + ", ".join(missing)
            + ". Add a coverage entry for each (mapped with its "
              "target equivalent, or unsupported with a note)")

    # Target-language check: no source-only mnemonics may survive
    # into the converted code.
    survivors = [m for m in sorted(PLATFORM_ONLY[src_platform])
                 if _contains_token(converted_code, m)]
    if survivors:
        raise ValueError(
            "converted_code still uses source-platform-only "
            "instructions: " + ", ".join(survivors)
            + " — replace them with "
            + PLATFORM_LABELS[tgt_platform]
            + " equivalents or flag them unsupported and remove "
              "them from the converted code")

    # Distinctive target instructions named as mapping targets
    # must actually be used in the converted code.
    unused = []
    for item in clean_coverage:
        if item["status"] != "mapped":
            continue
        for target in _mnemonic_tokens(item["target"]):
            if target in PLATFORM_ONLY[tgt_platform] \
                    and not _contains_token(converted_code,
                                            target):
                unused.append(f"{item['instruction']}->{target}")
    if unused:
        raise ValueError(
            "coverage claims mappings the converted code does "
            "not use: " + ", ".join(unused)
            + " — use those target instructions in "
              "converted_code or correct the coverage")

    mapped = [c for c in clean_coverage if c["status"] == "mapped"]
    flagged = [c for c in clean_coverage
               if c["status"] == "unsupported"]
    return {
        "direction": direction,
        "direction_label": DIRECTION_LABELS[direction],
        "program_name": program_name or "Converted program",
        "source_code": source_code,
        "converted_code": converted_code,
        "coverage": clean_coverage,
        "notes": str(params.get("notes") or "").strip()[:1000],
        "stats": {
            "source_instructions": len(source_instructions),
            "coverage_entries": len(clean_coverage),
            "mapped": len(mapped),
            "unsupported": len(flagged),
            "flagged": [c["instruction"] for c in flagged],
        },
    }


# ------------------------------------------------------ manuals corpus
def manuals_dir() -> str:
    return os.environ.get(
        "EMBER_PLC_MANUALS_DIR", os.path.join(ROOT, "plc_manuals"))


def _manual_files(platform: str, directory: str) -> list[str]:
    prefix = "ab-" if platform == "ab" else "siemens-"
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [os.path.join(directory, n) for n in names
            if n.startswith(prefix) and n.endswith(".txt")]


def load_manual_context(direction: str,
                        directory: str | None = None,
                        char_budget: int = MANUAL_CHAR_BUDGET
                        ) -> tuple[str, dict]:
    """Assemble the long-context manuals block for a conversion.

    Returns (text, stats). The source platform's reference gets
    ~45% of the budget, the target's ~55% (writing correct target
    code matters most). Missing manuals yield an empty block and
    zeroed stats — the caller states that honestly."""
    directory = directory or manuals_dir()
    src = SOURCE_PLATFORM[direction]
    tgt = TARGET_PLATFORM[direction]
    budgets = {src: int(char_budget * 0.45),
               tgt: int(char_budget * 0.55)}
    parts: list[str] = []
    stats: dict = {"manuals": [], "chars": 0, "approx_tokens": 0}
    for platform in (src, tgt):
        files = _manual_files(platform, directory)
        budget = budgets[platform]
        for path in files:
            if budget <= 0:
                break
            try:
                with open(path, encoding="utf-8",
                          errors="replace") as f:
                    text = f.read(budget)
            except OSError:
                continue
            if not text.strip():
                continue
            name = os.path.basename(path)
            parts.append(
                f"--- VENDOR MANUAL ({PLATFORM_LABELS[platform]}): "
                f"{name} ---\n" + text)
            stats["manuals"].append({
                "file": name, "platform": platform,
                "chars": len(text),
                "approx_tokens": len(text) // 4})
            stats["chars"] += len(text)
            budget -= len(text)
    stats["approx_tokens"] = stats["chars"] // 4
    return "\n\n".join(parts), stats


# ------------------------------------------------------ sample programs
AB_SAMPLE_NAME = "Conveyor_Motor_Control"
AB_SAMPLE = """\
(* Conveyor_Motor_Control — Allen-Bradley Studio 5000 Structured Text *)
(* Start/Stop seal-in, E-stop interlock, run-delay timer, part
   counter, 4-20 mA speed scaling, overload alarm latch, peer MSG *)

IF Start_PB AND NOT Stop_PB AND EStop_OK THEN
    Run_Cmd := 1;
ELSIF Stop_PB OR NOT EStop_OK THEN
    Run_Cmd := 0;
END_IF;

TON(RunDelay);
RunDelay.PRE := 2000;
Motor_Run := RunDelay.DN AND EStop_OK;

CTU(PartCount);
PartCount.PRE := 500;
Batch_Done := PartCount.DN;
IF Reset_Count THEN
    RES(PartCount);
END_IF;

Speed_Pct := CPT((Analog_In - 4000.0) * 100.0 / 16000.0);
Speed_OK := LIM(0.0, Speed_Pct, 100.0);
Speed_Cmd := MUL(Speed_Pct, 0.6);

Batch_Near_Done := GRT(PartCount.ACC, 450);

IF Motor_Run AND GRT(Motor_Current, 42.0) THEN
    Alarm_Latched := 1;
END_IF;
IF Alarm_Reset AND NOT GRT(Motor_Current, 42.0) THEN
    Alarm_Latched := 0;
END_IF;
Alarm_Active := Alarm_Latched;

MSG(Peer_Msg);
Peer_Msg.EN := Batch_Done;
"""

SIEMENS_SAMPLE_NAME = "Conveyor_Motor_Control"
SIEMENS_SAMPLE = """\
(* Conveyor_Motor_Control — Siemens S7-1500 SCL *)
(* Start/Stop seal-in, E-stop interlock, run-delay timer, part
   counter, 4-20 mA speed scaling, overload alarm latch, drive
   parameter record write *)

IF #Start_PB AND NOT #Stop_PB AND #EStop_OK THEN
    #Run_Cmd := TRUE;
ELSIF #Stop_PB OR NOT #EStop_OK THEN
    #Run_Cmd := FALSE;
END_IF;

#RunDelay(IN := #Run_Cmd, PT := T#2S);
#Motor_Run := #RunDelay.Q AND #EStop_OK;

#PartCount(CU := #Photoeye, R := #Reset_Count, PV := 500);
#Batch_Done := #PartCount.Q;
#Batch_Near_Done := #PartCount.CV > 450;

#Speed_Pct := SCALE_X(MIN := 0.0, VALUE := NORM_X(MIN := 4000.0, VALUE := #Analog_In, MAX := 20000.0), MAX := 100.0);
#Speed_Cmd := LIMIT(MN := 0.0, IN := #Speed_Pct, MX := 100.0) * 0.6;

IF #Motor_Run AND (#Motor_Current > 42.0) THEN
    #Alarm_Latched := TRUE;
END_IF;
IF #Alarm_Reset AND (#Motor_Current <= 42.0) THEN
    #Alarm_Latched := FALSE;
END_IF;
#Alarm_Active := #Alarm_Latched;

#DriveWrStatus := WRREC(REQ := #Batch_Done, ID := #DriveHwId, INDEX := 47, LEN := 4, RECORD := #SpeedRecord);
"""

SAMPLES = {
    "ab_to_siemens": (AB_SAMPLE_NAME, AB_SAMPLE),
    "siemens_to_ab": (SIEMENS_SAMPLE_NAME, SIEMENS_SAMPLE),
}

# Canned conversions of the samples (stub client + tests + the
# recorded demo base). These pass validate_conversion.
AB_TO_SIEMENS_CONVERTED = """\
(* Conveyor_Motor_Control — converted Allen-Bradley -> Siemens SCL *)
(* DRAFT — generated by MAC for qualified-engineer review.
   Not safety-certified. Do not deploy without review. *)

IF #Start_PB AND NOT #Stop_PB AND #EStop_OK THEN
    #Run_Cmd := TRUE;
ELSIF #Stop_PB OR NOT #EStop_OK THEN
    #Run_Cmd := FALSE;
END_IF;

#RunDelay(IN := #Run_Cmd, PT := T#2S);
#Motor_Run := #RunDelay.Q AND #EStop_OK;

#PartCount(CU := #Photoeye, R := #Reset_Count, PV := 500);
#Batch_Done := #PartCount.Q;

#Speed_Pct := SCALE_X(MIN := 0.0, VALUE := NORM_X(MIN := 4000.0, VALUE := #Analog_In, MAX := 20000.0), MAX := 100.0);
#Speed_OK := (#Speed_Pct >= 0.0) AND (#Speed_Pct <= 100.0);
#Speed_Cmd := #Speed_Pct * 0.6;

#Batch_Near_Done := #PartCount.CV > 450;

IF #Motor_Run AND (#Motor_Current > 42.0) THEN
    #Alarm_Latched := TRUE;
END_IF;
IF #Alarm_Reset AND NOT (#Motor_Current > 42.0) THEN
    #Alarm_Latched := FALSE;
END_IF;
#Alarm_Active := #Alarm_Latched;

(* UNSUPPORTED: MSG (peer message) has no direct Siemens
   equivalent. Redesign with PUT/GET or TSEND/TRCV over a
   configured S7 connection. *)
"""

AB_TO_SIEMENS_COVERAGE = [
    {"instruction": "TON", "status": "mapped", "target": "TON",
     "note": "RunDelay timer: PRE 2000 ms -> PT T#2S; DN -> Q."},
    {"instruction": "CTU", "status": "mapped", "target": "CTU",
     "note": "PartCount: PRE -> PV 500; DN -> Q; ACC -> CV."},
    {"instruction": "RES", "status": "mapped", "target": "CTU",
     "note": "Counter reset -> CTU R input wired to Reset_Count."},
    {"instruction": "CPT", "status": "mapped", "target": "SCALE_X",
     "note": "4-20 mA scaling expression -> NORM_X + SCALE_X."},
    {"instruction": "LIM", "status": "mapped", "target": ">",
     "note": "Range test -> SCL >= / <= comparison pair."},
    {"instruction": "MUL", "status": "mapped", "target": "*",
     "note": "Multiply -> SCL * operator."},
    {"instruction": "GRT", "status": "mapped", "target": ">",
     "note": "Greater-than -> SCL > comparison."},
    {"instruction": "MSG", "status": "unsupported", "target": "",
     "note": "Peer-controller message has no direct Siemens "
             "equivalent; redesign with PUT/GET or TSEND/TRCV "
             "over a configured connection."},
]

SIEMENS_TO_AB_CONVERTED = """\
(* Conveyor_Motor_Control — converted Siemens -> Allen-Bradley ST *)
(* DRAFT — generated by MAC for qualified-engineer review.
   Not safety-certified. Do not deploy without review. *)

IF Start_PB AND NOT Stop_PB AND EStop_OK THEN
    Run_Cmd := 1;
ELSIF Stop_PB OR NOT EStop_OK THEN
    Run_Cmd := 0;
END_IF;

TON(RunDelay);
RunDelay.PRE := 2000;
Motor_Run := RunDelay.DN AND EStop_OK;

CTU(PartCount);
PartCount.PRE := 500;
Batch_Done := PartCount.DN;
IF Reset_Count THEN
    RES(PartCount);
END_IF;
Batch_Near_Done := GRT(PartCount.ACC, 450);

Speed_Pct := CPT((Analog_In - 4000.0) * 100.0 / 16000.0);
Speed_Cmd := MUL(MAX(MIN(Speed_Pct, 100.0), 0.0), 0.6);

IF Motor_Run AND GRT(Motor_Current, 42.0) THEN
    Alarm_Latched := 1;
END_IF;
IF Alarm_Reset AND NOT GRT(Motor_Current, 42.0) THEN
    Alarm_Latched := 0;
END_IF;
Alarm_Active := Alarm_Latched;

(* UNSUPPORTED: WRREC (write data record to a module) has no
   direct Allen-Bradley equivalent. Redesign with a MSG (CIP
   generic) or the drive vendor's AOI/AOP. *)
"""

SIEMENS_TO_AB_COVERAGE = [
    {"instruction": "NORM_X", "status": "mapped", "target": "CPT",
     "note": "Normalize 4000..20000 -> CPT scaling expression."},
    {"instruction": "SCALE_X", "status": "mapped", "target": "CPT",
     "note": "Scale to 0..100% -> same CPT expression."},
    {"instruction": "LIMIT", "status": "mapped", "target": "MAX",
     "note": "Clamp 0..100 -> MAX(MIN(x, 100.0), 0.0)."},
    {"instruction": "WRREC", "status": "unsupported", "target": "",
     "note": "Write data record to a module has no direct "
             "Allen-Bradley equivalent; redesign with MSG (CIP "
             "generic) or the drive vendor's AOI/AOP."},
    {"instruction": "TON", "status": "mapped", "target": "TON",
     "note": "RunDelay instance -> TON with PRE 2000; Q -> DN."},
    {"instruction": "CTU", "status": "mapped", "target": "CTU",
     "note": "PartCount instance -> CTU; PV -> PRE; CV -> ACC; "
             "R input -> RES."},
]

SAMPLE_CONVERSIONS = {
    "ab_to_siemens": {
        "program_name": AB_SAMPLE_NAME,
        "source_code": AB_SAMPLE,
        "converted_code": AB_TO_SIEMENS_CONVERTED,
        "coverage": AB_TO_SIEMENS_COVERAGE,
    },
    "siemens_to_ab": {
        "program_name": SIEMENS_SAMPLE_NAME,
        "source_code": SIEMENS_SAMPLE,
        "converted_code": SIEMENS_TO_AB_CONVERTED,
        "coverage": SIEMENS_TO_AB_COVERAGE,
    },
}
