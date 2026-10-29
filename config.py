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
# Ollama model name (the "tag"). Right now we only chat with "general".
# Later phases add "code" and "vision", and the router (Phase 4) will pick
# the right role automatically for each task.
#
# On an 8 GB RAM laptop we use small 4-bit models (~1-2 GB each).
# On MRPL's GPU server you would simply replace these names with larger
# models, e.g. "qwen2.5:32b" — nothing else changes.
MODELS = {
    "general": {
        "name": "qwen2.5:3b",          # ~1.9 GB download. Fallback: "qwen2.5:1.5b"
        "purpose": "Conversation, summaries, drafting notes, planning",
    },
    # Added in later phases (left here, commented, so you can see the plan):
    # "code":   {"name": "qwen2.5-coder:1.5b", "purpose": "Writing and fixing code"},
    # "vision": {"name": "moondream",          "purpose": "Photos and drawings"},
}

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


# ---------------------------------------------------------------------------
# 4. Sovereignty (no-data-leaves-the-machine) settings
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
# 5. Assistant behaviour
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
