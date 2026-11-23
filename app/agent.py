"""
app/agent.py — the agent loop.

A plain chatbot answers once. An AGENT works in a loop until the task is done:

    ┌──────────────────────────────────────────────────────────────┐
    │ 1. THINK   the model reads the task, documents and results   │
    │ 2. ACT     it either asks for a tool ...                     │
    │ 3. OBSERVE ... we run the tool and give it the result        │
    │ 4. REPEAT  back to 1 — until it replies WITHOUT a tool call, │
    │            which is its final answer                         │
    └──────────────────────────────────────────────────────────────┘

This is often called a "ReAct" loop (Reason + Act).

HOW TOOL CALLS REACH US (Phase 3.4) — two modes, chosen in config.py:

  "prompt" (default): WE describe the tools inside the model's instructions,
      in the exact format Qwen models were trained on, and the model answers
      in plain text containing  <tool_call>{"name": ..., "arguments": ...}</tool_call>.
      We read that text ourselves with a FORGIVING parser that repairs common
      small-model mistakes (unescaped quotes or raw line breaks in code).
      Nothing can be silently lost, and it works with any model.

  "native": Ollama reads the tool calls for us. Simpler, but in testing,
      when the model wrote a slightly broken tool call, Ollama discarded it
      silently and we received an EMPTY reply (295 tokens generated, nothing
      returned). Kept as an option for larger models that rarely make mistakes.

Safety rails for a SMALL model (it can get confused):
  - At most AGENT_MAX_STEPS rounds, then it must answer with what it has.
  - The exact same tool call twice in a row is refused ("you already did this").
  - Tool errors are sent back as messages, so the model can fix its input.
  - One retry after an empty reply.
  - Up to two retries when a tool call can't be read, with an explanation.
  - Only ONE tool call per reply is run.

The agent reports what it does as "events" (see app/main.py), so the page
can show every step live: which tool, what input, what result.
"""

import json
import re
from datetime import datetime

import config
from app import knowledge, llm, tools

TOOL_CALL_TAG = "<tool_call>"


# ===========================================================================
# 1. Describing the tools to the model ("prompt" mode)
# ===========================================================================
def tools_prompt() -> str:
    """
    The tool instructions, in the same wording and format as Qwen's own chat
    template, so the model sees exactly what it was trained on.
    """
    signatures = "\n".join(json.dumps(t, ensure_ascii=False) for t in tools.TOOL_DEFINITIONS)
    return (
        "\n\n# Tools\n\n"
        "You may call one or more functions to assist with the user query.\n\n"
        "You are provided with function signatures within <tools></tools> XML tags:\n"
        f"<tools>\n{signatures}\n</tools>\n\n"
        "For each function call, return a json object with function name and arguments "
        "within <tool_call></tool_call> XML tags:\n"
        "<tool_call>\n{\"name\": <function-name>, \"arguments\": <args-json-object>}\n</tool_call>\n\n"
        "Call ONE tool at a time, then wait for its result in <tool_response> tags. "
        "Inside JSON strings, write a line break as \\n and a double quote as \\\"."
    )


# ===========================================================================
# 2. Reading tool calls from the model's text (forgiving parser)
# ===========================================================================
def _unescape(text: str) -> str:
    r"""Turn JSON escapes back into characters: \n -> line break, \" -> ", \\ -> \."""
    table = {"n": "\n", "r": "\r", "t": "\t"}
    return re.sub(r'\\(["\\/nrt])', lambda m: table.get(m.group(1), m.group(1)), text)


