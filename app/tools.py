"""
app/tools.py — the agent's tools.

A "tool" is a normal Python function that the AI model is allowed to ask us
to run. The model never runs anything itself: it replies with a request like

    {"name": "run_python", "arguments": {"code": "print(6.4 > 4.5)"}}

and OUR code (app/agent.py) decides whether and how to run it. That keeps
all real actions under the workbench's control.

For each tool there are two parts:
  1. A DESCRIPTION (name, purpose, inputs) in the format Ollama expects.
     The model reads these to decide which tool fits the task.
  2. An IMPLEMENTATION: the Python function that does the work.

Tools in Phase 3:
    search_documents      search the knowledge base + attached documents
    read_document         read a whole document
    run_python            run code in the sandbox (calculations, code tasks)
    write_text_file       save text / code / CSV into the workspace
    create_word_document  make a .docx file (approval notes, reports)
    create_spreadsheet    make a .xlsx file (tables, calculations)

Every file a tool creates goes into the workspace/ folder, and every tool
works offline.
"""

import asyncio
import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.shared import Pt
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

import config
from app import documents, knowledge, sandbox


# ===========================================================================
# Shared state for one agent run
# ===========================================================================

@dataclass
class ToolContext:
    """
    Information the tools share during ONE request:
      attachment_ids  documents attached in the chat
      sources         every excerpt shown to the model so far, in order;
                      its position + 1 is the citation number [n]
      files_created   files made during this request
    """
    attachment_ids: list
    sources: list = field(default_factory=list)
    files_created: list = field(default_factory=list)

    def add_sources(self, new: list) -> list:
        """Add excerpts that aren't already in the list. Returns only the new ones."""
        seen = {(s["source"], s["page"], s["text"]) for s in self.sources}
        added = [s for s in new if (s["source"], s["page"], s["text"]) not in seen]
        self.sources.extend(added)
        return added


class ToolError(Exception):
    """A problem the MODEL should hear about (bad input, file not found...)."""


# ===========================================================================
# Helpers
# ===========================================================================

def _clip(text: str) -> str:
    """Keep tool results short enough for the model's context window."""
    if len(text) <= config.TOOL_RESULT_MAX_CHARS:
        return text
    return text[:config.TOOL_RESULT_MAX_CHARS] + "\n[... cut short ...]"


def safe_output_path(filename: str, allowed: set, default_ext: str) -> Path:
    """
    Turn a file name chosen by the model into a SAFE path inside workspace/.

    - Path(...).name drops any folders, so "../../Windows/x.txt" -> "x.txt".
    - Only letters, digits, spaces and - _ . ( ) are kept.
    - The extension must be one we allow for this tool.
    - An existing file is never overwritten: "note.docx" -> "note (2).docx".
    """
    name = Path(str(filename or "")).name
    name = re.sub(r"[^A-Za-z0-9 _.()\-]", "_", name).strip(" .") or "output"
    stem, ext = Path(name).stem, Path(name).suffix.lower()
    if ext not in allowed:
        stem, ext = Path(name).stem if ext else name, default_ext
    config.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

    path = config.WORKSPACE_DIR / f"{stem}{ext}"
    counter = 2
    while path.exists():
        path = config.WORKSPACE_DIR / f"{stem} ({counter}){ext}"
        counter += 1
    return path


def _find_document(name: str, ctx: ToolContext):
    """
    Find a document by (part of) its name, e.g. "SOP-MECH-004" or
    "inspection report". Attached documents are checked first.
    Returns (display_name, pages).
    """
    wanted = (name or "").strip().lower()
    if not wanted:
        raise ToolError("Give the document name, e.g. read_document(name='SOP-MECH-004').")

    for doc_id in ctx.attachment_ids:
        saved = knowledge.attachment_document(doc_id)
        filename = saved["summary"]["filename"]
        if wanted in filename.lower() or filename.lower() in wanted or wanted in ("attachment", "attached"):
            return filename, saved["pages"]

    if config.KNOWLEDGE_BASE_DIR.exists():
        for path in sorted(config.KNOWLEDGE_BASE_DIR.iterdir()):
            if path.suffix.lower() in config.SUPPORTED_EXTENSIONS and wanted in path.name.lower():
                return path.name, documents.extract(path)

    raise ToolError(f"No document matching '{name}'. Available: " + ", ".join(available_documents(ctx)))


