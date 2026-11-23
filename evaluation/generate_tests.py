"""
evaluation/generate_tests.py — builds the LARGE test sets (Phase 6.15).

WHY
Hand-writing 5,000 test questions is not practical. Instead, this script
takes hand-written SEED questions (each with a checked label) and makes
VARIANTS of each one: the same request, written differently. Every variant
keeps its seed's label, because the change does not alter what is being
asked. Examples, for the seed "Explain NPSH in simple words":
    lower       explain npsh in simple words
    typo        Explain NPHS in simple words
    polite      Please help: Explain NPSH in simple words
    hinglish    Bhai, Explain NPSH in simple words
    context     I'm the shift engineer on the crude unit. Explain NPSH in simple words

HONEST LIMIT (please read before quoting the numbers)
5,000 variants of 250 seeds are NOT 5,000 independent tests: variants of one
seed tend to be all right or all wrong together. So the evaluation reports:
  - accuracy with a confidence interval computed over SEEDS (cluster
    bootstrap), not over rows, so the interval is not falsely narrow;
  - ROBUSTNESS: how often a variant gets the same answer as its own seed;
  - accuracy per variant type (e.g. typos, Hinglish).
And a random sample is written to audit_sample.csv so a person can check
that the generated labels are right.

HOW TO RUN (from the project folder, with .venv active)
    python evaluation/generate_tests.py
It is deterministic: the same seeds always give the same files.
Writes: evaluation/router_bigset.csv, evaluation/attachment_bigset.csv,
        evaluation/search_bigset.csv, evaluation/audit_sample.csv
"""

import csv
import random
import re
from pathlib import Path

HERE = Path(__file__).parent
VARIANTS_ROUTER = 20          # 250 seeds x 20 = 5,000
VARIANTS_ATTACHMENT = 25      # 200 seeds x 25 = 5,000
VARIANTS_SEARCH = 17          # 60 seeds x 17 = 1,020

