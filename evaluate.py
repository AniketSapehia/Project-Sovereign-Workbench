"""
evaluate.py — measures how well the Sovereign Workbench works (Phase 6).

Run it from the project folder (with .venv activated and Ollama running):

    python evaluate.py              quick tests (a few minutes)
    python evaluate.py --workflow   + the flagship report -> approval note workflow
    python evaluate.py --agent      + 4 agent tasks (slow on a CPU: ~2-4 min each)
    python evaluate.py --all        everything

It writes a report to results/eval_<date>_<time>.md (plus a .json copy),
ready to paste into your project report. Run it again after changing a
model in config.py, and compare the two reports.

WHAT IS MEASURED, AND HOW
  1. Model router         labelled test tasks (DIFFERENT from the router's own
                          example tasks) -> confusion matrix, accuracy,
                          precision / recall / F1 per model.
  2. Missing-attachment   labelled questions -> 2x2 confusion matrix.
     check
  3. OCR                  OCR output of the sample scans vs. the exact text
                          printed on them (samples/ground_truth/) ->
                          character error rate (CER), word accuracy, and
                          how many key values were read correctly.
  4. Knowledge-base       questions with a known correct SOP section ->
     search               hit@1, hit@k, mean reciprocal rank (MRR).
  5. Workflow (optional)  the flagship workflow on the sample report ->
                          correct zone / approver / invented values caught.
  6. Agent (optional)     4 tasks with known answers -> task success, steps, time.
  7. Sovereignty          external connections during the whole evaluation
                          (from the network monitor) -> should be 0.

A note on terms:
  - Precision (for "code"): of the tasks the router SENT to code, how many
    really were code tasks.
  - Recall (for "code"): of the real code tasks, how many it CAUGHT.
  - F1: one number combining both (their harmonic mean).
  - CER: share of characters OCR got wrong (0% = perfect).
  - hit@k: the right SOP section was among the top k search results.
  - MRR: average of 1/rank of the right section (1.0 = always first).
"""

import argparse
import asyncio
import json

import numpy as np
import platform
import re
import csv
import random
import sys
import threading
import time
from difflib import SequenceMatcher
from datetime import datetime
from pathlib import Path

import psutil

import config
from app import agent, codeflow, documents, knowledge, llm, main, netmon, router, trained, workflow

RESULTS_DIR = config.PROJECT_DIR / "results"
SAMPLES = config.PROJECT_DIR / "samples"


# ===========================================================================
# Test data (labelled by hand). Kept separate from the router's EXAMPLES.
# ===========================================================================
ROUTER_TESTS = [
    # (task, correct role)
    ("Summarise the attached maintenance memo in five bullet points", "general"),
    ("What is the minimum oxygen level allowed before hot work can start?", "general"),
    ("Draft a letter to the vendor asking for a revised quotation", "general"),
    ("Who approves maintenance spending of 12 lakh rupees?", "general"),
    ("Explain why cavitation damages pump impellers", "general"),
    ("List the responsibilities of the condition monitoring engineer", "general"),
    ("How often must critical pumps be checked for vibration?", "general"),
    ("Calculate the remaining life of a shell that is 10.2 mm thick", "general"),
    ("Prepare a short note on the seal failures of P-2105A", "general"),
    ("What does a bearing temperature of 92 degrees mean for the pump?", "general"),
    ("Create a table of the cost items in the inspection report", "general"),
    ("Translate the safety precautions into simple language for contractors", "general"),
    ("Is a reading of 3.9 mm/s acceptable for long-term operation?", "general"),
    ("Compare the hot work permit rules with the approval note rules", "general"),
    ("What records must be kept after an ultrasonic thickness survey?", "general"),
    ("Write a Python script that reads vibration readings from a CSV file", "code"),
    ("My function returns None instead of the zone, can you debug it?", "code"),
    ("Write a regex that extracts equipment tags like P-2105B from text", "code"),
    ("Add unit tests for the corrosion rate function", "code"),
    ("Convert this formula into a Python function: rate = (t0 - t1) / years", "code"),
    ("Why does my script throw a ZeroDivisionError?", "code"),
    ("Write SQL to list all pumps with more than three seal failures", "code"),
    ("Refactor this loop to use a list comprehension", "code"),
    ("Write a program that converts degrees Celsius to Fahrenheit", "code"),
    ("Explain this Python traceback: IndexError list index out of range", "code"),
    ("Make my pandas code faster, it takes ten minutes on a big file", "code"),
    ("Write a JavaScript function that formats rupees with commas", "code"),
    ("Fix the bug in this code that reads the wrong column", "code"),
    ("Create a Python class to store pump inspection data", "code"),
    ("Write a script to rename all PDF reports in a folder by date", "code"),
]

ATTACHMENT_TESTS = [
    # (question, should it warn when NOTHING is attached?)
    ("Summarise this report", True),
    ("What does the attached file say about bearings?", True),
    ("List the findings in this document", True),
    ("Read this PDF and give me the costs", True),
    ("Describe this photo", True),
    ("Check the uploaded scan for errors", True),
    ("What is in the attachment?", True),
    ("Translate these documents into Hindi", True),
    ("Is this drawing up to date?", True),
    ("Extract the table from the attached report", True),
    ("What is the vibration limit for Zone C?", False),
    ("Explain what a hot work permit is", False),
    ("What does SOP-MECH-004 say about records?", False),
    ("Write a report template for pump inspections", False),
    ("How should a report on bearing failure be structured?", False),
    ("Who approves expenditure above 25 lakh?", False),
    ("Calculate the corrosion rate for 12 mm to 9.1 mm in 10 years", False),
    ("What documents are needed for an approval note?", False),
    ("Draft an email to the inspection team", False),
    ("Give me a checklist for laser alignment", False),
]

RETRIEVAL_TESTS = [
    # (question, file that should be found (start of name), words that must appear in the section)
    ("What action is required when vibration exceeds 7.1 mm/s?", "SOP-MECH-004", "Vibration Severity"),
    ("How often are critical pumps monitored?", "SOP-MECH-004", "Frequency"),
    ("What is the alarm limit for bearing housing temperature?", "SOP-MECH-004", "Temperature"),
    ("Who plans corrective maintenance after a limit is exceeded?", "SOP-MECH-004", "Responsibilities"),
    ("How is the remaining life of a pressure vessel calculated?", "SOP-INSP-011", "Remaining Life"),
    ("When is the next thickness inspection due?", "SOP-INSP-011", "Next Inspection"),
    ("What happens if actual thickness is below the minimum required?", "SOP-INSP-011", "Acceptance"),
    ("Who can approve spending of 20 lakh rupees?", "SOP-ADM-002", "Delegation"),
    ("What sections must an approval note contain?", "SOP-ADM-002", "Format"),
    ("What gas test limits apply before welding?", "SOP-SAFE-007", "Gas Test"),
    ("How long is a hot work permit valid?", "SOP-SAFE-007", "Validity"),
    ("Why did the seal of the crude charge pump keep failing?", "MEMO-2026-031", ""),
]

OCR_KEY_VALUES = ["IR-2026-118", "14-10-2026", "P-2105B", "6.4", "4.1", "3.2", "5.1", "3.6", "2.7",
                  "68", "86", "65", "74", "18", "65,000", "48,000", "12,000", "1,25,000"]

AGENT_TESTS = [
    # (name, task, attach the sample report?, list of (description, regex that must appear))
    ("Corrosion calculation",
     "A pressure vessel shell was 12.0 mm thick when built in 2016. It measures 9.1 mm now, in 2026. "
     "The minimum required thickness is 8.0 mm. Using SOP-INSP-011, calculate the corrosion rate, "
     "remaining life and the next inspection date.", False,
     # Accept any precision: 3.79, 3.8, 3.80, 3.793 / 1.89, 1.9, 1.90, 1.897.
     # (Phase 6.8: the first version rejected "1.90" and failed a correct answer.)
     [("rate 0.29", r"0\.29"), ("remaining life ~3.8", r"\b3\.(?:79|8)\d*"),
      ("next inspection ~1.9", r"\b1\.(?:89|9)\d*")]),
    ("Coding task",
     "Write a Python function that classifies a vibration reading into Zone A, B, C or D as per "
     "SOP-MECH-004. Test it on 2.1, 4.5, 6.4 and 7.5 and print the results.", False,
     [("4.5 -> B", r"4\.5\D{0,40}B\b"), ("6.4 -> C", r"6\.4\D{0,40}C\b"), ("7.5 -> D", r"7\.5\D{0,40}D\b")]),
    ("Zone check on scanned report",
     "Which vibration zone is the highest reading in this report, and what does SOP-MECH-004 require?", True,
     [("6.4 mm/s", r"6\.4"), ("Zone C", r"Zone C"), ("30 days", r"30 days")]),
    ("Unit conversion",
     "Write a Python function that converts pressure from psi to bar, test it on 100 psi and print the result.", False,
     [("100 psi = 6.89 bar", r"6\.89")]),
    ("Unit conversion (flow)",
     "Write a Python function that converts oil flow from barrels per day to cubic metres per hour, "
     "and test it on 1000 bbl/day.", False,
     [("1000 bbl/day = 6.62 m3/h", r"6\.62")]),
]


