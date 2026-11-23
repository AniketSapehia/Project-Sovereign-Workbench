/*
  static/app.js — the page's behaviour (runs in the browser).

  Jobs:
    1. Health check: are Ollama, the models and OCR ready?
    2. Chat: send the conversation to /api/chat and show the answer as it
       streams in, plus the SOURCES it was based on (Phase 2).
    3. Attachments (Phase 2): upload a file to /api/upload and remember its
       id, so following questions can use it.
    4. Knowledge base panel (Phase 2): show status, rebuild the index.
    5. Network monitor: refresh every 2 seconds.

  Safety note: text from the model or from documents is always inserted
  with `textContent` (or as text nodes), never `innerHTML`, so it can never
  run as code in the page.
*/

// ---------------------------------------------------------------
// Grab the page elements we need (defined in index.html)
// ---------------------------------------------------------------
const messagesEl    = document.getElementById("messages");
const formEl        = document.getElementById("chat-form");
const promptEl      = document.getElementById("prompt");
const sendBtn       = document.getElementById("send-btn");
const clearBtn      = document.getElementById("clear-btn");
const attachBtn     = document.getElementById("attach-btn");
const fileInput     = document.getElementById("file-input");
const attachmentsEl = document.getElementById("attachments");
const useKbEl       = document.getElementById("use-kb");
const agentModeEl   = document.getElementById("agent-mode");
const modelChoiceEl = document.getElementById("model-choice");
const reindexBtn    = document.getElementById("reindex-btn");
const modelStatusEl = document.getElementById("model-status");
const ocrStatusEl   = document.getElementById("ocr-status");
const badgeEl       = document.getElementById("sovereign-badge");

// The whole conversation so far. Sent to the server with every message,
// because the model itself has no memory between requests.
let history = [];

// Documents attached to this conversation: [{id, filename, ...summary}]
let attachments = [];

// True while an answer is streaming or a file is uploading.
let busy = false;


// Small helper: create an element with a class and (safe) text.
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// Phase 4: fill the model dropdown with the chat models from config.py
// (not the embedding model, which can't chat). Done once.
function fillModelMenu(models) {
  if (modelChoiceEl.options.length > 1) return;
  for (const m of models.filter(m => m.role !== "embedding")) {
    const option = document.createElement("option");
    option.value = m.role;
    option.textContent = `${m.role} (${m.name})${m.installed ? "" : " – not installed"}`;
    modelChoiceEl.appendChild(option);
  }
}

// Small helper to colour a status "pill" green / red / grey.
function setPill(node, kind, text) {
  node.className = `pill pill-${kind}`;
  node.textContent = text;
}

function setBusy(value) {
  busy = value;
  sendBtn.disabled = value;
  attachBtn.disabled = value;
}


// ===============================================================
// 1. Health check (models + OCR)
// ===============================================================
async function checkHealth() {
  try {
    const data = await (await fetch("/api/health")).json();

    if (!data.ollama_running) {
      setPill(modelStatusEl, "bad", "Ollama not running");
    } else {
      // Phase 4: required models missing = red; optional ones (e.g. code)
      // missing = amber, because the router falls back to the general model.
      const missingRequired = data.models.filter(m => !m.installed && m.required);
      const missingOptional = data.models.filter(m => !m.installed && !m.required);
      const installed = data.models.filter(m => m.installed).map(m => `${m.role}: ${m.name}`).join(" · ");
      if (missingRequired.length > 0) {
        setPill(modelStatusEl, "bad", `Model missing: ${missingRequired.map(m => m.name).join(", ")}`);
      } else if (missingOptional.length > 0) {
        setPill(modelStatusEl, "warn", `${installed} · not installed: ${missingOptional.map(m => m.name).join(", ")}`);
      } else {
        setPill(modelStatusEl, "good", installed);
      }
      fillModelMenu(data.models);
    }

    if (data.ocr && data.ocr.available) {
      setPill(ocrStatusEl, "good", `OCR: Tesseract ${data.ocr.version}`);
    } else {
      setPill(ocrStatusEl, "bad", "OCR: Tesseract not found");
    }
  } catch (err) {
    setPill(modelStatusEl, "bad", "Server not reachable");
  }
}


