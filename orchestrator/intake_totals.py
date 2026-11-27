"""Deterministic invoice-totals verification for filed intakes.

Why this exists (Nov 27, 2026): Ember extracted a correct invoice
(intake #59) but populated amount_paid with the total's value, then —
instructed to "flag any arithmetic mismatch" — wrote its own
arithmetic critique into notes and repeated it in the summary as a
"Flagged mismatch". The invoice was fine; the flag was the model's
free-form reasoning over its own mis-extraction.

The fix: totals are verified HERE, deterministically, from the
extracted fields. Ember's job is extraction + reporting this verdict
exactly — never its own discrepancy claims. Conservative by design:
anything unparseable is skipped, never guessed, so a correct invoice
cannot produce a false alarm.

Checks (tolerance 1 cent):
1. line items sum vs subtotal.
2. subtotal + tax - discount + shipping vs total due (absent
   components count as 0 — the INV-88410 planted $7.50 overstatement
   has no tax/shipping lines and must still flag).
3. balance arithmetic: total - amount_paid vs balance due — only
   when amount_paid is present, distinct from the total, and not an
   overpayment. The pattern amount_paid == total AND balance_due ==
   total is a known mis-extraction signature (the model copied the
   total into amount_paid); it is treated as unreliable and NOT
   flagged.
"""
from __future__ import annotations

import ast
import re

TOL = 0.01

_ALIASES = {
    "subtotal": ("subtotal", "sub_total"),
    "tax": ("tax", "sales_tax", "vat"),
    "discount": ("discount",),
    "shipping": ("shipping", "freight", "delivery"),
    "total": ("total_due", "total", "grand_total", "amount_due",
              "invoice_total"),
    "paid": ("amount_paid", "paid", "payments", "payment_received"),
    "balance": ("balance_due", "balance", "amount_owing"),
}

_MONEY_RE = re.compile(r"^-?\d+(\.\d+)?$")
_EQ_TAIL_RE = re.compile(r"=\s*\$?\s*([\d,]+(?:\.\d+)?)\s*$")
_QTY_PRICE_RE = re.compile(
    r"[x×]\s*(\d+(?:\.\d+)?)\s*@\s*\$?\s*([\d,]+(?:\.\d+)?)")


