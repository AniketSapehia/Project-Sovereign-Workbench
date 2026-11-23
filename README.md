# Sovereign Workbench — Phase 6 (Flagship Workflow + Evaluation)

SIH 2026 · Problem Statement **SIH26117** · Mangalore Refinery and Petrochemicals Limited (MRPL)

- **Phase 1:** local AI chat + live **Network Monitor** proving no data leaves the computer.
- **Phase 2:** documents, **OCR** for scanned pages, a local **knowledge base** with **citations**.
- **Phase 3:** the assistant becomes an **agent**. It plans, uses **tools** (a Python sandbox for
  calculations and code, document search, Word and Excel file creation) and iterates until the
  task is done, showing every step.
- **Phase 4:** a **model router** picks the right open-weight model for each task (a code model
  for programming, the general model for documents and calculations) and shows why.
- **Phase 5:** a **vision model** looks at photos, engineering drawings and scanned pages,
  together with their OCR text.
- **Phase 6:** the **flagship workflow** (scanned report → verified findings → SOP checks →
  approval note in Word) and an **evaluation script** that measures the whole system.

---

## What's in the project now

```
sovereign-workbench/
├── config.py            ← all settings (models, OCR, agent limits, allowed hosts)
├── requirements.txt     ← Python libraries (Phase 3 adds openpyxl)
├── run.bat              ← double-click to start
├── app/
│   ├── main.py          ← web server: page + API endpoints
│   ├── llm.py           ← talks to Ollama (chat, tool calling, embeddings); egress guard
│   ├── netmon.py        ← network monitor (now also watches the sandbox)
│   ├── documents.py     ← reads PDF/Word/text/images, OCR, chunking
│   ├── knowledge.py     ← knowledge base index, search, attachments
│   ├── router.py        ← Phase 4: picks the right model for each task
│   ├── vision.py        ← Phase 5: answers questions about images
│   ├── workflow.py      ← NEW (Phase 6): report → verified findings → approval note
│   ├── agent.py         ← the agent loop (think → use tool → observe → repeat)
│   ├── tools.py         ← NEW: the 6 tools the agent can use
│   └── sandbox.py       ← NEW: runs AI-written Python safely
├── static/              ← the web page (index.html, style.css, app.js)
├── knowledge_base/      ← the "company documents" (fictional samples)
├── samples/             ← test files: scanned report, photo, sample P&ID drawing
│   └── ground_truth/    ← NEW (Phase 6): exact text of the sample scans, for OCR scoring
├── evaluate.py          ← NEW (Phase 6): measures the system, writes results/eval_*.md
├── workspace/           ← NEW (created automatically): files the agent makes
└── data/                ← created automatically: uploads + search index
```

All documents in `knowledge_base/` and `samples/` are **fictional sample data** for
"Sample Refinery Ltd.", made for this prototype. Their values are illustrative only.

---

## Phase 6: Flagship workflow + Evaluation

### Part A: the flagship workflow (`app/workflow.py`)
The brief's example: *"reading a scanned inspection report, pulling out key findings and drafting an
approval note as a Word file."*

Testing in Phase 3 showed the 3B model, working freely, invents values, mixes up limits and claims
files it never made. So the flagship is a **guided workflow** following one rule:
**AI for language, code for numbers and rules.**

| Step | Done by | What happens |
|---|---|---|
| 📑 read_report | OCR | The attached scan is already read (Phase 2) |
| 🧾 extract_findings | **AI** | Fills a fixed **form** (JSON Schema) using Ollama **structured output**: it can only produce that form. Unreadable values must be `null`, not guessed |
| ✅ verify_values | **code** | Every extracted number must appear **on the right line** of the OCR text (e.g. a DE-bearing value on a "DE bearing" line). Otherwise: "not found in source" |
| 📏 read_sop_rules | **AI + code** | Zone limits, temperature limits and approval powers are read from SOP-MECH-004 and SOP-ADM-002 into forms; **every number must appear in the SOP**, or the rules are rejected. Cached in `data/rules_cache.json`, re-read automatically if an SOP changes |
| ⚖️ apply_rules | **code** | Zone of the highest *verified* reading, temperature alarm and rise checks, approving authority from the total cost |
| ✍️ draft_text | **AI + code** | Writes Subject, Background and Proposal from the verified facts; code flags any number in the text that isn't one of those facts |
| 📄 create_word_document | **code** | Builds the note in the SOP-ADM-002 format, plus a **Verification table** and "Points to check before signing" |

**Trigger:** attach a report and either click **📝 Draft approval note**, or type a message containing
"approval note". The 🧭 badge then says the guided workflow is used.

**Tested here** with a stand-in model that invented the unreadable NDE values (5.1, 2.7) and added
"within 15 days" to the proposal: both were caught and flagged, the invented values were left out of
the zone check, and the result was 6.4 mm/s → **Zone C**, DE bearing 86 °C → **ALARM** with an
18 °C rise to investigate, ₹1,25,000 → **Department Head**.

**Scope (say this honestly):** this workflow is designed for condition-monitoring inspection reports.
Other documents go through the agent. Adding another document type means adding another form and
its rules, not changing the design.

### Part B: the evaluation script (`evaluate.py`)
Run it from the project folder, with `.venv` activated and Ollama running:
```
python evaluate.py
```
- `python` runs a Python program; `evaluate.py` is the program.
- With no options it runs the quick tests (a few minutes): router, attachment check, OCR, search,
  sovereignty.

Options (each adds tests):
```
python evaluate.py --workflow
python evaluate.py --agent
python evaluate.py --all
```
- `--workflow` adds the flagship workflow on the sample report (a few minutes).
- `--agent` adds 4 agent tasks with known answers (slow on a CPU: about 2–4 minutes each).
- `--all` runs everything.

It writes `results\eval_<date>_<time>.md` (open it in any text editor or VS Code) plus a `.json`
copy. **Run it again after changing a model in `config.py`** (e.g. qwen3:4b at home) and compare.

| Section | How it's measured | Why it matters |
|---|---|---|
| Router | 30 labelled tasks, **different from** the router's example tasks → **confusion matrix**, accuracy, precision/recall/F1 | The brief's "auto selection across task types" |
| Missing-attachment check | 20 labelled questions → 2×2 confusion matrix | A safety check, tested |
| OCR | OCR output vs. the exact text printed on the samples (`samples/ground_truth/`) → character error rate (CER), word accuracy, key values read | How reliable the scanned-document reading is |
| Search | 12 questions with a known correct SOP section → hit@1, hit@4, MRR | Whether grounding finds the right paragraph |
| Workflow | 6 checks on the sample report | The flagship task, end to end |
| Agent | 4 tasks with known answers → success, steps, time | Agent reliability per model |
| Sovereignty | Network monitor totals during the whole run | Should be **0 external** |

**Terms used in the report:** *precision* = of the tasks the router sent to a model, how many were
right for it; *recall* = of the tasks that belonged to a model, how many it caught; *F1* = both
combined; *CER* = share of characters OCR got wrong; *hit@k* = the right section was in the top k
results; *MRR* = average of 1/rank of the right section (1.0 = always first).

**Test-set honesty:** the test sets are small and written by us, and the sample documents are
synthetic. Say so in the report, and present the numbers as a prototype evaluation, not a benchmark.

### Phase 6.1 update (first real run of the workflow)