// ===============================================================
// 2. Chat
// ===============================================================

// Create a message bubble and add it to the chat.
function addBubble(role, text) {
  const hint = messagesEl.querySelector(".empty-hint");
  if (hint) hint.remove();                    // remove the welcome text once

  const div = el("div", `msg ${role}`, text);
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;  // auto-scroll down
  return div;
}

// Phase 3.1: small models often write maths in LaTeX, e.g.
//   \[ t_{\text{life}} = \frac{9.1 - 8.0}{0.29} \]
// which looks like gibberish on the page. This turns common LaTeX into
// plain text:   t_life = (9.1 - 8.0) / (0.29)
function plainMath(text) {
  if (!/\\[a-zA-Z\[\]()]/.test(text)) return text;        // no LaTeX: leave as is
  let t = text.replace(/\\\[|\\\]|\\\(|\\\)/g, "");      // remove \[ \] \( \)
  const rules = [
    [/\\(?:text|mathrm|mathbf|mathit|operatorname)\{([^{}]*)\}/g, "$1"],  // \text{x} -> x
    [/_\{([^{}]*)\}/g, "_$1"],                            // t_{min} -> t_min
    [/\^\{([^{}]*)\}/g, "^$1"],                           // x^{2}  -> x^2
    [/\\frac\{([^{}]*)\}\{([^{}]*)\}/g, "($1) / ($2)"],   // \frac{a}{b} -> (a) / (b)
  ];
  // Repeat until nothing changes, because LaTeX can be nested.
  for (let i = 0; i < 6; i++) {
    const before = t;
    for (const [pattern, replacement] of rules) t = t.replace(pattern, replacement);
    if (t === before) break;
  }
  const symbols = { "\\times": "×", "\\cdot": "·", "\\approx": "≈", "\\leq": "≤", "\\le": "≤",
    "\\geq": "≥", "\\ge": "≥", "\\neq": "≠", "\\min": "min", "\\max": "max", "\\left": "",
    "\\right": "", "\\,": " ", "\;": " ", "\\%": "%", "\\_": "_", "\\Delta": "Δ" };
  // Longest first, so "\\left" is handled before "\\le" (else "\\left(" -> "≤ft(").
  const ordered = Object.entries(symbols).sort((a, b) => b[0].length - a[0].length);
  for (const [latex, plain] of ordered) t = t.split(latex).join(plain);
  return t;
}

// Show the answer text, turning "[1]" into clickable citation markers.
// We build the nodes ourselves (text nodes + <span>s), so it stays safe.
function renderAnswer(container, text, sourcesBox) {
  container.textContent = "";
  text = plainMath(text);
  const parts = text.split(/(\[\d+\])/);      // keeps the [n] pieces
  for (const part of parts) {
    const match = part.match(/^\[(\d+)\]$/);
    if (match && sourcesBox) {
      const cite = el("span", "cite", part);
      cite.title = "Show this source";
      cite.addEventListener("click", () => openSource(sourcesBox, match[1]));
      container.appendChild(cite);
    } else {
      container.appendChild(document.createTextNode(part));
    }
  }
}

// Clicking a [n] marker opens that source in the list and highlights it.
function openSource(sourcesBox, number) {
  const item = sourcesBox.querySelector(`details[data-n="${number}"]`);
  if (!item) return;
  item.open = true;
  item.classList.add("flash");
  item.scrollIntoView({ behavior: "smooth", block: "nearest" });
  setTimeout(() => item.classList.remove("flash"), 1500);
}