# ===========================================================================
# Small maths helpers
# ===========================================================================
def edit_distance(a, b) -> int:
    """Levenshtein distance: fewest insertions/deletions/changes to turn a into b."""
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def normalise(text: str) -> str:
    """For OCR scoring: unwrap [unclear: x], keep only word-like tokens, lower case."""
    text = re.sub(r"\[unclear:\s*([^\]]*)\]", r"\1", text)
    return " ".join(t for t in text.lower().split() if any(c.isalnum() for c in t))


def confusion(labels: list, pairs: list) -> dict:
    """Confusion matrix + accuracy + precision/recall/F1 per label. pairs = [(actual, predicted)]."""
    matrix = {a: {p: 0 for p in labels} for a in labels}
    for actual, predicted in pairs:
        matrix[actual][predicted] += 1
    per = {}
    for label in labels:
        tp = matrix[label][label]
        fp = sum(matrix[a][label] for a in labels if a != label)
        fn = sum(matrix[label][p] for p in labels if p != label)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per[label] = {"precision": precision, "recall": recall, "f1": f1}
    accuracy = sum(matrix[l][l] for l in labels) / max(1, len(pairs))
    return {"labels": labels, "matrix": matrix, "accuracy": accuracy, "per_label": per}


def matrix_markdown(result: dict, actual_name="actual", predicted_name="predicted") -> list:
    labels = [str(l) for l in result["labels"]]
    lines = [f"| {actual_name} ↓ / {predicted_name} → | " + " | ".join(labels) + " |",
             "|---|" + "---|" * len(labels)]
    for actual in result["labels"]:
        lines.append(f"| **{actual}** | " + " | ".join(str(result["matrix"][actual][p]) for p in result["labels"]) + " |")
    lines += ["", "| | precision | recall | F1 |", "|---|---|---|---|"]
    for label, m in result["per_label"].items():
        lines.append(f"| {label} | {m['precision']:.2f} | {m['recall']:.2f} | {m['f1']:.2f} |")
    lines.append(f"\n**Accuracy: {result['accuracy']:.1%}** ({sum(result['matrix'][l][l] for l in result['labels'])} "
                 f"of {sum(sum(r.values()) for r in result['matrix'].values())} correct)")
    return lines


# ===========================================================================
# The tests
# ===========================================================================
def roc_curve(rows: list) -> dict:
    """
    ROC curve for the router (Phase 6.12), treating "code" as the positive class.

    The router's decision score is  margin = code score - general score  and it
    picks code when margin > 0. A ROC curve asks: what if the cut-off were not 0?
    For every possible cut-off t we count, among tasks with margin > t:
        TPR (true positive rate)  = code tasks caught    / all code tasks
        FPR (false positive rate) = general tasks caught / all general tasks
    AUC (area under the curve) is the chance that a random code task gets a
    higher margin than a random general task: 1.0 = perfect, 0.5 = guessing.
    """
    positives = [r["margin"] for r in rows if r["correct"] == "code"]
    negatives = [r["margin"] for r in rows if r["correct"] == "general"]
    if not positives or not negatives:
        return {}
    cutoffs = [float("inf")] + sorted({r["margin"] for r in rows}, reverse=True) + [float("-inf")]
    points = []
    for t in cutoffs:
        tpr = sum(m > t for m in positives) / len(positives) if t != float("-inf") else 1.0
        fpr = sum(m > t for m in negatives) / len(negatives) if t != float("-inf") else 1.0
        points.append({"cutoff": None if abs(t) == float("inf") else round(t, 3),
                       "fpr": round(fpr, 4), "tpr": round(tpr, 4)})
    # AUC by counting pairs (ties count half) - exact, no curve-drawing needed.
    wins = sum((p > n) + 0.5 * (p == n) for p in positives for n in negatives)
    auc = wins / (len(positives) * len(negatives))
    at_zero = {"fpr": sum(m > 0 for m in negatives) / len(negatives),
               "tpr": sum(m > 0 for m in positives) / len(positives)}
    return {"auc": round(auc, 4), "points": points, "router_cutoff_0": at_zero}


# ---------------------------------------------------------------------------
# Phase 6.14: HELD-OUT test sets (evaluation/*.csv)
# ---------------------------------------------------------------------------
# The small sets above (30 router tasks, 20 attachment questions) were written
# while the router and the attachment rule were being built, so they are a
# DEVELOPMENT set: good for spotting bugs, too small and too familiar for a
# final score. The held-out sets are bigger (200 and 100), grouped into
# categories, and were written separately, AFTER the rules were frozen.
# They are plain CSV files, so anyone can open and check every label.
# Rule: never change the router or the rule to fit these sets. If they are
# changed, a NEW held-out set is needed for an honest score.
EVAL_DIR = Path(__file__).parent / "evaluation"


def load_csv(name: str) -> list:
    path = EVAL_DIR / name
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def by_category(rows: list) -> dict:
    """Accuracy per category, e.g. {'Debug and fix': [14, 15]} = 14 of 15 right."""
    out = {}
    for row in rows:
        right, total = out.get(row["category"], [0, 0])
        out[row["category"]] = [right + (row["correct"] == row["predicted"]), total + 1]
    return out


# ---------------------------------------------------------------------------
# Phase 6.15: the LARGE generated test sets (evaluation/*_bigset.csv)
# ---------------------------------------------------------------------------
# Made by evaluation/generate_tests.py: ~250 hand-written seeds per check,
# each rewritten 20-25 ways (typos, Hinglish, extra context...). Variants of
# one seed are NOT independent, so the confidence interval is computed over
# SEEDS (a "cluster bootstrap"), and robustness is reported separately.
def seed_bootstrap(rows: list, repeats: int = 2000) -> list:
    """
    95% confidence interval for accuracy, resampling whole SEEDS.
    Repeat 2000 times: pick seeds at random (with replacement), compute the
    accuracy over all their variants. The middle 95% of those accuracies is
    the interval. Resampling rows instead would pretend the 5,000 variants
    are independent, and give a falsely narrow interval.
    """
    by_seed = {}
    for row in rows:
        right, total = by_seed.get(row["seed_id"], (0, 0))
        by_seed[row["seed_id"]] = (right + (row["correct"] == row["predicted"]), total + 1)
    seeds = list(by_seed.values())
    rnd = random.Random(42)
    accuracies = []
    for _ in range(repeats):
        pick = [seeds[rnd.randrange(len(seeds))] for _ in seeds]
        accuracies.append(sum(r for r, _ in pick) / sum(t for _, t in pick))
    accuracies.sort()
    return [accuracies[int(0.025 * repeats)], accuracies[int(0.975 * repeats) - 1]]