def available_documents(ctx: ToolContext) -> list:
    """Names of all documents the agent can read (shown in its instructions)."""
    names = []
    for doc_id in ctx.attachment_ids:
        try:
            names.append(knowledge.attachment_document(doc_id)["summary"]["filename"] + " (attached)")
        except Exception:
            pass
    if config.KNOWLEDGE_BASE_DIR.exists():
        names += sorted(p.name for p in config.KNOWLEDGE_BASE_DIR.iterdir()
                        if p.suffix.lower() in config.SUPPORTED_EXTENSIONS)
    return names


# ===========================================================================
# Tool 1: search_documents
# ===========================================================================

async def search_documents(ctx: ToolContext, query: str = "") -> dict:
    if not query.strip():
        raise ToolError("search_documents needs a 'query', e.g. 'bearing temperature limits'.")

    hits = await knowledge.search_knowledge_base(query)
    for doc_id in ctx.attachment_ids:
        hits += await knowledge.search_attachment(doc_id, query, config.TOP_K)

    # Phase 3.7: skip parts of documents that are already given IN FULL
    # (in testing, searching an already-loaded SOP added 7 duplicate excerpts).
    whole = {x["source"] for x in ctx.sources if x.get("section") == "(whole document)"}
    skipped = sorted({h["source"] for h in hits if h["source"] in whole})
    hits = [h for h in hits if h["source"] not in whole]

    first_new = len(ctx.sources) + 1
    added = ctx.add_sources(hits)
    if not added:
        text = ("No NEW excerpts found (the relevant ones may already be in the "
                "excerpts above). Use what you have, or try different words.")
        if skipped:
            text += " These documents are already given in full above: " + ", ".join(skipped) + "."
    else:
        text = ("New excerpts (cite them by number):\n\n"
                + knowledge.format_sources(added, start=first_new))
    return {"text": _clip(text), "summary": f"{len(added)} new excerpt(s) found",
            "sources_changed": bool(added)}


# ===========================================================================
# Tool 2: read_document
# ===========================================================================

async def read_document(ctx: ToolContext, name: str = "") -> dict:
    display_name, pages = await asyncio.to_thread(_find_document, name, ctx)
    full_text = "\n\n".join(f"--- page {p['page']} ---\n{p['text']}" for p in pages)

    # Register the whole document as one citable source.
    added = ctx.add_sources([{"text": full_text, "source": display_name, "page": 1,
                              "section": "(whole document)", "method": pages[0]["method"] if pages else "text",
                              "score": None}])
    number = ctx.sources.index(added[0]) + 1 if added else None
    header = f"[{number}] " if number else ""
    return {"text": _clip(f"{header}{display_name}:\n{full_text}"),
            "summary": f"read {display_name} ({len(full_text):,} characters)",
            "sources_changed": bool(added)}


# ===========================================================================
# Tool 3: run_python
# ===========================================================================

async def run_python(ctx: ToolContext, code: str = "", save_as: str = "") -> dict:
    if not code.strip():
        raise ToolError("run_python needs 'code'. Remember to print() the results.")

    # The sandbox is slow, blocking work, so it runs in a separate thread.
    result = await asyncio.to_thread(sandbox.run_python, code)
    files = list(result["new_files"])

    # Phase 3.6: "save_as" saves the code as a file, but ONLY if it ran
    # without errors. The model writes the code once, and the saved file is
    # exactly the code that was tested (no copy-paste mistakes).
    saved_note = ""
    if save_as:
        if result["ok"]:
            path = safe_output_path(save_as, {".py"}, ".py")
            path.write_text(code, encoding="utf-8")
            files.append(path.name)
            saved_note = f"\nTested code saved as {path.name}."
        else:
            saved_note = f"\nNOT saved as {save_as}, because the code failed. Fix it first."

    ctx.files_created.extend(files)
    status = "Code ran successfully." if result["ok"] else "The code FAILED."

    # Phase 4.1: code that prints nothing has not been TESTED. In testing,
    # the model defined a function without calling it, then described a
    # test output it had never seen. Say it plainly, to the model and user.
    untested = result["ok"] and result["output"].startswith("(the code printed nothing")
    if untested:
        saved_note = saved_note.replace("Tested code saved", "Code saved (UNTESTED)")
        saved_note += ("\nWARNING: nothing was printed, so the code was NOT tested. Do not "
                       "claim any test results. Add test calls with print() and run it again.")

    text = f"{status}\nOutput:\n{result['output']}{saved_note}"
    if result["new_files"]:
        text += "\nFiles created by the code: " + ", ".join(result["new_files"])
    if not result["ok"]:
        text += "\nFix the code and call run_python again, or explain the problem."
    summary = f"{'ok' if result['ok'] else 'error'} in {result['seconds']}s"
    if untested:
        summary = f"⚠ ran, but nothing tested ({result['seconds']}s)"
    if save_as and result["ok"]:
        summary += f", saved {files[-1]}"
    return {"text": _clip(text), "summary": summary, "ok": result["ok"],
            "display": {"code": code, "output": result["output"] + saved_note},
            "files": files}