// Build the "Sources" box shown under an answer.
function buildSources(sources) {
  const box = el("div", "sources");
  box.appendChild(el("div", "sources-title", "Sources used for this answer"));

  for (const s of sources) {
    const item = el("details");
    item.dataset.n = s.n;

    const summary = el("summary");
    let label = `[${s.n}] ${s.source} — page ${s.page}`;
    if (s.section) label += ` — ${s.section}`;
    summary.appendChild(document.createTextNode(label));
    if (s.score !== null && s.score !== undefined) {
      summary.appendChild(el("span", "tag", `match ${Math.round(s.score * 100)}%`));
    }
    if (s.method === "ocr") summary.appendChild(el("span", "tag ocr", "OCR"));

    item.appendChild(summary);
    item.appendChild(el("pre", "", s.text));  // the exact excerpt the model saw
    box.appendChild(item);
  }
  return box;
}

async function sendMessage(text) {
  setBusy(true);

  history.push({ role: "user", content: text });
  addBubble("user", text);

  // The assistant bubble is built from parts (top to bottom):
  //   stepsBox   - Phase 3: the agent's steps (tool calls), shown live
  //   statusLine - "Searching knowledge base…", "Thinking (step 2)…"
  //   answerEl   - the answer text, with clickable [1] citations
  //   filesBox   - Phase 3: download links for files the agent created
  //   sourcesBox - the excerpts the answer is based on
  const bubble = addBubble("assistant", "");
  const statusLine = el("div", "status", "Starting…");
  const answerEl = el("div");
  bubble.append(statusLine, answerEl);

  const started = performance.now();
  let answer = "";
  let sourcesBox = null;
  let stepsBox = null;     // created when the first agent step happens
  let filesBox = null;     // created when the first file is made
  const stepItems = {};    // step id -> its <details> element
  let failed = false;
  let warned = false;      // true if the server stopped before asking the model
  let routedModel = "";    // Phase 4: the model that answered

  // Show or hide the status line (it comes back between agent steps).
  const showStatus = (text) => {
    statusLine.textContent = text;
    statusLine.style.display = "";
  };

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: history,
        use_knowledge_base: useKbEl.checked,
        attachment_ids: attachments.map(a => a.id),
        agent_mode: agentModeEl.checked,
        model_choice: modelChoiceEl.value,
      }),
    });

    // The server sends one JSON event per line. Network chunks don't
    // line up with lines, so we keep a buffer and only handle full lines.
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let newline;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (!line) continue;
        const event = JSON.parse(line);

        if (event.type === "status") {
          showStatus(event.text);

        } else if (event.type === "route") {
          // Phase 4: show which model the router chose, and why.
          bubble.prepend(buildRouteBadge(event));
          routedModel = event.model;

        } else if (event.type === "sources") {
          // May arrive again when an agent tool finds more excerpts.
          sourcesBox = buildSources(event.sources);
          renderAnswer(answerEl, answer, sourcesBox);

        } else if (event.type === "token") {
          statusLine.style.display = "none";      // words are arriving
          answer += event.text;
          renderAnswer(answerEl, answer, sourcesBox);

        } else if (event.type === "draft_reset") {
          // Phase 3: what the model just wrote was "thinking" before a tool
          // call, not the final answer. Move it into the steps list.
          if (!stepsBox) stepsBox = createStepsBox(bubble);
          if (event.text && event.text.trim()) addNote(stepsBox, event.text.trim());
          answer = "";
          answerEl.textContent = "";

        } else if (event.type === "step_start") {
          if (!stepsBox) stepsBox = createStepsBox(bubble);
          stepItems[event.id] = addStep(stepsBox, event.tool, event.arguments);
          showStatus(`Running ${event.tool}…`);

        } else if (event.type === "step_end") {
          finishStep(stepItems[event.id], event);

        } else if (event.type === "file") {
          if (!filesBox) {
            filesBox = el("div", "files-box");
            filesBox.appendChild(el("div", "sources-title", "Files created"));
            bubble.appendChild(filesBox);
          }
          filesBox.appendChild(fileLink(event.name));
          refreshFiles();

        } else if (event.type === "warning") {
          // Phase 2.1: the server stopped BEFORE asking the model,
          // e.g. "this report" was mentioned but nothing is attached.
          failed = true;
          warned = true;
          statusLine.remove();
          answerEl.appendChild(el("div", "warning-text", `⚠ ${event.text}`));

        } else if (event.type === "error") {
          failed = true;
          statusLine.remove();
          answerEl.appendChild(el("div", "bad-text", `[Error] ${event.text}`));
        }
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }
    }

    statusLine.remove();
    if (filesBox) bubble.appendChild(filesBox);     // keep files below the answer
    if (sourcesBox) bubble.appendChild(sourcesBox);

    // Footer: time taken, steps, and how many sources were used.
    const seconds = ((performance.now() - started) / 1000).toFixed(1);
    const n = sourcesBox ? sourcesBox.querySelectorAll("details").length : 0;
    const steps = Object.keys(stepItems).length;
    bubble.appendChild(el("span", "meta", warned
      ? "stopped before asking the model"
      : `${seconds}s · generated locally` + (routedModel ? ` by ${routedModel}` : "") +
        (steps ? ` · ${steps} tool step(s)` : "") +
        (n ? ` · ${n} source(s)` : " · no documents used")));

    if (failed || answer === "") {
      history.pop();            // don't keep a question that got no answer
    } else {
      history.push({ role: "assistant", content: answer });
    }
  } catch (err) {
    statusLine.remove();
    answerEl.textContent = `[Error] Could not reach the workbench server: ${err}`;
    history.pop();
  } finally {
    setBusy(false);
    promptEl.focus();
  }
}


