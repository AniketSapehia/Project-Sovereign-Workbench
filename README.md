# Sovereign Workbench — Phase 2 (Documents, OCR, Knowledge Base)

SIH 2026 · Problem Statement **SIH26117** · Mangalore Refinery and Petrochemicals Limited (MRPL)

- **Phase 1** gave you a local AI chat plus a live **Network Monitor** proving no data leaves your computer.
- **Phase 2** adds: attaching documents (PDF, Word, text, images), **OCR** for scanned pages,
  and a local **knowledge base** of SOPs and memos that the assistant searches and **cites** in its answers.

---

## What's in the project now

```
sovereign-workbench/
├── config.py            ← all settings (models, OCR, chunk sizes, allowed hosts)
├── requirements.txt     ← Python libraries (Phase 2 adds 7 new ones)
├── run.bat              ← double-click to start
├── app/
│   ├── main.py          ← web server: page + API endpoints
│   ├── llm.py           ← talks to Ollama (chat + embeddings); egress guard
│   ├── netmon.py        ← network monitor (2 layers of proof)
│   ├── documents.py     ← NEW: reads PDF/Word/text/images, OCR, chunking
│   └── knowledge.py     ← NEW: knowledge base index, search, attachments
├── static/              ← the web page (index.html, style.css, app.js)
├── knowledge_base/      ← NEW: the "company documents" the assistant searches
│   ├── SOP-MECH-004 Centrifugal Pump Condition Monitoring.md
│   ├── SOP-INSP-011 Pressure Vessel Thickness Inspection.md
│   ├── SOP-ADM-002 Approval Notes and Delegation of Powers.md
│   ├── SOP-SAFE-007 Hot Work Permit.docx
│   └── MEMO-2026-031 Seal failures on P-2105.txt
├── samples/             ← NEW: test files to attach in the chat
│   ├── IR-2026-118 Scanned Inspection Report.pdf   (scanned, no text layer → OCR)
│   └── IR-2026-118 page1 photo.jpg                  (photo of page 1 → OCR)
└── data/                ← created automatically: uploads + search index
```

All documents in `knowledge_base/` and `samples/` are **fictional sample data** for
"Sample Refinery Ltd.", made for this prototype. Their values are illustrative only.

---

## Upgrading from Phase 1 (step by step)

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

## Try it (3 tests)

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

## Troubleshooting

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
| 3 | Agent loop + tools: file read/write, Python sandbox, Word/Excel output |
| 4 | Automatic model router (general / code / vision) |
| 5 | Vision: photos and engineering drawings |
| 6 | Flagship demo: scanned report → findings → SOP check → approval note (.docx) |
#   P r o j e c t - S o v e r e i g n - W o r k b e n c h  
 