# ===========================================================================
# Tool 4: write_text_file
# ===========================================================================

TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".py", ".json", ".log"}


async def write_text_file(ctx: ToolContext, filename: str = "", content: str = "") -> dict:
    if not content:
        raise ToolError("write_text_file needs 'content'.")
    path = safe_output_path(filename, TEXT_EXTENSIONS, ".txt")
    path.write_text(content, encoding="utf-8")
    ctx.files_created.append(path.name)
    return {"text": f"Saved {path.name} ({len(content):,} characters).",
            "summary": f"saved {path.name}", "files": [path.name]}


# ===========================================================================
# Tool 5: create_word_document
# ===========================================================================

def plain_math(text: str) -> str:
    """
    Turn common LaTeX maths into plain text (Phase 3.1), e.g.
        \\frac{9.1 - 8.0}{0.29}  ->  (9.1 - 8.0) / (0.29)
    Same idea as plainMath() in static/app.js, so Word files stay readable.
    """
    if not re.search(r"\\[a-zA-Z\[\]()]", text):
        return text
    text = re.sub(r"\\\[|\\\]|\\\(|\\\)", "", text)
    rules = [
        (r"\\(?:text|mathrm|mathbf|mathit|operatorname)\{([^{}]*)\}", r"\1"),
        (r"_\{([^{}]*)\}", r"_\1"),
        (r"\^\{([^{}]*)\}", r"^\1"),
        (r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1) / (\2)"),
    ]
    for _ in range(6):                      # repeat: LaTeX can be nested
        before = text
        for pattern, replacement in rules:
            text = re.sub(pattern, replacement, text)
        if text == before:
            break
    symbols = {"\\times": "×", "\\cdot": "·", "\\approx": "≈", "\\leq": "≤", "\\le": "≤",
               "\\geq": "≥", "\\ge": "≥", "\\neq": "≠", "\\min": "min", "\\max": "max",
               "\\left": "", "\\right": "", "\\,": " ", "\\;": " ", "\\%": "%", "\\_": "_",
               "\\Delta": "Δ"}
    # Longest first, so "\\left" is handled before "\\le" (which would turn
    # "\\left(" into "≤ft(").
    for latex, plain in sorted(symbols.items(), key=lambda item: -len(item[0])):
        text = text.replace(latex, plain)
    return text


def _add_formatted_text(paragraph, text: str) -> None:
    """Add text to a Word paragraph, turning **bold** parts into bold."""
    for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
        if part:
            paragraph.add_run(part).bold = (i % 2 == 1)


def _cited_sources(content: str, ctx: ToolContext) -> list:
    """The sources whose numbers [n] appear in the text (or all, if none appear)."""
    numbers = {int(n) for n in re.findall(r"\[(\d+)\]", content)}
    cited = [(i + 1, s) for i, s in enumerate(ctx.sources) if (i + 1) in numbers]
    return cited or list(enumerate(ctx.sources, start=1))