# ---------------------------------------------------------------------------
# 1. Extra hand-written seeds (the rest come from the held-out CSV files)
# ---------------------------------------------------------------------------
EXTRA_ROUTER_SEEDS = [
    # (task, label, category)
    ("What is the recommended lubrication interval for pump bearings?", "general", "SOP and procedure questions"),
    ("Who must be informed before a pump is taken out of service?", "general", "SOP and procedure questions"),
    ("What isolation is needed before opening a pump casing?", "general", "SOP and procedure questions"),
    ("When is a gas test repeated during hot work?", "general", "SOP and procedure questions"),
    ("What does the SOP say about records after a seal change?", "general", "SOP and procedure questions"),
    ("How much will three bearings cost at 45,000 rupees each?", "general", "Engineering calculations"),
    ("What is the wall loss if thickness went from 12.5 mm to 10.9 mm?", "general", "Engineering calculations"),
    ("Is 88 degrees inside the bearing temperature alarm band?", "general", "Engineering calculations"),
    ("How many hours of downtime fit in a 3-day shutdown with two shifts?", "general", "Engineering calculations"),
    ("Convert 6 bar into psi for the vendor", "general", "Engineering calculations"),
    ("Draft a thank-you note to the contractor team", "general", "Writing and drafting"),
    ("Write an apology to the operations team for the delayed permit", "general", "Writing and drafting"),
    ("Prepare talking points for the monthly safety meeting", "general", "Writing and drafting"),
    ("Write a request to stores for two spare seals", "general", "Writing and drafting"),
    ("Summarise the risks of running a pump in Zone D", "general", "Writing and drafting"),
    ("Why does cavitation make a gravel-like noise?", "general", "Explaining engineering concepts"),
    ("What is the difference between a standby and a duty pump?", "general", "Explaining engineering concepts"),
    ("Explain what a bearing defect frequency is", "general", "Explaining engineering concepts"),
    ("Make a table of who approves which amount", "general", "Tables and records (no code)"),
    ("List the steps of a laser alignment job", "general", "Tables and records (no code)"),
    ("What is the function of the seal flush plan?", "general", "General tasks using code-like words"),
    ("Explain the code of practice for working at height", "general", "General tasks using code-like words"),
    ("Run me through the startup program for the new pump", "general", "General tasks using code-like words"),
    ("OK, go on", "general", "Short or casual requests"),
    ("Can you check that again?", "general", "Short or casual requests"),
    ("Write a Python function that flags readings above the alarm limit", "code", "Write Python"),
    ("Write a script that reads a folder of CSV readings and makes one report", "code", "Write Python"),
    ("Write Python that calculates the corrosion rate for every row of a table", "code", "Write Python"),
    ("Write a program to convert all temperatures in a file from F to C", "code", "Write Python"),
    ("Create a Python dataclass for a pump with tag, location and limits", "code", "Write Python"),
    ("My script hangs when the file is open in Excel, how do I fix it?", "code", "Debug and fix"),
    ("Why does my code give ValueError: could not convert string to float?", "code", "Debug and fix"),
    ("Fix the bug where my loop skips the last reading", "code", "Debug and fix"),
    ("My Flask app returns 404 for every page, what is wrong?", "code", "Debug and fix"),
    ("Write a SQL query to average the vibration per month", "code", "Other languages and queries"),
    ("Write a regex that matches temperatures like 74°C", "code", "Other languages and queries"),
    ("Write a PowerShell one-liner to list files bigger than 1 GB", "code", "Other languages and queries"),
    ("Write a VBA macro that emails the sheet to the planner", "code", "Other languages and queries"),
    ("Explain what this lambda function does", "code", "Explain or review code"),
    ("Review my SQL query for performance problems", "code", "Explain or review code"),
    ("What does the yield keyword do in Python?", "code", "Explain or review code"),
    ("How do I read a PDF's text in Python?", "code", "Coding questions without obvious keywords"),
    ("How do I connect to a SQLite database?", "code", "Coding questions without obvious keywords"),
    ("How do I make a chart from a dataframe?", "code", "Coding questions without obvious keywords"),
    ("How do I send an HTTP request to a local server?", "code", "Coding questions without obvious keywords"),
    ("How can I run two tasks at the same time in Python?", "code", "Coding questions without obvious keywords"),
    ("Add logging to every function in this module", "code", "Tests, refactoring and performance"),
    ("Speed up this nested loop over two lists", "code", "Tests, refactoring and performance"),
    ("Write unit tests for the rupee formatting function", "code", "Tests, refactoring and performance"),
    ("Split this 300-line script into modules", "code", "Tests, refactoring and performance"),
]

