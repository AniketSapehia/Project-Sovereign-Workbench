"""
app/workflow.py — the FLAGSHIP workflow (Phase 6):
    scanned inspection report  ->  verified findings  ->  SOP checks  ->  approval note (.docx)

This is the end-to-end task named in the problem statement: "reading a
scanned inspection report, pulling out key findings and drafting an
approval note as a Word file."

WHY A GUIDED WORKFLOW INSTEAD OF THE FREE AGENT?
Testing in Phase 3 showed that a small model working freely invents values,
mixes up limits and sometimes claims files it never made. So this workflow
follows one rule:

        AI for LANGUAGE  —  CODE for NUMBERS and RULES

    step                                   done by
    1. read the scanned report             OCR (Phase 2)
    2. extract findings into a fixed form  AI, with Ollama "structured output"
    3. verify every number                 code: is it really in the report?
    4. read the limits from the SOPs       AI fills a form; code verifies every
                                           number appears in the SOP (cached)
    5. classify zones, temperatures,       code
       choose the approving authority
    6. write the narrative paragraphs      AI; code checks it only uses
                                           verified numbers
    7. assemble the Word file              code, in the SOP-ADM-002 format,
                                           with a Verification table

Each step is reported to the page like an agent step, so the user can see
exactly what happened, and what could NOT be verified.

Scope (be honest about it): this workflow is built for condition-monitoring
inspection reports like IR-2026-118. Other documents go through the agent.
"""

import hashlib
import json
import re
from datetime import datetime

import config
from app import documents, knowledge, llm, tools

# Phrases that start the workflow when a report is attached.
_TRIGGER = re.compile(r"\bapproval\s+note\b", re.IGNORECASE)


def should_run(question: str, attachment_ids: list) -> bool:
    """Run the workflow when a document is attached and an approval note is requested."""
    return bool(attachment_ids) and bool(_TRIGGER.search(question))


# ===========================================================================
# Forms (JSON Schemas) the model must fill in
# ===========================================================================
# "anyOf number/null" lets the model say "unreadable" (null) instead of guessing.
_NUM = {"anyOf": [{"type": "number"}, {"type": "null"}]}

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "report_no": {"type": "string"},
        "date": {"type": "string"},
        "equipment_tag": {"type": "string"},
        "equipment_description": {"type": "string"},
        "vibration": {"type": "array", "items": {"type": "object", "properties": {
            "location": {"type": "string"}, "horizontal": _NUM, "vertical": _NUM, "axial": _NUM},
            "required": ["location", "horizontal", "vertical", "axial"]}},
        "temperatures": {"type": "array", "items": {"type": "object", "properties": {
            "location": {"type": "string"}, "previous_c": _NUM, "current_c": _NUM},
            "required": ["location", "previous_c", "current_c"]}},
        "observations": {"type": "array", "items": {"type": "string"}},
        "recommendations": {"type": "array", "items": {"type": "string"}},
        "costs": {"type": "array", "items": {"type": "object", "properties": {
            "item": {"type": "string"}, "amount_rs": _NUM},
            "required": ["item", "amount_rs"]}},
        "total_cost_rs": _NUM,
    },
    "required": ["report_no", "date", "equipment_tag", "equipment_description", "vibration",
                 "temperatures", "observations", "recommendations", "costs", "total_cost_rs"],
}

ZONES_SCHEMA = {
    "type": "object",
    "properties": {"zones": {"type": "array", "items": {"type": "object", "properties": {
        "zone": {"type": "string"},
        "upper_limit_mm_s": _NUM,          # null for the highest zone ("above ...")
        "action": {"type": "string"}},
        "required": ["zone", "upper_limit_mm_s", "action"]}}},
    "required": ["zones"],
}

TEMPERATURE_SCHEMA = {
    "type": "object",
    "properties": {"normal_max_c": _NUM, "alarm_max_c": _NUM, "rise_investigate_c": _NUM,
                   "alarm_action": {"type": "string"}, "trip_action": {"type": "string"}},
    "required": ["normal_max_c", "alarm_max_c", "rise_investigate_c", "alarm_action", "trip_action"],
}

