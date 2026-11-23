"""
app/knowledge.py — the local knowledge base and document attachments.

This is "RAG" — Retrieval-Augmented Generation. In plain words:

    1. INDEX   (done once, when you click "Rebuild index")
       Read every file in knowledge_base/, cut it into chunks, and turn each
       chunk into a meaning-vector with the embedding model. Save to disk.

    2. RETRIEVE   (every question)
       Turn the question into a vector too, and find the chunks whose
       vectors are closest to it = the most relevant paragraphs.

    3. GENERATE   (every question)
       Paste those paragraphs, numbered [1], [2], ..., in front of the
       question and tell the model: "answer from these, and cite the numbers".

Why this matters for the problem statement:
    The brief asks the assistant to "ground itself in the organization's own
    manuals, SOPs and past correspondence through a local knowledge base
    connector, again with nothing going external". Grounding + citations
    also means an officer can CHECK every answer against the source.

Why not a database like ChromaDB?
    For a few thousand chunks, a plain NumPy matrix searches in milliseconds,
    has no extra services to run, is easy to explain line by line, and adds
    no third-party code that might phone home. (Some vector databases have
    shipped with anonymous usage telemetry switched on by default — exactly
    what a sovereign system must avoid.) On MRPL's server, with millions of
    chunks, a dedicated self-hosted vector database would be the next step.

The index is stored in data/ as two files:
    kb_index.npz     the vectors (a matrix: one row per chunk)
    kb_chunks.json   the chunk texts and where each came from
"""

import asyncio
import json
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

import numpy as np

import config
from app import documents, llm

# How many chunks we send to the embedding model per request. Batching is
# much faster than one request per chunk, but too many at once uses more RAM.
EMBED_BATCH_SIZE = 16

# Upload IDs are 32 hexadecimal characters (a UUID). We check this format
# before using an ID in a file path, so a malicious ID like "../../secret"
# can never make us read files outside the uploads folder.
_VALID_ID = re.compile(r"^[0-9a-f]{32}$")


# ===========================================================================
# Shared helpers
# ===========================================================================

def _normalise(matrix: np.ndarray) -> np.ndarray:
    """
    Scale every vector to length 1.

    After this, the "dot product" of two vectors equals their COSINE
    SIMILARITY: 1.0 = same meaning, ~0 = unrelated. That turns search into
    a single fast matrix multiplication.
    """
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0            # avoid dividing by zero
    return matrix / norms


async def _embed_chunks(chunks: list) -> np.ndarray:
    """Embed the text of many chunks, in batches. Returns an (n, d) matrix."""
    vectors = []
    for start in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[start:start + EMBED_BATCH_SIZE]
        texts = [config.EMBED_DOCUMENT_PREFIX + c["text"] for c in batch]
        vectors.extend(await llm.embed(texts))
    return _normalise(np.array(vectors, dtype=np.float32))


async def _embed_query(question: str) -> np.ndarray:
    vector = (await llm.embed([config.EMBED_QUERY_PREFIX + question]))[0]
    return _normalise(np.array([vector], dtype=np.float32))[0]


def _top_matches(query: np.ndarray, matrix: np.ndarray, chunks: list, k: int) -> list:
    """
    Score every chunk against the question and return the best k.

    `matrix @ query` multiplies the (n, d) matrix by the (d,) vector, giving
    n similarity scores at once — this IS the search.
    """
    if len(chunks) == 0:
        return []
    scores = matrix @ query
    best = np.argsort(-scores)[:k]     # indices of the highest scores
    results = []
    for i in best:
        if scores[i] < config.MIN_SIMILARITY:
            continue                   # too weak a match to be useful
        results.append({**chunks[i], "score": round(float(scores[i]), 3)})
    return results


# ===========================================================================
# Part A — the knowledge base (the knowledge_base/ folder)
# ===========================================================================

# The loaded index lives in memory while the server runs.
_kb = {"vectors": np.zeros((0, 0), dtype=np.float32), "chunks": [], "meta": {}}
_rebuild_lock = asyncio.Lock()   # stops two rebuilds running at the same time


def load_index() -> None:
    """Load a previously built index from disk (called when the server starts)."""
    if config.INDEX_FILE.exists() and config.INDEX_META_FILE.exists():
        _kb["vectors"] = np.load(config.INDEX_FILE)["vectors"]
        saved = json.loads(config.INDEX_META_FILE.read_text(encoding="utf-8"))
        _kb["chunks"] = saved["chunks"]
        _kb["meta"] = saved["meta"]


