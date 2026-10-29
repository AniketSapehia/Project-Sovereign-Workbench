"""
app/main.py — the web server (the "backend").

We use FastAPI, a Python framework for building web APIs. It does two jobs:
  1. Serves the web page (static/index.html, style.css, app.js).
  2. Provides API "endpoints" the page calls with JavaScript:

        GET  /api/health       -> Ollama, models and OCR status
        GET  /api/network      -> the network monitor report
        POST /api/chat         -> send a conversation, get the answer streamed back
        POST /api/upload       -> attach a document (Phase 2)
        GET  /api/kb/status    -> knowledge base summary (Phase 2)
        POST /api/kb/reindex   -> rebuild the knowledge base index (Phase 2)

The server is started by run.bat with:
    uvicorn app.main:app --host 127.0.0.1 --port 8000
"--host 127.0.0.1" means ONLY this computer can open the page. Other machines
on the network cannot reach it, which is what we want for confidential data.
"""

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Literal

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from app import documents, knowledge, llm, netmon

# The folder that holds index.html, style.css and app.js.
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Code that runs once when the server starts (before `yield`).
    We make sure our folders exist and load the saved knowledge-base index.
    """
    for folder in (config.DATA_DIR, config.UPLOADS_DIR, config.KNOWLEDGE_BASE_DIR):
        folder.mkdir(parents=True, exist_ok=True)
    knowledge.load_index()
    yield


app = FastAPI(title="Sovereign Workbench", version="0.2 (Phase 2)", lifespan=lifespan)

# Make everything inside /static available at http://127.0.0.1:8000/static/...
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# Data shapes for incoming requests
# ---------------------------------------------------------------------------
# Pydantic checks that the browser sent data in the right format. If not,
# FastAPI automatically replies with a clear error instead of crashing.

class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]   # only these two values are accepted
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    use_knowledge_base: bool = True        # Phase 2: search the KB?
    attachment_ids: List[str] = []         # Phase 2: documents attached in chat


# ---------------------------------------------------------------------------
# Pages and status endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def home():
    """Open http://127.0.0.1:8000 and you get the main page."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health():
    """Tell the page whether Ollama is up, which models exist, and if OCR works."""
    result = await llm.check_health()
    result["ocr"] = documents.ocr_status()
    return result


@app.get("/api/network")
async def network():
    """Return the live network monitor report (see app/netmon.py)."""
    return netmon.get_report()


# ---------------------------------------------------------------------------
# Knowledge base endpoints (Phase 2)
# ---------------------------------------------------------------------------

@app.get("/api/kb/status")
async def kb_status():
    return knowledge.status()


@app.post("/api/kb/reindex")
async def kb_reindex():
    """Re-read every file in knowledge_base/ and rebuild the search index."""
    try:
        return await knowledge.rebuild_index()
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


# ---------------------------------------------------------------------------
# Document upload (Phase 2)
# ---------------------------------------------------------------------------

@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    """
    Receive a file from the page, extract its text (OCR if it is scanned)
    and return a short summary. The page then sends the returned `id`
    with chat messages so the assistant can read the document.
    """
    content = await file.read()
    if len(content) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413,
                            detail=f"File is larger than {config.MAX_UPLOAD_MB} MB.")
    try:
        return await knowledge.ingest_upload(file.filename or "upload", content)
    except ValueError as error:                 # e.g. unsupported file type
        raise HTTPException(status_code=400, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not process file: {error}")


# ---------------------------------------------------------------------------
# Chat (updated in Phase 2)
# ---------------------------------------------------------------------------
# The reply is now a stream of small JSON "events", one per line (NDJSON):
#     {"type": "status",  "text": "Searching knowledge base…"}
#     {"type": "sources", "sources": [...]}      <- what the answer is based on
#     {"type": "token",   "text": "Hel"}         <- a piece of the answer
#     {"type": "error",   "text": "..."}
#     {"type": "done"}
# Sending typed events (instead of plain text) lets the page show sources
# and progress, and later phases will add agent steps the same way.

def _event(kind: str, **data) -> str:
    return json.dumps({"type": kind, **data}, ensure_ascii=False) + "\n"


@app.post("/api/chat")
async def chat(request: ChatRequest):
    messages = [m.model_dump() for m in request.messages]
    question = messages[-1]["content"] if messages else ""

    async def generate():
        try:
            # ---- Step 1: RETRIEVE — gather excerpts the answer can rely on ----
            sources = []
            for doc_id in request.attachment_ids:
                yield _event("status", text="Reading attached document…")
                sources += await knowledge.context_from_attachment(doc_id, question)

            if request.use_knowledge_base:
                yield _event("status", text="Searching knowledge base…")
                sources += await knowledge.search_knowledge_base(question)

            # ---- Step 2: AUGMENT — put the excerpts in front of the question ----
            if sources:
                yield _event("sources", sources=[
                    {"n": i + 1, "source": s["source"], "page": s["page"],
                     "section": s.get("section", ""), "score": s.get("score"),
                     "method": s.get("method"), "text": s["text"]}
                    for i, s in enumerate(sources)
                ])
                # Only the LAST message is changed; earlier turns stay as typed.
                messages[-1] = {"role": "user",
                                "content": knowledge.build_grounded_question(question, sources)}

            # ---- Step 3: GENERATE — stream the model's answer ----
            yield _event("status", text="Generating answer…")
            async for piece in llm.chat_stream(messages, role=config.DEFAULT_ROLE):
                yield _event("token", text=piece)

        except llm.EgressBlocked as error:
            yield _event("error", text=f"[BLOCKED] {error}")
        except (FileNotFoundError, ValueError):
            # Missing upload folder, or an id that failed the safety check.
            yield _event("error", text="Attached document not found. Please attach it again.")
        except Exception as error:
            # Show the problem in the chat rather than failing silently.
            yield _event("error", text=(
                f"{error}\nCheck that Ollama is running and the models are downloaded "
                f"(ollama pull {llm.model_for_role(config.DEFAULT_ROLE)} and "
                f"ollama pull {config.EMBEDDING_MODEL})."))
        yield _event("done")

    return StreamingResponse(generate(), media_type="application/x-ndjson")