DELEGATION_SCHEMA = {
    "type": "object",
    "properties": {"levels": {"type": "array", "items": {"type": "object", "properties": {
        "up_to_rs": _NUM,                  # null for the highest level ("above ...")
        "authority": {"type": "string"}},
        "required": ["up_to_rs", "authority"]}}},
    "required": ["levels"],
}

NARRATIVE_SCHEMA = {
    "type": "object",
    "properties": {"subject": {"type": "string"}, "background": {"type": "string"},
                   "proposal": {"type": "string"}},
    "required": ["subject", "background", "proposal"],
}

EXTRACT_PROMPT = (
    "You copy information from an OCR-read inspection report into a form. Rules: copy numbers "
    "EXACTLY as written; amounts in rupees as plain numbers (1,25,000 -> 125000); if a value is "
    "marked [unclear: ...], missing or unreadable, use null — NEVER guess; 'location' is the row "
    "label in the FIRST column of a table (e.g. 'DE bearing', 'NDE bearing'), never a column "
    "heading such as 'Horizontal'; keep observation and recommendation sentences short and "
    "close to the original wording."
)


# ===========================================================================
# Number checking — the heart of "code for numbers"
# ===========================================================================
def format_inr(amount: float) -> str:
    """Indian digit grouping: 125000 -> '1,25,000', 500000 -> '5,00,000'."""
    whole = str(int(round(amount)))
    if len(whole) <= 3:
        return whole
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups + [tail])


def _spellings(value: float) -> set:
    """All the ways a number may be written: 6.4, 65000, 65,000 (both groupings)."""
    out = {repr(value), str(value)}
    if float(value).is_integer():
        n = int(value)
        out |= {str(n), format_inr(n), f"{n:,}"}
    else:
        out |= {f"{value:.1f}", f"{value:.2f}".rstrip("0")}
    return {s for s in out if s}


def number_in(value, text: str) -> bool:
    """Is this number written in the text, as a whole number (not part of a bigger one)?"""
    if value is None:
        return False
    for spelling in _spellings(value):
        if re.search(r"(?<![\d.,])" + re.escape(spelling) + r"(?![\d]|[.,]\d)", text):
            return True
    return False


def _lines_about(location: str, text: str) -> list:
    """OCR lines that mention a location, e.g. 'DE bearing' (but NOT 'NDE bearing')."""
    pattern = re.compile(r"\b" + re.escape(location.strip()) + r"\b", re.IGNORECASE)
    return [line for line in text.splitlines() if pattern.search(line)]


def repair_location(row: dict, fields: tuple, text: str):
    """
    Phase 6.1: fix a wrong row label using the row's own numbers.

    In the first real test, the model labelled the "DE bearing" row
    "Horizontal" (a column heading). Its correct numbers were then looked
    for on a line about "Horizontal", not found, and wrongly flagged.
    If NO line about the given label contains any of the row's numbers, we
    find the line that contains most of them, and take the label from the
    start of that line (the text before the first number).
    Returns the corrected label, or None if no correction is needed/possible.
    """
    values = [row[f] for f in fields if row.get(f) is not None]
    if not values:
        return None
    needed = min(2, len(values))       # a real row line holds most of the row's numbers
    if any(sum(1 for v in values if number_in(v, line)) >= needed
           for line in _lines_about(row["location"], text)):
        return None                    # the label is fine (one stray mention isn't enough:
                                       # "6.4 mm/s at DE horizontal" fooled the first version)
    best_line, best_count = None, 0
    for line in text.splitlines():
        count = sum(1 for v in values if number_in(v, line))
        if count > best_count:
            best_line, best_count = line, count
    if best_line is None or best_count < needed:
        return None                                   # not confident enough to correct
    label = re.match(r"^(.*?)(?=\s+(?:\[unclear|\d))", best_line.strip())
    return label.group(1).strip() if label and label.group(1).strip() else None


# Phase 6.13: the evaluation reads these after a run, to measure how often
# the extracted values were right, and how often the verification caught the
# wrong ones. (A plain module variable: the page never uses it.)
LAST_RUN = {"checks": [], "narrative_source": ""}