def robustness(rows: list) -> dict:
    """
    How often does rewording change the answer? For every variant, compare
    its answer with the answer for its own ORIGINAL seed wording. Also:
    accuracy and flip rate per kind of change (typo, Hinglish, ...).
    """
    original = {r["seed_id"]: r["predicted"] for r in rows if r["variant"] == "original"}
    changed = [r for r in rows if r["variant"] != "original" and r["seed_id"] in original]
    same = sum(r["predicted"] == original[r["seed_id"]] for r in changed)
    kinds = {}
    for r in changed:
        for kind in r["variant"].split("+"):
            k = kinds.setdefault(kind, {"n": 0, "right": 0, "flipped": 0})
            k["n"] += 1
            k["right"] += r["predicted"] == r["correct"]
            k["flipped"] += r["predicted"] != original[r["seed_id"]]
    seeds = {}
    for r in rows:
        seeds.setdefault(r["seed_id"], []).append(r["predicted"] == r["correct"])
    return {"same_answer_as_seed": same / len(changed) if changed else None, "variants": len(changed),
            "by_change": {k: {**v, "accuracy": v["right"] / v["n"], "flip_rate": v["flipped"] / v["n"]}
                          for k, v in sorted(kinds.items())},
            "seeds_all_right": sum(all(v) for v in seeds.values()), "seeds_all_wrong": sum(not any(v) for v in seeds.values()),
            "seeds": len(seeds)}


def big_summary(rows: list, labels: list) -> dict:
    out = {**confusion(labels, [(r["correct"], r["predicted"]) for r in rows]), "n": len(rows),
           "seeds": len({r["seed_id"] for r in rows}), "ci95": seed_bootstrap(rows),
           "robustness": robustness(rows), "by_category": by_category(rows)}
    return out


async def test_router_big() -> dict:
    tests = load_csv("router_bigset.csv")
    started = time.time()
    decisions = await router.route_batch([t["task"] for t in tests], use_trained=False)   # rules: these are training data now
    rows = []
    for t, d in zip(tests, decisions):
        rows.append({"id": t["id"], "seed_id": t["seed_id"], "variant": t["variant"], "category": t["category"],
                     "correct": t["label"], "predicted": d["intended_role"],
                     "margin": round(d["scores"].get("code", 0.0) - d["scores"].get("general", 0.0), 4)})
    out = big_summary(rows, ["general", "code"])
    out["roc"] = {k: v for k, v in roc_curve(rows).items() if k != "points"}   # 5,000 points: keep the summary
    out["seconds"] = round(time.time() - started, 1)
    out["rows"] = rows
    return out


def test_attachment_big() -> dict:
    tests = load_csv("attachment_bigset.csv")
    rows = []
    for t in tests:
        warned = main.refers_to_missing_attachment(t["question"], [], use_trained=False)
        rows.append({"id": t["id"], "seed_id": t["seed_id"], "variant": t["variant"], "category": t["category"],
                     "correct": "warn" if t["should_warn"] == "yes" else "no warning",
                     "predicted": "warn" if warned else "no warning"})
    out = big_summary(rows, ["warn", "no warning"])
    out["rows"] = rows
    return out


async def test_search_big() -> dict:
    """
    ~1,000 reworded search questions (60 seeds). Ranking is computed the same
    way as the workbench's search: cosine similarity between the question and
    every section, best first, ignoring sections below MIN_SIMILARITY.
    Questions are embedded in batches of 64 for speed.
    """
    if not knowledge.status()["chunk_count"] or knowledge.status()["needs_rebuild"]:
        await knowledge.rebuild_index()
    tests = load_csv("search_bigset.csv")
    chunks, matrix = knowledge._kb["chunks"], knowledge._kb["vectors"]
    started, rows = time.time(), []
    for start in range(0, len(tests), 64):
        batch = tests[start:start + 64]
        vectors = await llm.embed([config.EMBED_QUERY_PREFIX + t["question"] for t in batch])
        for t, vector in zip(batch, vectors):
            query = np.array(vector, dtype=np.float32)
            query /= np.linalg.norm(query)
            scores = matrix @ query
            order = [int(i) for i in np.argsort(-scores) if scores[int(i)] >= config.MIN_SIMILARITY]
            doc, section = t["answer_in"].split("|")
            rank = next((n for n, i in enumerate(order, 1) if chunks[i]["source"].startswith(doc)
                         and section.lower() in (chunks[i].get("section") or "").lower()), None)
            top = f"{chunks[order[0]]['source'][:14]}|{chunks[order[0]].get('section', '')}" if order else "-"
            rows.append({"seed_id": t["seed_id"], "variant": t["variant"], "rank": rank, "top": top})
    n, k = len(rows), config.TOP_K
    original_top = {r["seed_id"]: r["top"] for r in rows if r["variant"] == "original"}
    changed = [r for r in rows if r["variant"] != "original"]
    as_hits = lambda limit: [{"seed_id": r["seed_id"], "correct": 1, "predicted": int(bool(r["rank"]) and r["rank"] <= limit)}
                             for r in rows]
    return {"n": n, "seeds": len({r["seed_id"] for r in rows}), "k": k, "seconds": round(time.time() - started, 1),
            "hit@1": sum(1 for r in rows if r["rank"] == 1) / n,
            "hit@k": sum(1 for r in rows if r["rank"] and r["rank"] <= k) / n,
            "mrr": sum(1 / r["rank"] for r in rows if r["rank"]) / n,
            "ci95_hit@1": seed_bootstrap(as_hits(1)), "ci95_hit@k": seed_bootstrap(as_hits(k)),
            "same_top_as_seed": sum(r["top"] == original_top.get(r["seed_id"]) for r in changed) / max(1, len(changed)),
            "rank_counts": {str(i): sum(1 for r in rows if r["rank"] == i) for i in range(1, 11)}
                           | {"later or not found": sum(1 for r in rows if not r["rank"] or r["rank"] > 10)},
            "rows": rows}