EXTRA_ATTACHMENT_SEEDS = [
    # (question, should_warn "yes"/"no", category)
    ("Summarise this inspection report for the GM", "yes", "Direct mentions (this/attached/uploaded + file word)"),
    ("Pull out the readings from the attached scan", "yes", "Direct mentions (this/attached/uploaded + file word)"),
    ("Is the uploaded drawing signed?", "yes", "Direct mentions (this/attached/uploaded + file word)"),
    ("What is the total in this PDF?", "yes", "Direct mentions (this/attached/uploaded + file word)"),
    ("Check this photo for leaks", "yes", "Direct mentions (this/attached/uploaded + file word)"),
    ("List the equipment in the attached document", "yes", "Direct mentions (this/attached/uploaded + file word)"),
    ("Translate this report into simple English", "yes", "Direct mentions (this/attached/uploaded + file word)"),
    ("Who prepared this document?", "yes", "Direct mentions (this/attached/uploaded + file word)"),
    ("What does this circular say about overtime?", "yes", "Other document words (memo, sheet, letter...)"),
    ("Check this checklist for missing items", "yes", "Other document words (memo, sheet, letter...)"),
    ("Summarise this email chain", "yes", "Other document words (memo, sheet, letter...)"),
    ("Find errors in this bill of materials", "yes", "Other document words (memo, sheet, letter...)"),
    ("What is the deadline in this notice?", "yes", "Other document words (memo, sheet, letter...)"),
    ("Is this work order complete?", "yes", "Other document words (memo, sheet, letter...)"),
    ("Read this datasheet and give me the rated flow", "yes", "Other document words (memo, sheet, letter...)"),
    ("Check these minutes for action owners", "yes", "Other document words (memo, sheet, letter...)"),
    ("What are the totals in this workbook?", "yes", "Other document words (memo, sheet, letter...)"),
    ("Review this SOP draft for gaps", "yes", "Other document words (memo, sheet, letter...)"),
    ("Look at the scan I uploaded and tell me the zone", "yes", "Possessive or past-tense references"),
    ("Summarise the file I attached earlier", "yes", "Possessive or past-tense references"),
    ("Check my report before I send it to the GM", "yes", "Possessive or past-tense references"),
    ("What does my photo show?", "yes", "Possessive or past-tense references"),
    ("Use the PDF I sent to fill the approval note", "yes", "Possessive or past-tense references"),
    ("Is my spreadsheet formula right?", "yes", "Possessive or past-tense references"),
    ("Read my handwritten notes and type them up", "yes", "Possessive or past-tense references"),
    ("Compare the two reports I shared", "yes", "Possessive or past-tense references"),
    ("Go through the drawings I sent", "yes", "Possessive or past-tense references"),
    ("Proofread my letter", "yes", "Possessive or past-tense references"),
    ("Here's the scan, what zone is the highest reading?", "yes", "Pronoun or indirect references"),
    ("Please see attached and confirm the costs", "yes", "Pronoun or indirect references"),
    ("From the picture, is the coupling guard fitted?", "yes", "Pronoun or indirect references"),
    ("In the report, which bearing is in alarm?", "yes", "Pronoun or indirect references"),
    ("According to the attachment, who approved it?", "yes", "Pronoun or indirect references"),
    ("Based on the screenshot, what is the trip setting?", "yes", "Pronoun or indirect references"),
    ("On page 3 of the document, what is the date?", "yes", "Pronoun or indirect references"),
    ("Extract every number from the image", "yes", "Pronoun or indirect references"),
    ("Summarise what's in the file", "yes", "Pronoun or indirect references"),
    ("Using the enclosed sheet, calculate the total", "yes", "Pronoun or indirect references"),
    ("Tell me what the scan says about the NDE bearing", "yes", "Pronoun or indirect references"),
    ("Check the attached for spelling mistakes", "yes", "Pronoun or indirect references"),
    ("Read the photo and list the gauges", "yes", "Pronoun or indirect references"),
    ("Can you read the table in the PDF?", "yes", "Pronoun or indirect references"),
    ("Does the drawing show a bypass line?", "yes", "Pronoun or indirect references"),
    ("What is the conclusion of the report?", "yes", "Pronoun or indirect references"),
    ("Extract the signatures from the document", "yes", "Pronoun or indirect references"),
    ("Look at the image, is there corrosion on the flange?", "yes", "Pronoun or indirect references"),
    ("Summarise the attached in two lines", "yes", "Pronoun or indirect references"),
    ("Check the spreadsheet for duplicate tags", "yes", "Pronoun or indirect references"),
    ("What does the memo say about overtime?", "yes", "Pronoun or indirect references"),
    ("Translate the letter into Hindi", "yes", "Pronoun or indirect references"),
    ("What does SOP-ADM-002 say about urgent approvals?", "no", "Questions about SOPs and the knowledge base"),
    ("What is the hot work gas test limit for oxygen?", "no", "Questions about SOPs and the knowledge base"),
    ("Which SOP covers bearing temperature limits?", "no", "Questions about SOPs and the knowledge base"),
    ("What are the Zone D actions in the vibration SOP?", "no", "Questions about SOPs and the knowledge base"),
    ("Who can approve 3 crore of spending?", "no", "Questions about SOPs and the knowledge base"),
    ("What records does SOP-INSP-011 require?", "no", "Questions about SOPs and the knowledge base"),
    ("Is there an SOP for confined space entry?", "no", "Questions about SOPs and the knowledge base"),
    ("What does the seal failure memo in the knowledge base conclude?", "no", "Questions about SOPs and the knowledge base"),
    ("Search the knowledge base for cavitation", "no", "Questions about SOPs and the knowledge base"),
    ("What is the reference conversion for bbl per day?", "no", "Questions about SOPs and the knowledge base"),
    ("Write a report on last month's pump trips", "no", "Writing new documents (no attachment needed)"),
    ("Draft a memo about the new permit rules", "no", "Writing new documents (no attachment needed)"),
    ("Create a checklist document for pump handover", "no", "Writing new documents (no attachment needed)"),
    ("Make a spreadsheet template for thickness readings", "no", "Writing new documents (no attachment needed)"),
    ("Prepare a letter to the vendor about warranty", "no", "Writing new documents (no attachment needed)"),
    ("Write a one-page document on seal flush plans", "no", "Writing new documents (no attachment needed)"),
    ("Draft a PDF-ready notice for contractors", "no", "Writing new documents (no attachment needed)"),
    ("Write an inspection report template", "no", "Writing new documents (no attachment needed)"),
    ("Create a Word document summarising the SOPs", "no", "Writing new documents (no attachment needed)"),
    ("Draft a photo log format for site inspections", "no", "Writing new documents (no attachment needed)"),
    ("What file size can I upload?", "no", "Questions about documents in general"),
    ("How should I scan drawings for best results?", "no", "Questions about documents in general"),
    ("What should a datasheet contain?", "no", "Questions about documents in general"),
    ("How do I convert a photo to PDF?", "no", "Questions about documents in general"),
    ("What is the right format for an approval note?", "no", "Questions about documents in general"),
    ("Can you read Excel files?", "no", "Questions about documents in general"),
    ("How do I upload more than one file?", "no", "Questions about documents in general"),
    ("What makes a good incident report?", "no", "Questions about documents in general"),
    ("Where do saved documents go?", "no", "Questions about documents in general"),
    ("Do you keep my files after I close the chat?", "no", "Questions about documents in general"),
    ("Is this value high: 7.4 mm/s?", "no", "'This/that' about things that are not files"),
    ("What does this abbreviation mean: DE?", "no", "'This/that' about things that are not files"),
    ("Is this plan OK: replace bearings, then align?", "no", "'This/that' about things that are not files"),
    ("Does this make sense: Zone B is fine long-term?", "no", "'This/that' about things that are not files"),
    ("Is this correct: 1 bar = 14.5 psi?", "no", "'This/that' about things that are not files"),
    ("Explain this phrase: run to failure", "no", "'This/that' about things that are not files"),
    ("Is that cost within a Department Head's limit: 4 lakh?", "no", "'This/that' about things that are not files"),
    ("What should I do about this: bearing at 92 degrees?", "no", "'This/that' about things that are not files"),
    ("Is this message clear: pump P-101 is down?", "no", "'This/that' about things that are not files"),
    ("What does this error mean: ZeroDivisionError?", "no", "'This/that' about things that are not files"),
    ("Calculate the next inspection for 4 years remaining life", "no", "Calculations and general questions"),
    ("What is soft foot?", "no", "Calculations and general questions"),
    ("Convert 75 degrees C to F", "no", "Calculations and general questions"),
    ("How do I stop seal leaks?", "no", "Calculations and general questions"),
    ("Give me tips for a good toolbox talk", "no", "Calculations and general questions"),
    ("What is the cost of 3 seals at 1.2 lakh each?", "no", "Calculations and general questions"),
    ("Which zone is 2.5 mm/s?", "no", "Calculations and general questions"),
    ("Why do bearings fail early?", "no", "Calculations and general questions"),
    ("Write a Python function for the vibration zone", "no", "Calculations and general questions"),
    ("What should I check before starting a pump?", "no", "Calculations and general questions"),
]


