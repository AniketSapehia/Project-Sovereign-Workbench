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
      const missing = data.models.filter(m => !m.installed);
      if (missing.length > 0) {
        setPill(modelStatusEl, "bad", `Model missing: ${missing.map(m => m.name).join(", ")}`);
      } else {
        setPill(modelStatusEl, "good", data.models.map(m => `${m.role}: ${m.name}`).join(" · "));
      }
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

// Show the answer text, turning "[1]" into clickable citation markers.
// We build the nodes ourselves (text nodes + <span>s), so it stays safe.
function renderAnswer(container, text, sourcesBox) {
  container.textContent = "";
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

  // The assistant bubble has: a status line, the answer text, and sources.
  const bubble = addBubble("assistant", "");
  const statusLine = el("div", "status", "Starting…");
  const answerEl = el("div");
  bubble.append(statusLine, answerEl);

  const started = performance.now();
  let answer = "";
  let sourcesBox = null;
  let failed = false;

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: history,
        use_knowledge_base: useKbEl.checked,
        attachment_ids: attachments.map(a => a.id),
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
          statusLine.textContent = event.text;
        } else if (event.type === "sources") {
          sourcesBox = buildSources(event.sources);
        } else if (event.type === "token") {
          if (answer === "") statusLine.remove();  // first words arrived
          answer += event.text;
          renderAnswer(answerEl, answer, sourcesBox);
        } else if (event.type === "error") {
          failed = true;
          statusLine.remove();
          answerEl.appendChild(el("div", "bad-text", `[Error] ${event.text}`));
        }
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }
    }

    if (sourcesBox) bubble.appendChild(sourcesBox);

    // Footer: time taken, and how many sources were used.
    const seconds = ((performance.now() - started) / 1000).toFixed(1);
    const n = sourcesBox ? sourcesBox.querySelectorAll("details").length : 0;
    bubble.appendChild(el("span", "meta",
      `${seconds}s · generated locally${n ? ` · ${n} source(s)` : " · no documents used"}`));

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
    addBubble("assistant", describeUpload(data));
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
  attachmentsEl.querySelectorAll(".chip:not(.working)").forEach(c => c.remove());
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

    renderList("request-log", data.requests, (r) => ({
      text: `${r.time}  ${r.method} ${r.target}  ${r.allowed ? "✓ local" : "✗ BLOCKED"}`,
      bad: !r.allowed,
    }), "No requests yet. Send a chat message to see one.");

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
refreshNetwork();
setInterval(refreshNetwork, 2000);    // monitor refresh every 2 s
setInterval(checkHealth, 15000);      // re-check models every 15 s
promptEl.focus();
