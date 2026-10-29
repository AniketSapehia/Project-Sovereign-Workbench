"""
app/llm.py — talking to the local models through Ollama.

Ollama is a free program that downloads open-weight models once and then runs
them on your own computer. It exposes a small web API on 127.0.0.1:11434,
so our Python code "chats" with the model by sending HTTP requests to it.

Two safety features live in this file:
  1. An EGRESS GUARD: before any request is sent, we check the destination.
     If it is not this machine, the request is BLOCKED and logged in red.
  2. Every request is written to the network monitor log (app/netmon.py).
"""

import json
from typing import AsyncIterator

import httpx               # a modern HTTP client that supports streaming

import config
from app import netmon


class EgressBlocked(Exception):
    """Raised when code tries to contact a host outside this machine."""


# ---------------------------------------------------------------------------
# The egress guard — runs automatically before EVERY request
# ---------------------------------------------------------------------------
async def _guard_and_log(request: httpx.Request) -> None:
    """
    httpx calls this "event hook" just before a request is sent.
    We log it, and if the host is not allowed we stop it right here.
    """
    allowed = netmon.record_request(request.method, str(request.url))
    if not allowed:
        raise EgressBlocked(
            f"Blocked attempt to contact external host: {request.url.host}"
        )


def _make_client() -> httpx.AsyncClient:
    """
    Create an HTTP client with our guard attached.

    trust_env=False is important: it makes httpx IGNORE any system proxy
    settings, so traffic cannot be silently routed through a proxy server.
    """
    return httpx.AsyncClient(
        base_url=config.OLLAMA_URL,
        timeout=config.OLLAMA_TIMEOUT_SECONDS,
        trust_env=False,
        event_hooks={"request": [_guard_and_log]},
    )


# ---------------------------------------------------------------------------
# Helper: which model name belongs to a role?
# ---------------------------------------------------------------------------
def model_for_role(role: str) -> str:
    """Look up the Ollama model name for a role such as 'general'."""
    return config.MODELS[role]["name"]


# ---------------------------------------------------------------------------
# Health check: is Ollama running, and are our models downloaded?
# ---------------------------------------------------------------------------
async def check_health() -> dict:
    """
    Ask Ollama which models are installed (GET /api/tags) and compare them
    with the models listed in config.py.
    """
    try:
        async with _make_client() as client:
            response = await client.get("/api/tags")
            response.raise_for_status()
            installed = {m["name"] for m in response.json().get("models", [])}
    except Exception as error:
        # Ollama is probably not running.
        return {"ollama_running": False, "error": str(error), "models": []}

    # Every chat model from config.py, plus the embedding model (Phase 2).
    wanted = [(role, info["name"], info["purpose"]) for role, info in config.MODELS.items()]
    wanted.append(("embedding", config.EMBEDDING_MODEL, "Searching documents by meaning"))

    models = []
    for role, name, purpose in wanted:
        # Ollama sometimes reports "qwen2.5:3b" and sometimes adds ":latest"
        # for models without a tag, so we accept both forms.
        present = name in installed or f"{name}:latest" in installed
        models.append({
            "role": role,
            "name": name,
            "purpose": purpose,
            "installed": present,
        })

    return {"ollama_running": True, "models": models}


# ---------------------------------------------------------------------------
# Embeddings (NEW IN PHASE 2): turn texts into "meaning vectors"
# ---------------------------------------------------------------------------
async def embed(texts: list) -> list:
    """
    Send a list of texts to the local embedding model and get back one
    vector (a list of ~768 numbers) per text, in the same order.

    These vectors are what make "search by meaning" possible: the vector for
    "pump is shaking a lot" ends up close to the vector for "excessive
    vibration of centrifugal pump", even though they share almost no words.
    """
    payload = {"model": config.EMBEDDING_MODEL, "input": texts}
    async with _make_client() as client:
        response = await client.post("/api/embed", json=payload)
        if response.status_code != 200:
            raise RuntimeError(
                f"Embedding failed ({response.status_code}): {response.text}. "
                f"Is the model downloaded? Run: ollama pull {config.EMBEDDING_MODEL}"
            )
        return response.json()["embeddings"]


# ---------------------------------------------------------------------------
# Chat: send the conversation and STREAM the answer back word by word
# ---------------------------------------------------------------------------
async def chat_stream(messages: list, role: str = config.DEFAULT_ROLE) -> AsyncIterator[str]:
    """
    Send the conversation to the model and yield the reply piece by piece.

    Why stream? On a CPU a full answer can take 30+ seconds. Streaming shows
    words as soon as they are generated, so the user knows it is working.

    `messages` looks like:
        [{"role": "user", "content": "Hello"},
         {"role": "assistant", "content": "Hi!"},
         {"role": "user", "content": "Explain NPSH"}]
    """
    # Put the hidden system prompt in front of the user's conversation.
    full_messages = [{"role": "system", "content": config.SYSTEM_PROMPT}] + messages

    payload = {
        "model": model_for_role(role),
        "messages": full_messages,
        "stream": True,           # ask Ollama to send the answer in pieces
        # How much text the model may look at in one go (see config.py).
        # Phase 2 needs more room, because document excerpts are added.
        "options": {"num_ctx": config.CONTEXT_TOKENS},
    }

    async with _make_client() as client:
        # client.stream(...) keeps the connection open while pieces arrive.
        async with client.stream("POST", "/api/chat", json=payload) as response:
            if response.status_code != 200:
                body = (await response.aread()).decode(errors="replace")
                raise RuntimeError(f"Ollama returned {response.status_code}: {body}")

            # Ollama sends one JSON object per line ("NDJSON"), e.g.
            #   {"message": {"content": "Hel"}, "done": false}
            #   {"message": {"content": "lo"},  "done": false}
            #   {"done": true, ...timing stats...}
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                piece = json.loads(line)
                if "error" in piece:
                    raise RuntimeError(piece["error"])
                text = piece.get("message", {}).get("content", "")
                if text:
                    yield text
                if piece.get("done"):
                    break
