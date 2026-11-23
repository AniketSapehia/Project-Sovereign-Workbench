"""
app/codeflow.py — the guided CODE workflow (Phase 6.3).

WHY THIS EXISTS
In testing, the small code model (qwen2.5-coder:1.5b) could not follow the
agent's tool protocol: instead of a tool request it echoed the tool
descriptions and invented a fake tool result, so nothing ran. Small models
ARE good at one thing here: writing a block of Python. So, like the approval
note workflow, CODE runs the procedure and the model only writes:

    1. the model writes ONE ```python block, including test calls that print
    2. the WORKBENCH takes that block and runs it in the sandbox
       (the model never has to call a tool)
    3. if it fails, or prints nothing, the error goes back to the model,
       which writes a fixed version (at most CODE_MAX_ATTEMPTS tries)
    4. when it succeeds, the tested code is saved as a .py file, and the
       output shown is the sandbox's real output, never the model's claim

Honest limit: the sandbox proves the code RUNS, not that its formula is
RIGHT. A wrong formula runs fine. That is why the real printed output is
always shown: a wrong result is easy to spot.
"""

import re

import config
from app import knowledge, llm, tools

CODE_MAX_ATTEMPTS = 3

# Phase 6.5: reference data sheets (conversion factors, constants) are ALWAYS
# searched for code tasks. In testing, both the 1.5B and the 3B code model
# got the psi->bar factor wrong from memory (1.47 and 14.5 bar for 100 psi;
# correct: 6.89). Facts come from documents, not from the model's memory.
# Note (Phase 6.6): the example test in CODE_SYSTEM_PROMPT is deliberately a
# neutral placeholder. An earlier version used the psi->bar check value as the
# example, which leaked the answer into every code task and would have made
# evaluation results look better than they are.
REFERENCE_PREFIXES = ("REF-",)

# Phase 6.10: ...but ONLY when the task is about units or constants. In the
# 5 Nov evaluation the zone-classifier task pulled in the reference sheet's
# "1 in/s = 25.4 mm/s" line; the model then converted every reading to in/s
# and compared it with limits in mm/s, so 4.5 mm/s came out as Zone A.
# An unneeded fact is not harmless to a small model: it tends to use it.
_NEEDS_REFERENCE = re.compile(r"\bconver(?:t|ts|ted|ting|sion|sions)\b|\bunits?\b|\bfactors?\b"
                              r"|\bconstants?\b|\bdensity\b|\bgravity\b", re.IGNORECASE)


def needs_reference(question: str) -> bool:
    """True if the task is about unit conversion or physical constants."""
    return bool(_NEEDS_REFERENCE.search(question))

CODE_SYSTEM_PROMPT = (
    "You are a careful Python programmer for engineers at an Indian refinery. "
    "Reply with exactly ONE ```python code block containing the COMPLETE program, "
    "including test calls that print() their results, followed by one to three short "
    "sentences explaining it. Use only the Python standard library unless told otherwise. "
    "Never write what the output will be: the program will be run and its real output shown. "
    "If the documents give a conversion factor, limit or formula, use exactly that value, and "
    "include a test that checks your function against a known value from the documents: for a "
    "NUMBER result, assert abs(your_function(CHECK_INPUT) - CHECK_OUTPUT) < TOLERANCE; for a TEXT "
    "or CATEGORY result, assert your_function(CHECK_INPUT) == EXPECTED_TEXT. The expected value must "
    "be what the function SHOULD return, taken from the documents, never the input itself. "
    "If the documents do not give a factor you need, say so in a comment. "
    "Do not convert units unless the task asks for it: compare values in the units the limits use."
)
# Phase 6.9: in the 5 Nov evaluation the zone classifier was CORRECT (it printed
# A, B, C, D), but the model's own tests were wrong: assert classify(2.1) == 2.1,
# comparing a zone letter with the input number. The old prompt only showed a
# numeric test (abs(... - ...) < TOLERANCE), which pushed the model into that.

_CODE_BLOCK = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_code(text: str):
    """The LAST complete ```python block in the reply (the newest version), or None."""
    blocks = _CODE_BLOCK.findall(text)
    return blocks[-1].strip() if blocks else None


_WORKBENCH_NOTE = re.compile(r"\n*✓ Ran in the sandbox without errors\..*?before using it\.", re.DOTALL)


def _strip_notes(text: str) -> str:
    """Remove the workbench's own notes from an earlier answer."""
    return _WORKBENCH_NOTE.sub("", text).strip()


_ERROR_LINE = re.compile(r'File "ai_code\.py", line (\d+)')


def _failing_line(code: str, error_text: str) -> str:
    """
    Phase 6.9: the line of the model's code where the error happened.
    Python's traceback only says "line 30" (the code came from a string, so
    the line itself is not printed). Small models then guess which line is
    wrong. We look it up and show it, e.g.  line 30: assert f(2.1) == 2.1
    """
    found = _ERROR_LINE.findall(error_text)
    if not found:
        return ""
    number = int(found[-1])                       # the deepest frame = where it failed
    lines = code.splitlines()
    if not 1 <= number <= len(lines):
        return ""
    line = lines[number - 1].strip()
    hint = ""
    if line.startswith("assert"):
        hint = (" This is one of YOUR OWN tests. Decide which is wrong: the function, or the "
                "expected value in the test. The expected value must be what the function should "
                "return (from the documents), not the input.")
    return f"\nThe error happened at line {number}: {line}\n{hint}"