// ===============================================================
// 2a. Router decision badge (Phase 4)
// ===============================================================
const ROLE_ICONS = { general: "💬", code: "💻", vision: "👁" };

// "🧭 code model · qwen2.5-coder:3b — closest to code tasks (0.84); keywords: python"
// Click it to see every role's score.
function buildRouteBadge(d) {
  const box = el("details", "route-badge" + (d.fallback ? " fallback" : ""));
  const summary = el("summary");
  summary.appendChild(el("span", "", `🧭 ${ROLE_ICONS[d.role] || ""} ${d.role} model · ${d.model}`));
  summary.appendChild(el("span", "muted", ` — ${d.reason}`));
  box.appendChild(summary);
  const scores = Object.entries(d.scores || {}).sort((a, b) => b[1] - a[1]);
  if (scores.length) {
    box.appendChild(el("div", "route-scores",
      "Scores: " + scores.map(([role, score]) => `${role} ${score.toFixed(2)}`).join(" · ") +
      `   (method: ${d.method})`));
  }
  return box;
}


// ===============================================================
// 2b. Agent steps (Phase 3)
// ===============================================================

// A collapsible "Agent steps" list at the top of the answer bubble.
function createStepsBox(bubble) {
  const box = el("details", "steps-box");
  box.open = true;
  box.appendChild(el("summary", "", "Agent steps"));
  // Keep the router badge (Phase 4) on top, steps right below it.
  const badge = bubble.querySelector(".route-badge");
  if (badge) badge.after(box); else bubble.prepend(box);
  return box;
}

// The model's "thinking out loud" before it called a tool.
function addNote(box, text) {
  box.appendChild(el("div", "step-note", `💭 ${plainMath(text)}`));
}

const TOOL_ICONS = {
  search_documents: "🔎", read_document: "📖", run_python: "🐍",
  write_text_file: "📝", create_word_document: "📄", create_spreadsheet: "📊",
  // Phase 6: steps of the flagship workflow
  read_report: "📑", extract_findings: "🧾", verify_values: "✅",
  read_sop_rules: "📏", apply_rules: "⚖️", draft_text: "✍️",
};