def _field_lines(ocr_text: str, labels: list) -> dict:
    """
    Find the OCR line for each field label (e.g. "Vibration DE horizontal").
    OCR can garble a label slightly, so each label is matched to the line
    whose start is closest to it (edit distance), each line used once.
    """
    lines = [l for l in ocr_text.splitlines() if l.strip()]
    pairs = []
    for label in labels:
        for i, line in enumerate(lines):
            head = normalise(line)[:len(label) + 1].rstrip(" :")
            d = edit_distance(head, label.lower())
            if d <= max(2, len(label) // 6):
                pairs.append((d, label, i))
    found, used = {}, set()
    for d, label, i in sorted(pairs):
        if label not in found and i not in used:
            found[label], _ = lines[i], used.add(i)
    return found


def _field_outcome(true_value: str, line) -> str:
    """
    correct / marked unclear / missing / WRONG (a different value, NOT flagged).
    None of the field labels contains a digit, so every number-like token on
    the line belongs to the value. Tokens inside an [unclear: ...] marker were
    flagged by the workbench; tokens outside it were read "cleanly".
    """
    if line is None:
        return "missing"
    token = re.compile(r"[A-Za-z]*-?\d[\d,.\-]*[A-Z]?")
    clean = token.findall(re.sub(r"\[unclear:[^\]]*\]", " ", line))
    if not clean:
        return "marked unclear" if "[unclear" in line else "missing"
    want, got = token.findall(true_value)[0], clean[0]

    def same(a, b):
        try:
            return float(a.replace(",", "")) == float(b.replace(",", ""))   # "2." == "2.0"
        except ValueError:
            return a.rstrip(".,") == b.rstrip(".,")                        # tags, dates, report numbers
    return "correct" if same(want, got) else "WRONG (not flagged)"


def test_ocr_big(pages: int) -> dict:
    """
    OCR on generated scanned pages (evaluation/generate_ocr_tests.py).
    For each page: character error rate (CER), and for each of its 13 values
    whether OCR read it correctly, marked it [unclear], lost it, or - the
    dangerous case - read a DIFFERENT value without flagging it.
    """
    folder = EVAL_DIR / "ocr_synthetic"
    truth_file = folder / "truth.json"
    if not truth_file.exists() or len(json.loads(truth_file.read_text(encoding="utf-8"))) < pages:
        print(f"   generating {pages} synthetic pages…")
        sys.path.insert(0, str(EVAL_DIR))
        import generate_ocr_tests
        generate_ocr_tests.main(pages)
    truth = json.loads(truth_file.read_text(encoding="utf-8"))[:pages]
    started, results = time.time(), []
    for n, page in enumerate(truth, 1):
        if n % 25 == 0:
            print(f"   OCR page {n} of {len(truth)}…")
        ocr_text = "\n".join(p["text"] for p in documents.extract(folder / page["file"]))
        o, t = normalise(ocr_text), normalise(page["text"])
        fields = {k: v for k, v in page["fields"].items() if k != "Remarks"}
        lines = _field_lines(ocr_text, list(fields))
        results.append({"file": page["file"], "damage": page["damage"],
                        "cer": edit_distance(o, t) / max(1, len(t)),
                        "fields": {k: _field_outcome(v, lines.get(k)) for k, v in fields.items()}})
    outcomes = [o for r in results for o in r["fields"].values()]
    count = lambda name: sum(1 for o in outcomes if o == name)
    by_damage = {}
    for r in results:
        kinds = [k for k in r["damage"] if k != "smudged_field"] or ["none (clean)"]
        for kind in kinds:
            d = by_damage.setdefault(kind, {"pages": 0, "cer": 0.0, "fields": 0, "wrong": 0, "correct": 0})
            d["pages"] += 1
            d["cer"] += r["cer"]
            d["fields"] += len(r["fields"])
            d["wrong"] += sum(1 for o in r["fields"].values() if o.startswith("WRONG"))
            d["correct"] += sum(1 for o in r["fields"].values() if o == "correct")
    for d in by_damage.values():
        d["cer"] /= d["pages"]
    cers = sorted(r["cer"] for r in results)
    return {"pages": len(results), "seconds": round(time.time() - started, 1),
            "median_cer": cers[len(cers) // 2], "mean_cer": sum(cers) / len(cers),
            "fields": len(outcomes), "outcomes": {name: count(name) for name in
                                                 ("correct", "marked unclear", "missing", "WRONG (not flagged)")},
            "by_damage": by_damage, "pages_detail": results}


# ---------------------------------------------------------------------------
# Phase 6.16: OLD RULES vs TRAINED MODELS, on the FRESH test sets
# ---------------------------------------------------------------------------
# The dev, held-out and generated sets above were used to TRAIN the two
# classifiers (evaluation/train_models.py), so they are evaluated with the
# old rules only: testing a model on its own training data would be cheating.
# The fresh sets were written before training and never used for it.
def _bootstrap_rows(rows: list) -> list:
    """95% CI for accuracy; every fresh question is its own seed."""
    return seed_bootstrap([{**r, "seed_id": str(i)} for i, r in enumerate(rows)])


async def test_fresh() -> dict:
    out = {}
    tasks = load_csv("fresh_router_testset.csv")
    if tasks:
        questions = [t["task"] for t in tasks]
        for name, use in (("rule", False), ("trained", True)):
            decisions = await router.route_batch(questions, use_trained=use)
            rows = [{"correct": t["label"], "predicted": d["intended_role"], "category": t["category"],
                     "task": t["task"]} for t, d in zip(tasks, decisions)]
            out[f"router_{name}"] = {**confusion(["general", "code"], [(r["correct"], r["predicted"]) for r in rows]),
                                     "n": len(rows), "ci95": _bootstrap_rows(rows), "by_category": by_category(rows),
                                     "wrong": [r["task"] for r in rows if r["correct"] != r["predicted"]],
                                     "active": use and trained.info("router") is not None or not use}
    questions = load_csv("fresh_attachment_testset.csv")
    if questions:
        for name, use in (("rule", False), ("trained", True)):
            rows = []
            for q in questions:
                warned = main.refers_to_missing_attachment(q["question"], [], use_trained=use)
                rows.append({"correct": "warn" if q["should_warn"] == "yes" else "no warning",
                             "predicted": "warn" if warned else "no warning", "category": q["category"],
                             "task": q["question"]})
            out[f"attachment_{name}"] = {**confusion(["warn", "no warning"], [(r["correct"], r["predicted"]) for r in rows]),
                                         "n": len(rows), "ci95": _bootstrap_rows(rows), "by_category": by_category(rows),
                                         "wrong": [r["task"] for r in rows if r["correct"] != r["predicted"]],
                                         "active": use and trained.info("attachment") is not None or not use}
    out["training"] = {"router": trained.info("router"), "attachment": trained.info("attachment")}
    return out


async def test_router(tests=None, use_trained=False):
    tests = tests or [(t, c, "") for t, c in ROUTER_TESTS]
    pairs, wrong, rows = [], [], []
    for task, correct, category in tests:
        decision = await router.route(task, [], "auto", log=False, use_trained=use_trained)
        predicted = decision["intended_role"]
        pairs.append((correct, predicted))
        scores = decision["scores"]
        # Phase 6.12: keep EVERY task's scores (not only the wrong ones), for the ROC curve.
        rows.append({"task": task, "correct": correct, "predicted": predicted, "scores": scores, "category": category,
                     "margin": round(scores.get("code", 0.0) - scores.get("general", 0.0), 4)})
        if predicted != correct:
            wrong.append({"task": task, "correct": correct, "predicted": predicted, "scores": scores})
    labels = [r for r in ("general", "code") if r in config.MODELS]
    return {**confusion(labels, pairs), "wrong": wrong, "n": len(pairs), "rows": rows, "roc": roc_curve(rows),
            "by_category": by_category(rows)}


def test_attachment_check(tests=None, use_trained=False):
    tests = tests or [(q, w, "") for q, w in ATTACHMENT_TESTS]
    pairs, wrong, rows = [], [], []
    for question, should_warn, category in tests:
        warned = main.refers_to_missing_attachment(question, [], use_trained=use_trained)
        actual, predicted = ("warn" if should_warn else "no warning"), ("warn" if warned else "no warning")
        pairs.append((actual, predicted))
        rows.append({"question": question, "correct": actual, "predicted": predicted, "category": category})
        if warned != should_warn:
            wrong.append({"question": question, "expected_warning": should_warn, "category": category})
    return {**confusion(["warn", "no warning"], pairs), "wrong": wrong, "n": len(pairs), "rows": rows,
            "by_category": by_category(rows)}


# ---------------------------------------------------------------------------
# Phase 6.13: does Tesseract's confidence predict real OCR errors?
# ---------------------------------------------------------------------------
def word_calibration(words: list, truth_text: str) -> list:
    """
    Mark every OCR'd word as correct or wrong, by lining the OCR words up
    with the true printed words (difflib finds the longest runs that match,
    the same idea as a "diff" between two files). A word inside a matching
    run is correct; anything else was misread, merged, split or invented.
    Returns [{"conf": 0-100, "correct": True/False}, ...] in reading order.
    """
    kept = [(normalise(w["word"]), w["conf"]) for w in words]
    kept = [(token, conf) for token, conf in kept if token]
    tokens, truth = [t for t, _ in kept], normalise(truth_text).split()
    correct = [False] * len(tokens)
    for block in SequenceMatcher(None, tokens, truth, autojunk=False).get_matching_blocks():
        for i in range(block.a, block.a + block.size):
            correct[i] = True
    return [{"conf": conf, "correct": ok} for (_, conf), ok in zip(kept, correct)]


def calibration_summary(rows: list, cutoff: float) -> dict:
    """How well 'confidence below cutoff' (the [unclear] marker) finds the wrong words."""
    wrong = [r for r in rows if not r["correct"]]
    right = [r for r in rows if r["correct"]]
    flagged = [r for r in rows if r["conf"] < cutoff]
    caught = sum(1 for r in flagged if not r["correct"])
    # AUC: chance that a random WRONG word has lower confidence than a random right one.
    pairs = sum((w["conf"] < g["conf"]) + 0.5 * (w["conf"] == g["conf"]) for w in wrong for g in right)
    bins = []
    for low in range(0, 100, 10):
        inside = [r for r in rows if low <= r["conf"] < low + 10 or (low == 90 and r["conf"] == 100)]
        bins.append({"from": low, "to": low + 10, "words": len(inside),
                     "accuracy": (sum(r["correct"] for r in inside) / len(inside)) if inside else None})
    return {"words": len(rows), "wrong_words": len(wrong), "cutoff": cutoff,
            "flagged": len(flagged), "flagged_and_wrong": caught,
            "share_of_errors_flagged": caught / len(wrong) if wrong else None,
            "share_of_flags_that_were_errors": caught / len(flagged) if flagged else None,
            "auc": pairs / (len(wrong) * len(right)) if wrong and right else None, "bins": bins}


def test_ocr():
    results = []
    truth = {n: (SAMPLES / "ground_truth" / f"IR-2026-118 page{n}.txt").read_text(encoding="utf-8")
             for n in (1, 2)}
    for filename, pages_truth in [("IR-2026-118 Scanned Inspection Report.pdf", [truth[1], truth[2]]),
                                  ("IR-2026-118 page1 photo.jpg", [truth[1]])]:
        started = time.time()
        documents.KEEP_OCR_WORDS = True          # Phase 6.13: keep every word's confidence
        try:
            pages = documents.extract(SAMPLES / filename)
        finally:
            documents.KEEP_OCR_WORDS = False
        seconds = time.time() - started
        ocr_text = "\n".join(p["text"] for p in pages)
        true_text = "\n".join(pages_truth)
        o, t = normalise(ocr_text), normalise(true_text)
        cer = edit_distance(o, t) / max(1, len(t))
        word_errors = edit_distance(o.split(), t.split()) / max(1, len(t.split()))
        keys = [k for k in OCR_KEY_VALUES if k.lower() in normalise(true_text)]
        found = []
        for key in keys:
            if re.fullmatch(r"[\d.,]+", key):          # a number: check it as a whole number
                if workflow.number_in(float(key.replace(",", "")), ocr_text):
                    found.append(key)
            elif key.lower() in ocr_text.lower():       # a code like IR-2026-118: plain text search
                found.append(key)
        results.append({
            "file": filename, "pages": len(pages), "seconds": round(seconds, 1),
            "cer": cer, "word_accuracy": max(0.0, 1 - word_errors),
            "tesseract_confidence": [p["ocr_confidence"] for p in pages],
            "uncertain_words": sum(p.get("uncertain_words", 0) for p in pages),
            "key_values_found": len(found), "key_values_total": len(keys),
            "key_values_missed": [k for k in keys if k not in found],
            # Phase 6.13: every word with its confidence and whether it was right
            "words": word_calibration([w for p in pages for w in p.get("ocr_words", [])], true_text),
        })
        results[-1]["calibration"] = calibration_summary(results[-1]["words"], documents.LOW_CONFIDENCE)
    return results


async def test_retrieval():
    if not knowledge.status()["chunk_count"] or knowledge.status()["needs_rebuild"]:
        await knowledge.rebuild_index()
    rows, reciprocal, score_rows = [], [], []
    for question, file_start, section_word in RETRIEVAL_TESTS:
        hits = await knowledge.search_knowledge_base(question)
        rank = None
        for i, h in enumerate(hits, 1):
            if h["source"].startswith(file_start) and section_word.lower() in (h.get("section") or "").lower():
                rank = i
                break
        reciprocal.append(1 / rank if rank else 0.0)
        # Phase 6.13: the similarity of EVERY section to this question, and
        # whether it is a correct one. Shows whether MIN_SIMILARITY (0.45) is a
        # good cut-off: it should keep the right sections and drop the rest.
        query = await knowledge._embed_query(question)
        similarity = knowledge._kb["vectors"] @ query
        order = list(reversed(similarity.argsort()))
        top = set(int(i) for i in order[:config.TOP_K])
        for i, chunk in enumerate(knowledge._kb["chunks"]):
            score_rows.append({
                "question": len(rows), "score": round(float(similarity[i]), 4),
                "relevant": chunk["source"].startswith(file_start)
                            and section_word.lower() in (chunk.get("section") or "").lower(),
                "in_top_k": i in top})
        rows.append({"question": question, "expected": f"{file_start} / {section_word or '(any)'}",
                     "rank": rank, "top": f"{hits[0]['source'][:24]} / {hits[0].get('section', '')[:30]}" if hits else "-"})
    n = len(rows)
    return {"rows": rows, "hit@1": sum(1 for r in rows if r["rank"] == 1) / n,
            "hit@k": sum(1 for r in rows if r["rank"]) / n, "k": config.TOP_K, "mrr": sum(reciprocal) / n,
            "scores": score_rows, "score_summary": score_summary(score_rows)}


def score_summary(rows: list) -> dict:
    """Phase 6.13: how the MIN_SIMILARITY cut-off treats right and wrong sections."""
    cut = config.MIN_SIMILARITY
    good = [r["score"] for r in rows if r["relevant"]]
    bad = [r["score"] for r in rows if not r["relevant"]]
    pairs = sum((g > b) + 0.5 * (g == b) for g in good for b in bad)
    return {"cutoff": cut, "relevant": len(good), "other": len(bad),
            "relevant_below_cutoff": sum(1 for g in good if g < cut),
            "other_above_cutoff": sum(1 for b in bad if b >= cut),
            "other_given_to_model": sum(1 for r in rows if not r["relevant"] and r["in_top_k"] and r["score"] >= cut),
            "lowest_relevant": min(good) if good else None, "highest_other": max(bad) if bad else None,
            "auc": pairs / (len(good) * len(bad)) if good and bad else None}


# ---------------------------------------------------------------------------
# Phase 6.13: speed and memory
# ---------------------------------------------------------------------------
async def test_speed():
    """
    Each chat model writes the same short answer twice. Ollama reports, in
    nanoseconds: how long LOADING the model took, how long it took to READ the
    prompt, and how long it took to WRITE the answer. Tokens per second =
    tokens / seconds. The first call may include loading the model from disk.
    Also times the embedding model on the 12 search questions.
    """
    prompt = "In about 60 words, explain what a centrifugal pump does."
    installed = await llm.installed_models()
    calls = []
    for role in ("general", "code"):
        name = config.MODELS.get(role, {}).get("name")
        if not name or not llm.is_installed(name, installed):
            continue
        for call in ("1st call", "2nd call"):
            try:
                async with llm._make_client() as client:
                    response = await client.post("/api/generate", json={
                        "model": name, "prompt": prompt, "stream": False,
                        "options": {"num_predict": 100, "temperature": 0, "num_ctx": config.CONTEXT_TOKENS}})
                response.raise_for_status()
                d = response.json()
                if not d.get("eval_count"):
                    raise RuntimeError("Ollama returned no timing data")
            except Exception as error:          # e.g. not enough memory to load the model
                print(f"   speed test {name} {call}: failed ({error})")
                continue
            calls.append({"role": role, "model": name, "call": call,
                          "load_s": d.get("load_duration", 0) / 1e9,
                          "prompt_tokens": d.get("prompt_eval_count", 0),
                          "prompt_s": d.get("prompt_eval_duration", 0) / 1e9,
                          "output_tokens": d.get("eval_count", 0),
                          "output_s": d.get("eval_duration", 0) / 1e9,
                          "total_s": d.get("total_duration", 0) / 1e9})
    embed_seconds = []
    for question, _, _ in RETRIEVAL_TESTS:
        started = time.perf_counter()
        await llm.embed([config.EMBED_QUERY_PREFIX + question])
        embed_seconds.append(time.perf_counter() - started)
    return {"calls": calls, "embedding_seconds": embed_seconds}


class ResourceMonitor:
    """
    Samples memory and CPU once a second in a background thread while the
    evaluation runs: RAM used by the whole laptop, RAM used by Ollama (which
    holds the models), and CPU use. stage() marks which test is running.
    """

    def __init__(self, every: float = 1.0):
        self.every, self.samples, self.stages = every, [], []
        self._stop = threading.Event()
        self._t0 = time.time()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        psutil.cpu_percent(interval=None)          # the first reading is always 0
        self._thread.start()

    def stage(self, name: str):
        self.stages.append({"name": name, "start": round(time.time() - self._t0, 1)})

    def stop(self) -> dict:
        self._stop.set()
        self._thread.join(timeout=5)
        end = round(time.time() - self._t0, 1)
        for i, st in enumerate(self.stages):
            st["end"] = self.stages[i + 1]["start"] if i + 1 < len(self.stages) else end
        return {"total_gb": psutil.virtual_memory().total / 1e9, "samples": self.samples, "stages": self.stages}

    def _loop(self):
        while not self._stop.is_set():
            ollama = 0
            for process in psutil.process_iter(["name", "memory_info"]):
                try:
                    if "ollama" in (process.info["name"] or "").lower() and process.info["memory_info"]:
                        ollama += process.info["memory_info"].rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            self.samples.append({"t": round(time.time() - self._t0, 1),
                                 "used_gb": round(psutil.virtual_memory().used / 1e9, 3),
                                 "ollama_gb": round(ollama / 1e9, 3),
                                 "cpu": psutil.cpu_percent(interval=None)})
            self._stop.wait(self.every)


async def _upload_sample_report():
    content = (SAMPLES / "IR-2026-118 Scanned Inspection Report.pdf").read_bytes()
    return (await knowledge.ingest_upload("IR-2026-118 Scanned Inspection Report.pdf", content))["id"]


async def test_workflow(runs: int = 1):
    """
    Phase 6.13: the workflow can run several times (--workflow-runs N). For each
    value the AI extracted, we record whether the workbench VERIFIED it against
    the scan, and whether it is really RIGHT (checked against the true printed
    text in samples/ground_truth/). That gives four outcomes:
        right + verified  -> correct and accepted
        right + flagged   -> a false alarm (safe: a human checks it)
        wrong + flagged   -> an error CAUGHT by the verification
        wrong + verified  -> an error MISSED (e.g. the scan itself was misread)
    """
    truth = "\n".join((SAMPLES / "ground_truth" / f"IR-2026-118 page{n}.txt").read_text(encoding="utf-8")
                      for n in (1, 2))
    doc_id = await _upload_sample_report()
    results = []
    for _ in range(runs):
        results.append(await _one_workflow_run(doc_id, truth))
    return {**results[0], "runs": results}


async def _one_workflow_run(doc_id: str, truth: str) -> dict:
    workflow.LAST_RUN.update({"checks": [], "narrative_source": ""})
    started, steps, answer, files = time.time(), {}, "", []
    async for e in workflow.run([{"role": "user", "content": "Draft an approval note"}], [doc_id]):
        if e["type"] == "step_end":
            steps[e["id"]] = e["summary"]
        elif e["type"] == "token":
            answer += e["text"]
        elif e["type"] == "file":
            files.append(e["name"])
    checks = {
        "highest reading 6.4 mm/s found": "6.4" in answer,
        "classified as Zone C": "Zone C" in answer,
        "approver = Department Head": "Department Head" in answer,
        "DE bearing temperature flagged (ALARM)": "ALARM" in answer,
        "Word file created": bool(files),
        "no invented value used unflagged": "not found" not in steps.get("w3", "") or "Check before signing" in answer,
    }
    values = []
    for c in workflow.LAST_RUN["checks"]:
        really_right = (None if c["value"] is None
                        else workflow.check_value(c["value"], c["location"], truth) == "verified")
        values.append({**c, "really_right": really_right})
    return {"seconds": round(time.time() - started, 1), "checks": checks, "steps": steps, "files": files,
            "values": values, "narrative_source": workflow.LAST_RUN["narrative_source"]}


async def test_agent():
    results = []
    report_id = None
    for name, task, attach, expectations in AGENT_TESTS:
        if attach and report_id is None:
            report_id = await _upload_sample_report()
        role = (await router.route(task, [report_id] if attach else [], "auto", log=False))["role"]
        started, steps, tools_used, files, step_tool = time.time(), 0, [], [], {}
        answer, code_output = "", ""     # graded: the final answer + code output
        # The same path the page uses: code tasks go through the guided code
        # workflow (Phase 6.3), everything else through the agent.
        if role == "code":
            runner = codeflow.run([{"role": "user", "content": task}], [report_id] if attach else [],
                                  True, role=role)
        else:
            runner = agent.run([{"role": "user", "content": task}], [report_id] if attach else [],
                               True, role=role)
        try:
            async for e in runner:
                if e["type"] == "token":
                    answer += e["text"]
                elif e["type"] == "draft_reset":
                    # Text before a tool call (or a draft sent back for a fix)
                    # is not the final answer: keep it out of the checks.
                    answer = ""
                elif e["type"] == "step_start":
                    steps += 1
                    tools_used.append(e["tool"])
                    step_tool[e["id"]] = e["tool"]
                elif (e["type"] == "step_end" and e.get("display")
                      and step_tool.get(e["id"]) == "run_python"):
                    # Phase 6.11: only CODE output counts, never document text.
                    # In the 5 Nov 02:14 run the zone-check's final answer listed
                    # the wrong SOP requirements, yet the "30 days" check passed:
                    # the words could come from document text a tool returned,
                    # or from text written before a tool call. Now only the
                    # FINAL answer and real code output are checked.
                    code_output += "\n" + (e["display"].get("output") or "")
                elif e["type"] == "file":
                    files.append(e["name"])
        except Exception as error:
            answer += f"\n[error: {error}]"
        text = code_output + "\n" + answer
        passed = {desc: bool(re.search(pattern, text, re.IGNORECASE)) for desc, pattern in expectations}
        # Phase 6.9: a code task only passes if the workbench FINISHED it (the
        # code ran, its tests passed, and it was saved). In the 5 Nov run the
        # "Coding task" was marked PASS because a FAILED attempt had printed the
        # right zones, although the user was told "could not be made to run".
        if role == "code":
            passed["tests passed, code saved"] = bool(files)
        results.append({"name": name, "model": config.MODELS[role]["name"], "seconds": round(time.time() - started, 1),
                        "steps": steps, "tools": tools_used, "checks": passed, "success": all(passed.values()),
                        # Phase 6.8: keep the answer, so every PASS/FAIL can be audited.
                        "answer": text})
        print(f"   agent task '{name}': {'PASS' if results[-1]['success'] else 'FAIL'} ({results[-1]['seconds']}s)")
    return results


# ===========================================================================
# Report
# ===========================================================================
def write_report(r: dict) -> Path:
    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    md = [f"# Sovereign Workbench — evaluation report", "",
          f"Date: {r['date']}  ", f"Machine: {r['machine']}  ",
          "Models: " + ", ".join(f"{k} = `{v}`" for k, v in r["models"].items()), ""]

    md += ["## 1. Model router", f"{r['router']['n']} labelled tasks, **different from** the router's own "
           "example tasks. Measures the router's own choice (before any fallback to an installed model).", ""]
    md += matrix_markdown(r["router"], "actual task", "router chose")
    if r["router"]["wrong"]:
        md += ["", "Misrouted tasks:"] + [f"- \"{w['task']}\": {w['correct']} → routed to {w['predicted']} "
                                          f"(scores {w['scores']})" for w in r["router"]["wrong"]]

    roc = r["router"].get("roc")
    if roc:
        z = roc["router_cutoff_0"]
        md += ["", f"**ROC AUC: {roc['auc']:.3f}** (code = positive class; score = code score − general score). "
               f"At the router's cut-off of 0: true positive rate {z['tpr']:.2f}, false positive rate {z['fpr']:.2f}. "
               "Every task's scores are in the .json file."]

    if "router_heldout" in r:
        h = r["router_heldout"]
        md += ["", f"### 1b. Router on the HELD-OUT test set ({h['n']} tasks, `evaluation/router_testset.csv`)", ""]
        md += matrix_markdown(h, "actual task", "router chose")
        if h.get("roc"):
            md += ["", f"ROC AUC: {h['roc']['auc']:.3f}"]
        md += ["", "| Category | Correct |", "|---|---|"]
        md += [f"| {k} | {v[0]} / {v[1]} |" for k, v in h["by_category"].items()]
        if h["wrong"]:
            md += ["", "Misrouted:"] + [f"- \"{w['task']}\": {w['correct']} → {w['predicted']} ({w['scores']})"
                                       for w in h["wrong"]]

    for key, title, row_name, col_name in [("router_big", "1c. Router on the LARGE generated set", "actual task", "router chose"),
                                           ("attachment_big", "2c. Attachment check on the LARGE generated set", "should", "did")]:
        if key not in r:
            continue
        b, rb = r[key], r[key]["robustness"]
        md += ["", f"### {title} ({b['n']} tests from {b['seeds']} seeds)", ""]
        md += matrix_markdown(b, row_name, col_name)
        md += ["", f"95% confidence interval (bootstrap over seeds): {b['ci95'][0]:.1%} to {b['ci95'][1]:.1%}"]
        if b.get("roc"):
            md.append(f"ROC AUC: {b['roc']['auc']:.3f}. Time: {b['seconds']} s.")
        md += [f"Robustness: {rb['same_answer_as_seed']:.1%} of reworded variants get the same answer as their seed. "
               f"Seeds right in every variant: {rb['seeds_all_right']} of {rb['seeds']}; wrong in every variant: "
               f"{rb['seeds_all_wrong']}.", "", "| Change | Variants | Accuracy | Answer flipped |", "|---|---|---|---|"]
        md += [f"| {k} | {v['n']} | {v['accuracy']:.1%} | {v['flip_rate']:.1%} |" for k, v in rb["by_change"].items()]

    f = r.get("fresh", {})
    if f:
        md += ["", "## 10. Trained models vs old rules (FRESH test sets, never used for training)", "",
               "Sections 1, 1b, 1c, 2, 2b and 2c use the OLD RULES: their questions were used to train the models.", "",
               "| Check | Method | Accuracy | 95% CI | Errors |", "|---|---|---|---|---|"]
        for check, title in (("router", "Router"), ("attachment", "Attachment check")):
            for name in ("rule", "trained"):
                res = f.get(f"{check}_{name}")
                if not res:
                    continue
                label = "old rule" if name == "rule" else ("trained model" if f["training"].get(check) else
                                                         "trained model NOT FOUND (rule used)")
                md.append(f"| {title} ({res['n']} questions) | {label} | {res['accuracy']:.1%} | "
                          f"{res['ci95'][0]:.1%}–{res['ci95'][1]:.1%} | {len(res['wrong'])} |")
        for check, title, rows_n, cols_n in (("router", "Router", "actual task", "chose"),
                                              ("attachment", "Attachment check", "should", "did")):
            if f.get(f"{check}_trained"):
                md += ["", f"**{title}, trained model:**", ""] + matrix_markdown(f[f"{check}_trained"], rows_n, cols_n)
                md += ["", "Wrong: " + "; ".join(f"\"{w}\"" for w in f[f"{check}_trained"]["wrong"])]

    md += ["", "## 2. Missing-attachment check", f"{r['attachment']['n']} labelled questions, asked with nothing attached.", ""]
    md += matrix_markdown(r["attachment"], "should", "did")
    if r["attachment"]["wrong"]:
        md += ["", "Errors:"] + [f"- \"{w['question']}\" (expected warning: {w['expected_warning']})" for w in r["attachment"]["wrong"]]
    if "attachment_heldout" in r:
        h = r["attachment_heldout"]
        md += ["", f"### 2b. Attachment check on the HELD-OUT test set ({h['n']} questions, "
               "`evaluation/attachment_testset.csv`)", ""]
        md += matrix_markdown(h, "should", "did")
        md += ["", "| Category | Correct |", "|---|---|"]
        md += [f"| {k} | {v[0]} / {v[1]} |" for k, v in h["by_category"].items()]
        if h["wrong"]:
            md += ["", "Errors:"] + [f"- \"{w['question']}\" (expected warning: {w['expected_warning']})"
                                     for w in h["wrong"]]

    md += ["", "## 3. OCR accuracy", "Compared with the exact text printed on the sample scans (`samples/ground_truth/`).", "",
           "| File | Pages | CER | Word accuracy | Key values read | Tesseract confidence | Hard-to-read words | Time |",
           "|---|---|---|---|---|---|---|---|"]
    for o in r["ocr"]:
        md.append(f"| {o['file']} | {o['pages']} | {o['cer']:.1%} | {o['word_accuracy']:.1%} | "
                  f"{o['key_values_found']}/{o['key_values_total']} | {o['tesseract_confidence']} | "
                  f"{o['uncertain_words']} | {o['seconds']} s |")
    for o in r["ocr"]:
        if o["key_values_missed"]:
            md.append(f"\nMissed in {o['file']}: {', '.join(o['key_values_missed'])}")
    md += ["", "**Does Tesseract's confidence find the errors?** (Phase 6.13) A word below the cut-off is "
           "marked [unclear] in the text.", "",
           "| File | Words | Wrong words | Marked [unclear] | Errors marked | Marks that were errors | AUC |",
           "|---|---|---|---|---|---|---|"]
    for o in r["ocr"]:
        c = o.get("calibration")
        if c:
            pct = lambda v: "–" if v is None else f"{v:.0%}"
            md.append(f"| {o['file']} | {c['words']} | {c['wrong_words']} | {c['flagged']} (conf < {c['cutoff']}) | "
                      f"{pct(c['share_of_errors_flagged'])} | {pct(c['share_of_flags_that_were_errors'])} | "
                      f"{'–' if c['auc'] is None else round(c['auc'], 2)} |")

    q = r["retrieval"]
    md += ["", "## 4. Knowledge-base search", f"{len(q['rows'])} questions with a known correct SOP section.", "",
           f"**hit@1: {q['hit@1']:.0%} · hit@{q['k']}: {q['hit@k']:.0%} · MRR: {q['mrr']:.2f}**", "",
           "| Question | Expected | Rank | Top result |", "|---|---|---|---|"]
    md += [f"| {x['question']} | {x['expected']} | {x['rank'] or 'not found'} | {x['top']} |" for x in q["rows"]]
    ss = q.get("score_summary")
    if ss:
        md += ["", f"**Similarity scores** (Phase 6.13): correct sections {ss['relevant']}, other sections {ss['other']}, "
               f"AUC {ss['auc']:.3f}. Cut-off MIN_SIMILARITY = {ss['cutoff']}: correct sections below it "
               f"{ss['relevant_below_cutoff']}; other sections above it {ss['other_above_cutoff']}, of which "
               f"{ss['other_given_to_model']} were given to the model (in the top {q['k']}). Lowest correct "
               f"{ss['lowest_relevant']:.3f}, highest other {ss['highest_other']:.3f}."]

    if "workflow" in r:
        w = r["workflow"]
        md += ["", "## 5. Flagship workflow (sample report → approval note)", f"Time: {w['seconds']} s", "",
               "| Check | Result |", "|---|---|"] + [f"| {k} | {'✔' if v else '✘'} |" for k, v in w["checks"].items()]
        md += ["", "Steps:"] + [f"- {k}: {v}" for k, v in w["steps"].items()]
        # Phase 6.13: the verification catch rate, over all runs
        every = [v for run in w.get("runs", []) for v in run.get("values", [])]
        values = [v for v in every if v["really_right"] is not None]
        blank = sum(1 for v in every if v["value"] is None)
        if values:
            ok = lambda v: v["status"] == "verified"
            tally = {"right, accepted": sum(1 for v in values if v["really_right"] and ok(v)),
                     "right, flagged (false alarm)": sum(1 for v in values if v["really_right"] and not ok(v)),
                     "WRONG, flagged (caught)": sum(1 for v in values if not v["really_right"] and not ok(v)),
                     "WRONG, accepted (missed)": sum(1 for v in values if not v["really_right"] and ok(v))}
            md += ["", f"**Verification over {len(w['runs'])} run(s)** (Phase 6.13): {len(values)} extracted values, "
                   "each compared with the true printed report.", "", "| Outcome | Values |", "|---|---|"]
            md += [f"| {k} | {n} |" for k, n in tally.items()]
            md.append(f"| left blank: unreadable on the scan, not guessed | {blank} |")
            md += ["", "Narrative written by: " + "; ".join(run["narrative_source"] for run in w["runs"])]

    if "agent" in r:
        md += ["", "## 6. Agent tasks", "| Task | Model | Result | Checks | Tool steps | Time |", "|---|---|---|---|---|---|"]
        for a in r["agent"]:
            checks = ", ".join(f"{k} {'✔' if v else '✘'}" for k, v in a["checks"].items())
            md.append(f"| {a['name']} | {a['model']} | {'PASS' if a['success'] else 'FAIL'} | {checks} | {a['steps']} | {a['seconds']} s |")
        passed = sum(1 for a in r["agent"] if a["success"])
        md.append(f"\n**Task success: {passed} of {len(r['agent'])}**")
        md += ["", "### Agent answers (for checking each result)",
               "The answer text and the printed tool output, as seen by the checks (last 1,500 characters)."]
        for a in r["agent"]:
            md += ["", f"**{a['name']}** ({'PASS' if a['success'] else 'FAIL'})", "```", a["answer"][-1500:].strip(), "```"]

    s = r["sovereignty"]
    md += ["", "## 7. Sovereignty during the evaluation",
           f"- Requests sent by the workbench: {s['allowed']} local, **{s['blocked']} blocked (external)**",
           f"- External connections seen by the operating system at the end: **{s['external_connections']}**",
           f"- Verdict: **{'no data left the machine' if s['sovereign'] else 'EXTERNAL TRAFFIC DETECTED — investigate'}**"]

    if "speed" in r:
        md += ["", "## 8. Speed and memory (Phase 6.13)",
               "| Model | Call | Loading | Reading the prompt | Writing the answer | Total |", "|---|---|---|---|---|---|"]
        for c in r["speed"]["calls"]:
            read_rate = c["prompt_tokens"] / c["prompt_s"] if c["prompt_s"] else 0
            write_rate = c["output_tokens"] / c["output_s"] if c["output_s"] else 0
            md.append(f"| {c['model']} | {c['call']} | {c['load_s']:.1f} s | {read_rate:.0f} tokens/s | "
                      f"{write_rate:.1f} tokens/s | {c['total_s']:.1f} s |")
        e = r["speed"]["embedding_seconds"]
        if e:
            md.append(f"\nEmbedding one search question: median {sorted(e)[len(e) // 2] * 1000:.0f} ms "
                      f"(slowest {max(e) * 1000:.0f} ms, includes loading).")
    if r.get("resources", {}).get("samples"):
        res = r["resources"]
        md += ["", f"Memory during the evaluation (laptop total {res['total_gb']:.1f} GB):", "",
               "| Stage | Time | Laptop RAM used (max) | Ollama RAM (max) | CPU (average) |", "|---|---|---|---|---|"]
        for st in res["stages"]:
            inside = [x for x in res["samples"] if st["start"] <= x["t"] < st["end"]]
            if inside:
                md.append(f"| {st['name']} | {st['end'] - st['start']:.0f} s | "
                          f"{max(x['used_gb'] for x in inside):.1f} GB | {max(x['ollama_gb'] for x in inside):.1f} GB | "
                          f"{sum(x['cpu'] for x in inside) / len(inside):.0f}% |")

    if "search_big" in r or "ocr_big" in r:
        md += ["", "## 9. Large generated test sets: search and OCR (Phase 6.15)"]
    if "search_big" in r:
        b = r["search_big"]
        md += ["", f"**Search, {b['n']} reworded questions from {b['seeds']} seeds:** hit@1 {b['hit@1']:.1%} "
               f"(95% CI over seeds {b['ci95_hit@1'][0]:.1%}–{b['ci95_hit@1'][1]:.1%}), hit@{b['k']} {b['hit@k']:.1%} "
               f"(CI {b['ci95_hit@k'][0]:.1%}–{b['ci95_hit@k'][1]:.1%}), MRR {b['mrr']:.2f}. Rewording kept the same top "
               f"section {b['same_top_as_seed']:.1%} of the time. Time {b['seconds']} s."]
    if "ocr_big" in r:
        b = r["ocr_big"]
        md += ["", f"**OCR, {b['pages']} generated scanned pages:** median CER {b['median_cer']:.1%}, mean {b['mean_cer']:.1%}. "
               f"Of {b['fields']} values:", "", "| Outcome | Values | Share |", "|---|---|---|"]
        md += [f"| {k} | {v} | {v / b['fields']:.1%} |" for k, v in b["outcomes"].items()]
        md += ["", "| Damage | Pages | Mean CER | Values correct | Values WRONG (not flagged) |", "|---|---|---|---|---|"]
        md += [f"| {k} | {d['pages']} | {d['cer']:.1%} | {d['correct'] / d['fields']:.1%} | {d['wrong'] / d['fields']:.1%} |"
               for k, d in sorted(b["by_damage"].items())]

    md_path = RESULTS_DIR / f"eval_{stamp}.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    (RESULTS_DIR / f"eval_{stamp}.json").write_text(json.dumps(r, indent=1, ensure_ascii=False, default=str),
                                                     encoding="utf-8")
    return md_path


async def run(args):
    for folder in (config.DATA_DIR, config.UPLOADS_DIR, config.WORKSPACE_DIR):
        folder.mkdir(parents=True, exist_ok=True)
    knowledge.load_index()
    health = await llm.check_health()
    if not health["ollama_running"]:
        sys.exit("Ollama is not running. Start it and try again.")

    memory_gb = psutil.virtual_memory().total / 1e9
    r = {"date": datetime.now().strftime("%Y-%m-%d %H:%M"),
         "machine": f"{platform.system()} {platform.release()}, {psutil.cpu_count()} CPU threads, {memory_gb:.1f} GB RAM",
         "models": {role: info["name"] for role, info in config.MODELS.items()} | {"embedding": config.EMBEDDING_MODEL}}

    monitor = ResourceMonitor()      # Phase 6.13: memory and CPU, sampled every second
    monitor.start()
    monitor.stage("router");         print("1/8 router…");           r["router"] = await test_router()
    monitor.stage("attachment");     print("2/8 attachment check…"); r["attachment"] = test_attachment_check()
    monitor.stage("trained vs rules"); print("   fresh test sets: old rules vs trained models…")
    r["fresh"] = await test_fresh()
    router_set, attach_set = load_csv("router_testset.csv"), load_csv("attachment_testset.csv")
    if router_set:
        monitor.stage("router (held-out)")
        print(f"   held-out router test set: {len(router_set)} tasks…")
        r["router_heldout"] = await test_router([(x["task"], x["label"], x["category"]) for x in router_set])
    if attach_set:
        r["attachment_heldout"] = test_attachment_check(
            [(x["question"], x["should_warn"] == "yes", x["category"]) for x in attach_set])
    if args.big:
        if not load_csv("router_bigset.csv"):
            print("   no large test sets yet: run  python evaluation/generate_tests.py  first")
        else:
            monitor.stage("router (5,000)")
            print("   LARGE router test set (5,000 tasks, in batches)…")
            r["router_big"] = await test_router_big()
            r["attachment_big"] = test_attachment_big()
            if load_csv("search_bigset.csv"):
                print("   LARGE search test set…")
                r["search_big"] = await test_search_big()
    if args.ocr_pages:
        monitor.stage("OCR (synthetic)")
        print(f"   OCR on {args.ocr_pages} synthetic pages… (several seconds per page)")
        r["ocr_big"] = await asyncio.to_thread(test_ocr_big, args.ocr_pages)
    monitor.stage("OCR");            print("3/8 OCR…");              r["ocr"] = await asyncio.to_thread(test_ocr)
    monitor.stage("search");         print("4/8 search…");           r["retrieval"] = await test_retrieval()
    monitor.stage("model speed");    print("5/8 model speed… (loads each model)")
    r["speed"] = await test_speed()
    runs = args.workflow_runs or (1 if (args.workflow or args.all) else 0)
    if runs:
        monitor.stage("workflow")
        print(f"6/8 workflow × {runs}… (a few minutes each on a CPU)")
        r["workflow"] = await test_workflow(runs)
    if args.agent or args.all:
        monitor.stage("agent tasks")
        print("7/8 agent tasks… (slow on a CPU)")
        r["agent"] = await test_agent()
    r["resources"] = monitor.stop()
    report = netmon.get_report()
    r["sovereignty"] = {**report["request_totals"], "external_connections": report["external_connection_count"],
                        "sovereign": report["sovereign"]}
    print("8/8 sovereignty checked")

    path = write_report(r)
    print(f"\nRouter accuracy {r['router']['accuracy']:.0%} · attachment check {r['attachment']['accuracy']:.0%} · "
          f"OCR CER {r['ocr'][0]['cer']:.1%} · search hit@{r['retrieval']['k']} {r['retrieval']['hit@k']:.0%} · "
          f"external connections {r['sovereignty']['external_connections']}")
    print(f"Report written to: {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate the Sovereign Workbench.")
    parser.add_argument("--workflow", action="store_true", help="also run the flagship workflow")
    parser.add_argument("--agent", action="store_true", help="also run agent tasks (slow)")
    parser.add_argument("--all", action="store_true", help="run everything")
    parser.add_argument("--big", action="store_true",
                        help="also run the large generated test sets (evaluation/*_bigset.csv)")
    parser.add_argument("--ocr-pages", type=int, default=0, metavar="N",
                        help="also OCR N generated scanned pages (evaluation/generate_ocr_tests.py)")
    parser.add_argument("--workflow-runs", type=int, default=0, metavar="N",
                        help="run the flagship workflow N times (for the verification catch rate)")
    asyncio.run(run(parser.parse_args()))