def build_docx(path: Path, title: str, content: str, ctx: ToolContext) -> None:
    """
    Build a Word file from simple text the model writes:
        ## Heading        -> heading
        - item / * item   -> bullet point
        1. item           -> numbered item
        | a | b |         -> table (consecutive lines starting with |)
        **text**          -> bold
        other lines       -> normal paragraphs
    A "Sources" section and a review notice are added automatically, so every
    document the AI drafts can be traced back to the company's own documents.
    """
    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)

    title, content = plain_math(title or ""), plain_math(content or "")
    doc.add_heading(title or "Document", level=0)
    meta = doc.add_paragraph()
    meta.add_run(f"Draft prepared on {datetime.now().strftime('%d-%m-%Y %H:%M')} "
                 "by Sovereign Workbench (on-premise AI)").italic = True

    table_rows = []

    def flush_table():
        # Turn collected "| a | b |" lines into a real Word table.
        rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in table_rows
                if not re.fullmatch(r"[\s|:\-]+", r)]          # skip |---|---| lines
        table_rows.clear()
        if not rows:
            return
        width = max(len(r) for r in rows)
        table = doc.add_table(rows=0, cols=width)
        table.style = "Table Grid"
        for r_index, row in enumerate(rows):
            cells = table.add_row().cells
            for c_index in range(width):
                value = row[c_index] if c_index < len(row) else ""
                cells[c_index].text = ""
                run = cells[c_index].paragraphs[0].add_run(value.replace("**", ""))
                run.bold = r_index == 0                       # header row in bold

    for raw in (content or "").splitlines():
        line = raw.rstrip()
        if line.strip().startswith("|"):
            table_rows.append(line)
            continue
        flush_table()

        stripped = line.strip()
        heading = re.match(r"^(#{1,3})\s+(.*)", stripped)
        if not stripped:
            continue
        elif heading:
            doc.add_heading(heading.group(2).strip("* "), level=len(heading.group(1)))
        elif re.match(r"^[-*•]\s+", stripped):
            _add_formatted_text(doc.add_paragraph(style="List Bullet"), re.sub(r"^[-*•]\s+", "", stripped))
        elif re.match(r"^\d+[.)]\s+", stripped):
            _add_formatted_text(doc.add_paragraph(style="List Number"), re.sub(r"^\d+[.)]\s+", "", stripped))
        else:
            _add_formatted_text(doc.add_paragraph(), stripped)
    flush_table()

    # ---- Traceability: list the sources behind this document ----
    cited = _cited_sources(content or "", ctx)
    if cited:
        doc.add_heading("Sources", level=2)
        for number, s in cited:
            label = f"[{number}] {s['source']}, page {s['page']}"
            if s.get("section"):
                label += f", {s['section']}"
            if s.get("method") == "ocr":
                label += " (read by OCR — verify values against the original)"
            doc.add_paragraph(label, style="List Bullet")

    notice = doc.add_paragraph()
    notice.add_run("This is an AI-generated draft. All values must be checked against the "
                   "source documents before signature.").italic = True
    doc.save(path)


async def create_word_document(ctx: ToolContext, filename: str = "", title: str = "",
                               content: str = "") -> dict:
    if not content.strip():
        raise ToolError("create_word_document needs 'content' (use ## for headings, - for bullets).")
    path = safe_output_path(filename or title, {".docx"}, ".docx")
    await asyncio.to_thread(build_docx, path, title, content, ctx)
    ctx.files_created.append(path.name)
    return {"text": f"Word document saved as {path.name}.",
            "summary": f"created {path.name}", "files": [path.name]}


# ===========================================================================
# Tool 6: create_spreadsheet
# ===========================================================================

def _cell_value(value):
    """Turn text like '6.4' or '1,25,000' into numbers; keep '=SUM(...)' formulas."""
    if isinstance(value, (int, float)) or value is None:
        return value
    text = str(value).strip()
    if text.startswith("="):
        return text                                   # Excel formula
    plain = text.replace(",", "")
    try:
        return int(plain) if re.fullmatch(r"-?\d+", plain) else float(plain)
    except ValueError:
        return text


def build_xlsx(path: Path, rows: list, sheet_name: str) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = (sheet_name or "Sheet1")[:31]       # Excel's limit is 31 characters
    for row in rows:
        sheet.append([_cell_value(v) for v in row])

    # Header row: bold on a light green background.
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="E2F4EA")

    # Make each column roughly as wide as its longest value.
    for column in sheet.columns:
        longest = max(len(str(c.value)) if c.value is not None else 0 for c in column)
        sheet.column_dimensions[column[0].column_letter].width = min(max(10, longest + 2), 60)
    workbook.save(path)


async def create_spreadsheet(ctx: ToolContext, filename: str = "", rows=None,
                             sheet_name: str = "Sheet1") -> dict:
    # Small models sometimes send the table as CSV text instead of a list.
    if isinstance(rows, str):
        rows = list(csv.reader(io.StringIO(rows.strip())))
    if not rows or not isinstance(rows, list) or not all(isinstance(r, list) for r in rows):
        raise ToolError("create_spreadsheet needs 'rows': a list of rows, first row = headers, "
                        "e.g. [[\"Item\", \"Cost\"], [\"Bearings\", 65000]].")
    path = safe_output_path(filename, {".xlsx"}, ".xlsx")
    await asyncio.to_thread(build_xlsx, path, rows, sheet_name)
    ctx.files_created.append(path.name)
    return {"text": f"Spreadsheet saved as {path.name} ({len(rows)} rows).",
            "summary": f"created {path.name}", "files": [path.name]}