# Search: (question, file the answer is in, words in its section heading).
# One correct section per question, checked against the SOP text.
SEARCH_SEEDS = [
    ("Which pumps does the condition monitoring SOP apply to?", "SOP-MECH-004", "Scope"),
    ("Are pumps above 300 kW covered by the vibration SOP?", "SOP-MECH-004", "Scope"),
    ("Who collects the vibration readings?", "SOP-MECH-004", "Responsibilities"),
    ("Who must be told immediately about a Zone D reading?", "SOP-MECH-004", "Responsibilities"),
    ("How often are essential pumps measured?", "SOP-MECH-004", "Frequency"),
    ("When must readings be taken after an overhaul?", "SOP-MECH-004", "Frequency"),
    ("How often are general pumps checked?", "SOP-MECH-004", "Frequency"),
    ("Which reading is used to classify the vibration zone?", "SOP-MECH-004", "Vibration Severity"),
    ("What must be done for a Zone C vibration reading?", "SOP-MECH-004", "Vibration Severity"),
    ("Is 3.5 mm/s acceptable for long-term operation?", "SOP-MECH-004", "Vibration Severity"),
    ("Within how many hours must a Zone D pump be stopped?", "SOP-MECH-004", "Vibration Severity"),
    ("What bearing temperature is normal?", "SOP-MECH-004", "Temperature"),
    ("What should be checked when a bearing is in temperature alarm?", "SOP-MECH-004", "Temperature"),
    ("When must a bearing temperature rise be investigated?", "SOP-MECH-004", "Temperature"),
    ("At what bearing temperature must the pump be stopped?", "SOP-MECH-004", "Temperature"),
    ("What are common causes of high vibration?", "SOP-MECH-004", "Corrective"),
    ("What must accompany a bearing replacement?", "SOP-MECH-004", "Corrective"),
    ("Which zone must a pump return to after repair?", "SOP-MECH-004", "Corrective"),
    ("How long are vibration records kept?", "SOP-MECH-004", "Records"),
    ("Where are spectrum analysis reports recorded?", "SOP-MECH-004", "Records"),
    ("How many UT readings are taken at each location?", "SOP-INSP-011", "Measurement Method"),
    ("When must the thickness gauge be verified on a reference block?", "SOP-INSP-011", "Measurement Method"),
    ("What is the minimum required thickness?", "SOP-INSP-011", "Key Definitions"),
    ("Where does the original thickness come from?", "SOP-INSP-011", "Key Definitions"),
    ("How is the long-term corrosion rate calculated?", "SOP-INSP-011", "Corrosion Rate"),
    ("Which corrosion rate is used if a previous survey exists?", "SOP-INSP-011", "Corrosion Rate"),
    ("What is the formula for remaining life?", "SOP-INSP-011", "Remaining Life"),
    ("If remaining life is 6 years, when is the next inspection?", "SOP-INSP-011", "Next Inspection"),
    ("What is the longest allowed interval between UT inspections?", "SOP-INSP-011", "Next Inspection"),
    ("What if remaining life is less than 4 years?", "SOP-INSP-011", "Acceptance"),
    ("When must a vessel be taken out of service?", "SOP-INSP-011", "Acceptance"),
    ("Who approves the next inspection date?", "SOP-INSP-011", "Records"),
    ("Who signs the UT survey sheet?", "SOP-INSP-011", "Records"),
    ("What is the first section of an approval note?", "SOP-ADM-002", "Format"),
    ("Who recommends an approval note before it goes for approval?", "SOP-ADM-002", "Format"),
    ("What goes in the financial implication section?", "SOP-ADM-002", "Format"),
    ("Who approves spending of 3 lakh?", "SOP-ADM-002", "Delegation"),
    ("Who can approve 60 lakh rupees?", "SOP-ADM-002", "Delegation"),
    ("Who approves emergency work above the normal limit?", "SOP-ADM-002", "Delegation"),
    ("Within how many days is post-facto approval needed?", "SOP-ADM-002", "Delegation"),
    ("Should approval notes be written in the first person?", "SOP-ADM-002", "Language"),
    ("Must every measured value state its source?", "SOP-ADM-002", "Language"),
    ("Who issues a hot work permit?", "SOP-SAFE-007", "Who Issues"),
    ("Who must countersign hot work permits in hazardous areas?", "SOP-SAFE-007", "Who Issues"),
    ("How often is the gas test repeated during hot work?", "SOP-SAFE-007", "Gas Test"),
    ("What instrument is used for the gas test?", "SOP-SAFE-007", "Gas Test"),
    ("How long must the fire watcher stay after hot work?", "SOP-SAFE-007", "Precautions"),
    ("How must drains near hot work be covered?", "SOP-SAFE-007", "Precautions"),
    ("Is valve isolation enough for hot work?", "SOP-SAFE-007", "Precautions"),
    ("Can a hot work permit be used for two shifts?", "SOP-SAFE-007", "Validity"),
    ("Where must the permit be displayed?", "SOP-SAFE-007", "Validity"),
    ("How many seal failures did P-2105A have?", "MEMO-2026-031", ""),
    ("What misalignment was found on the crude charge pump?", "MEMO-2026-031", ""),
    ("What is the delivery time for a cartridge seal?", "MEMO-2026-031", ""),
    ("How much does a cartridge seal cost?", "MEMO-2026-031", ""),
    ("How many psi are in one bar?", "REF-ENG-001", "Pressure"),
    ("How do I convert Fahrenheit to Celsius?", "REF-ENG-001", "Temperature"),
    ("How many litres are in an oil barrel?", "REF-ENG-001", "Flow"),
    ("How many kilograms is one pound?", "REF-ENG-001", "Length and mass"),
    ("How many kW is one horsepower?", "REF-ENG-001", "Power"),
]