def check_value(value, location: str, text: str) -> str:
    """
    'verified'  - the number appears on a line about this location
    'unclear'   - the model said null (unreadable / marked [unclear])
    'not found' - the model gave a number that is NOT in the source: it may
                  be invented, so it must be checked by a person
    """
    if value is None:
        return "unclear"
    lines = _lines_about(location, text) if location else [text]
    return "verified" if any(number_in(value, line) for line in lines) else "not found"


# ===========================================================================
# SOP rules: extracted by AI, verified by code, cached
# ===========================================================================
async def _sop_rules(sop_id: str, kind: str, schema: dict, instruction: str) -> dict:
    """
    Read one set of rules (e.g. vibration zones) from an SOP, as a form.
    Every number in the result must appear in the SOP text, otherwise the
    rules are rejected. Results are cached in data/rules_cache.json and
    re-read automatically if the SOP file changes (its fingerprint changes).
    """
    paths = knowledge.documents_named_in(sop_id)
    if not paths:
        raise RuntimeError(f"{sop_id} is not in the knowledge base")
    path = paths[0]
    sop_text = "\n".join(p["text"] for p in documents.extract(path))
    fingerprint = hashlib.sha256(sop_text.encode("utf-8")).hexdigest()[:16]

    cache_file = config.DATA_DIR / "rules_cache.json"
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    key = f"{path.name}::{kind}"
    if cache.get(key, {}).get("fingerprint") == fingerprint:
        return {**cache[key]["rules"], "_source": path.name, "_cached": True, "_text": sop_text}

    rules = await llm.chat_json(
        [{"role": "user", "content": f"{instruction}\n\n=== {path.name} ===\n{sop_text}"}],
        schema, system_prompt="You copy rules from a procedure document into a form. "
                              "Copy numbers exactly; amounts in rupees as plain numbers.")

    # Verify: every number in the extracted rules must be written in the SOP.
    numbers = []
    def collect(node):
        if isinstance(node, dict):
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            numbers.append(node)
    collect(rules)
    missing = [n for n in numbers if not number_in(n, sop_text)]
    if missing:
        raise RuntimeError(f"rules read from {path.name} contain numbers not found in the SOP: "
                           f"{missing} — not used")

    cache[key] = {"fingerprint": fingerprint, "rules": rules}
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    return {**rules, "_source": path.name, "_cached": False, "_text": sop_text}


def classify_zone(reading: float, zones: list):
    """The first zone whose upper limit is >= the reading (limits are 'up to and including')."""
    ordered = sorted(zones, key=lambda z: float("inf") if z["upper_limit_mm_s"] is None
                     else z["upper_limit_mm_s"])
    for zone in ordered:
        if zone["upper_limit_mm_s"] is None or reading <= zone["upper_limit_mm_s"]:
            return zone
    return ordered[-1]


def approving_authority(amount: float, levels: list) -> str:
    """The lowest authority whose limit covers the amount."""
    ordered = sorted(levels, key=lambda l: float("inf") if l["up_to_rs"] is None else l["up_to_rs"])
    for level in ordered:
        if level["up_to_rs"] is None or amount <= level["up_to_rs"]:
            return level["authority"]
    return ordered[-1]["authority"]


# ===========================================================================
# Events (shown on the page like agent steps)
# ===========================================================================
def _start(step_id, name, args=None):
    return {"type": "step_start", "id": step_id, "tool": name, "arguments": args or {}}


def _end(step_id, ok, summary, output=None):
    return {"type": "step_end", "id": step_id, "ok": ok, "summary": summary,
            "display": {"output": output} if output is not None else None}


def _fmt(value, unit=""):
    if value is None:
        return "—"
    return (f"{value:g}" if isinstance(value, float) else str(value)) + unit