def _repair_tool_call(text: str):
    """
    Rescue a tool call whose JSON is broken. The most common small-model
    mistake: Python code containing  print("Zone A")  where the quotes should
    have been written as \\" inside JSON. A normal JSON reader gives up; we
    instead find each argument by its NAME and take the text between its
    opening quote and the point where the next argument (or the end) begins.

    Returns {"function": {...}} or None if it can't be rescued.
    """
    name_match = re.search(r'"name"\s*:\s*"([A-Za-z_]\w*)"', text)
    if not name_match or name_match.group(1) not in tools.TOOL_FUNCTIONS:
        return None
    name = name_match.group(1)
    definition = next(t for t in tools.TOOL_DEFINITIONS if t["function"]["name"] == name)
    properties = definition["function"]["parameters"]["properties"]

    # Where does each argument start?  [(position, key, value_start), ...]
    start = text.find('"arguments"')
    start = start if start >= 0 else name_match.end()
    found = []
    for key in properties:
        match = re.search(r'"%s"\s*:\s*' % re.escape(key), text[start:])
        if match:
            found.append((start + match.start(), key, start + match.end()))
    found.sort()

    arguments = {}
    for index, (_pos, key, value_start) in enumerate(found):
        rest = text[value_start:]
        if not rest.startswith('"'):
            # Not a string (e.g. a list of rows): let the JSON reader try.
            try:
                arguments[key], _ = json.JSONDecoder(strict=False).raw_decode(rest)
            except json.JSONDecodeError:
                pass
            continue
        if index < len(found) - 1:
            # The value ends just before the comma that precedes the next argument.
            segment = text[value_start:found[index + 1][0]]
            match = re.match(r'"(.*)"\s*,\s*$', segment, re.DOTALL)
        else:
            # The last value ends just before the closing braces.
            # (Any number of closing braces: models sometimes add an extra "}".)
            match = re.match(r'"(.*)"(?:\s*\})+\s*$', rest.strip(), re.DOTALL)
        if match:
            arguments[key] = _unescape(match.group(1))

    return {"function": {"name": name, "arguments": arguments}} if arguments else None


def parse_text_tool_calls(content: str) -> list:
    """
    Find tool calls written as text, e.g.
        <tool_call>{"name": "run_python", "arguments": {"code": "..."}}</tool_call>
    (also accepted: a missing closing tag, a ```json block, or bare JSON).
    Returns them in Ollama's format: [{"function": {"name": ..., "arguments": {...}}}].
    Only names of real tools are accepted.
    """
    candidates = re.findall(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", content, re.DOTALL)
    if not candidates:
        candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if not candidates and content.strip().startswith("{"):
        candidates = [content.strip()]

    calls = []
    for text in candidates:
        # Ignore anything around the JSON object: small models sometimes add
        # brackets, "json" labels or stray characters, e.g.  ( {...} )
        # (seen in testing, Phase 3.5). Keep only first "{" .. last "}".
        first, last = text.find("{"), text.rfind("}")
        if first == -1 or last <= first:
            continue
        text = text[first:last + 1]
        try:
            # strict=False accepts real line breaks inside strings.
            data = json.loads(text, strict=False)
        except json.JSONDecodeError:
            repaired = _repair_tool_call(text)          # second chance
            if repaired:
                calls.append(repaired)
            continue
        if isinstance(data, dict) and data.get("name") in tools.TOOL_FUNCTIONS:
            arguments = data.get("arguments", data.get("parameters", {}))
            calls.append({"function": {"name": data["name"], "arguments": arguments}})
    return calls


def _arguments(call: dict) -> dict:
    """Tool arguments usually arrive as a dict, sometimes as a JSON string."""
    arguments = call.get("function", {}).get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments, strict=False)
        except json.JSONDecodeError:
            arguments = {}
    return arguments if isinstance(arguments, dict) else {}


def _visible_part(content: str) -> str:
    """
    The part of the reply to show live on the page: everything BEFORE a tool
    call starts. A trailing "<", "<to", "<tool_c"... is held back too, because
    it might be the beginning of "<tool_call>".
    """
    visible = content.split(TOOL_CALL_TAG)[0]
    for length in range(len(TOOL_CALL_TAG) - 1, 0, -1):
        if visible.endswith(TOOL_CALL_TAG[:length]):
            return visible[:-length]
    return visible


# A word that looks like a file name, e.g. "Approval_Note.docx" or "(2).xlsx".
# (No spaces: names with spaces are matched by their last word, see below.)
_FILE_MENTION = re.compile(r"[\w\-()]+(?:\.[\w\-()]+)*\.(?:docx|xlsx|py|txt|csv|md|json)\b",
                           re.IGNORECASE)
_COPY_SUFFIX = re.compile(r" \(\d+\)(?=\.\w+$)")     # the " (2)" we add to avoid overwriting