# ---------------------------------------------------------------------------
# 2. Label-preserving changes ("variant types")
# ---------------------------------------------------------------------------
POLITE = ["Please help: ", "Quick question - ", "Hi, ", "Hello team, ", "Can you help? ", "Kindly help: "]
SUFFIX = [" Thanks.", " It's urgent.", " Need this today.", " - for tomorrow's meeting.", " Thank you!", " ASAP please."]
HINGLISH_PRE = ["Bhai, ", "Sir ji, ", "Yaar, ", "Arre, ", "Ji, "]
HINGLISH_SUF = [" jaldi batao", " please bata do", " kar do please", " zara dekho", " thoda jaldi"]
CONTEXT = ["I'm the shift engineer on the crude unit. ", "For the P-2105B job: ", "Our planner asked me this. ",
           "Working on the turnaround plan. ", "Night shift here. ", "Before the 9 am meeting: "]
SYNONYMS = [("Explain", "Describe"), ("explain", "describe"), ("Calculate", "Work out"), ("calculate", "work out"),
            ("Summarise", "Sum up"), ("summarise", "sum up"), ("Draft", "Prepare"), ("Fix", "Correct"),
            ("Check", "Review"), ("check", "review"), ("What is", "What's"), ("How do I", "How can I"),
            ("Make", "Create"), ("List", "Give me"), ("Find", "Locate"), ("Write", "Put together")]