# ===========================================================================
# The workflow
# ===========================================================================
async def run(messages: list, attachment_ids: list):
    """Run the flagship workflow. Yields page events (status, step_*, sources, token, file, done)."""
    ctx = tools.ToolContext(attachment_ids=list(attachment_ids))
    warnings = []

    # ---------------- 1. Read the report (already OCR'd at upload) ----------------
    yield _start("w1", "read_report")
    report = None
    for doc_id in attachment_ids:
        saved = knowledge.attachment_document(doc_id)
        if saved["pages"]:
            report = saved
            break
    if not report:
        yield _end("w1", False, "no readable document attached")
        yield {"type": "token", "text": "Attach the scanned inspection report first (📎)."}
        yield {"type": "done", "files": []}
        return
    name = report["summary"]["filename"]
    text = "\n".join(p["text"] for p in report["pages"])
    ctx.add_sources([{"text": p["text"], "source": name, "page": p["page"], "section": "",
                      "method": p["method"], "score": None} for p in report["pages"]])
    yield _end("w1", True, f"{name}: {len(report['pages'])} page(s), "
               f"{report['summary'].get('uncertain_words', 0)} hard-to-read word(s)")

    # ---------------- 2. Extract findings into the form (AI) ----------------
    yield {"type": "status", "text": "Extracting findings (the model fills in a fixed form)…"}
    yield _start("w2", "extract_findings")
    try:
        f = await llm.chat_json([{"role": "user", "content":
                                  f"Fill in the form from this report.\n\n=== {name} ===\n{text}"}],
                                REPORT_SCHEMA, system_prompt=EXTRACT_PROMPT)
    except Exception as error:
        yield _end("w2", False, f"extraction failed: {error}")
        yield {"type": "token", "text": "The findings could not be extracted. Try again, or use the agent."}
        yield {"type": "done", "files": []}
        return
    yield _end("w2", True, f"{len(f['vibration'])} vibration row(s), {len(f['temperatures'])} "
               f"temperature row(s), {len(f['costs'])} cost item(s)",
               json.dumps(f, indent=1, ensure_ascii=False))

    # ---------------- 3. Verify every number against the OCR text (code) ----------------
    yield _start("w3", "verify_values")
    checks = []          # rows for the Verification table: (what, value, status)
    record = []          # Phase 6.13: the same, with raw values, for the evaluation
    corrections = []

    # Phase 6.2: the model sometimes lists "Total" as a cost ITEM. Then the
    # table shows two Total rows and the items "add up" to double. Remove it
    # (keeping its amount as the total if no total was given).
    for item in list(f["costs"]):
        if re.fullmatch(r"\s*(grand\s+)?total(\s+cost)?\s*:?\s*", item["item"] or "", re.IGNORECASE):
            f["costs"].remove(item)
            if f["total_cost_rs"] is None:
                f["total_cost_rs"] = item["amount_rs"]
            corrections.append(f"'{item['item']}' removed from the cost items (it is the total, not an item)")
    for rows, fields in ((f["vibration"], ("horizontal", "vertical", "axial")),
                         (f["temperatures"], ("previous_c", "current_c"))):
        for row in rows:
            fixed = repair_location(row, fields, text)
            if fixed and fixed.lower() != row["location"].lower():
                corrections.append(f"Row label '{row['location']}' corrected to '{fixed}' (matched by its values)")
                row["location"] = fixed
    for row in f["vibration"]:
        for direction in ("horizontal", "vertical", "axial"):
            status = check_value(row[direction], row["location"], text)
            row[f"{direction}_status"] = status
            checks.append((f"Vibration {row['location']} {direction}", _fmt(row[direction], " mm/s"), status))
            record.append({"what": checks[-1][0], "location": row["location"], "value": row[direction], "status": status})
    for row in f["temperatures"]:
        for field in ("previous_c", "current_c"):
            status = check_value(row[field], row["location"], text)
            row[f"{field}_status"] = status
            checks.append((f"Temperature {row['location']} {field.split('_')[0]}", _fmt(row[field], " °C"), status))
            record.append({"what": checks[-1][0], "location": row["location"], "value": row[field], "status": status})
    for item in f["costs"]:
        item["status"] = check_value(item["amount_rs"], "", text)
        checks.append((f"Cost: {item['item']}", format_inr(item["amount_rs"]) if item["amount_rs"] else "—",
                       item["status"]))
        record.append({"what": checks[-1][0], "location": "", "value": item["amount_rs"], "status": item["status"]})
    f["total_status"] = check_value(f["total_cost_rs"], "", text)
    # Consistency: do the items add up to the stated total?
    amounts = [c["amount_rs"] for c in f["costs"] if c["amount_rs"] is not None]
    if f["total_cost_rs"] and amounts and abs(sum(amounts) - f["total_cost_rs"]) > 1:
        warnings.append(f"Cost items add up to Rs {format_inr(sum(amounts))}, but the stated total is "
                        f"Rs {format_inr(f['total_cost_rs'])}.")
    checks.append(("Total cost", format_inr(f["total_cost_rs"]) if f["total_cost_rs"] else "—", f["total_status"]))
    record.append({"what": "Total cost", "location": "", "value": f["total_cost_rs"], "status": f["total_status"]})
    LAST_RUN["checks"] = record

    counts = {s: sum(1 for c in checks if c[2] == s) for s in ("verified", "unclear", "not found")}
    problems = [f"{what}: {value} ({status})" for what, value, status in checks if status != "verified"]
    yield _end("w3", counts["not found"] == 0,
               f"{counts['verified']} verified, {counts['unclear']} unclear, {counts['not found']} NOT in source",
               "\n".join([f"🔧 {c}" for c in corrections] +
                         [f"{'✓' if s == 'verified' else '⚠'} {w}: {v} — {s}" for w, v, s in checks]))
    if problems:
        warnings.append("These values must be checked on the original report: " + "; ".join(problems) + ".")

    # ---------------- 4. Read the limits from the SOPs (AI + code check, cached) ----------------
    yield {"type": "status", "text": "Reading limits from the SOPs…"}
    yield _start("w4", "read_sop_rules")
    sops = config.WORKFLOW_SOPS
    rules, rule_notes = {}, []
    for kind, schema, sop_id, instruction in [
        ("zones", ZONES_SCHEMA, sops["condition_monitoring"],
         "From the section on vibration severity zones, list every zone with its UPPER limit in "
         "mm/s (null for the highest zone) and its required action."),
        ("temperature", TEMPERATURE_SCHEMA, sops["condition_monitoring"],
         "From the section on bearing temperature limits: the normal maximum, the alarm maximum "
         "(above it = trip/stop), the temperature rise that must be investigated, and the actions."),
        ("delegation", DELEGATION_SCHEMA, sops["approvals"],
         "From the delegation of financial powers, list each level's upper amount in rupees "
         "(null for the highest level) and its approving authority."),
    ]:
        try:
            rules[kind] = await _sop_rules(sop_id, kind, schema, instruction)
            rule_notes.append(f"✓ {kind} from {rules[kind]['_source']}"
                              + (" (cached)" if rules[kind]["_cached"] else " (read now, numbers verified)"))
        except Exception as error:
            rule_notes.append(f"✗ {kind}: {error}")
            warnings.append(f"Could not use the {kind} rules: {error}")
    for sop_id in {sops["condition_monitoring"], sops["approvals"]}:
        for path in knowledge.documents_named_in(sop_id):
            text_ = next((r["_text"] for r in rules.values() if r["_source"] == path.name), None)
            if text_:
                ctx.add_sources([{"text": text_, "source": path.name, "page": 1,
                                  "section": "(whole document)", "method": "text", "score": None}])
    yield _end("w4", len(rules) == 3, f"{len(rules)} of 3 rule sets ready", "\n".join(rule_notes))
    yield {"type": "sources", "sources": [
        {"n": i + 1, "source": s["source"], "page": s["page"], "section": s.get("section", ""),
         "score": None, "method": s.get("method"), "text": s["text"]} for i, s in enumerate(ctx.sources)]}
    cite = {}                                                          # file -> FIRST citation number
    for i, s_ in enumerate(ctx.sources):
        cite.setdefault(s_["source"], i + 1)
    report_ref = f"[{cite.get(name, 1)}]"
    cm_ref = next((f"[{n}]" for src, n in cite.items() if src.startswith(sops["condition_monitoring"])), "")
    adm_ref = next((f"[{n}]" for src, n in cite.items() if src.startswith(sops["approvals"])), "")

    # ---------------- 5. Apply the rules (code) ----------------
    yield _start("w5", "apply_rules")
    facts, notes = {}, []
    verified_readings = [(row["location"], d, row[d]) for row in f["vibration"]
                         for d in ("horizontal", "vertical", "axial") if row.get(f"{d}_status") == "verified"]
    if "zones" in rules and verified_readings:
        location, direction, value = max(verified_readings, key=lambda r: r[2])
        zone = classify_zone(value, rules["zones"]["zones"])
        facts.update(highest=value, highest_at=f"{location} {direction}", zone=zone["zone"],
                     zone_action=zone["action"])
        for row in f["vibration"]:
            row["zones"] = {d: classify_zone(row[d], rules["zones"]["zones"])["zone"]
                            if row.get(f"{d}_status") == "verified" else "check"
                            for d in ("horizontal", "vertical", "axial")}
        notes.append(f"Highest verified vibration {value:g} mm/s ({location} {direction}) -> {zone['zone']}")
    if "temperature" in rules:
        t = rules["temperature"]
        for row in f["temperatures"]:
            current, previous = row["current_c"], row["previous_c"]
            if row.get("current_c_status") != "verified" or current is None:
                row["assessment"] = "check original"
                continue
            if t["alarm_max_c"] is not None and current > t["alarm_max_c"]:
                row["status_word"] = "TRIP"
                row["assessment"] = f"TRIP (above {t['alarm_max_c']:g} °C)"
            elif t["normal_max_c"] is not None and current > t["normal_max_c"]:
                row["status_word"] = "ALARM"
                row["assessment"] = f"ALARM (above {t['normal_max_c']:g} °C)"
            else:
                row["status_word"] = "normal"
                row["assessment"] = "normal"
            if (previous is not None and row.get("previous_c_status") == "verified"
                    and t["rise_investigate_c"] is not None and current - previous > t["rise_investigate_c"]):
                row["rise"] = current - previous
                row["assessment"] += f"; rise of {current - previous:g} °C (> {t['rise_investigate_c']:g}) to investigate"
            notes.append(f"Temperature {row['location']}: {current:g} °C -> {row['assessment']}")
    total = f["total_cost_rs"] if f["total_status"] == "verified" else None
    if total and "delegation" in rules:
        facts.update(total=total, approver=approving_authority(total, rules["delegation"]["levels"]))
        notes.append(f"Total Rs {format_inr(total)} -> approved by {facts['approver']}")
    elif not total:
        warnings.append("The total cost could not be verified, so no approving authority was chosen.")
    yield _end("w5", bool(facts), f"{len(notes)} rule result(s)", "\n".join(notes))

    # ---------------- 6. Narrative text (AI), checked by code ----------------
    yield {"type": "status", "text": "Writing the narrative…"}
    yield _start("w6", "draft_text")
    allowed_facts = {
        "equipment": f"{f['equipment_tag']} ({f['equipment_description']})",
        "report": f"{f['report_no']} dated {f['date']}",
        "highest_vibration_mm_s": facts.get("highest"), "measured_at": facts.get("highest_at"),
        "zone": facts.get("zone"), "zone_required_action": facts.get("zone_action"),
        # Plain facts only (no rule thresholds), so that any number the AI
        # invents can be detected by the check below.
        "bearing_temperatures": [f"{r['location']}: {_fmt(r['current_c'], ' °C')}"
                                 + (f", {r['status_word']}" if r.get("status_word") else "")
                                 + (f", rose by {r['rise']:g} °C, to be investigated" if r.get("rise") else "")
                                 for r in f["temperatures"]],
        "recommendations": f["recommendations"],
        "total_cost_rs": format_inr(total) if total else None,
        "approving_authority": facts.get("approver"),
    }
    # Every number in the AI text must be one of the facts it was given.
    # (Only the facts, not the whole report: in testing, "within 15 days",
    # "104 °C" and "Rs 12,500" were invented by the model and must be caught.)
    allowed_text = json.dumps(allowed_facts, ensure_ascii=False)

    def stray_numbers(narr: dict) -> list:
        found = set()
        for part in narr.values():
            for n in re.findall(r"\d[\d,]*(?:\.\d+)?", part):
                n = n.rstrip(",")
                if not number_in(float(n.replace(",", "")), allowed_text):
                    found.add(n)
        return sorted(found)

    def template() -> dict:
        """Plain text built ONLY from verified facts (used if the AI text can't be trusted)."""
        background = f"Inspection report {f['report_no']} dated {f['date']} on {allowed_facts['equipment']}"
        if facts.get("highest") is not None:
            background += (f" recorded a highest vibration of {facts['highest']:g} mm/s "
                           f"({facts['highest_at']}), which is in {facts['zone']} as per {sops['condition_monitoring']}")
        background += "."
        alarms = [t for t in allowed_facts["bearing_temperatures"] if "ALARM" in t or "TRIP" in t]
        if alarms:
            background += " Bearing temperature: " + "; ".join(alarms) + "."
        proposal = "It is proposed to carry out the recommendations of the inspection report listed below"
        if total:
            proposal += f", at an estimated cost of Rs {format_inr(total)}"
        return {"subject": f"Corrective maintenance of {allowed_facts['equipment']}",
                "background": background, "proposal": proposal + "."}

    request = [{"role": "user", "content":
        "Write a short Subject line, a Background paragraph (2-3 sentences) and a Proposal "
        "paragraph (2-3 sentences) for an internal approval note. Use ONLY these verified "
        "facts and no other numbers. Do not calculate anything.\n"
        + json.dumps(allowed_facts, indent=1, ensure_ascii=False)}]
    system = "You write brief, factual, third-person approval notes for an Indian refinery."
    try:
        narrative = await llm.chat_json(request, NARRATIVE_SCHEMA, system_prompt=system)
        stray, source = stray_numbers(narrative), "AI"
        if stray:
            # Phase 6.1: one second chance, telling the model exactly what was wrong.
            first_stray = stray
            request += [{"role": "assistant", "content": json.dumps(narrative, ensure_ascii=False)},
                        {"role": "user", "content": "Your text contains numbers that are NOT in the facts: "
                         + ", ".join(stray) + ". Rewrite it using only numbers that appear in the facts."}]
            narrative = await llm.chat_json(request, NARRATIVE_SCHEMA, system_prompt=system)
            stray, source = stray_numbers(narrative), f"AI (rewritten: first draft had {', '.join(first_stray)})"
        if stray:
            # Still wrong: NEVER put unverified numbers into the note. Use the template.
            warnings.append("The AI-written paragraphs contained numbers not in the verified facts ("
                            + ", ".join(stray) + "), so plain template text was used instead.")
            narrative, source = template(), f"template (AI text had unverified numbers: {', '.join(stray)})"
    except Exception as error:
        narrative, source = template(), f"template (AI failed: {error})"
    LAST_RUN["narrative_source"] = source
    yield _end("w6", source == "AI" or source.startswith("AI (rewritten"), f"written by {source}",
               json.dumps(narrative, indent=1, ensure_ascii=False))

    # ---------------- 7. Assemble the Word file (code) ----------------
    yield _start("w7", "create_word_document")
    tag = f["equipment_tag"] or "equipment"
    body = [f"## Subject", narrative["subject"],
            "## Reference",
            f"- Inspection Report {f['report_no']} dated {f['date']} {report_ref}",
            f"- {sops['condition_monitoring']} (condition monitoring limits) {cm_ref}",
            f"- {sops['approvals']} (approval note format, delegation of powers) {adm_ref}",
            "## Background", narrative["background"],
            "## Findings", "**Vibration (mm/s RMS)**",
            "| Location | Horizontal | Vertical | Axial |", "|---|---|---|---|"]
    for row in f["vibration"]:
        cells = []
        for d in ("horizontal", "vertical", "axial"):
            value, status = row[d], row.get(f"{d}_status")
            zone = row.get("zones", {}).get(d, "")
            cell = f"{_fmt(value)} ({zone})" if status == "verified" and zone else _fmt(value)
            cells.append(cell if status == "verified" else f"{cell} ⚠ check")
        body.append(f"| {row['location']} | " + " | ".join(cells) + " |")
    if facts.get("zone"):
        body.append(f"- Highest verified reading **{facts['highest']:g} mm/s** ({facts['highest_at']}) "
                    f"is in **{facts['zone']}** {cm_ref}. Required action: {facts['zone_action']}")
    body += ["**Bearing temperature**", "| Location | Previous | Current | Assessment |", "|---|---|---|---|"]
    for row in f["temperatures"]:
        body.append(f"| {row['location']} | {_fmt(row['previous_c'], ' °C')} | {_fmt(row['current_c'], ' °C')} "
                    f"| {row.get('assessment', '')} |")
    body += ["**Observations**"] + [f"- {o}" for o in f["observations"]]
    body += ["## Proposal", narrative["proposal"]] + [f"{i}. {r}" for i, r in enumerate(f["recommendations"], 1)]
    body += ["## Financial implication", "| Item | Amount (Rs) |", "|---|---|"]
    body += [f"| {c['item']} | {format_inr(c['amount_rs']) if c['amount_rs'] else '—'}"
             f"{'' if c['status'] == 'verified' else ' ⚠ check'} |" for c in f["costs"]]
    body.append(f"| **Total** | **{format_inr(f['total_cost_rs']) if f['total_cost_rs'] else '—'}** |")
    body += ["## Approval sought"]
    if facts.get("approver") and facts.get("total"):
        body.append(f"Approval of the **{facts['approver']}** is requested for the above proposal at an "
                    f"estimated cost of **Rs {format_inr(facts['total'])}**, which is within the "
                    f"powers delegated under {sops['approvals']} {adm_ref}.")
    else:
        body.append("⚠ The approving authority could not be determined automatically; see Verification.")
    body += ["## Verification (generated by code)",
             "| Value | As extracted | Status |", "|---|---|---|"]
    body += [f"| {w} | {v} | {'✓ verified in source' if s == 'verified' else '⚠ ' + s} |"
             for w, v, s in checks]
    body += [f"- {n}" for n in rule_notes]
    if warnings:
        body += ["**Points to check before signing**"] + [f"- {w}" for w in warnings]

    path = tools.safe_output_path(f"Approval_Note_{tag}.docx", {".docx"}, ".docx")
    title = re.sub(r"^\s*approval\s+note\s*(?:for|:|-|–)?\s*", "", narrative["subject"], flags=re.IGNORECASE)
    tools.build_docx(path, f"Approval Note: {title or narrative['subject']}", "\n".join(body), ctx)
    ctx.files_created.append(path.name)
    yield _end("w7", True, f"created {path.name}")
    yield {"type": "file", "name": path.name}

    # ---------------- Summary in the chat ----------------
    summary = [f"Approval note drafted: {path.name}", ""]
    if facts.get("zone"):
        summary.append(f"- Highest verified vibration: {facts['highest']:g} mm/s ({facts['highest_at']}) "
                       f"→ {facts['zone']} {cm_ref}")
    for row in f["temperatures"]:
        if row.get("assessment"):
            summary.append(f"- {row['location']} temperature: {_fmt(row['current_c'], ' °C')} → {row['assessment']}")
    if facts.get("approver"):
        summary.append(f"- Total Rs {format_inr(facts['total'])} → approving authority: {facts['approver']} {adm_ref}")
    summary.append(f"- Values: {counts['verified']} verified in the report, {counts['unclear']} unclear, "
                   f"{counts['not found']} not found in the source")
    if warnings:
        summary += ["", "⚠ Check before signing:"] + [f"- {w}" for w in warnings]
    yield {"type": "token", "text": "\n".join(summary)}
    yield {"type": "done", "files": ctx.files_created}
