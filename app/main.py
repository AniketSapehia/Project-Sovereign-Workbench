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
        GET  /api/uploads/{id}/image -> preview of an attached image (Phase 5)
        GET  /api/files        -> list files the agent created (Phase 3)
        GET  /api/files/{name} -> download one of those files (Phase 3)

The server is started by run.bat with:
    uvicorn app.main:app --host 127.0.0.1 --port 8000
"--host 127.0.0.1" means ONLY this computer can open the page. Other machines
on the network cannot reach it, which is what we want for confidential data.
"""

import json
import re                      # "regular expressions": text pattern matching
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Literal

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from app import agent, codeflow, documents, knowledge, llm, netmon, router, trained, vision, workflow

# The folder that holds index.html, style.css and app.js.
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Code that runs once when the server starts (before `yield`).
    We make sure our folders exist and load the saved knowledge-base index.
    """
    for folder in (config.DATA_DIR, config.UPLOADS_DIR, config.KNOWLEDGE_BASE_DIR,
                   config.WORKSPACE_DIR):
        folder.mkdir(parents=True, exist_ok=True)
    knowledge.load_index()
    yield


app = FastAPI(title="Sovereign Workbench", version="0.3 (Phase 3)", lifespan=lifespan)

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
    agent_mode: bool = True                # Phase 3: let the model use tools?
    model_choice: str = "auto"             # Phase 4: "auto" = let the router decide


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


@app.get("/api/uploads/{doc_id}/image")
async def upload_image(doc_id: str):
    """
    Phase 5: show an attached IMAGE as a preview in the chat.
    The id is checked by knowledge.attachment_document (32 hex characters
    only), so this can never be used to read other files.
    """
    try:
        name = knowledge.attachment_document(doc_id)["summary"]["filename"]
    except Exception:
        raise HTTPException(status_code=404, detail="Not found")
    extension = Path(name).suffix.lower()
    path = config.UPLOADS_DIR / doc_id / f"original{extension}"
    if extension not in documents.IMAGE_EXTENSIONS or not path.exists():
        raise HTTPException(status_code=404, detail="Not an image")
    return FileResponse(path)


# ---------------------------------------------------------------------------
# Files created by the agent (Phase 3)
# ---------------------------------------------------------------------------

def _workspace_file(name: str) -> Path:
    """
    Turn a requested file name into a path INSIDE workspace/, or refuse.
    .resolve() follows any ".." in the name; we then check the result is
    still inside the workspace, so "../config.py" can't be downloaded.
    """
    workspace = config.WORKSPACE_DIR.resolve()
    path = (workspace / name).resolve()
    if workspace not in path.parents or ".sandbox" in path.parts or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return path


@app.get("/api/files")
async def list_files():
    """Newest first: the files the agent has created in workspace/."""
    workspace = config.WORKSPACE_DIR
    files = [p for p in workspace.rglob("*") if p.is_file() and ".sandbox" not in p.parts] \
        if workspace.exists() else []
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"name": p.relative_to(workspace).as_posix(), "size": p.stat().st_size}
            for p in files[:50]]


@app.get("/api/files/{name:path}")
async def download_file(name: str):
    """Download one file; the browser saves it under its own name."""
    path = _workspace_file(name)
    return FileResponse(path, filename=path.name)


# ---------------------------------------------------------------------------
# Chat (updated in Phase 2 and Phase 3)
# ---------------------------------------------------------------------------
# The reply is now a stream of small JSON "events", one per line (NDJSON):
#     {"type": "route",   "role": "code", ...}  <- Phase 4: which model, and why
#     {"type": "status",  "text": "Searching knowledge base…"}
#     {"type": "sources", "sources": [...]}      <- what the answer is based on
#     {"type": "token",   "text": "Hel"}         <- a piece of the answer
#     {"type": "warning", "text": "..."}         <- stopped before the model ran
#     {"type": "error",   "text": "..."}
#     {"type": "done"}
# Sending typed events (instead of plain text) lets the page show sources
# and progress, and later phases will add agent steps the same way.

def _event(kind: str, **data) -> str:
    return json.dumps({"type": kind, **data}, ensure_ascii=False) + "\n"


# Phrases that clearly point at a document the user means to have ATTACHED,
# e.g. "this report", "the attached file", "uploaded scan".
# How to read this pattern ("regular expression"):
#   \b                 a word boundary, so "this" doesn't match inside "thistle"
#   (this|attached|…)  any ONE of these words
#   \s+                one or more spaces
#   (report|document…) followed by any ONE of these words
#   s?                 optional plural "s"
_MISSING_ATTACHMENT = re.compile(
    r"\b(this|these|attached|uploaded|the attached|the uploaded)\s+"
    r"(report|document|doc|file|pdf|scan|scanned report|image|photo|picture|drawing|attachment)s?\b"
    r"|\battachment\b",
    re.IGNORECASE,
)