# ===========================================================================
# The registry: tool descriptions (for the model) + implementations
# ===========================================================================

def _tool(name: str, description: str, properties: dict, required: list) -> dict:
    """Build one tool description in the JSON format Ollama expects."""
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }}


TOOL_DEFINITIONS = [
    _tool("search_documents",
          "Search the company's SOPs, manuals, memos and the attached documents for "
          "information. Returns numbered excerpts to cite.",
          {"query": {"type": "string", "description": "What to look for, in plain words"}},
          ["query"]),
    _tool("read_document",
          "Read the full text of one document by (part of) its name.",
          {"name": {"type": "string", "description": "Document name, e.g. SOP-MECH-004"}},
          ["name"]),
    _tool("run_python",
          "Run Python code in a secure sandbox and get what it prints. Use it for ALL "
          "calculations and comparisons with limits, and to test code. Always print() results. "
          "To save code as a .py file, give save_as: the exact code is saved only if it runs "
          "without errors.",
          {"code": {"type": "string", "description": "Python code to run"},
           "save_as": {"type": "string", "description": "Optional file name, e.g. vibration_zones.py"}},
          ["code"]),
    _tool("write_text_file",
          "Save text, CSV or JSON to a file in the workspace. (For Python code, use "
          "run_python with save_as instead, so the code is tested before saving.)",
          {"filename": {"type": "string", "description": "e.g. corrosion_calc.py"},
           "content": {"type": "string", "description": "The full file content"}},
          ["filename", "content"]),
    _tool("create_word_document",
          "Create a Word (.docx) document such as an approval note or report. In content, "
          "use '## ' for headings, '- ' for bullets, '| a | b |' rows for tables, and cite "
          "sources like [1].",
          {"filename": {"type": "string", "description": "e.g. Approval_Note_P-2105B.docx"},
           "title": {"type": "string", "description": "Document title"},
           "content": {"type": "string", "description": "The document body"}},
          ["filename", "title", "content"]),
    _tool("create_spreadsheet",
          "Create an Excel (.xlsx) file from a table. First row is the header. "
          "Text starting with '=' becomes an Excel formula.",
          {"filename": {"type": "string", "description": "e.g. readings.xlsx"},
           "rows": {"type": "array", "description": "List of rows; each row is a list of cell values",
                    "items": {"type": "array", "items": {}}},
           "sheet_name": {"type": "string", "description": "Optional sheet name"}},
          ["filename", "rows"]),
]

TOOL_FUNCTIONS = {
    "search_documents": search_documents,
    "read_document": read_document,
    "run_python": run_python,
    "write_text_file": write_text_file,
    "create_word_document": create_word_document,
    "create_spreadsheet": create_spreadsheet,
}


async def call_tool(name: str, arguments: dict, ctx: ToolContext) -> dict:
    """
    Run one tool safely. Any problem becomes a short message for the model
    ("ok": False) instead of crashing the agent, so the model can correct
    itself on the next step.
    """
    function = TOOL_FUNCTIONS.get(name)
    if function is None:
        return {"ok": False, "text": f"Unknown tool '{name}'. Available tools: "
                                     + ", ".join(TOOL_FUNCTIONS), "summary": "unknown tool"}
    if not isinstance(arguments, dict):
        return {"ok": False, "text": "Tool arguments must be a JSON object.", "summary": "bad input"}

    # Ignore any argument names the function doesn't have (small models
    # sometimes invent extra ones).
    accepted = function.__code__.co_varnames[1:function.__code__.co_argcount]
    clean = {k: v for k, v in arguments.items() if k in accepted}
    try:
        result = await function(ctx, **clean)
        result.setdefault("ok", True)
        return result
    except ToolError as error:
        return {"ok": False, "text": f"Tool error: {error}", "summary": str(error)[:80]}
    except Exception as error:
        return {"ok": False, "text": f"Tool '{name}' failed: {error}", "summary": f"failed: {error}"[:80]}