TAGS = ["P-2105A", "P-2105B", "P-3101A", "P-4402B", "P-1207C", "P-5510A"]


def _typo(text: str, rnd: random.Random) -> str:
    """One realistic typing slip in a word of 4+ letters: swap, drop or double a letter."""
    words = [m for m in re.finditer(r"[A-Za-z]{4,}", text)]
    if not words:
        return text
    w = rnd.choice(words)
    word, i = w.group(0), rnd.randrange(1, len(w.group(0)) - 1)
    kind = rnd.choice(["swap", "drop", "double"])
    if kind == "swap":
        new = word[:i] + word[i + 1] + word[i] + word[i + 2:]
    elif kind == "drop":
        new = word[:i] + word[i + 1:]
    else:
        new = word[:i] + word[i] + word[i:]
    return text[:w.start()] + new + text[w.end():]


def _numbers(text: str, rnd: random.Random) -> str:
    """Change the numbers and equipment tags (the request itself stays the same)."""
    text = re.sub(r"P-\d{4}[A-Z]", lambda m: rnd.choice(TAGS), text)

    def change(m):
        value = m.group(0)
        if "." in value:
            places = len(value.split(".")[1])
            return f"{float(value) * rnd.uniform(0.8, 1.2):.{places}f}"
        number = int(value)
        return value if number in (1, 2, 3) or number > 1000 else str(max(1, round(number * rnd.uniform(0.8, 1.2))))
    return re.sub(r"(?<![\w-])\d+(?:\.\d+)?(?![\w-])", change, text)


def _first_lower(text: str) -> str:
    return text[:1].lower() + text[1:] if text[:2] != text[:2].upper() else text