def refers_to_missing_attachment(question: str, attachment_ids: list, use_trained: bool = None) -> bool:
    """
    True if the question talks about "this report" (or similar) while
    nothing is attached.

    Why (Phase 2.1): in testing, the user asked about "this report" after the
    report had been removed. The model never saw the report, but instead of
    saying so it answered anyway and got it wrong. Small models rarely admit
    missing information, so we check for this in CODE, before the model runs.
    """
    if attachment_ids:
        return False
    # Phase 6.16: the TRAINED detector, if one has been trained. On 5,000
    # generated tests the regular expression below caught only ~19% of the
    # questions that needed a warning. It stays as the fallback.
    p = trained.attachment_probability(question, use_trained)
    if p is not None:
        return p >= 0.5
    return bool(_MISSING_ATTACHMENT.search(question))


@app.post("/api/chat")
async def chat(request: ChatRequest):
    messages = [m.model_dump() for m in request.messages]
    question = messages[-1]["content"] if messages else ""

    async def generate():
        # ---- Step 0: SAFETY CHECK — is the user asking about a missing file? ----
        if refers_to_missing_attachment(question, request.attachment_ids):
            yield _event("warning", text=(
                "You mentioned a report or document, but nothing is attached to "
                "this chat, so I can't read it. Attach it with 📎 and ask again. "
                "(If you meant a document in the knowledge base, name it, "
                "e.g. \"What does SOP-MECH-004 say about …\")"))
            yield _event("done")
            return

        # ---- Phase 6: FLAGSHIP WORKFLOW — approval note from an attached report ----
        if workflow.should_run(question, request.attachment_ids):
            yield _event("route", role=config.DEFAULT_ROLE,
                         model=config.MODELS[config.DEFAULT_ROLE]["name"], method="workflow",
                         reason="approval note requested with a document attached, so the "
                                "guided report → approval note workflow is used",
                         scores={}, fallback=False)
            try:
                async for item in workflow.run(messages, request.attachment_ids):
                    yield json.dumps(item, ensure_ascii=False) + "\n"
            except llm.EgressBlocked as error:
                yield _event("error", text=f"[BLOCKED] {error}")
                yield _event("done")
            except Exception as error:
                yield _event("error", text=f"The workflow failed: {error}")
                yield _event("done")
            return

        # ---- Phase 4: ROUTER — which model should handle this task? ----
        yield _event("status", text="Choosing a model…")
        decision = await router.route(question, request.attachment_ids, request.model_choice)
        yield _event("route", **decision)
        role = decision["role"]

        # ---- Phase 6.3: CODE tasks — guided write → run → fix loop ----
        # (The small code model can't follow the agent's tool protocol, so the
        # workbench runs its code itself. See app/codeflow.py.)
        if role == "code" and request.agent_mode:
            try:
                async for item in codeflow.run(messages, request.attachment_ids,
                                               request.use_knowledge_base, role=role):
                    yield json.dumps(item, ensure_ascii=False) + "\n"
            except llm.EgressBlocked as error:
                yield _event("error", text=f"[BLOCKED] {error}")
                yield _event("done")
            except Exception as error:
                yield _event("error", text=f"{error}\nCheck that the code model is downloaded.")
                yield _event("done")
            return

        # ---- Phase 5: VISION — the vision model looks at the attached images ----
        if role == "vision":
            try:
                async for item in vision.answer(messages, request.attachment_ids, role=role):
                    yield json.dumps(item, ensure_ascii=False) + "\n"
            except llm.EgressBlocked as error:
                yield _event("error", text=f"[BLOCKED] {error}")
                yield _event("done")
            except Exception as error:
                yield _event("error", text=(
                    f"{error}\nCheck that the vision model is downloaded "
                    f"(ollama pull {config.MODELS['vision']['name']})."))
                yield _event("done")
            return

        # ---- Phase 3: AGENT MODE — the model may plan and use tools ----
        if request.agent_mode:
            try:
                async for item in agent.run(messages, request.attachment_ids,
                                            request.use_knowledge_base, role=role):
                    yield json.dumps(item, ensure_ascii=False) + "\n"
            except llm.EgressBlocked as error:
                yield _event("error", text=f"[BLOCKED] {error}")
                yield _event("done")
            except (FileNotFoundError, ValueError):
                yield _event("error", text="Attached document not found. Please attach it again.")
                yield _event("done")
            except Exception as error:
                yield _event("error", text=(
                    f"{error}\nCheck that Ollama is running and the models are downloaded."))
                yield _event("done")
            return

        # ---- Plain mode (Phase 2): retrieve, then answer once ----
        try:
            # ---- Step 1: RETRIEVE — gather excerpts the answer can rely on ----
            # Attachments, documents named in the question (in full), and a
            # knowledge-base search (see knowledge.gather_sources).
            yield _event("status", text="Gathering documents…")
            sources = await knowledge.gather_sources(question, request.attachment_ids,
                                                     request.use_knowledge_base,
                                                     search=role != "code")

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
            async for piece in llm.chat_stream(messages, role=role):
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
