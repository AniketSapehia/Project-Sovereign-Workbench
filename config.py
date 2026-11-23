"""
config.py — ONE place for all settings of the Sovereign Workbench.

Why a separate config file?
    The problem statement says new open-weight models must be "addable later
    without redesigning the system". Keeping every model name here means that
    switching from a small laptop model to a big server model is a one-line
    change in this file — no other code needs to be touched.
"""

from pathlib import Path

# The folder this file lives in = the project folder. Every other path below
# is built from it, so the project works wherever you unzip it.
PROJECT_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# 1. Where the local model runtime (Ollama) lives
# ---------------------------------------------------------------------------
# Ollama runs as a background program on THIS computer and listens on port
# 11434. "127.0.0.1" is the "loopback" address: it always means "this same
# machine", so traffic to it never touches the network card or the internet.
OLLAMA_URL = "http://127.0.0.1:11434"

# How long (in seconds) we wait for a model to answer. Small models on a CPU
# are slow, and the very first request also has to load the model into RAM,
# so we are generous here.
OLLAMA_TIMEOUT_SECONDS = 600

# The model's "context window" in tokens (1 token ≈ 3/4 of an English word).
# This is how much text the model can look at in one go: system prompt +
# conversation + document excerpts + its own answer. 8192 tokens ≈ 6,000
# words. Bigger = can read more, but uses more RAM. On 8 GB RAM, 8192 is a
# safe value for a 3B model. Lower it to 4096 if your laptop struggles.
CONTEXT_TOKENS = 8192


# ---------------------------------------------------------------------------
# 2. The models the workbench knows about
# ---------------------------------------------------------------------------
# Each entry is a "role" (what the model is good at) mapped to the exact
# Ollama model name (the "tag"). The ROUTER (Phase 4, app/router.py) picks
# the right role for each task automatically.
#
# On an 8 GB RAM laptop we use small 4-bit models (~1-3 GB each).
# On MRPL's GPU server you would simply replace these names with larger
# models, e.g. "qwen2.5:32b" — nothing else changes.
#
# Adding a NEW model later = adding one entry here (and, optionally, a few
# example tasks for it in app/router.py). No other code changes.
#
# "think" switches a model's "thinking mode" (long hidden reasoning before
# answering) on or off. On a CPU, thinking is very slow, so we turn it off
# for models that have it (qwen3). None = don't send the setting (for models
# without it, like qwen2.5).
MODELS = {
    "general": {
        "name": "qwen2.5:3b",          # ~1.9 GB download. Fallback: "qwen2.5:1.5b"
        "purpose": "Conversation, summaries, documents, calculations, planning",
        # Phase 3.8 — to use the newer, stronger qwen3:4b (~2.5 GB):
        #   1. run:  ollama pull qwen3:4b
        #   2. change "qwen2.5:3b" above to "qwen3:4b"
        #   3. change None below to False
        "think": None,
    },
    # NEW IN PHASE 4: a model trained specifically on program code.
    # Download with:  ollama pull qwen2.5-coder:3b   (~1.9 GB)
    # Until it is downloaded, the router uses "general" for code tasks.
    "code": {
        "name": "qwen2.5-coder:3b",    # smaller option: "qwen2.5-coder:1.5b" (~1 GB)
        "purpose": "Writing, testing and fixing program code",
        "think": None,
    },
    # NEW IN PHASE 5: a VISION model, which can look at images.
    # Download with:  ollama pull qwen3-vl:2b   (~1.9 GB)
    # Better at documents but heavier: "qwen3-vl:4b" (~3.3 GB).
    # Until it is downloaded, images are read with OCR by the general model.
    "vision": {
        "name": "qwen3-vl:2b",
        "purpose": "Photos, drawings and scanned pages",
        "think": None,
    },
}

# Roles that MUST be installed for the workbench to work. Other roles are
# optional: if one is missing, the router falls back to DEFAULT_ROLE.
REQUIRED_ROLES = {"general"}

# The role used when nothing else is specified.
DEFAULT_ROLE = "general"

# NEW IN PHASE 2 — the EMBEDDING model.
# An embedding model does not write text. It turns a piece of text into a
# list of numbers (a "vector") that captures its MEANING. Texts with similar
# meaning get similar vectors — that is how we search documents by meaning
# instead of by exact keywords. nomic-embed-text is small (~274 MB).
EMBEDDING_MODEL = "nomic-embed-text"

# nomic-embed-text was trained to expect a short label in front of each text,
# telling it whether the text is a stored document or a search question.
# Using these labels noticeably improves search quality. If you switch to a
# different embedding model that doesn't use labels, set both to "".
EMBED_DOCUMENT_PREFIX = "search_document: "
EMBED_QUERY_PREFIX = "search_query: "