// A short, readable description of the tool's input.
function describeArguments(tool, args) {
  args = args || {};
  if (tool === "search_documents") return `"${args.query || ""}"`;
  if (tool === "read_document") return args.name || "";
  if (tool === "run_python") return `${(args.code || "").split("\n").length} line(s) of code` + (args.save_as ? `, save as ${args.save_as}` : "");
  if (tool === "create_spreadsheet") return `${args.filename || ""} (${Array.isArray(args.rows) ? args.rows.length : "?"} rows)`;
  return args.filename || "";
}

// One step = an expandable item: "🐍 run_python — 4 line(s) of code … ok".
function addStep(box, tool, args) {
  const item = el("details", "step running");
  const summary = el("summary");
  summary.appendChild(el("span", "", `${TOOL_ICONS[tool] || "🔧"} ${tool} — ${describeArguments(tool, args)}`));
  const state = el("span", "step-state", "running…");
  summary.appendChild(state);
  item.appendChild(summary);

  // The full input, so the user can check exactly what the AI did.
  if (tool === "run_python" && args && args.code) {
    item.appendChild(el("div", "step-label", "Code:"));
    item.appendChild(el("pre", "", args.code));
  } else if (tool === "create_word_document" && args && args.content) {
    item.appendChild(el("div", "step-label", `Title: ${args.title || ""}`));
    item.appendChild(el("pre", "", args.content));
  } else if (args && Object.keys(args).length) {
    item.appendChild(el("pre", "", JSON.stringify(args, null, 2)));
  }
  item._state = state;
  box.appendChild(item);
  return item;
}

// When the tool finishes: tick or cross, its summary, and any output.
function finishStep(item, event) {
  if (!item) return;
  item.classList.remove("running");
  item.classList.add(event.ok ? "ok" : "failed");
  item._state.textContent = `${event.ok ? "✓" : "✗"} ${event.summary || ""}`;
  if (event.display && event.display.output !== undefined) {
    item.appendChild(el("div", "step-label", "Output:"));
    item.appendChild(el("pre", event.ok ? "" : "bad-text", event.display.output));
    // Calculations are the point of this tool: open it so the result is visible.
    item.open = true;
  }
}

// A download link for a file in the workspace.
function fileLink(name) {
  const link = el("a", "file-link", `⬇ ${name}`);
  link.href = "/api/files/" + name.split("/").map(encodeURIComponent).join("/");
  link.setAttribute("download", "");
  return link;
}

// Send on form submit (the Send button).
formEl.addEventListener("submit", (event) => {
  event.preventDefault();                     // stop the page reloading
  const text = promptEl.value.trim();
  if (!text || busy) return;
  promptEl.value = "";
  sendMessage(text);
});

// Enter sends; Shift+Enter makes a new line.
promptEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    formEl.requestSubmit();
  }
});

// "New chat" clears the conversation and its attachments.
clearBtn.addEventListener("click", () => {
  if (busy) return;
  history = [];
  attachments = [];
  renderAttachments();
  messagesEl.textContent = "";
  addBubble("assistant", "New conversation started.");
});


// ===============================================================
// 3. Attachments (Phase 2)
// ===============================================================
attachBtn.addEventListener("click", () => fileInput.click());