**What went right with the real `qwen2.5:3b`:** with a fixed form to fill, the model left the
unreadable NDE values **empty** instead of inventing them (it had invented 5.1 and 2.7 in Phase 3).
Verification caught a genuine misreading (NDE temperature 71 °C; the report says 74 °C) and the
narrative's invented numbers ("104 °C", "Rs 12,500"). The SOP rules were read and verified, and
the decisions were right: Zone C, DE bearing ALARM, Department Head.

**Problems and fixes:**
1. The model labelled the DE row "Horizontal" (a column heading), so its correct values were wrongly
   flagged. **Fix:** if no line about the label holds most of the row's numbers, code finds the line
   that does and takes the correct label from it (🔧 shown in the verify step). The extraction
   instructions now also say the label is the first-column text.
2. Flagged numbers still appeared in the note's paragraphs. **Fix:** the AI gets one retry with the
   list of bad numbers; if they're still there, a **template built only from verified facts** is used.
   Unverified numbers never reach the document.
3. Smaller: no more "Approval Note: Approval Note for …" titles; "+" is kept in OCR text ("DE + NDE").

Replaying the real run with these fixes: 11 values verified, 2 unclear (the smudged NDE row), and only
the genuine misreading (71 °C) flagged.

**Phase 6.2:** in the second real run the model labelled the rows correctly by itself (12 values
verified), but listed **"Total" as a cost item**, giving two Total rows and a false "items add up to
Rs 2,50,000" warning. Code now removes a cost item named "Total" (🔧 shown in the verify step).

### Phase 6.3 update (lighter models and the guided code workflow)

**Models:** `qwen3:4b` made the 8 GB laptop lag badly, so it was removed (`ollama rm qwen3:4b`). The
setup is now `qwen2.5:3b` (general), **`qwen2.5-coder:1.5b`** (code, ~1 GB) and no vision model for now
(images use OCR). Ollama was set to keep only one model in memory (`setx OLLAMA_MAX_LOADED_MODELS 1`)
and to unload it after one minute (`setx OLLAMA_KEEP_ALIVE 1m`).

**What happened:** the router correctly sent a psi→bar task to the 1.5B code model, but the model
couldn't follow the agent's tool protocol. It echoed the tool descriptions and invented a fake tool
result, so nothing ran. Its formula was also wrong (÷ 6.89 instead of × 0.0689).