# ---------------------------------------------------------------------------
# 3. Documents, OCR and the knowledge base (NEW IN PHASE 2)
# ---------------------------------------------------------------------------
# Folder of the organisation's own manuals, SOPs and past correspondence.
# Put .pdf / .docx / .txt / .md / image files in here and click
# "Rebuild index" on the page.
KNOWLEDGE_BASE_DIR = PROJECT_DIR / "knowledge_base"

# Where the workbench keeps its own working files (uploads, search index).
# Everything stays inside the project folder, on this computer.
DATA_DIR = PROJECT_DIR / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
INDEX_FILE = DATA_DIR / "kb_index.npz"        # the numbers (vectors)
INDEX_META_FILE = DATA_DIR / "kb_chunks.json"  # the text pieces + sources

# File types we can read.
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md",
                        ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}

# Largest upload we accept, in megabytes.
MAX_UPLOAD_MB = 25

# Tesseract is the free OCR (Optical Character Recognition) program that
# reads text out of scanned pages and photos. On Windows the installer puts
# it at the path below. If you installed it somewhere else, change this line.
# If this file does not exist we fall back to "tesseract" on the PATH.
TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# OCR language(s). "eng" = English. Later you could use "eng+hin" for
# English + Hindi, after installing the Hindi language pack.
OCR_LANGUAGE = "eng"

# Scanned pages are turned into images at this resolution before OCR.
# 300 DPI is the standard for good OCR accuracy.
OCR_DPI = 300

# If a PDF page has fewer characters of real text than this, we treat it as
# a scanned page and run OCR on it.
MIN_TEXT_CHARS_PER_PAGE = 30

# How documents are cut into pieces ("chunks") for searching.
# ~900 characters ≈ one or two paragraphs. The overlap repeats the end of one
# chunk at the start of the next, so a sentence cut in half is not lost.
CHUNK_SIZE_CHARS = 900
CHUNK_OVERLAP_CHARS = 150

# How many of the best-matching chunks we give the model per question.
TOP_K = 4

# Chunks scoring below this similarity (0 to 1) are treated as "not
# relevant" and not shown to the model. The page shows each source's score
# ("match 62%"), so you can tune this: if unrelated sources keep appearing,
# raise it (e.g. 0.55); if relevant ones are missing, lower it (e.g. 0.40).
MIN_SIMILARITY = 0.45

# An attached document shorter than this is given to the model in full.
# A longer one is searched, and only the most relevant parts are given.
# 12,000 characters ≈ 3,000 tokens, which fits well in CONTEXT_TOKENS.
FULL_DOC_LIMIT_CHARS = 12000


# NEW IN PHASE 4 — the model ROUTER (see app/router.py).
# Every routing decision is saved here, one JSON line each. This is the
# data for evaluating the router later (e.g. a confusion matrix).
ROUTER_LOG_FILE = PROJECT_DIR / "data" / "router_log.jsonl"

# How much a clear keyword (e.g. "python", "function") adds to a role's
# similarity score. Similarity scores are 0-1, so 0.15 is a strong nudge.
ROUTER_KEYWORD_BOOST = 0.15

# Phase 6.16: TRAINED models (evaluation/train_models.py writes them here).
# If a file is missing or scikit-learn is not installed, the hand-written
# rules above are used instead. Set to False to switch a model off.
TRAINED_MODELS_DIR = PROJECT_DIR / "data" / "models"
USE_TRAINED_ROUTER = True        # logistic regression on the task's embedding
USE_TRAINED_ATTACHMENT = True    # TF-IDF + logistic regression on the question text


# NEW IN PHASE 5 — vision settings (see app/vision.py).
# Images are shrunk so their longest side is at most this many pixels before
# they are sent to the vision model: bigger images are much slower on a CPU.
VISION_MAX_IMAGE_SIDE = 1024
# For a PDF, how many pages are shown to the vision model (from page 1).
VISION_MAX_PDF_PAGES = 2
# Instructions for the vision model.
VISION_SYSTEM_PROMPT = (
    "You are the vision assistant of Sovereign Workbench, an on-premise AI for "
    "engineers at an Indian refinery. You are shown images (photos, engineering "
    "drawings or scanned pages), plus text that OCR read from them. Describe only "
    "what you can actually see. Use the OCR text to read labels and numbers, but if "
    "the image and the OCR text disagree, or something is unclear, say so. Never "
    "invent equipment tags, values or defects."
)


# NEW IN PHASE 6 — the flagship workflow (app/workflow.py): which SOPs hold
# the condition-monitoring limits and the approval rules. Change these IDs if
# your organisation's SOP numbers are different.
WORKFLOW_SOPS = {
    "condition_monitoring": "SOP-MECH-004",
    "approvals": "SOP-ADM-002",
}


# ---------------------------------------------------------------------------
# 4. The agent and its tools (NEW IN PHASE 3)
# ---------------------------------------------------------------------------
# The agent's own working folder. Every file it creates (Word, Excel, code,
# text) is saved here, and the Python sandbox may only WRITE inside it.
WORKSPACE_DIR = PROJECT_DIR / "workspace"