TRANSFORMS = {
    "lower": lambda t, r: t.lower(),
    "no punctuation": lambda t, r: re.sub(r"[?.!,;:]", "", t),
    "typo": _typo,
    "two typos": lambda t, r: _typo(_typo(t, r), r),
    "polite prefix": lambda t, r: r.choice(POLITE) + t,
    "suffix": lambda t, r: t.rstrip() + r.choice(SUFFIX),
    "Hinglish": lambda t, r: (r.choice(HINGLISH_PRE) + _first_lower(t)) if r.random() < 0.5
                             else t.rstrip("?.!") + r.choice(HINGLISH_SUF),
    "context sentence": lambda t, r: r.choice(CONTEXT) + t,
    "synonym": lambda t, r: _synonym(t, r),
    "numbers and tags": _numbers,
    "chat style": lambda t, r: t.replace("I'm", "im").replace("don't", "dont").replace("please", "pls")
                               .replace("Please", "pls").rstrip("?.!").lower(),
}


def _synonym(text: str, rnd: random.Random) -> str:
    options = [(a, b) for a, b in SYNONYMS if re.search(rf"\b{re.escape(a)}\b", text)]
    if not options:
        return text
    a, b = rnd.choice(options)
    return re.sub(rf"\b{re.escape(a)}\b", b, text, count=1)


def variants(seed_text: str, count: int, rnd: random.Random) -> list:
    """The seed itself plus count-1 different variants: (text, variant type)."""
    out, seen, tries = [(seed_text, "original")], {seed_text}, 0
    names = list(TRANSFORMS)
    while len(out) < count and tries < count * 40:
        tries += 1
        k = 1 if tries < count * 3 else rnd.choice([2, 2, 3])     # single changes first, then mixes
        chosen = rnd.sample(names, k)
        text = seed_text
        for name in chosen:
            text = TRANSFORMS[name](text, rnd)
        text = re.sub(r"\s+", " ", text).strip()
        if text and text not in seen:
            seen.add(text)
            out.append((text, "+".join(chosen)))
    return out


# ---------------------------------------------------------------------------
# 3. Build the files
# ---------------------------------------------------------------------------
def read_seeds(name: str, text_col: str, label_col: str) -> list:
    with open(HERE / name, encoding="utf-8", newline="") as f:
        return [(row[text_col], row[label_col], row["category"]) for row in csv.DictReader(f)]


def build(seeds: list, per_seed: int, out_name: str, text_col: str, label_col: str, prefix: str) -> list:
    rows = []
    for n, (text, label, category) in enumerate(seeds, start=1):
        rnd = random.Random(f"{prefix}{n}")               # deterministic per seed
        for text_v, kind in variants(text, per_seed, rnd):
            rows.append({"id": f"{prefix}{len(rows) + 1:05d}", "seed_id": f"{prefix}S{n:03d}",
                         text_col: text_v, label_col: label, "category": category,
                         "variant": kind, "seed_text": text})
    with open(HERE / out_name, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"{out_name}: {len(rows)} tests from {len(seeds)} seeds")
    return rows


def main():
    router_seeds = read_seeds("router_testset.csv", "task", "label") + EXTRA_ROUTER_SEEDS
    attach_seeds = read_seeds("attachment_testset.csv", "question", "should_warn") + EXTRA_ATTACHMENT_SEEDS
    router_rows = build(router_seeds, VARIANTS_ROUTER, "router_bigset.csv", "task", "label", "R")
    attach_rows = build(attach_seeds, VARIANTS_ATTACHMENT, "attachment_bigset.csv", "question", "should_warn", "A")
    # Search: the "label" is the section that answers the question.
    search_seeds = [(q, f"{doc}|{section}", "search") for q, doc, section in SEARCH_SEEDS]
    build(search_seeds, VARIANTS_SEARCH, "search_bigset.csv", "question", "answer_in", "Q")

    # A random sample for a PERSON to check the generated labels.
    rnd = random.Random("audit")
    sample = ([("router", r["id"], r["task"], r["label"]) for r in rnd.sample(router_rows, 100)] +
              [("attachment", r["id"], r["question"], r["should_warn"]) for r in rnd.sample(attach_rows, 100)])
    with open(HERE / "audit_sample.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["set", "id", "text", "generated_label", "your_label (fill in)", "agree? (yes/no)"])
        for row in sample:
            writer.writerow(list(row) + ["", ""])
    print("audit_sample.csv: 200 rows for a person to check")


if __name__ == "__main__":
    main()