**Fix: a guided code workflow** (`app/codeflow.py`), following the same rule as the approval note:
1. The code model writes **one ```python block** with test calls that print results.
2. **The workbench runs that block in the sandbox.** The model never has to call a tool.
3. If it fails or prints nothing, the real error goes back to the model (up to 3 attempts in total).
4. Only code that ran **and** printed results is saved as a `.py` file, and the output shown is the
   sandbox's, never the model's claim.

**Honest limit:** the sandbox proves the code *runs*, not that its formula is *right*. That's why the
real output is always shown: a wrong result (like 14.5 bar for 100 psi) is easy to spot.

**Phase 6.4–6.5 (facts from documents, not from model memory):** both the 1.5B and the 3B code model
got the psi→bar factor wrong from memory (1.47 and 14.50 bar for 100 psi; correct 6.89). Fixes:
1. New reference data sheet in the knowledge base: `REF-ENG-001 Units and Conversion Factors`
   (pressure, temperature, flow, length, mass, power, energy), each with a check value.
2. Code tasks always search reference sheets (documents whose name starts with `REF-`), but still
   skip unrelated SOPs (`codeflow.REFERENCE_PREFIXES`, `knowledge.search_knowledge_base(only_prefixes=…)`).
3. The code model must use the documents' factor **and include a known-value test**, e.g.
   `assert abs(psi_to_bar(14.5038) - 1) < 0.001`. A wrong factor makes the test fail, the error goes
   back to the model, and it fixes the code. Tested: the wrong 0.145 factor was caught automatically
   and the second attempt printed 100 psi = 6.89 bar.
4. Code results no longer say "Verified": they say the code ran, and that its output has not been
   checked for correctness.

**Real results with the reference sheet (Phase 6.6–6.7):** with `REF-ENG-001` in the knowledge base,
`qwen2.5-coder:3b` got **100 psi = 6.89476 bar** and **1000 bbl/day = 6.62447 m³/h**, both citing the
right section of REF-ENG-001 and testing against its check values. The flow conversion was never
mentioned in any instruction, so it can only have come from the document. (6.6 removed an example from
the code instructions that had leaked the psi→bar answer; 6.7 strips the workbench's own notes from the
history so the model doesn't repeat them, and the evaluation now runs code tasks through the code workflow
and includes the flow-conversion test.)

**Phase 6.8 (full evaluation, 4 Nov):** code tasks 3/3 PASS with `qwen2.5-coder:3b`; Zone C check PASS;
workflow 6/6. The one FAIL (corrosion, "next inspection ~1.9") was most likely a grading error: the check
rejected "1.90". The checks now accept any precision (1.9, 1.90, 1.896) and still reject wrong values, and
the report now saves each agent answer so every result can be audited. Everything ran much slower
(workflow 514 s vs 82 s) because `OLLAMA_MAX_LOADED_MODELS=1` made Ollama unload the chat model each time
the small embedding model was used; set it to 2 (`setx OLLAMA_MAX_LOADED_MODELS 2`). Search hit@1 fell
from 92% to 83% because the new reference sheet's "Vibration velocity" section now outranks the SOP's
vibration zones for one question (still rank 2; hit@4 100%): adding documents can shift rankings.

**Phase 6.9 (evaluation of 5 Nov, with the saved answers):** reading the saved answers showed the
report's "4 of 5" was wrong, in both directions:
- *Corrosion calculation*: a **real** model error, not a grading error (the 6.8 guess was wrong). The agent
  did the maths in its head: "(9.1 − 8.0) / 0.29 = 4.0 years" (correct 3.79) and next inspection at the full
  life instead of half of it. Fix: if an answer shows arithmetic and no code ran, the agent is sent back
  once to calculate with `run_python`; if it still doesn't, the answer carries a ⚠ warning.
- *Coding task*: marked PASS, but the workbench had actually told the user "could not be made to run". The
  function was right; the model's own tests were wrong (`assert classify(2.1) == 2.1`), because the
  prompt only showed a numeric test. Fixes: the prompt now shows a text test too; the exact failing line is
  sent back with the error; and the evaluation only passes a code task if the tested code was saved.
- The variable list showed `tolerance = 0.0` for 1e-9; very small numbers now show as `1e-09`.

Honest score for 5 Nov: **3 of 5**.

**Phase 6.10 (evaluation of 5 Nov, 01:09):** 4 of 5, graded honestly. The corrosion task now passes for
real: the agent ran the calculation as code (remaining life 3.79 years, next inspection 1.90 years). The
coding task failed for a NEW reason: the reference sheet's "1 in/s = 25.4 mm/s" line was given to the code
model, which then converted every reading to in/s and compared it with limits in mm/s (4.5 mm/s became
Zone A). Fix: reference sheets are now only searched when the task is about units, conversion, factors or
constants, and the code prompt says not to convert units unless asked. Lesson: for a small model, an
unneeded fact is not harmless. It tends to use it.

Speed: allowing 2 loaded models made everything slower (agent tasks 1.6–2× slower, and even OCR, which does
not use Ollama, went from 18 s to 36 s). That points to the laptop running short of memory or power, not to
model reloading. Use `setx OLLAMA_MAX_LOADED_MODELS 1`, keep the charger plugged in, and close other
programs during evaluation.

**Phase 6.11 (evaluation of 5 Nov, 02:14):** the coding task now passes (6.10 worked), and OCR was back to
10.5 s with 1 loaded model. But psi→bar FAILED although its function was correct: the model tested
"14.5038 psi = 1 bar" with a tolerance of 1e-6, and the reference sheet's rounded factors give 1.0000022.
The model then explained the failure with a value it made up (0.9999999999999999). Fixes:
- When one of the AI's own tests fails, the sandbox prints the **real values of both sides**
  (`[Failed test] … = 2.2e-06`), so the model works from facts, not guesses.
- REF-ENG-001 now states its precision, and code tests use a **relative** tolerance (1e-4 of the expected
  value).
- The grader now checks only the model's **final answer** and **real code output**. Before, words could
  also come from document text a tool returned, or from text written before a tool call. This run's
  zone-check answer listed the wrong SOP requirements but still passed.

Across the three runs of 5 Nov (graded honestly), the five tasks passed 10 of 15 times. **Every run had
a failure, and they were not always in the same task** (only the flow conversion passed all three). Small models vary from run to run, so one run is not a reliable score.

**Phase 6.12 (router ROC curve):** the evaluation now saves every router task's scores (before, only the
wrong ones) and reports a **ROC AUC**. The score is *code similarity − general similarity*; the router
picks code when it is above 0. The ROC curve shows how well that score separates code tasks from general
tasks at every possible cut-off, and AUC sums it up as one number (1.0 = perfect, 0.5 = guessing). The
missing-attachment check has no ROC curve: it is a yes/no rule with no score, so it is a single point.

**Phase 6.13 (more evaluation measurements):** the evaluation now also records:
- **Search score distributions:** the similarity of *every* section to each test question, marked right or
  wrong. Shows whether the `MIN_SIMILARITY` cut-off (0.45) keeps the right sections and drops the rest.
- **OCR confidence calibration:** every word Tesseract reads, with its confidence, lined up against the true
  printed text (`difflib`) to mark it right or wrong. Shows how many real errors the `[unclear]` rule
  (confidence below 60) catches. Only switched on during evaluation (`documents.KEEP_OCR_WORDS`).
- **Speed and memory:** each chat model writes the same short answer twice. Ollama reports loading time,
  prompt-reading speed and writing speed (tokens per second). A background monitor samples laptop RAM,
  Ollama's RAM and CPU once a second, labelled by test stage.
- **Verification catch rate:** `--workflow-runs N` runs the approval-note workflow N times. Every value the
  AI extracted is compared with the true printed report: right and accepted, right but flagged (false
  alarm), wrong and caught, wrong and missed, or left blank because it was unreadable.

Quick run (about 3–5 minutes): `python evaluate.py`. With the catch rate: `python evaluate.py --workflow-runs 3`.

**Phase 6.14 (held-out test sets):** the first test sets (30 router tasks, 20 attachment questions) were
written while the router and the attachment rule were being built. They are now called the
**development set**: useful for finding bugs, but too small, and too familiar to the rules, for a final
score. Two larger **held-out** sets were written afterwards, with the rules frozen:
- `evaluation/router_testset.csv`: 200 tasks (100 general, 100 code) in 13 categories, including hard
  cases such as general questions that use code words ("the function of a balance line") and coding
  questions with no obvious keyword ("How do I read an Excel sheet into pandas?").
- `evaluation/attachment_testset.csv`: 100 questions (50 should warn, 50 should not) in 9 categories.

Both are plain CSV files (open them in Excel) so every label can be checked. Labelling rule for the
router: **code** = the task needs program code, a query or a formula to be written, fixed or explained;
everything else is **general**, including calculations, which the general model does with `run_python`.
Attachment questions are labelled as if they were the first message in a new chat.

Result of the attachment check on the held-out set: **64%** (100% on the development set). It almost never
raises a false alarm (1 of 50), but it misses 35 of 50 real cases, because it only recognises
"this/attached/uploaded" + a file word. Do not tune the rules to these files: if the rules change, write
a new held-out set to measure them honestly.

**Phase 6.15 (large generated test sets, about 11,000 tests):** made by two scripts in `evaluation/`:
- `generate_tests.py` takes hand-written **seed** questions (the held-out sets plus 50 router, 100 attachment
  and 60 search seeds) and rewrites each one 17–25 ways without changing what is asked: typos, Hinglish,
  polite or context phrases, synonyms, lower case, chat style, changed numbers. Result: `router_bigset.csv`
  (5,000 tests from 250 seeds), `attachment_bigset.csv` (5,000 from 200 seeds) and `search_bigset.csv`
  (1,020 from 60 seeds). It also writes `audit_sample.csv`: 200 random generated tests for a **person** to
  check, so the label error rate can be reported.
- `generate_ocr_tests.py` draws inspection-report pages with random values (so the true text is known),
  then damages them like real scans: rotation, blur, noise, faded contrast, low resolution, JPEG, smudges.

Run: `python evaluate.py --big --ocr-pages 300`. Routing uses `router.route_batch()`, which embeds 64
questions at a time; it uses the same `_scores()` function as the normal router (checked: identical
decisions on all 200 held-out tasks).

**Honest statistics:** variants of one seed are not independent (they tend to be all right or all wrong),
so 5,000 variants of 250 seeds are not 5,000 independent tests. The report therefore gives a 95%
confidence interval computed over **seeds** (a cluster bootstrap), and reports **robustness** (how often
rewording changes the answer) separately. For OCR, every value is classed as read correctly, marked
[unclear], lost, or **wrong without a flag**, which is the dangerous case.

**Phase 6.16 (trained classifiers):** the two weakest hand-written parts are replaced by models
**trained on this laptop, offline**, from the labelled data the project already had:
- **Router:** logistic regression on the task's nomic-embed-text vector (768 numbers). The router already
  computes this vector, so routing costs nothing extra. It replaces "closest example + keyword boost".
- **Attachment check:** logistic regression on TF-IDF features (words, word pairs and 3–5 letter pieces,
  which still match with typos and Hinglish). It replaces one regular expression.

Train with `pip install scikit-learn`, then `python evaluation/train_models.py`. That takes a few minutes:
5,000 embeddings are computed once and cached. Models are saved in `data/models/`; the training report is
`results/training_<date>.md`. The regularisation strength C is chosen by **grouped 5-fold
cross-validation**: all rewordings of one seed question stay in the same fold, so the model is never tested
on near-copies of its training questions. The **final test** is on `evaluation/fresh_router_testset.csv`
(118) and `fresh_attachment_testset.csv` (100): new questions in new styles, written **before** training.
`evaluate.py` compares the old rule and the trained model on them (report section 10). The older test sets
are now training data, so they are evaluated with the old rules only.

If a model file is missing or scikit-learn is not installed, the old rules are used automatically
(`app/trained.py`). Switch either model off in `config.py` with `USE_TRAINED_ROUTER` /
`USE_TRAINED_ATTACHMENT`.

**Why the language model itself was not fine-tuned:** fine-tuning a 3B model needs a GPU with 8–16 GB of
memory, and this laptop has none. Fine-tuning is also unreliable for teaching *facts*: SOP limits change,
and answers must quote their source, which is why the project uses retrieval (RAG) instead. A LoRA
fine-tune of a smaller model on a GPU is listed as future work.

### Install
1. Copy the files into your project and choose **Replace** (this replaces `config.py`; redo any model
   changes). Restart `run.bat`, press **Ctrl + F5**.
2. No new libraries and no new models are needed.

### Demo script for your presentation (about 8 minutes)
Each step maps to a line of the brief's "Expected Solution".

1. **Sovereignty first:** turn **Wi-Fi off**. Show the Network Monitor: **0 external connections**.
2. **Auto model selection across two task types:** with Model = Auto, ask
   `Summarise SOP-SAFE-007.` (🧭 general) and then
   `Write a Python function that converts psi to bar and test it on 100 psi.` (🧭 code). Point at
   the badges and click one to show the scores.
3. **Coding task run and verified in the sandbox:** in step 2's answer, open the 🐍 step and show the
   code, its printed test output, and "Tested code saved as …". Download the file.
4. **Multimodal task:** attach the scanned report, show the OCR message (confidence, hard-to-read
   words). Optionally attach the sample P&ID and ask what it shows (🧭 vision).
5. **The flagship end-to-end task:** with the report attached, click **📝 Draft approval note**. Walk
   through the 7 steps, especially ✅ verify_values. Open the Word file and show the Verification table
   and "Points to check before signing".
6. **Proof again:** the monitor still shows **0 external**, with Wi-Fi still off.
7. **Evidence:** show your `results\eval_*.md` (router confusion matrix, OCR accuracy, sovereignty).

---

## Phase 5: Vision

### What the problem statement asks
*"It needs to handle more than text too: scanned PDFs, handwritten notes, engineering drawings,
photographs, read through on device OCR and vision models."* The demo must include *"a multimodal
task involving image or scanned document understanding."*

### OCR vs. vision: why both
- **OCR (Phase 2)** turns pictures of **text** into text. Good at small print; blind to everything else.
- **A vision model** understands **what is in the picture**: equipment, layout, connections, damage.
  Small vision models are weak at reading small print.

On the sample drawing, OCR read most tags (T-2101, P-2105A/B, FCV-2110, H-2201, line numbers)
but **missed the instrument bubbles** (PI-2107, PI-2108, FT-2110) and could say nothing about
how the pumps are connected. So the workbench gives the vision model **the image plus its OCR
text**, and tells it to say when they disagree.

### What's new
| File | What it does |
|---|---|
| `app/vision.py` **(new)** | Prepares images (shrinks to 1024 px, JPEG, base64), renders the first 2 pages of PDFs, sends image + OCR text to the vision model |
| `config.py` | New `"vision"` role (`qwen3-vl:2b`) and vision settings |
| `app/router.py` | An attached image → vision; a PDF plus a question about a "drawing", "diagram", "photo" or "P&ID" → vision |
| `app/main.py` | Vision tasks go to `vision.py`; new endpoint for image previews |
| `app/documents.py` | OCR drops "words" made only of symbols (pieces of drawn lines, like `><—`) |
| `static/*` | Preview of attached images in the chat |
| `samples/SR-PID-2105 Sample P&ID crude charge pumps.png` | A fictional P&ID drawing to test with |

**Why no agent loop for vision?** Small vision models can't use tools reliably, so vision questions
are answered in one step. With a larger model on a GPU server, the agent could get a
"look at image" tool. That's future work.

### Install
1. Copy the files into your project and choose **Replace** (this replaces `config.py` again: redo any
   model changes you made, e.g. `qwen3:4b`). Restart `run.bat`, press **Ctrl + F5**.
2. **At home (needs a good connection):**
   ```
   ollama pull qwen3-vl:2b
   ```
   Downloads **Qwen3-VL 2B**, a 2-billion-parameter vision-language model from the Qwen 3 family:
   it takes images and text together. About 1.9 GB, suitable for an 8 GB laptop.
   If your laptop copes well, `qwen3-vl:4b` (~3.3 GB) is noticeably better at documents: change
   the name in `config.py` to use it.

Until the vision model is downloaded, image questions fall back to the general model with OCR
text (amber 🧭 badge), exactly as before.

### Try it (after downloading)
1. **New chat**, attach `samples\SR-PID-2105 Sample P&ID crude charge pumps.png`.
2. Ask: `What equipment is shown in this drawing and how are the pumps arranged?`
3. Check the 🧭 badge says **👁 vision model**. A good answer mentions the feed drum T-2101, two
   pumps P-2105A/B **in parallel** (one working, one standby) with suction and discharge valves and
   non-return valves, pressure gauges PI-2107/2108, flow transmitter FT-2110, control valve
   FCV-2110, and flow going to heater H-2201.
4. Also try a PDF: attach the scanned inspection report and ask
   `Describe the layout of this scanned page, like a drawing.` A PDF plus a word like "drawing",
   "diagram" or "photo" goes to the vision model. A normal question about the same PDF
   ("What are the vibration readings?") goes to the general model with OCR text, which is
   better for reading exact values.

**Speed:** the first vision answer loads the model (can take a minute); answers then take
roughly 30–120 s on a CPU, depending on image size.

---

## Phase 4: Model Router

### What the problem statement asks
*"Support multiple open weight models at once and automatically pick the right one for a given
task … a coding request handled differently from a document summary request. New open weight
models should be addable later without redesigning the system."* The demo must show *"model auto
selection across at least two different task types"*.

### What's new
| File | What it does |
|---|---|
| `app/router.py` **(new)** | Decides which model handles each task, and records why |
| `config.py` | New `"code"` model role (`qwen2.5-coder:3b`), `REQUIRED_ROLES`, router settings |
| `app/main.py`, `app/agent.py` | Every task is routed first; the chosen model then does the work |
| `app/llm.py` | Checks which models are installed |
| `static/*` | **Model** dropdown, 🧭 router badge on every answer, amber pill for optional models |

### How the router decides (`app/router.py`)
1. **Manual choice:** if you pick a model in the **Model** dropdown, it's used.
2. **Attachment type:** an attached photo goes to the vision model (from Phase 5).
3. **Meaning:** the task is turned into a meaning vector by `nomic-embed-text` and compared with
   ~10 **example tasks** per model ("Write a Python function…", "Summarise this report…").
   Each model's score is its similarity to its closest example (0 to 1).
4. **Keywords:** clear words ("python", "function", "script", "debug", "sql"…) add +0.15.
5. **Fallback:** if the winning model isn't installed, the general model is used, and the badge
   says so.

The highest score wins. Click the 🧭 badge on an answer to see every model's score.

**Why not ask the language model to classify the task?** That would add a whole extra model call
(tens of seconds on a CPU) to every message. One embedding call takes well under a second, and
the decision is explainable: you can see the scores.

**Why examples plus keywords?** Keywords alone miss tasks worded differently ("my script keeps
crashing" contains no word "code"); meaning-matching catches them. Keywords are a transparent nudge.

**Every decision is logged** in `data/router_log.jsonl` (question, scores, chosen model). This is
the data for evaluating the router later, e.g. accuracy and a **confusion matrix**.

### Install
1. Copy the files into your project and choose **Replace**. **This replaces `config.py`:** if you
   switched to `qwen3:4b`, redo that change (see "Trying qwen3:4b" below).
2. Restart `run.bat`, then press **Ctrl + F5** in the browser.
3. Download the code model (can be done later; until then code tasks use the general model):
   ```
   ollama pull qwen2.5-coder:3b
   ```
   `qwen2.5-coder` is Qwen 2.5 trained mainly on program code: better at writing and fixing code
   than a general model of the same size. About 1.9 GB. A smaller option is `qwen2.5-coder:1.5b`
   (about 1 GB): change the name in `config.py` if you use it.

### Try it
With **Model: Auto**, ask one task of each kind and look at the 🧭 badge:
- `Write a Python function that converts pressure from psi to bar, and test it.` → **code model**
- `Summarise SOP-SAFE-007.` → **general model**
- `My script keeps crashing with a KeyError, what could be wrong?` → **code model** (no word "code")

Before the code model is downloaded, the first and third show an amber badge: *"… not installed,
so the general model is used instead."*

**Memory note:** Ollama keeps a model in memory for about 5 minutes after use. Switching between
models means loading the other one (10–30 s the first time). On 8 GB RAM, Ollama unloads the
previous model automatically if memory runs short.

### Adding another model later (the brief's "addable later" requirement)
Add one entry to `MODELS` in `config.py` (a role name, the Ollama model name, a purpose), then add a
few example tasks for that role in `EXAMPLES` in `app/router.py`. Nothing else changes.

---

### Phase 4.1 update (first real router test)

**Router result:** 3 out of 3 tasks routed correctly (psi→bar function → code; summarise
SOP-SAFE-007 → general; "my script keeps crashing with a KeyError" → code, with no word "code").

**Problems found in the answers (model, not router):**
- The model **described test results it never ran**: it defined `psi_to_bar` without calling
  it, then wrote "the output of the function is 6.89475729".
- Code questions pulled in unrelated SOPs from the knowledge-base search.

**Fixes:**
1. If code runs but prints nothing, the sandbox result says *"nothing was printed, so the code
   was NOT tested. Do not claim any test results"*, the step shows **⚠ ran, but nothing
   tested**, and a file saved with `save_as` is reported as **UNTESTED** (`app/tools.py`).
2. Code tasks skip the general knowledge-base search; attached documents and documents named in
   the question are still loaded (`app/agent.py`, `app/knowledge.py`, `app/main.py`).

---

## Phase 3 upgrade reference (from Phase 2)

### Step 1 — Replace the project files
Unzip the new zip, copy everything inside its `sovereign-workbench` folder into your project
folder, and choose **Replace**. Your `.venv`, `data` (index + uploads) are kept.
If you had edited `config.py`, redo your change in the new one.

### Step 2 — Install the one new library
In a Command Prompt opened in your project folder:
```
.venv\Scripts\activate.bat
pip install -r requirements.txt
```
- The first command switches to the project's private Python (as before).
- The second reads `requirements.txt` and installs what's missing. Only **openpyxl** is new:
  the standard Python library for creating Excel `.xlsx` files. Everything else is skipped
  because it's already installed.

No new models and no new programs this time.

### Step 3 — Restart
Close the old `run.bat` window (Ctrl + C), double-click `run.bat`, refresh the page.
You should see a new **Agent mode** tick box in the chat panel, and a new **Output Files** panel.

---

## Try it (4 tests)

Agent tasks are slower than plain chat: on a CPU laptop expect **1–4 minutes**, because the
model thinks several times (once per step). Watch the **Agent steps** box fill in live.

**Test 1 — Calculation with steps shown (no attachment needed)**
> A pressure vessel shell was 12.0 mm thick when built in 2016. It measures 9.1 mm now, in 2026.
> The minimum required thickness is 8.0 mm. Using SOP-INSP-011, calculate the corrosion rate,
> remaining life and the next inspection date.

Correct results: corrosion rate **0.29 mm/year**, remaining life **≈ 3.8 years**, next inspection
within **≈ 1.9 years**, and because remaining life is under 4 years, SOP-INSP-011 requires an
approval note. Check that a **🐍 run_python** step appears with the code and its output.

**Test 2 — Coding task, run and verified in the sandbox**
> Write a Python function that classifies a vibration reading into Zone A, B, C or D as per
> SOP-MECH-004. Test it on 2.1, 4.5, 6.4 and 7.5, then save it as vibration_zones.py.

Correct results: 2.1 → A, 4.5 → B (the SOP says "up to 4.5"), 6.4 → C, 7.5 → D.
A `vibration_zones.py` download should appear.

**Test 3 — The Zone C retest (the Phase 2 failure)**
Attach `samples\IR-2026-118 Scanned Inspection Report.pdf`, then ask:
> Which vibration zone is the highest reading in this report, and what does SOP-MECH-004 require?

This time the model should compare the value **with code**, giving **6.4 mm/s → Zone C**
(weekly monitoring, corrective maintenance within 30 days). Take a screenshot for your
before/after slide.

**Test 4 — Word and Excel deliverables**
With the report still attached:
> Draft an approval note as a Word file for replacing the bearings of P-2105B, using the
> findings and cost estimate in this report.

then:
> Make a spreadsheet of the vibration readings in this report, with a column for the zone.

Download the files from the chat or the **Output Files** panel. The Word file has an automatic
**Sources** section and a "draft for review" notice.

All four still work with **Wi-Fi off**. The monitor stays at **0 external connections**.

### If the model doesn't use a tool
A 3B model sometimes answers directly instead of calling a tool. If a calculation has no
🐍 step, say so plainly: *"Use run_python to calculate this."* If it keeps failing, untick
**Agent mode** to get the faster Phase 2 behaviour for simple questions.

---

## How Phase 3 works (for your report and viva)

### The agent loop (`app/agent.py`)
```
                ┌──────────────────────────────────────────┐
  task + docs → │ THINK  model reads everything so far      │
                │   ├─ asks for a tool? ──► ACT: we run it  │
                │   │                         │             │
                │   │      OBSERVE: result goes back ◄──┘   │
                │   │      (repeat, max 6 steps)            │
                │   └─ no tool call? ──► FINAL ANSWER       │
                └──────────────────────────────────────────┘
```
This pattern is called **ReAct** (Reason + Act). The model never runs anything itself. It
only *asks* for a tool, in a structured format (**tool calling**, supported by Ollama). Our code
decides whether and how to run it.

### The 6 tools (`app/tools.py`)
| Tool | What it does |
|---|---|
| 🔎 `search_documents` | Searches the knowledge base + attachments; new excerpts get citation numbers |
| 📖 `read_document` | Reads a whole document by name, e.g. "SOP-INSP-011" |
| 🐍 `run_python` | Runs code in the sandbox: all calculations, comparisons and code tests |
| 📝 `write_text_file` | Saves code, text, CSV or JSON |
| 📄 `create_word_document` | Builds a `.docx` with headings, bullets, tables, Sources, review notice |
| 📊 `create_spreadsheet` | Builds an `.xlsx`, with numbers as numbers and `=SUM(...)` as real formulas |

### The Python sandbox (`app/sandbox.py`)
AI-written code runs in a **separate Python process** with a **20-second time limit**, in
**isolated mode**, and with an **audit hook**: a Python feature that checks sensitive actions
before they happen. The hook blocks network access, starting other programs, and writing or
deleting files outside `workspace/`. In testing, attempts to open a web page, open a socket,
run `os.system`, write to another folder, or delete `config.py` were all blocked, while normal
calculations, NumPy and saving files in the workspace worked.

*Honest limitation:* this is a prototype-grade sandbox. In production, the same tool would
run code inside a locked-down container or virtual machine with no network card.

### Design decisions you can explain
1. **"AI for language, code for numbers."** In Phase 2 testing the model read 6.4 mm/s correctly
   but called it Zone D. Now every comparison and calculation goes through `run_python`, so the
   arithmetic is done by code and shown to the user.
2. **Pre-loaded evidence.** Relevant excerpts are given to the model *before* the loop starts, so
   a small model needs fewer steps. Tools are there for anything extra.
3. **Guard rails for a small model:** max 6 steps; the identical call twice in a row is refused;
   tool errors go back to the model so it can fix its input; tool calls written as plain text are
   still recognised.
4. **Transparency:** every step (code, inputs, outputs) is visible on the page. Nothing is hidden.
5. **Traceable documents:** Word files list the sources they were based on and flag OCR'd sources
   for verification.
6. **Safe files:** file names chosen by the AI are cleaned (`../../x.docx` becomes `x.docx`),
   nothing is overwritten, and downloads only work for files inside `workspace/`.
7. **Sovereignty still holds:** the sandbox is a child process of the workbench, and the
   Network Monitor now watches it too (shown as `sandbox (python)`).

---

## Phase 3.1 update (fixes from the first real test)

**What happened:** asked to calculate remaining life "using SOP-INSP-011", the real `qwen2.5:3b`
called `run_python` correctly, but answered **10 years** instead of **≈ 3.8 years**. The sources
list showed why: search had returned the SOP's title, purpose, method and acceptance sections, but
**not** sections 4–6 with the formulas, so the model invented a formula. It also typed its own
result (`corrosion_rate = 0.29`) into the code instead of calculating it.

**Fixes:**
1. **Named documents are loaded in full** (`knowledge.gather_sources`). If a question mentions a
   document ID such as `SOP-INSP-011` or `MEMO-2026-031`, that whole document is given to the
   model (or its best parts, if it's very long). Search is still used for everything else.
2. **Stricter calculation rules** (`AGENT_SYSTEM_PROMPT` in `config.py`): put given inputs in
   variables, compute every result from them using the documents' formulas, and look up any
   missing formula before calculating.
3. **No LaTeX:** formulas are written in plain text, so the steps display cleanly.

Lesson for the report: the model's mistake was visible only because the answer lists its
sources. Check the sources first whenever an answer looks wrong.

---

## Phase 3.8 update (false claims, and trying a stronger model)

**What happened:** asked for a spreadsheet, the model produced wrong values again, invented
"previous instructions", called `create_word_document` with no content (✗ failed), and then
**claimed** it had created `Inspection_Report_P-2105B.docx`. That claim was false.

**Assessment:** after seven rounds of fixes, the workbench's own machinery (sandbox, parser,
retries, OCR markers, guards) is solid, but each test fails in a new way that comes from the
model's reasoning. That is the limit of a 3-billion-parameter model as an agent.

**Fixes:**
1. **False file-claim check** (`app/agent.py`): every file name mentioned in the final answer is
   compared with the files the tools actually created (and with the known documents). Anything
   else gets a warning: *"⚠ Check: this answer mentions X, but no such file was created in this task."*
2. **Support for "thinking" models** (`config.py`, `app/llm.py`): a per-model `"think"` setting
   (sent to Ollama only when set), and any `<think>…</think>` reasoning blocks are filtered out of
   the answer.

### Trying qwen3:4b (newer, stronger, ~2.5 GB)
1. Command Prompt: `ollama pull qwen3:4b` (downloads the model once).
2. In `config.py`, under `"general"`: change `"qwen2.5:3b"` to `"qwen3:4b"`, and `"think": None`
   to `"think": False` (thinking mode is very slow on a CPU).
3. Restart `run.bat`. To go back, undo both changes.

This one-line model swap is exactly what the brief asks for: "new open weight models should be
addable later without redesigning the system".

---

## Phase 3.7 update (fixes from Tests 3 and 4)

**Results:** Test 3 gave the correct answer (**6.4 mm/s → Zone C**, the question it got wrong in
Phase 2). Test 4 created the Word file, but the spreadsheet showed three problems: the model wrote
the same tool call **14 times in one reply**; the NDE row had **invented values** (`3.6 4.1 3.2`
instead of `5.1 3.6 2.7`) because OCR couldn't read that row; and there was no zone column.

**Fixes:**
1. **Unclear OCR words are marked in the text** (`app/documents.py`): words Tesseract read with
   low confidence appear as `[unclear: aya]`. A new rule tells the model never to guess such values
   and to say they must be checked on the original. On the sample report this marks exactly the
   misread NDE row: `NDE bearing [unclear: aya] 3.6 [unclear: PAST]`. (Re-attach documents
   uploaded before this update to get the markers.)
2. **One tool call per reply** (`app/agent.py`): only the first is run, and the model is reminded
   to call one tool at a time.
3. **Search skips documents already loaded in full** (`app/tools.py`), so the sources list isn't
   cluttered with duplicates (Test 3 had 11 sources, mostly repeats).

**Lesson for the report:** a model inventing a plausible number is more dangerous than an obvious
error. Marking uncertainty explicitly, instead of hiding it, is a core design choice of the workbench.

---

## Phase 3.6 update (saving tested code)

**What happened:** Test 2's coding part **passed**: the function was correct and its tests in the
sandbox gave 2.1 → A, 4.5 → B, 6.4 → C, 7.5 → D. But saving the file failed: the model called
`write_text_file` without the content, then tried a broken workaround, repeated itself, and ran
out of steps. Its last tool call also had a stray comma and an extra `}`.

**Fixes:**
1. **`run_python` has a new `save_as` option** (`app/tools.py`). The model writes the code once;
   the sandbox runs it and, **only if it runs without errors**, saves that exact code to the file.
   This is easier for a small model and more trustworthy: the saved file is exactly the tested code.
   `write_text_file` is now described as being for non-code text.
2. The parser accepts any number of closing braces (`app/agent.py`).
3. If the agent reaches the step limit, any tool-call text in its last reply is removed instead of
   being shown as the answer (`app/agent.py`).
4. The page shows "save as …" on 🐍 steps that save a file (`static/app.js`).

---

## Phase 3.5 update (tool calls wrapped in junk)

**What happened:** on the Test 2 retry, the first step worked. The model then noticed nothing was
printed and decided to add `print()` calls, a good self-correction. But it wrapped its next tool
call in round brackets: `<tool_call>( {...} )</tool_call>`. The parser didn't recognise it, so
the raw tool call was shown as the "final answer".

**Fixes** (`app/agent.py`):
1. Inside a tool-call tag, the parser keeps only the text from the first `{` to the last `}`,
   ignoring brackets, "json" labels and other stray characters.
2. If a reply contains `<tool_call>` but still can't be read, it is **never treated as the
   answer**. The model is told its tool call couldn't be read, shown the correct format, and gets
   up to 2 retries.

---

## Phase 3.4 update (reading tool calls ourselves)

**What happened:** on Test 2 the model generated **295 tokens**, but the workbench received an
empty reply. Ollama's built-in tool calling had silently thrown the reply away. The most likely
reason: the model's tool call wrapped Python code containing `"Zone A"` inside JSON without
escaping the quotes (`\"`), so the JSON was invalid and Ollama discarded it.

**Fix: "prompt" tool-calling mode** (`TOOL_CALLING_MODE` in `config.py`, `app/agent.py`):
1. The tools are described in the model's instructions **by the workbench**, in exactly the format
   Qwen was trained on (`<tool_call>{"name": ..., "arguments": ...}</tool_call>`).
2. Ollama returns the model's **raw text**, so nothing can be discarded.
3. A **forgiving parser** reads the tool calls. If the JSON is broken, it repairs it by locating
   each argument by name (e.g. everything between `"code": "` and the closing `"}}`), then
   converts `\n` and `\"` back into real characters.
4. Tool results go back to the model as `<tool_response>…</tool_response>` messages (again
   Qwen's own format), and the raw tool-call text is never shown on the page; it appears as a step.

Tested with broken tool calls (unescaped quotes, raw line breaks, missing closing tag): all were
repaired and ran correctly. Bonus: this works with open-weight models that **don't support
Ollama's tool calling at all**, so the workbench is less tied to one model. `"native"` mode is
still available for larger models.

---

## Phase 3.3 update (empty replies)

**What happened:** on the third test the model returned **nothing**: no text and no tool call.
Most likely it tried to call a tool in a broken format (e.g. code with line breaks inside the tool
request), which Ollama could not read, or it simply stopped.

**Fixes:**
1. **Retry once** (`app/agent.py`): on an empty reply, the agent asks the model to continue
   ("call ONE tool, or give your final answer") instead of giving up.
2. **Lower temperature for the agent** (`AGENT_TEMPERATURE = 0.2` in `config.py`). Temperature
   controls how random the model's word choices are. Tool calls need an exact format, so less
   randomness means fewer broken calls.
3. **More forgiving tool-call reading:** tool calls written as text are now accepted even with
   raw line breaks inside the code (`json.loads(..., strict=False)`).
4. **Diagnostic log** (`data/agent_log.jsonl`): every model reply in agent mode is saved locally:
   its text, tool calls, number of tokens generated (`eval_count`) and why it stopped. If a reply
   is empty but `eval_count` is large, the model *did* write something that couldn't be read.

---

## Phase 3.2 update (fixes from the second real test)

**What happened:** with the whole SOP loaded, the final numbers were correct (0.29 mm/yr,
≈ 3.79 years, ≈ 1.9 years), but the **code itself was wrong** (it divided by the wrong variable,
giving `corr_rate = 10`), and it **printed nothing**, so the model ignored the code and did the
maths in its head. It also missed the SOP rule that remaining life under 4 years requires an
approval note, and still wrote LaTeX.

**Fixes (in code, because small models often ignore instructions):**
1. **Automatic results in the sandbox** (`app/sandbox.py`): after the code runs, every simple
   variable is listed with its value, under `[Variables after running]`. If the last line is just
   a value, it is shown too, as in a Jupyter notebook. A forgotten `print()` no longer hides the
   results, and a wrong intermediate value is visible to both the user and the model.
2. **LaTeX converted to plain text** on the page (`plainMath` in `static/app.js`) and in Word files
   (`plain_math` in `app/tools.py`): `\frac{9.1 - 8.0}{0.29}` → `(9.1 - 8.0) / (0.29)`.
3. **Two more agent rules** (`config.py`): the final answer must use the numbers from the code
   output (fix and re-run the code if they look wrong); and after calculating, check the documents
   for limits, required actions or approvals that apply to the results.

---

## Phase 2 setup reference (already done)

You keep your existing `.venv` folder; we only replace the code and add two things.

### Step 1 — Copy the new files into your project

1. Unzip the new `sovereign-workbench.zip` somewhere temporary, e.g. your Downloads folder.
2. Open the unzipped `sovereign-workbench` folder, select **everything inside it** (Ctrl + A), and copy (Ctrl + C).
3. Open your project folder
   `C:\Users\sapeh\Desktop\Project Sovereign Workbench\sovereign-workbench`
   and paste (Ctrl + V). When Windows asks, choose **Replace the files in the destination**.

> If you had changed `config.py` (for example to use `qwen2.5:1.5b`), make that change again
> in the new `config.py`, because it was replaced.

### Step 2 — Install Tesseract OCR

Tesseract is a free program that reads text from images. It runs fully offline.

1. Search the web for **"Tesseract UB Mannheim"** and open the GitHub page `github.com/UB-Mannheim/tesseract/wiki`.
   (UB Mannheim, a German university library, publishes the standard Windows build of Tesseract.)
2. Download the latest **64-bit installer** (`tesseract-ocr-w64-setup-....exe`) and run it.
3. Keep the default install folder: `C:\Program Files\Tesseract-OCR`.
   English is included by default; you don't need extra languages now.
4. That's it. You don't need to add it to PATH, because `config.py` already points to
   `C:\Program Files\Tesseract-OCR\tesseract.exe`.

### Step 3 — Download the embedding model

Open Command Prompt and run:
```
ollama pull nomic-embed-text
```
About 274 MB. This small model turns text into "meaning vectors" for searching documents.

### Step 4 — Install the new Python libraries

Open Command Prompt **in your project folder** (address bar → `cmd` → Enter), then:
```
.venv\Scripts\activate.bat
pip install -r requirements.txt
```
It should end with **Successfully installed …** (python-multipart, numpy, pypdf, pypdfium2, python-docx, pytesseract, pillow).

### Step 5 — Start and check

1. Double-click **run.bat**, open **http://127.0.0.1:8000**.
2. The top bar should show three green pills:
   - `general: qwen2.5:3b · embedding: nomic-embed-text`
   - `OCR: Tesseract 5.x`
   - `● 0 external connections`
3. In the **Knowledge Base** panel, click **Rebuild index**. After 10–60 seconds it should say
   **5 file(s), 31 chunks**.

---

## Phase 2 tests

**Test 1 — Ask the knowledge base**
> What is the vibration limit for Zone D on a centrifugal pump, and what must be done?

The answer should mention 7.1 mm/s with markers like **[1]**. Click a marker to see the
exact SOP paragraph it came from, listed under **Sources used for this answer**.

**Test 2 — Attach a scanned report (OCR)**
1. Click **📎** and choose `samples\IR-2026-118 Scanned Inspection Report.pdf`.
2. After a few seconds, the chat reports: 2 pages, read with OCR, confidence ~90%+,
   and how many words were hard to read.
3. Ask: **Summarise this report.**

**Test 3 — Combine the report with the SOPs**
With the report still attached and "Use knowledge base" ticked, ask:
> Which vibration zone is the highest reading in this report, and what does SOP-MECH-004 require?

The answer should use the report (6.4 mm/s) **and** the SOP (Zone C: plan corrective
maintenance within 30 days). Check the sources list: it shows which came from OCR.

Then repeat any test with **Wi-Fi off**. Everything still works, and the monitor stays at
**0 external connections**.

**Adding your own documents:** copy `.pdf`, `.docx`, `.txt`, `.md` or image files into the
`knowledge_base` folder and click **Rebuild index**.

---

## Speed and limits (be honest about these in your report)

- **Rebuild index:** ~10–60 s for the sample files on a CPU. Scanned PDFs take longer (OCR).
- **OCR:** roughly 2–5 seconds per scanned page.
- **Answers with documents** take longer than plain chat, because the model reads the excerpts first.
- **Small models make mistakes.** A 3B model sometimes cites the wrong number or misses a detail.
  That's exactly why every answer shows its sources: the officer can check.
- **OCR is not perfect.** Smudged or noisy table cells can be misread. The workbench reports the
  confidence and the number of hard-to-read words, instead of hiding it.
- **Handwriting:** Tesseract reads printed text well but handwriting poorly. Phase 5 (vision model) helps here.
- **Follow-up questions** like "what about the second one?" search poorly, because the search uses
  only the latest question. Ask full questions for best results.

---

## Troubleshooting (Phase 2)

| Problem | Fix |
|---|---|
| **OCR: Tesseract not found** | Install Tesseract (Step 2). If you installed it elsewhere, put its `tesseract.exe` path in `TESSERACT_CMD` in `config.py`. Restart run.bat. |
| **Model missing: nomic-embed-text** | Run `ollama pull nomic-embed-text`. |
| `ModuleNotFoundError: No module named 'numpy'` (or pypdf, docx…) | You skipped Step 4. Activate `.venv` and run `pip install -r requirements.txt`. |
| `Form data requires "python-multipart"` | Same as above: `pip install -r requirements.txt`. |
| Rebuild index shows a file in **red** | That file couldn't be read (damaged, password-protected PDF…). The rest still works. |
| Irrelevant sources keep appearing | Raise `MIN_SIMILARITY` in `config.py` (e.g. 0.55). Each source shows its "match %" to help you tune. |
| Relevant SOP not found | Lower `MIN_SIMILARITY` (e.g. 0.40), or ask using words closer to the SOP's wording. |
| Answers very slow / laptop freezing | Lower `CONTEXT_TOKENS` in `config.py` to 4096, or switch to `qwen2.5:1.5b`. Close browser tabs. |
| Rebuild says "folder changed, rebuild needed" | You added or removed files in `knowledge_base`. Click **Rebuild index**. |
| Old Phase 1 commands (Ollama not running, port in use, etc.) | See the table in the Phase 1 section below. |

---

## How Phase 2 works (for your report and viva)

### The idea: RAG (Retrieval-Augmented Generation)

A small model doesn't know your refinery's SOPs. Instead of retraining it, we **look up the
relevant paragraphs and hand them to the model with the question**. Three steps:

```
1. INDEX (once, "Rebuild index")
   knowledge_base/*  →  extract text (OCR if scanned)  →  cut into chunks (~900 chars)
                     →  embedding model turns each chunk into a vector  →  saved in data/

2. RETRIEVE (every question)
   question  →  vector  →  compare with all chunk vectors (cosine similarity)  →  best 4 chunks

3. GENERATE (every question)
   "[1] SOP-MECH-004, section 5: …  [2] …  Question: …"  →  qwen2.5:3b  →  answer with [1], [2]
```

### What happens when you attach a scanned PDF

```
PDF page has a text layer?  ── yes ──►  read text directly (fast, exact)
        │ no (it's a scanned image)
        ▼
render page at 300 DPI  →  greyscale + contrast  →  erase table lines  →  Tesseract OCR
        →  text + confidence score + count of uncertain words
```

- **Short document** (under ~12,000 characters): the model gets the whole thing, page by page.
- **Long document:** it's searched like the knowledge base, and only the relevant parts are sent.

### Key design decisions you can explain

1. **Grounding with citations.** Every answer shows the exact excerpts it used, with file, page
   and section. An officer can verify any claim in seconds. This is part 3 of our USP.
2. **Honest OCR.** We report OCR confidence and hard-to-read words instead of presenting OCR text
   as perfect. In a refinery a misread number matters.
3. **Table-line removal before OCR.** Tesseract skipped entire table rows in our scanned sample
   report. Erasing ruled lines first (a small NumPy routine in `documents.py`) recovered them.
   We also tested a de-speckle filter, found it turned "86" into "386", and **removed it**.
   This is a good example of testing-driven decisions to mention in the viva.
4. **No vector database.** A NumPy matrix handles thousands of chunks in milliseconds, needs no
   extra service, and adds no third-party code that could send telemetry. A self-hosted vector
   database is the upgrade path for millions of chunks on the server.
5. **Everything through the guarded client.** Embedding calls use the same egress guard and are
   logged in the Network Monitor (you'll see `POST 127.0.0.1/api/embed`).
6. **Safe file handling.** Only allowed file types, a size limit, uploads stored under random IDs,
   and IDs validated so a crafted request can't read other files on the computer.
7. **Configurable, model-agnostic.** Chunk size, number of results, similarity threshold, OCR
   language and models are all in `config.py`.

---

## Phase 2.1 update (two fixes from testing)

**1. Missing-attachment warning** (`app/main.py`, `static/app.js`)
In testing, we asked about "this report" after the report had been removed from the chat. The
model never saw the report, but answered anyway and got it wrong (it said Zone D; the correct
answer is Zone C). Small models rarely admit missing information, so the check now happens in
**code, before the model runs**: if the question mentions "this report", "the attached file",
"this document", etc. and nothing is attached, the workbench shows a yellow warning and asks
you to attach the file. It uses a *regular expression* (a text pattern) to spot those phrases.

**2. Grouped Network Monitor log** (`app/netmon.py`, `static/app.js`)
The page checks model status every 15 seconds, which filled Layer 1 with identical
`GET /api/tags` lines and pushed the important ones (`/api/embed`, `/api/chat`) out of view.
Identical requests in a row are now shown as one line with a count, e.g.
`12:10:05–12:13:20  GET 127.0.0.1/api/tags ×14  ✓ local`.
Nothing is hidden: every request is still counted in the totals, and any different request,
or any blocked request, always appears on its own line.

**Known limitation found in testing (fixed in Phase 3):** with the report attached, the model
read the value correctly (6.4 mm/s) but failed to compare it with the SOP's zone ranges. Phase 3
moves such comparisons and calculations into Python tools, so the model only extracts values and
writes text, and code does the maths.

---

## Phase 1 reference

### Every time you start your PC
1. Check Ollama is running (llama icon near the clock; otherwise open **Ollama** from the Start menu).
2. Double-click **run.bat**.
3. Open **http://127.0.0.1:8000**. Stop with **Ctrl + C** in the black window.

### Phase 1 troubleshooting

| Problem | Fix |
|---|---|
| `python` is not recognized | Reinstall Python and tick **"Add python.exe to PATH"**, or use `py` instead of `python`. |
| Page shows **"Ollama not running"** | Open Ollama from the Start menu, wait 10 s, refresh the page. |
| Page shows **"Model missing: qwen2.5:3b"** | Run `ollama pull qwen2.5:3b`. |
| `Activate.ps1 cannot be loaded` in PowerShell | Use **Command Prompt** (`cmd`) instead. |
| Port 8000 already in use | Edit `run.bat`, change `--port 8000` to `--port 8001`, open `http://127.0.0.1:8001`. |
| Layer 2 says "Permission denied" | Right-click `run.bat` → **Run as administrator**. |
| Monitor shows something red | Most likely Ollama checking for updates. Demo with Wi-Fi off, and explain it honestly: the monitor caught it, which proves the monitor is real. |

### Phase 1 design decisions
Local-only binding (`127.0.0.1`), egress guard that blocks non-local requests before they are
sent, two-layer monitor (app log + Windows connection scan), config-driven models, streaming
answers, no internet assets in the page, and safe rendering of model output.

---

## What comes next

| Phase | Adds |
|---|---|
| ~~1~~ | ~~Local chat + network monitor~~ ✔ |
| ~~2~~ | ~~Documents, OCR, knowledge base with citations~~ ✔ |
| ~~3~~ | ~~Agent loop + tools: Python sandbox, Word/Excel output~~ ✔ |
| ~~4~~ | ~~Automatic model router (general / code / vision)~~ ✔ |
| ~~5~~ | ~~Vision: photos and engineering drawings~~ ✔ |
| ~~6~~ | ~~Flagship demo: scanned report → findings → SOP check → approval note (.docx)~~ ✔ (+ evaluation) |