# The agent works in a loop: think -> use a tool -> look at the result ->
# think again. This caps the number of rounds, so a confused small model
# can never loop forever.
AGENT_MAX_STEPS = 6

# Tool results can be long (a whole document, big program output). We cut
# them to this many characters before showing them to the model, so they
# don't overflow its context window.
TOOL_RESULT_MAX_CHARS = 3000

# How tool calls are passed between the model and the workbench (Phase 3.4):
#   "prompt" - the tools are described in the model's instructions and WE
#              read the model's <tool_call> text, repairing small mistakes.
#              Nothing can be lost, and it works with any model. (default)
#   "native" - Ollama reads the tool calls. In testing, Ollama silently threw
#              away slightly broken tool calls from the 3B model, giving
#              empty replies. Fine for larger, more precise models.
TOOL_CALLING_MODE = "prompt"

# "Temperature" controls how random the model's word choices are
# (0 = always the most likely word, 1 = quite creative). Ollama's default
# for Qwen is about 0.7. Tool calls must follow an exact format, so the
# agent uses a low value: fewer broken tool calls, more consistent results.
AGENT_TEMPERATURE = 0.2

# Every reply the model gives in agent mode is saved here (one JSON line
# each), so problems like an empty reply can be diagnosed. Local file only.
AGENT_LOG_FILE = PROJECT_DIR / "data" / "agent_log.jsonl"

# Python sandbox limits: code is stopped after this many seconds, and only
# this much of its printed output is kept.
SANDBOX_TIMEOUT_SECONDS = 20
SANDBOX_MAX_OUTPUT_CHARS = 4000

# Instructions for the model when it works as an agent. Small models follow
# short, numbered, very explicit rules best.
AGENT_SYSTEM_PROMPT = (
    "You are Sovereign Workbench, an on-premise AI agent for engineers and "
    "officers at an Indian refinery. You run on local hardware; no data "
    "leaves the premises. You can call tools.\n"
    "Rules:\n"
    "1. Base your answer on the numbered document excerpts you are given and "
    "cite them like [1] or [2].\n"
    "2. For ANY arithmetic, unit conversion, or comparison of a number with "
    "limits or ranges, call run_python and print the result. Never do maths "
    "in your head.\n"
    "3. In run_python: first put every GIVEN input value in a variable, then "
    "compute EVERY result from those variables using the formulas written in "
    "the excerpts. Never type in a result you worked out yourself. Print each "
    "result with its formula. Your final answer must use the numbers from the "
    "code output; if the output looks wrong, fix the code and run it again.\n"
    "4. If a formula or limit you need is not in the excerpts, call "
    "read_document or search_documents BEFORE calculating. Never invent one.\n"
    "5. To make a Word file call create_word_document; for a table or "
    "spreadsheet call create_spreadsheet. To save Python code, call run_python "
    "with save_as (it is saved only if it runs). For other text, call "
    "write_text_file.\n"
    "6. When the task is done, reply WITHOUT calling a tool: give the answer, "
    "show calculation steps, and name any files you created.\n"
    "7. If something cannot be found or a tool fails, say so honestly.\n"
    "8. After calculating, check the excerpts for any limits, required "
    "actions or approvals that apply to your results, and state them.\n"
    "9. Text marked [unclear: ...] was read badly by OCR. NEVER guess or fill in "
    "such values: say they are unclear and must be checked on the original.\n"
    "10. Write formulas in plain text, e.g. rate = (t_original - t_actual) / years. "
    "Do not use LaTeX."
)


# ---------------------------------------------------------------------------
# 5. Sovereignty (no-data-leaves-the-machine) settings
# ---------------------------------------------------------------------------
# The ONLY hosts our own code is allowed to talk to. If any part of the app
# ever tries to contact another host, the request is blocked before it is
# sent (see app/llm.py) and the attempt shows up in red on the monitor.
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}

# Programs whose network connections the monitor watches. Our own Python
# process is always watched; we also watch every process whose name contains
# one of these words (Ollama runs as "ollama.exe" / "ollama app.exe").
WATCHED_PROCESS_KEYWORDS = ["ollama"]


# ---------------------------------------------------------------------------
# 6. Assistant behaviour (plain chat, without tools)
# ---------------------------------------------------------------------------
# The "system prompt" is a hidden instruction sent before the user's
# messages. It sets the assistant's role and tone.
SYSTEM_PROMPT = (
    "You are Sovereign Workbench, an on-premise AI assistant for engineers "
    "and officers at an Indian refinery. You run entirely on local hardware; "
    "no data leaves the premises. Answer clearly and concisely. When doing "
    "calculations, show the steps. If you are not sure, say so instead of "
    "guessing."
)