def unverified_file_claims(answer: str, ctx: tools.ToolContext) -> list:
    """
    File names mentioned in the answer that were NOT created in this task
    (Phase 3.8). In testing, the model said "I have created a Word document
    named Inspection_Report_P-2105B.docx" although that step had FAILED.
    We check each mentioned name against what the tools actually created,
    and against the documents the agent could read.
    """
    real = [n.lower() for n in ctx.files_created]
    real += [_COPY_SUFFIX.sub("", n) for n in real]               # "x (2).xlsx" -> "x.xlsx"
    real += [n.lower().replace(" (attached)", "") for n in tools.available_documents(ctx)]

    claims = []
    for word in _FILE_MENTION.findall(answer):
        mentioned = word.lower()
        # OK if a real file has exactly this name, or ends with it (the model
        # may have mentioned only the last word of "SOP-MECH-004 ... Monitoring.md").
        if any(name == mentioned or name.endswith(" " + mentioned) or name.endswith(mentioned)
               for name in real):
            continue
        if word not in claims:
            claims.append(word)
    return claims


def _claim_warning(answer: str, ctx: tools.ToolContext):
    """An event warning about files mentioned but never created, or None."""
    claims = unverified_file_claims(answer, ctx)
    if not claims:
        return None
    return {"type": "token", "text": "\n\n⚠ Check: this answer mentions "
            + ", ".join(claims) + ", but no such file was created in this task."}


# ---------------------------------------------------------------------------
# Phase 6.9: maths done "in the head"
# ---------------------------------------------------------------------------
# In the 5 Nov evaluation, the agent answered the corrosion task WITHOUT
# running any code. It wrote "(9.1 - 8.0) / 0.29 = 4.0 years" (correct: 3.79)
# and added the whole remaining life to the date instead of half of it.
# Rule 2 of the system prompt already says "use run_python for all maths", but
# a small model does not always obey. So, like the file-claim check, CODE
# checks it: an answer that shows arithmetic, when no code ran successfully in
# this task, is sent back once with an instruction to calculate with run_python.
# A digit, an operator (* / × ÷, or + - with spaces around them), a digit.
# The spaces rule keeps names like "IR-2026-118" from counting as subtraction.
_ARITHMETIC = re.compile(r"\d\s*\)?\s*(?:[*/×÷]|\s[-+]\s)\s*\(?\s*\d")


def maths_in_answer(answer: str) -> str:
    """The first line of the answer that shows a calculation, or ""."""
    for line in answer.splitlines():
        if _ARITHMETIC.search(line):
            return line.strip()
    return ""


# ===========================================================================
# 3. Helpers for events and the diagnostic log
# ===========================================================================
def _sources_event(ctx: tools.ToolContext) -> dict:
    """The full numbered source list, for the page's "Sources" box."""
    return {"type": "sources", "sources": [
        {"n": i + 1, "source": s["source"], "page": s["page"], "section": s.get("section", ""),
         "score": s.get("score"), "method": s.get("method"), "text": s["text"]}
        for i, s in enumerate(ctx.sources)
    ]}