def _file_name(question: str, code: str) -> str:
    """The .py name the user asked for, else the first function's name, else 'program.py'."""
    asked = re.search(r"[\w\-]+\.py\b", question)
    if asked:
        return asked.group(0)
    function = re.search(r"^def\s+(\w+)", code, re.MULTILINE)
    return f"{function.group(1)}.py" if function else "program.py"


async def run(messages: list, attachment_ids: list, use_knowledge_base: bool, role: str = "code"):
    """Yields page events: status, sources, token, draft_reset, step_start/step_end, file, done."""
    ctx = tools.ToolContext(attachment_ids=list(attachment_ids))
    question = messages[-1]["content"]

    # Phase 6.7: earlier answers come back as history, including the
    # workbench's own "✓ Ran in the sandbox…" note. In testing, the model
    # copied that note into its new reply, so it appeared twice. Remove it.
    messages = [{**m, "content": _strip_notes(m["content"])} if m["role"] == "assistant" else m
                for m in messages]

    # Documents attached or NAMED in the question (e.g. "as per SOP-MECH-004")
    # are given to the model, so it can use their limits. No general search:
    # unrelated SOPs only distract a code model (Phase 4.1).
    yield {"type": "status", "text": "Gathering documents…"}
    ctx.add_sources(await knowledge.gather_sources(question, ctx.attachment_ids,
                                                   use_knowledge_base, search=False))
    if use_knowledge_base and needs_reference(question):
        # Reference data sheets only (see REFERENCE_PREFIXES): the right
        # conversion factors, without unrelated SOPs.
        ctx.add_sources(await knowledge.search_knowledge_base(question, only_prefixes=REFERENCE_PREFIXES, k=2))
    task = question
    if ctx.sources:
        yield {"type": "sources", "sources": [
            {"n": i + 1, "source": s["source"], "page": s["page"], "section": s.get("section", ""),
             "score": s.get("score"), "method": s.get("method"), "text": s["text"]}
            for i, s in enumerate(ctx.sources)]}
        task = ("=== DOCUMENTS ===\n" + knowledge.format_sources(ctx.sources)
                + "\n=== END OF DOCUMENTS ===\n\nTask: " + question)
    conversation = messages[:-1] + [{"role": "user", "content": task}]

    for attempt in range(1, CODE_MAX_ATTEMPTS + 1):
        # ---- 1. The model writes the code (streamed to the page) ----
        yield {"type": "status", "text": "Writing code…" if attempt == 1 else f"Fixing the code (attempt {attempt})…"}
        reply = ""
        async for piece in llm.chat_stream(conversation, role=role, system_prompt=CODE_SYSTEM_PROMPT):
            reply += piece
            yield {"type": "token", "text": piece}

        code = extract_code(reply)
        if code is None:
            # No code block at all: ask once more, in plain words.
            yield {"type": "draft_reset", "text": reply.strip()[:400]}
            conversation += [{"role": "assistant", "content": reply},
                             {"role": "user", "content": "Reply with ONE ```python code block containing "
                              "the complete program and test calls that print their results."}]
            continue

        # ---- 2. The WORKBENCH runs it in the sandbox (not the model) ----
        step_id = f"c{attempt}"
        yield {"type": "step_start", "id": step_id, "tool": "run_python", "arguments": {"code": code}}
        result = await tools.call_tool("run_python", {"code": code}, ctx)
        tested = result.get("ok") and "NOT tested" not in result["text"]

        # Save ONLY code that ran AND printed results (i.e. was actually tested).
        saved = None
        if tested:
            path = tools.safe_output_path(_file_name(question, code), {".py"}, ".py")
            path.write_text(code + "\n", encoding="utf-8")
            ctx.files_created.append(path.name)
            saved = path.name
        summary = result.get("summary", "") + (f", tested code saved as {saved}" if saved else "")
        yield {"type": "step_end", "id": step_id, "ok": bool(tested),
               "summary": summary, "display": result.get("display")}

        if tested:
            yield {"type": "file", "name": saved}
            # Careful wording (Phase 6.4): the sandbox proves the code RUNS, not
            # that it is CORRECT. In testing, a 1.5B model's psi->bar code ran
            # fine but gave 1.47 bar for 100 psi (correct: 6.89). Never say "verified".
            yield {"type": "token", "text": "\n\n✓ Ran in the sandbox without errors. The output in the "
                                            "🐍 step is the real output, but it has NOT been checked for "
                                            "correctness: compare it with a value you know before using it."}
            yield {"type": "done", "files": ctx.files_created}
            return

        # ---- 3. Failed or untested: send the real error back for a fix ----
        yield {"type": "draft_reset", "text": ""}
        conversation += [{"role": "assistant", "content": reply},
                         {"role": "user", "content": "The program was run in the sandbox:\n"
                          + result["text"] + _failing_line(code, result["text"])
                          + "\nFix it and reply with the COMPLETE corrected "
                          "```python block, including test calls that print their results."}]

    yield {"type": "token", "text": f"The code could not be made to run correctly in {CODE_MAX_ATTEMPTS} "
                                    "attempts. The errors are shown in the steps above."}
    yield {"type": "done", "files": ctx.files_created}