def parse_money(value) -> float | None:
    """Parse a money-ish value to float, or None if not confidently
    a number. Accepts numbers and strings like '$1,242.50', '30',
    '(30.00)' (negative). Anything with stray letters -> None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    s = s.replace("$", "").replace("€", "").replace("£", "")
    s = s.replace(",", "").replace(" ", "")
    if not _MONEY_RE.match(s):
        return None
    try:
        out = float(s)
    except ValueError:
        return None
    return -out if negative else out


def _recover_dict(value):
    """A stored line item may be a repr'd dict string (file_intake
    stringifies list items). Recover literals only."""
    if isinstance(value, str) and value[:1] == "{":
        try:
            parsed = ast.literal_eval(value)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            return value
        if isinstance(parsed, dict):
            return parsed
    return value


def line_item_amount(item) -> float | None:
    """Best-effort line amount from one extracted line item: a dict
    (total/amount key, or qty x price), a repr'd dict string, or a
    plain string like 'Hose x2 @ 86.50 = 173.00'. None when the
    amount cannot be read confidently."""
    item = _recover_dict(item)
    if isinstance(item, (int, float)) and not isinstance(item, bool):
        return float(item)
    if isinstance(item, dict):
        for key in ("total", "amount", "line_total", "extended",
                    "value"):
            if key in item:
                amt = parse_money(item[key])
                if amt is not None:
                    return amt
        qty = price = None
        for key in ("qty", "quantity", "count"):
            if key in item:
                qty = parse_money(item[key])
                break
        for key in ("price", "unit_price", "rate", "each"):
            if key in item:
                price = parse_money(item[key])
                break
        if qty is not None and price is not None:
            return qty * price
        return None
    if isinstance(item, str):
        m = _EQ_TAIL_RE.search(item)
        if m:
            return parse_money(m.group(1))
        m = _QTY_PRICE_RE.search(item)
        if m:
            qty = parse_money(m.group(1))
            price = parse_money(m.group(2))
            if qty is not None and price is not None:
                return qty * price
    return None


def _field(fields: dict, role: str):
    """(present, value) for a money role via its aliases."""
    for key in _ALIASES[role]:
        if key in fields:
            return True, fields[key]
    return False, None


def _fmt(v: float) -> str:
    return f"${v:,.2f}"


def verify_invoice_totals(fields: dict) -> dict:
    """Deterministic totals verdict for one invoice extraction.

    Returns {status: ok|mismatch|unverifiable, detail, checks:[...]}
    where each check is {name, status: ok|mismatch|skipped, detail}.
    Conservative: unparseable inputs skip their check; only genuine
    arithmetic failures flag."""
    fields = fields or {}
    checks: list[dict] = []
    amount_paid_unreliable = False

    _, subtotal_v = _field(fields, "subtotal")
    _, total_v = _field(fields, "total")
    subtotal = parse_money(subtotal_v)
    total = parse_money(total_v)

    # 1. Line items vs subtotal.
    items = fields.get("line_items")
    if isinstance(items, list) and items and subtotal is not None:
        amounts = [line_item_amount(i) for i in items]
        if all(a is not None for a in amounts):
            items_sum = round(sum(amounts), 2)
            if abs(items_sum - subtotal) > TOL:
                checks.append({
                    "name": "line_items_vs_subtotal",
                    "status": "mismatch",
                    "detail": (f"Line items sum to {_fmt(items_sum)} "
                               f"but the subtotal shown is "
                               f"{_fmt(subtotal)} (difference "
                               f"{_fmt(abs(items_sum - subtotal))}).")})
            else:
                checks.append({
                    "name": "line_items_vs_subtotal",
                    "status": "ok",
                    "detail": (f"Line items sum to {_fmt(items_sum)}, "
                               f"matching the subtotal.")})
        else:
            checks.append({
                "name": "line_items_vs_subtotal", "status": "skipped",
                "detail": "Some line-item amounts could not be read."})

    # 2. Subtotal + components vs total due.
    if subtotal is not None and total is not None:
        components = {}
        bad_component = False
        for role in ("tax", "discount", "shipping"):
            present, raw = _field(fields, role)
            if not present:
                components[role] = 0.0
            else:
                val = parse_money(raw)
                if val is None:
                    bad_component = True
                    break
                components[role] = val
        if bad_component:
            checks.append({
                "name": "subtotal_vs_total", "status": "skipped",
                "detail": ("A tax/discount/shipping amount could "
                           "not be read.")})
        else:
            expected = round(subtotal + components["tax"]
                             - components["discount"]
                             + components["shipping"], 2)
            if abs(total - expected) > TOL:
                named = ", ".join(
                    f"{r} {_fmt(components[r])}"
                    for r in ("tax", "discount", "shipping")
                    if _field(fields, r)[0])
                tail = (f" (extracted: {named})" if named else
                        " (no tax, discount, or shipping amounts "
                        "were extracted)")
                checks.append({
                    "name": "subtotal_vs_total", "status": "mismatch",
                    "detail": (f"Total due {_fmt(total)} does not "
                               f"match subtotal {_fmt(subtotal)} "
                               f"(difference "
                               f"{_fmt(abs(total - expected))}){tail}.")})
            else:
                checks.append({
                    "name": "subtotal_vs_total", "status": "ok",
                    "detail": (f"Total due {_fmt(total)} is consistent "
                               f"with the subtotal.")})

    # 3. Balance arithmetic.
    paid_present, paid_v = _field(fields, "paid")
    bal_present, bal_v = _field(fields, "balance")
    paid = parse_money(paid_v) if paid_present else None
    balance = parse_money(bal_v) if bal_present else None
    if total is not None and paid is not None and balance is not None:
        if abs(paid - total) <= TOL and abs(balance - total) <= TOL:
            # Known mis-extraction signature: the model copied the
            # total into amount_paid on an invoice whose balance due
            # is the full total. Treat amount_paid as unreliable;
            # never flag this pattern (intake #59 false alarm).
            amount_paid_unreliable = True
            checks.append({
                "name": "balance_arithmetic", "status": "skipped",
                "detail": ("Amount paid equals the total while the "
                           "balance due also equals the total — "
                           "amount_paid treated as not shown; "
                           "balance arithmetic not checked.")})
        elif paid > total + TOL:
            checks.append({
                "name": "balance_arithmetic", "status": "skipped",
                "detail": ("Amount paid exceeds the total "
                           "(overpayment or credit) — balance "
                           "arithmetic not checked.")})
        else:
            expected_balance = round(total - paid, 2)
            if abs(balance - expected_balance) > TOL:
                checks.append({
                    "name": "balance_arithmetic",
                    "status": "mismatch",
                    "detail": (f"Balance due shown {_fmt(balance)}, "
                               f"but total {_fmt(total)} - amount "
                               f"paid {_fmt(paid)} = "
                               f"{_fmt(expected_balance)}.")})
            else:
                checks.append({
                    "name": "balance_arithmetic", "status": "ok",
                    "detail": (f"Balance due {_fmt(balance)} = total "
                               f"{_fmt(total)} - amount paid "
                               f"{_fmt(paid)}.")})

    mismatches = [c for c in checks if c["status"] == "mismatch"]
    ran = [c for c in checks if c["status"] == "ok"]
    if mismatches:
        status = "mismatch"
        detail = " ".join(c["detail"] for c in mismatches)
    elif ran:
        status = "ok"
        detail = "Totals verified: " + " ".join(
            c["detail"] for c in ran)
    else:
        status = "unverifiable"
        detail = ("Totals could not be verified from the extracted "
                  "fields (missing or unparseable amounts).")
    verdict = {"status": status, "detail": detail, "checks": checks}
    if amount_paid_unreliable:
        verdict["amount_paid_unreliable"] = True
    return verdict