def status() -> dict:
    """Summary of the knowledge base, shown in the side panel."""
    folder_files = sorted(
        p.name for p in config.KNOWLEDGE_BASE_DIR.glob("*")
        if p.suffix.lower() in config.SUPPORTED_EXTENSIONS
    ) if config.KNOWLEDGE_BASE_DIR.exists() else []

    meta = _kb["meta"]
    built_model = meta.get("embedding_model")
    return {
        "folder": str(config.KNOWLEDGE_BASE_DIR),
        "files_in_folder": folder_files,
        "indexed_files": meta.get("files", []),
        "chunk_count": len(_kb["chunks"]),
        "built_at": meta.get("built_at"),
        "errors": meta.get("errors", []),
        # If someone changed EMBEDDING_MODEL in config.py, old vectors no
        # longer match new questions, so we warn that a rebuild is needed.
        "needs_rebuild": (built_model is not None and built_model != config.EMBEDDING_MODEL)
                         or set(folder_files) != {f["name"] for f in meta.get("files", [])},
    }


async def rebuild_index() -> dict:
    """
    Read every file in knowledge_base/, chunk it, embed it, and save the index.
    A file that fails (e.g. a corrupt PDF) is reported, not fatal.
    """
    async with _rebuild_lock:
        config.KNOWLEDGE_BASE_DIR.mkdir(parents=True, exist_ok=True)
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)

        all_chunks, files, errors = [], [], []
        for path in sorted(config.KNOWLEDGE_BASE_DIR.glob("*")):
            if path.suffix.lower() not in config.SUPPORTED_EXTENSIONS:
                continue
            try:
                # Text extraction and OCR are slow, "blocking" work. Running
                # them in a separate thread keeps the web page responsive
                # (the network monitor keeps updating meanwhile).
                pages = await asyncio.to_thread(documents.extract, path)
                chunks = documents.chunk_pages(pages, source=path.name)
                all_chunks.extend(chunks)
                files.append({
                    "name": path.name,
                    "pages": len(pages),
                    "chunks": len(chunks),
                    "ocr_pages": sum(1 for p in pages if p["method"] == "ocr"),
                })
            except Exception as error:
                errors.append({"name": path.name, "error": str(error)})

        vectors = (await _embed_chunks(all_chunks) if all_chunks
                   else np.zeros((0, 0), dtype=np.float32))

        meta = {
            "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "embedding_model": config.EMBEDDING_MODEL,
            "files": files,
            "errors": errors,
        }

        # Save to disk so the index survives a restart.
        np.savez_compressed(config.INDEX_FILE, vectors=vectors)
        config.INDEX_META_FILE.write_text(
            json.dumps({"meta": meta, "chunks": all_chunks}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

        _kb.update({"vectors": vectors, "chunks": all_chunks, "meta": meta})
        return status()


def _document_id(filename: str) -> str:
    """
    The short ID people use for a document: the part of the file name
    before the first space, e.g.
        "SOP-INSP-011 Pressure Vessel Thickness Inspection.md" -> "SOP-INSP-011"
    """
    return Path(filename).stem.split(" ")[0]


def documents_named_in(question: str) -> list:
    """
    Knowledge-base files the question mentions BY NAME, e.g. "Using
    SOP-INSP-011, calculate ..." -> [the SOP-INSP-011 file].

    Why (Phase 3.1): in testing, a question naming SOP-INSP-011 retrieved its
    title, purpose and scope sections, but NOT the sections with the
    formulas, and the model invented a wrong formula. When a user names a
    document, they clearly want it used, so we give the model that document
    in full instead of trusting search to find the right parts.
    """
    text = question.lower()
    named = []
    if config.KNOWLEDGE_BASE_DIR.exists():
        for path in sorted(config.KNOWLEDGE_BASE_DIR.iterdir()):
            if path.suffix.lower() not in config.SUPPORTED_EXTENSIONS:
                continue
            doc_id = _document_id(path.name).lower()
            # Require a reasonably specific ID (e.g. "SOP-INSP-011"), so a
            # file called "notes.txt" doesn't match every question with "notes".
            if len(doc_id) >= 6 and any(c.isdigit() for c in doc_id) and doc_id in text:
                named.append(path)
    return named


async def context_from_named_documents(question: str) -> list:
    """
    Sources for documents named in the question.
    Short document -> the whole text. Long document -> its best-matching
    chunks only (it would not fit in the model's context window).
    """
    sources = []
    for path in documents_named_in(question):
        pages = await asyncio.to_thread(documents.extract, path)
        total = sum(len(p["text"]) for p in pages)
        if total <= config.FULL_DOC_LIMIT_CHARS:
            sources += [{"text": p["text"], "source": path.name, "page": p["page"],
                         "section": "(whole document)", "method": p["method"], "score": None}
                        for p in pages if p["text"].strip()]
        elif _kb["chunks"]:
            # Search only inside this document's chunks.
            rows = [i for i, c in enumerate(_kb["chunks"]) if c["source"] == path.name]
            if rows:
                query = await _embed_query(question)
                sources += _top_matches(query, _kb["vectors"][rows],
                                        [_kb["chunks"][i] for i in rows], config.TOP_K + 2)
    return sources


async def gather_sources(question: str, attachment_ids: list, use_knowledge_base: bool,
                         search: bool = True) -> list:
    """
    Collect every excerpt the model should see for this question, in order:
      1. attached documents
      2. knowledge-base documents named in the question (in full)
      3. a knowledge-base search for anything else that's relevant
    Duplicates are removed: a search hit from a document that is already
    included in full is skipped.
    """
    sources = []
    for doc_id in attachment_ids:
        sources += await context_from_attachment(doc_id, question)

    if use_knowledge_base:
        named = await context_from_named_documents(question)
        sources += named
        whole = {s["source"] for s in named if s.get("section") == "(whole document)"}

        if search:          # False for code tasks (Phase 4.1)
            hits = await search_knowledge_base(question)
            sources += [h for h in hits if h["source"] not in whole]
    return sources


async def search_knowledge_base(question: str, only_prefixes: tuple = None, k: int = None) -> list:
    """
    Return the most relevant knowledge-base chunks for a question.
    only_prefixes (Phase 6.5): search only documents whose name starts with
    one of these, e.g. ("REF-",) = the reference data sheets.
    """
    if not _kb["chunks"]:
        return []
    rows = list(range(len(_kb["chunks"])))
    if only_prefixes:
        prefixes = tuple(p.upper() for p in only_prefixes)
        rows = [i for i in rows if _kb["chunks"][i]["source"].upper().startswith(prefixes)]
        if not rows:
            return []
    query = await _embed_query(question)
    return _top_matches(query, _kb["vectors"][rows], [_kb["chunks"][i] for i in rows],
                        k or config.TOP_K)


# ===========================================================================
# Part B — documents attached in the chat
# ===========================================================================
# An attached file is processed the same way (extract -> chunk -> embed),
# but kept separately in data/uploads/<id>/ and only used in the
# conversation it was attached to. It does NOT go into the knowledge base.

def _upload_dir(doc_id: str) -> Path:
    if not _VALID_ID.match(doc_id):
        raise ValueError("Invalid document id")
    return config.UPLOADS_DIR / doc_id


async def ingest_upload(filename: str, content: bytes) -> dict:
    """Save an uploaded file, extract its text (OCR if needed) and embed it."""
    # Keep only the bare file name (never a path the browser sent us).
    safe_name = Path(filename).name or "upload"
    extension = Path(safe_name).suffix.lower()
    if extension not in config.SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file type '{extension}'. "
                         f"Supported: {', '.join(sorted(config.SUPPORTED_EXTENSIONS))}")

    doc_id = uuid.uuid4().hex
    folder = _upload_dir(doc_id)
    folder.mkdir(parents=True, exist_ok=True)
    original = folder / f"original{extension}"
    original.write_bytes(content)

    try:
        started = datetime.now()
        pages = await asyncio.to_thread(documents.extract, original)
        chunks = documents.chunk_pages(pages, source=safe_name)
        vectors = await _embed_chunks(chunks) if chunks else np.zeros((0, 0), dtype=np.float32)
        seconds = (datetime.now() - started).total_seconds()
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)   # don't leave half-processed files
        raise

    ocr_conf = [p["ocr_confidence"] for p in pages if p["ocr_confidence"] is not None]
    summary = {
        "id": doc_id,
        "filename": safe_name,
        "pages": len(pages),
        "ocr_pages": sum(1 for p in pages if p["method"] == "ocr"),
        "ocr_confidence": round(sum(ocr_conf) / len(ocr_conf), 1) if ocr_conf else None,
        "uncertain_words": sum(p.get("uncertain_words", 0) for p in pages),
        "characters": sum(len(p["text"]) for p in pages),
        "chunks": len(chunks),
        "seconds": round(seconds, 1),
        "preview": " ".join(pages[0]["text"].split())[:300] if pages else "",
    }

    (folder / "document.json").write_text(
        json.dumps({"summary": summary, "pages": pages, "chunks": chunks},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    np.savez_compressed(folder / "vectors.npz", vectors=vectors)
    return summary


def attachment_document(doc_id: str) -> dict:
    """Load a processed upload: {"summary": ..., "pages": [...], "chunks": [...]}."""
    folder = _upload_dir(doc_id)
    return json.loads((folder / "document.json").read_text(encoding="utf-8"))


async def search_attachment(doc_id: str, question: str, k: int) -> list:
    """Search inside one attached document (used by the agent's search tool)."""
    folder = _upload_dir(doc_id)
    saved = attachment_document(doc_id)
    vectors = np.load(folder / "vectors.npz")["vectors"]
    if len(saved["chunks"]) == 0:
        return []
    query = await _embed_query(question)
    return _top_matches(query, vectors, saved["chunks"], k)


async def context_from_attachment(doc_id: str, question: str) -> list:
    """
    Choose which parts of an attached document to show the model.

    - Short document: give the model ALL of it (page by page), because a
      request like "summarise this" needs the whole thing.
    - Long document: it won't fit in the model's context window, so search
      it like the knowledge base and give only the most relevant chunks.
    """
    folder = _upload_dir(doc_id)
    saved = json.loads((folder / "document.json").read_text(encoding="utf-8"))
    pages, name = saved["pages"], saved["summary"]["filename"]

    total = sum(len(p["text"]) for p in pages)
    if total <= config.FULL_DOC_LIMIT_CHARS:
        return [{"text": p["text"], "source": name, "page": p["page"],
                 "section": "", "method": p["method"], "score": None}
                for p in pages if p["text"].strip()]

    vectors = np.load(folder / "vectors.npz")["vectors"]
    query = await _embed_query(question)
    # A long attached document gets a few more excerpts than the KB,
    # because the user is explicitly asking about it.
    return _top_matches(query, vectors, saved["chunks"], config.TOP_K + 2)


# ===========================================================================
# Part C — building the final prompt
# ===========================================================================

def format_sources(sources: list, start: int = 1) -> str:
    """Write sources as numbered blocks: '[3] Source: file, page 1 ...'."""
    blocks = []
    for number, s in enumerate(sources, start=start):
        where = f"{s['source']}, page {s['page']}"
        if s.get("section"):
            where += f', section "{s["section"]}"'
        blocks.append(f"[{number}] Source: {where}\n{s['text']}")
    return "\n\n".join(blocks)


def build_grounded_question(question: str, sources: list) -> str:
    """
    Put the numbered excerpts in front of the user's question.

    Small models follow simple, explicit instructions best, so the rules are
    short and direct. The numbers [1], [2] match the "Sources" list the
    page shows under the answer, so every claim can be checked.
    """
    blocks = []
    for number, s in enumerate(sources, start=1):
        where = f"{s['source']}, page {s['page']}"
        if s.get("section"):
            where += f', section "{s["section"]}"'
        blocks.append(f"[{number}] Source: {where}\n{s['text']}")

    return (
        "Below are numbered excerpts from internal documents.\n"
        "Rules:\n"
        "- Answer using these excerpts. After each fact, cite its excerpt "
        "number in square brackets, like [1] or [2].\n"
        "- If the excerpts do not contain the answer, say: \"The provided "
        "documents do not cover this.\" Then you may answer from general "
        "knowledge, clearly labelled as general knowledge.\n"
        "- Do not invent document names, clause numbers or values.\n"
        "- Text marked [unclear: ...] was read badly by OCR: never guess those "
        "values; say they must be checked on the original.\n\n"
        "=== EXCERPTS ===\n"
        + "\n\n".join(blocks)
        + "\n=== END OF EXCERPTS ===\n\n"
        f"Question: {question}"
    )
