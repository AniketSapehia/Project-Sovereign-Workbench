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
import re
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
# Thinking models (Phase 3.8)
# ---------------------------------------------------------------------------
def _think_setting(role: str):
    """The model's "think" setting from config.py (None = don't send it)."""
    return config.MODELS[role].get("think")


class ThinkFilter:
    """
    Some models (e.g. qwen3) can write hidden reasoning between <think> and
    </think> before their answer. Even with thinking switched off, an empty
    "<think></think>" may appear. This removes those blocks from a stream of
    text pieces, so they never show up as the answer or confuse the parser.
    """
    def __init__(self):
        self.buffer = ""
        self.inside = False

    def feed(self, piece: str) -> str:
        self.buffer += piece
        out = ""
        while self.buffer:
            if self.inside:
                end = self.buffer.find("</think>")
                if end == -1:
                    self.buffer = self.buffer[-8:]      # keep a possible partial tag
                    return out
                self.buffer = self.buffer[end + len("</think>"):]
                self.inside = False
            else:
                start = self.buffer.find("<think>")
                if start == -1:
                    # Hold back a possible partial "<think" at the end.
                    keep = next((n for n in range(6, 0, -1) if self.buffer.endswith("<think>"[:n])), 0)
                    out += self.buffer[:len(self.buffer) - keep]
                    self.buffer = self.buffer[len(self.buffer) - keep:]
                    return out
                out += self.buffer[:start]
                self.buffer = self.buffer[start + len("<think>"):]
                self.inside = True
        return out

    def flush(self) -> str:
        rest = "" if self.inside else self.buffer
        self.buffer = ""
        return rest

# ---------------------------------------------------------------------------
# Helper: which model name belongs to a role?
# ---------------------------------------------------------------------------
def model_for_role(role: str) -> str:
    """Look up the Ollama model name for a role such as 'general'."""
    return config.MODELS[role]["name"]


# ---------------------------------------------------------------------------
# Which models are downloaded? (used by the health check and the router)
# ---------------------------------------------------------------------------
async def installed_models() -> set:
    """Names of all models Ollama has downloaded (GET /api/tags). Empty set on error."""
    try:
        async with _make_client() as client:
            response = await client.get("/api/tags")
            response.raise_for_status()
            return {m["name"] for m in response.json().get("models", [])}
    except Exception:
        return set()


def is_installed(name: str, installed: set) -> bool:
    """Ollama sometimes reports "name" and sometimes "name:latest"; accept both."""
    return name in installed or f"{name}:latest" in installed


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
        models.append({
            "role": role,
            "name": name,
            "purpose": purpose,
            "installed": is_installed(name, installed),
            # Phase 4: optional models (e.g. "code") may be missing; the
            # router then falls back to the general model.
            "required": role in config.REQUIRED_ROLES or role == "embedding",
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
async def chat_stream(messages: list, role: str = config.DEFAULT_ROLE,
                      system_prompt: str = None) -> AsyncIterator[str]:
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
    # (Phase 5: the vision path passes its own system prompt.)
    # A message may also carry "images": [base64 strings] for vision models.
    full_messages = [{"role": "system", "content": system_prompt or config.SYSTEM_PROMPT}] + messages

    payload = {
        "model": model_for_role(role),
        "messages": full_messages,
        "stream": True,           # ask Ollama to send the answer in pieces
        # How much text the model may look at in one go (see config.py).
        # Phase 2 needs more room, because document excerpts are added.
        "options": {"num_ctx": config.CONTEXT_TOKENS},
    }
    if _think_setting(role) is not None:
        payload["think"] = _think_setting(role)
    think_filter = ThinkFilter()

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
                text = think_filter.feed(piece.get("message", {}).get("content", ""))
                if text:
                    yield text
                if piece.get("done"):
                    break
    rest = think_filter.flush()
    if rest:
        yield rest


# ---------------------------------------------------------------------------
# Chat WITH TOOLS (NEW IN PHASE 3): used by the agent
# ---------------------------------------------------------------------------
async def chat_with_tools(messages: list, tools: list, role: str = config.DEFAULT_ROLE,
                          temperature: float = None):
    """
    Send the conversation plus the list of available tools, and stream back:
        ("token", "text piece")        while the model writes
        ("tool_calls", [ ... ])        if the model asks to use tools
        ("stats", {...})               how many tokens it generated, why it stopped
        ("content", "full text")       always last: everything it wrote

    How tool calling works: Ollama shows the model the tool descriptions.
    If the model decides a tool is needed, instead of normal text it returns
    a structured request: {"function": {"name": ..., "arguments": {...}}}.
    We pass that back to the agent, which runs the tool.
    """
    options = {"num_ctx": config.CONTEXT_TOKENS}
    if temperature is not None:
        options["temperature"] = temperature
    payload = {
        "model": model_for_role(role),
        "messages": messages,
        "stream": True,
        "options": options,
    }
    if tools:
        payload["tools"] = tools
    if _think_setting(role) is not None:
        payload["think"] = _think_setting(role)

    content, tool_calls, stats = "", [], {}
    think_filter = ThinkFilter()
    async with _make_client() as client:
        async with client.stream("POST", "/api/chat", json=payload) as response:
            if response.status_code != 200:
                body = (await response.aread()).decode(errors="replace")
                raise RuntimeError(f"Ollama returned {response.status_code}: {body}")
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                piece = json.loads(line)
                if "error" in piece:
                    raise RuntimeError(piece["error"])
                message = piece.get("message", {})
                text = think_filter.feed(message.get("content") or "")
                if text:
                    content += text
                    yield ("token", text)
                if message.get("tool_calls"):
                    tool_calls.extend(message["tool_calls"])
                if piece.get("done"):
                    # The last piece carries statistics. eval_count = number of
                    # tokens the model generated. If it is large but we got no
                    # text and no tool call, the reply was lost in parsing.
                    stats = {"eval_count": piece.get("eval_count"),
                             "done_reason": piece.get("done_reason"),
                             "seconds": round((piece.get("total_duration") or 0) / 1e9, 1)}
                    break

    rest = think_filter.flush()
    if rest:
        content += rest
        yield ("token", rest)
    if tool_calls:
        yield ("tool_calls", tool_calls)
    yield ("stats", stats)
    yield ("content", content)


# ---------------------------------------------------------------------------
# Structured output (NEW IN PHASE 6): make the model fill in a fixed form
# ---------------------------------------------------------------------------
async def chat_json(messages: list, schema: dict, role: str = config.DEFAULT_ROLE,
                    system_prompt: str = None) -> dict:
    """
    Ask the model to answer as JSON that MATCHES `schema` (a JSON Schema).

    Ollama's "structured outputs" feature (the "format" setting) forces the
    model's output to follow the schema: it literally cannot write anything
    else. For a small model this is far more reliable than asking nicely for
    JSON in the prompt, and the result can be checked by code.

    temperature 0 = always pick the most likely word: best for copying values.
    """
    full = ([{"role": "system", "content": system_prompt}] if system_prompt else []) + messages
    payload = {
        "model": model_for_role(role),
        "messages": full,
        "stream": False,
        "format": schema,
        "options": {"num_ctx": config.CONTEXT_TOKENS, "temperature": 0},
    }
    if _think_setting(role) is not None:
        payload["think"] = _think_setting(role)
    async with _make_client() as client:
        response = await client.post("/api/chat", json=payload)
        if response.status_code != 200:
            raise RuntimeError(f"Ollama returned {response.status_code}: {response.text}")
        content = response.json().get("message", {}).get("content", "")
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    return json.loads(content)