fileInput.addEventListener("change", async () => {
  const file = fileInput.files[0];
  fileInput.value = "";                       // allow picking the same file again
  if (!file || busy) return;

  setBusy(true);
  const working = el("div", "chip working", `Processing ${file.name}… (OCR can take a minute)`);
  attachmentsEl.appendChild(working);

  try {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch("/api/upload", { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);

    attachments.push(data);
    const note = addBubble("assistant", describeUpload(data));
    // Phase 5: show a small preview of attached images.
    if (/\.(png|jpe?g|bmp|tiff?)$/i.test(data.filename)) {
      const img = document.createElement("img");
      img.className = "upload-preview";
      img.alt = data.filename;
      img.src = `/api/uploads/${data.id}/image`;
      note.prepend(img);
    }
  } catch (err) {
    addBubble("assistant", `[Error] Could not attach ${file.name}: ${err.message}`);
  } finally {
    working.remove();
    renderAttachments();
    setBusy(false);
  }
});

// A short, honest description of what was extracted from the file.
function describeUpload(d) {
  let text = `📄 Attached "${d.filename}": ${d.pages} page(s), ${d.characters.toLocaleString()} characters, processed in ${d.seconds}s.`;
  if (d.ocr_pages > 0) {
    text += `\n${d.ocr_pages} page(s) were scanned images and were read with OCR` +
            ` (average confidence ${d.ocr_confidence}%).`;
    if (d.ocr_confidence < 70) text += " Confidence is low — please double-check key values.";
    if (d.uncertain_words > 0) {
      text += `\n⚠ ${d.uncertain_words} word(s) were hard to read. Check numbers against the original before relying on them.`;
    }
  }
  if (d.characters === 0) {
    text += "\nNo text could be read from this file.";
  } else {
    text += `\n\nFirst words: "${d.preview}…"\n\nAsk me anything about it, e.g. "Summarise this document."`;
  }
  return text;
}

// Draw the chips for the attached documents.
function renderAttachments() {
  attachmentsEl.querySelectorAll(".chip:not(.working), .workflow-btn").forEach(c => c.remove());
  // Phase 6: one-click flagship workflow when a document is attached.
  if (attachments.length > 0) {
    const button = el("button", "workflow-btn", "📝 Draft approval note");
    button.type = "button";
    button.title = "Report → verified findings → SOP checks → approval note (.docx)";
    button.addEventListener("click", () => {
      if (busy) return;
      sendMessage("Draft an approval note from the attached inspection report.");
    });
    attachmentsEl.appendChild(button);
  }
  for (const a of attachments) {
    const chip = el("div", "chip");
    chip.appendChild(el("span", "", `📄 ${a.filename}`));
    chip.appendChild(el("span", "muted", a.ocr_pages ? `${a.pages}p · OCR` : `${a.pages}p`));
    const remove = el("button", "", "×");
    remove.type = "button";
    remove.title = "Stop using this document";
    remove.addEventListener("click", () => {
      attachments = attachments.filter(x => x.id !== a.id);
      renderAttachments();
    });
    chip.appendChild(remove);
    attachmentsEl.appendChild(chip);
  }
}


// ===============================================================
// 4. Knowledge base panel (Phase 2)
// ===============================================================
async function refreshKb() {
  try {
    showKb(await (await fetch("/api/kb/status")).json());
  } catch (err) {
    document.getElementById("kb-summary").textContent = "Could not load status.";
  }
}

function showKb(kb) {
  const summary = document.getElementById("kb-summary");
  if (!kb.built_at) {
    summary.textContent = `${kb.files_in_folder.length} file(s) in the folder, not indexed yet. Click "Rebuild index".`;
  } else {
    summary.textContent = `${kb.indexed_files.length} file(s), ${kb.chunk_count} chunks · built ${kb.built_at}` +
      (kb.needs_rebuild ? " · folder changed, rebuild needed" : "");
  }

  const rows = kb.indexed_files.map(f => ({
    text: `${f.name}\n  ${f.pages} page(s) · ${f.chunks} chunks${f.ocr_pages ? ` · ${f.ocr_pages} OCR page(s)` : ""}`,
    bad: false,
  })).concat(kb.errors.map(e => ({ text: `${e.name}\n  ${e.error}`, bad: true })));

  renderList("kb-files", rows, r => r, "No documents indexed.");
}

reindexBtn.addEventListener("click", async () => {
  reindexBtn.disabled = true;
  reindexBtn.textContent = "Indexing…";
  document.getElementById("kb-summary").textContent =
    "Reading, chunking and embedding documents. This can take a minute or two…";
  try {
    const res = await fetch("/api/kb/reindex", { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    showKb(data);
  } catch (err) {
    document.getElementById("kb-summary").textContent = `Indexing failed: ${err.message}`;
  } finally {
    reindexBtn.disabled = false;
    reindexBtn.textContent = "Rebuild index";
  }
});


// ===============================================================
// 4b. Output files panel (Phase 3)
// ===============================================================
async function refreshFiles() {
  try {
    const files = await (await fetch("/api/files")).json();
    const ul = document.getElementById("file-list");
    ul.textContent = "";
    if (files.length === 0) {
      ul.appendChild(el("li", "none", "No files yet. Ask the agent to create a Word or Excel file."));
      return;
    }
    for (const f of files) {
      const li = el("li");
      li.appendChild(fileLink(f.name));
      li.appendChild(el("span", "muted", `  ${(f.size / 1024).toFixed(1)} KB`));
      ul.appendChild(li);
    }
  } catch (err) { /* panel is optional; ignore */ }
}


// ===============================================================
// 5. Network monitor
// ===============================================================
async function refreshNetwork() {
  try {
    const data = await (await fetch("/api/network")).json();

    document.getElementById("checked-at").textContent = `checked ${data.checked_at}`;

    // ---- Headline badge ----
    if (data.sovereign) {
      setPill(badgeEl, "good", "● 0 external connections");
    } else {
      const n = data.external_connection_count + data.request_totals.blocked;
      setPill(badgeEl, "bad", `● ${n} external connection(s)!`);
    }

    // ---- Layer 1: request log ----
    const t = data.request_totals;
    document.getElementById("request-totals").textContent =
      `${t.allowed} allowed (local) · ${t.blocked} blocked (external)`;

    // Repeated identical requests are grouped by the server (Phase 2.1):
    // "12:10:05–12:13:20  GET 127.0.0.1/api/tags ×14  ✓ local"
    renderList("request-log", data.requests, (r) => {
      const repeated = r.count > 1;
      const when = repeated ? `${r.first_time}–${r.time}` : r.time;
      return {
        text: `${when}  ${r.method} ${r.target}${repeated ? ` ×${r.count}` : ""}  ${r.allowed ? "✓ local" : "✗ BLOCKED"}`,
        bad: !r.allowed,
      };
    }, "No requests yet. Send a chat message to see one.");

    // ---- Layer 2: OS connections ----
    renderList("connection-list", data.connections, (c) => {
      if (c.error) return { text: c.error, bad: true };
      return {
        text: `${c.process}\n  ${c.local} → ${c.remote}  [${c.status}]${c.external ? "  EXTERNAL" : ""}`,
        bad: c.external,
      };
    }, "No open connections right now.");

  } catch (err) {
    setPill(badgeEl, "wait", "Monitor offline");
  }
}

// Draw a list of items into a <ul>. `format` turns each item into
// { text, bad }. Shows `emptyText` when the list is empty.
function renderList(listId, items, format, emptyText) {
  const ul = document.getElementById(listId);
  ul.textContent = "";
  if (!items || items.length === 0) {
    ul.appendChild(el("li", "none", emptyText));
    return;
  }
  for (const item of items.slice(0, 40)) {   // cap rows to keep the page fast
    const { text, bad } = format(item);
    const li = el("li", bad ? "bad" : "", text);
    li.style.whiteSpace = "pre-wrap";
    ul.appendChild(li);
  }
}


// ===============================================================
// Start everything when the page loads
// ===============================================================
checkHealth();
refreshKb();
refreshFiles();
refreshNetwork();
setInterval(refreshNetwork, 2000);    // monitor refresh every 2 s
setInterval(checkHealth, 15000);      // re-check models every 15 s
promptEl.focus();