def _log_reply(step: int, content: str, tool_calls: list, stats: dict) -> None:
    """
    Save one model reply to data/agent_log.jsonl (Phase 3.3), for diagnosing
    problems. Stays on this computer. Never breaks the agent if it fails.
    """
    try:
        config.AGENT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(config.AGENT_LOG_FILE, "a", encoding="utf-8") as log:
            log.write(json.dumps({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "mode": config.TOOL_CALLING_MODE, "step": step, "content": content,
                "tool_calls": tool_calls, "stats": stats,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ===========================================================================
# 4. The loop
# ===========================================================================
async def run(messages: list, attachment_ids: list, use_knowledge_base: bool,
              role: str = config.DEFAULT_ROLE):
    """
    Run the agent on a conversation, using the model for `role` (chosen by
    the router, Phase 4). Yields event dictionaries:
        status, sources, token, draft_reset, step_start, step_end, file, done
    """
    prompt_mode = config.TOOL_CALLING_MODE == "prompt"
    ctx = tools.ToolContext(attachment_ids=list(attachment_ids))
    question = messages[-1]["content"]

    # ---- Step 0: pre-load evidence (same retrieval as Phase 2) ----
    # Giving a small model the relevant excerpts up front means it needs
    # fewer tool calls, and fewer steps = faster and fewer mistakes.
    # (Phase 3.1: documents named in the question are included in full.)
    yield {"type": "status", "text": "Gathering documents…"}
    # Phase 4.1: for CODE tasks, skip the general knowledge-base search (it
    # added unrelated SOPs in testing). Attached documents and documents
    # NAMED in the question are still loaded.
    general_search = use_knowledge_base and role != "code"
    ctx.add_sources(await knowledge.gather_sources(question, ctx.attachment_ids,
                                                   use_knowledge_base,
                                                   search=general_search))
    if ctx.sources:
        yield _sources_event(ctx)

    # ---- Build what the model sees ----
    system = (config.AGENT_SYSTEM_PROMPT
              + "\n\nDocuments you can read: " + "; ".join(tools.available_documents(ctx)))
    if prompt_mode:
        system += tools_prompt()
    task = question
    if ctx.sources:
        task = ("=== EXCERPTS ===\n" + knowledge.format_sources(ctx.sources)
                + "\n=== END OF EXCERPTS ===\n\nTask: " + question)
    conversation = [{"role": "system", "content": system}] + messages[:-1] + \
                   [{"role": "user", "content": task}]

    previous_call = None
    empty_retries = 1          # Phase 3.3: one second chance after an empty reply
    unreadable_retries = 2     # Phase 3.5: chances to rewrite an unreadable tool call
    maths_reminders = 1        # Phase 6.9: one reminder to calculate with run_python
    python_ran = False         # did run_python succeed in this task?
    for step in range(1, config.AGENT_MAX_STEPS + 1):
        yield {"type": "status", "text": f"Thinking (step {step})…"}

        # ---- THINK: ask the model, streaming its words to the page ----
        # In prompt mode we pass NO tools to Ollama: the tools are described
        # in the system message, and we read the tool calls ourselves.
        content, tool_calls, stats, shown = "", [], {}, 0
        offered = [] if prompt_mode else tools.TOOL_DEFINITIONS
        async for kind, value in llm.chat_with_tools(conversation, offered, role=role,
                                                     temperature=config.AGENT_TEMPERATURE):
            if kind == "token":
                # Only stream the text BEFORE a tool call; the raw tool-call
                # JSON is not meant for the user (it appears as a step instead).
                visible = _visible_part(content + value)
                if len(visible) > shown:
                    yield {"type": "token", "text": visible[shown:]}
                    shown = len(visible)
                content += value
            elif kind == "tool_calls":
                tool_calls = value
            elif kind == "stats":
                stats = value
            elif kind == "content":
                content = value
        _log_reply(step, content, tool_calls, stats)

        if not tool_calls:
            tool_calls = parse_text_tool_calls(content)

        # ---- A tool call we could NOT read (Phase 3.5) ----
        # The model clearly tried to use a tool, so this is not its answer.
        # Tell it what went wrong and let it write the call again.
        if not tool_calls and TOOL_CALL_TAG in content and unreadable_retries > 0:
            unreadable_retries -= 1
            yield {"type": "draft_reset", "text": content.split(TOOL_CALL_TAG)[0].strip()}
            yield {"type": "status", "text": "Tool request could not be read — asking the model to rewrite it…"}
            conversation.append({"role": "assistant", "content": content})
            conversation.append({"role": "user", "content":
                "Your tool call could not be read. Write it again as ONE valid JSON object "
                "inside <tool_call></tool_call> tags, exactly like:\n<tool_call>\n"
                '{"name": "run_python", "arguments": {"code": "print(1)"}}\n</tool_call>'})
            continue

        # Phase 3.7: ONE tool call per reply. In testing, the model got stuck
        # and wrote the same tool call 14 times in a single reply. We run only
        # the first and tell the model to call one tool at a time.
        extra_calls = max(0, len(tool_calls) - 1)
        tool_calls = tool_calls[:1]

        # The held-back tail may turn out NOT to be a tool call: show it now.
        if not tool_calls and len(content) > shown:
            yield {"type": "token", "text": content[shown:]}

        # ---- Empty reply: no text and no tool call (Phase 3.3) ----
        if not tool_calls and not content.strip():
            if empty_retries > 0:
                empty_retries -= 1
                yield {"type": "status", "text": "The model returned nothing — asking it again…"}
                conversation.append({"role": "user", "content":
                    "Your last reply was empty. Continue the task: call ONE tool, "
                    "or give your final answer as plain text."})
                continue
            tokens = stats.get("eval_count")
            hint = (f" It generated {tokens} tokens that could not be read as text or as a "
                    "tool call (see data/agent_log.jsonl)." if tokens else "")
            yield {"type": "token", "text": "(The model returned an empty answer twice." + hint +
                   " Try again, or untick Agent mode for this question.)"}
            yield {"type": "done", "files": ctx.files_created}
            return

        # ---- No tool call = the final answer. Done. ----
        if not tool_calls:
            # Phase 6.9: calculations shown, but nothing was calculated by code?
            sum_line = "" if python_ran else maths_in_answer(content)
            if sum_line and maths_reminders > 0:
                maths_reminders -= 1
                # Move the draft into the steps; it is not the final answer.
                yield {"type": "draft_reset", "text": content.strip()}
                yield {"type": "status", "text": "Maths done without code — asking the model to use run_python…"}
                conversation.append({"role": "assistant", "content": content})
                conversation.append({"role": "user", "content":
                    f"You calculated in your head (for example: {sum_line}). Rule 2: ALL maths must "
                    "be done with run_python. Call run_python now: put each input in a variable, use "
                    "the formulas and rules exactly as the documents give them, and print every "
                    "result. Then give your final answer using the printed results."})
                continue
            if sum_line:
                # Asked once already; do not loop. Warn the user honestly instead.
                yield {"type": "token", "text": "\n\n⚠ Check: this answer contains calculations that "
                       "were NOT run as code. Check the numbers before using them."}
            warning = _claim_warning(content, ctx)
            if warning:
                yield warning
            yield {"type": "done", "files": ctx.files_created}
            return

        # The text before the tool call was "thinking out loud", not the
        # answer: tell the page to move it into the step list.
        yield {"type": "draft_reset", "text": content.split(TOOL_CALL_TAG)[0].strip()}
        if prompt_mode:
            conversation.append({"role": "assistant", "content": content})
        else:
            conversation.append({"role": "assistant", "content": content, "tool_calls": tool_calls})

        # ---- ACT + OBSERVE: run each requested tool ----
        for number, call in enumerate(tool_calls):
            name = call.get("function", {}).get("name", "")
            arguments = _arguments(call)
            step_id = f"{step}.{number}"
            yield {"type": "step_start", "id": step_id, "tool": name, "arguments": arguments}

            signature = (name, json.dumps(arguments, sort_keys=True))
            if signature == previous_call:
                result = {"ok": False, "summary": "repeated call skipped",
                          "text": "You already made this exact call and got its result above. "
                                  "Do not repeat it. Continue, or give your final answer."}
            else:
                result = await tools.call_tool(name, arguments, ctx)
            previous_call = signature
            if name == "run_python" and result.get("ok"):
                python_ran = True
            if extra_calls:
                result["text"] += (f"\n(Note: your reply contained {extra_calls + 1} tool calls; "
                                   "only the first was run. Call ONE tool at a time.)")

            yield {"type": "step_end", "id": step_id, "ok": result.get("ok", True),
                   "summary": result.get("summary", ""), "display": result.get("display")}
            for filename in result.get("files", []):
                yield {"type": "file", "name": filename}
            if result.get("sources_changed"):
                yield _sources_event(ctx)

            if prompt_mode:
                # Qwen's own format for tool results.
                conversation.append({"role": "user", "content":
                                     f"<tool_response>\n{result['text']}\n</tool_response>"})
            else:
                conversation.append({"role": "tool", "content": result["text"], "tool_name": name})

    # ---- Out of steps: make the model answer with what it has ----
    yield {"type": "status", "text": "Step limit reached — writing the final answer…"}
    conversation.append({"role": "user", "content":
                         "Stop using tools now. Write your final answer from the results above."})
    content, shown = "", 0
    async for kind, value in llm.chat_with_tools(conversation, tools=[], role=role,
                                                 temperature=config.AGENT_TEMPERATURE):
        if kind == "token":
            # Phase 3.6: even now the model may try another tool call; never
            # show that raw text as the answer.
            content += value
            visible = _visible_part(content)
            if len(visible) > shown:
                yield {"type": "token", "text": visible[shown:]}
                shown = len(visible)
    if not _visible_part(content).strip():
        yield {"type": "token", "text": "(The agent reached its step limit before finishing. "
               "The results of each step are shown above.)"}
    warning = _claim_warning(_visible_part(content), ctx)
    if warning:
        yield warning
    yield {"type": "done", "files": ctx.files_created}
