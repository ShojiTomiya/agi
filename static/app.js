// ── state ─────────────────────────────────────────
let sessionId   = null;
let pendingFiles = [];   // [{name, previewUrl}] — uploaded, not sent yet
let isLoading   = false;

// ── session files ─────────────────────────────────
// A session's files (uploads, files the model saved, charts) live in
// uploads/<session>/ under their own names — the same names the model sees.
const IMAGE_RE = /\.(png|jpg|jpeg|webp|gif)$/i;

// A new chat gets its id in the browser, so files can be uploaded into it
// before the first message is sent.
function ensureSessionId() {
  if (!sessionId) sessionId = crypto.randomUUID();
  return sessionId;
}

function fileUrl(name) {
  const encoded = name.split("/").map(part =>
    encodeURIComponent(part).replace(/\(/g, "%28").replace(/\)/g, "%29"));
  return `/uploads/${sessionId}/${encoded.join("/")}`;
}

function attachmentsFor(names) {
  return names.map(name => ({ name, previewUrl: IMAGE_RE.test(name) ? fileUrl(name) : null }));
}

function chartMarkdown(names) {
  return names.map(name => `\n\n![chart](${fileUrl(name)})`).join("");
}

function appendDownloads(div, names) {
  if (!names.length) return;
  const row = document.createElement("div");
  row.style.cssText = "display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px;";
  for (const name of names) {
    const a = document.createElement("a");
    a.href = fileUrl(name);
    a.download = name.split("/").pop();
    a.className = "file-download-btn";
    a.textContent = `⬇ ${name}`;
    row.appendChild(a);
  }
  div.appendChild(row);
}

// ── sessions ──────────────────────────────────────
async function loadSessions() {
  const res      = await fetch("/sessions");
  const sessions = await res.json();
  const el       = document.getElementById("sessions");
  el.innerHTML   = "";

  // built with DOM calls rather than inline onclick strings, which broke on
  // titles containing an apostrophe
  for (const s of sessions) {
    const title = s.title || "Conversation";
    const item  = document.createElement("div");
    item.className = "session-item" + (s.id === sessionId ? " active" : "");
    item.onclick = () => loadSession(s.id);

    const titleEl = document.createElement("span");
    titleEl.className = "session-title";
    titleEl.textContent = title;

    const renameEl = document.createElement("span");
    renameEl.className = "session-rename";
    renameEl.textContent = "✎";
    renameEl.onclick = (e) => { e.stopPropagation(); renameSession(s.id, title); };

    const deleteEl = document.createElement("span");
    deleteEl.className = "session-delete";
    deleteEl.textContent = "×";
    deleteEl.onclick = (e) => { e.stopPropagation(); deleteSession(s.id); };

    item.append(titleEl, renameEl, deleteEl);
    el.appendChild(item);
  }
}

async function loadSession(id) {
  sessionId = id;
  // files staged for another session don't belong to this one
  pendingFiles = [];
  renderFilePreviews();
  document.getElementById("messages").innerHTML = "";
  clearInspector();
  loadSessions();

  const res  = await fetch(`/sessions/${id}/messages`);
  const rows = await res.json();

  // every bubble remembers which turn (user message id) it belongs to, so clicking
  // it can show that turn's steps in the inspector
  let turnId = null;
  for (const row of rows) {
    if (row.role === "user") turnId = row.id;

    if (row.role === "assistant" && row.metadata?.error) {
      appendError(row.content).dataset.turnId = turnId;
      continue;
    }

    if (row.role === "user" || row.role === "assistant") {
      const meta = row.metadata || {};
      const attachments = row.role === "user" ? attachmentsFor(meta.files || []) : [];
      const content = row.content + (row.role === "assistant" ? chartMarkdown(meta.charts || []) : "");
      const msgDiv = appendMessage(row.role, content, attachments, row.id);
      msgDiv.dataset.turnId = turnId;
      if (row.role === "assistant") appendDownloads(msgDiv, meta.files || []);
    }
  }
  addCopyButtons();
  addChartButtons(document.getElementById("messages"));
  if (turnId) showTurn(turnId);
}

// Load a past turn's saved steps into the inspector.
async function showTurn(turnId) {
  const res   = await fetch(`/turns/${turnId}/steps`);
  const steps = await res.json();
  clearInspector();
  steps.forEach(appendStep);
  markInspected(turnId);
}

function markInspected(turnId) {
  document.querySelectorAll("#messages [data-turn-id]").forEach(el => {
    el.classList.toggle("inspected", el.dataset.turnId === String(turnId));
  });
}


// ── math ──────────────────────────────────────────
// Math is cut out while Markdown is parsed — rendering it afterwards was too late:
// Markdown had already turned \[ \( into [ (, \\ into \ and _ * into italics.
// $$…$$ and \[…\] are display math, \(…\) and $…$ inline. Like Pandoc, $…$ only
// counts when it hugs its content and isn't followed by a digit: "$5 and $10" stays text.
const MATH_BLOCK  = /^ {0,3}(?:\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\])[ \t]*(?:\n+|$)/;
const MATH_INLINE = /^(?:\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|\$(?!\s)((?:\\.|[^\\$\n])+?)\$(?!\d))/;

// a placeholder holding the TeX; renderMath fills it in after sanitizing
function mathHtml(tex, display, raw) {
  const tag = display ? "div" : "span";
  return `<${tag} class="math" data-tex="${escapeHtml(tex.trim())}"${display ? " data-display" : ""}>${escapeHtml(raw)}</${tag}>`;
}

marked.use({ extensions: [
  {
    name: "mathBlock", level: "block",
    start: src => src.match(/^ {0,3}(?:\$\$|\\\[)/m)?.index,
    tokenizer(src) {
      const m = MATH_BLOCK.exec(src);
      if (m) return { type: "mathBlock", raw: m[0], text: m[1] ?? m[2] };
    },
    renderer: token => mathHtml(token.text, true, token.raw.trim()) + "\n",
  },
  {
    name: "mathInline", level: "inline",
    start: src => src.match(/\$|\\[[(]/)?.index,
    tokenizer(src) {
      const m = MATH_INLINE.exec(src);
      if (!m || (m[4] !== undefined && /\s$/.test(m[4]))) return;
      return { type: "mathInline", raw: m[0], text: m[1] ?? m[2] ?? m[3] ?? m[4],
               display: m[1] !== undefined || m[2] !== undefined };
    },
    renderer: token => mathHtml(token.text, token.display, token.raw),
  },
]});

function renderMath(el) {
  if (!window.katex) return;   // KaTeX didn't load: the TeX source stays readable
  el.querySelectorAll(".math[data-tex]").forEach(m => {
    try {
      katex.render(m.dataset.tex, m, { displayMode: "display" in m.dataset, throwOnError: false });
    } catch { /* leave the source text */ }
  });
}

function renderMd(el, text) {
  // model output can echo HTML from search results or files — never run it;
  // KaTeX's own markup is added after sanitizing, from the TeX alone
  el.innerHTML = DOMPurify.sanitize(marked.parse(text));
  // charts are added by the app; an image the model wrote itself (e.g. attachment://…)
  // points nowhere and would show up as a broken frame
  el.querySelectorAll("img").forEach(img => {
    if (!/^(https?:\/\/|\/uploads\/)/.test(img.getAttribute("src") || "")) img.remove();
  });
  renderMath(el);
}

function addChartButtons(div) {
  div.querySelectorAll("img").forEach(img => {
    if (img.parentElement?.classList.contains("chart-wrap")) return;
    const wrap = document.createElement("div");
    wrap.className = "chart-wrap";
    img.parentNode.insertBefore(wrap, img);
    wrap.appendChild(img);
    const a = document.createElement("a");
    a.className = "chart-download";
    a.href = img.src;
    a.download = "";
    a.title = "Download";
    a.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>`;
    wrap.appendChild(a);
  });
}

function addCopyButtons() {
  document.querySelectorAll(".msg.assistant pre").forEach(pre => {
    if (pre.querySelector(".copy-btn")) return;

    const btn = document.createElement("button");
    btn.className = "copy-btn";
    btn.textContent = "Copy";

    btn.addEventListener("click", () => {
      const code = pre.querySelector("code")?.innerText || pre.innerText;
      navigator.clipboard.writeText(code).then(() => {
        btn.textContent = "Copied!";
        setTimeout(() => btn.textContent = "Copy", 2000);
      });
    });

    pre.style.position = "relative";
    pre.appendChild(btn);
  });
}

async function deleteSession(id) {
  if (!confirm("Delete this conversation?")) return;
  await fetch(`/sessions/${id}`, { method: "DELETE" });
  if (sessionId === id) newChat();
  else loadSessions();
}

async function renameSession(id, currentTitle) {
  const newTitle = prompt("Rename conversation:", currentTitle);
  if (!newTitle || newTitle.trim() === currentTitle) return;

  await fetch(`/sessions/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: newTitle.trim() })
  });

  loadSessions();
}

function newChat() {
  sessionId    = null;
  pendingFiles = [];
  document.getElementById("messages").innerHTML = "";
  clearInspector();
  renderFilePreviews();
  loadSessions();
}

// ── file upload ───────────────────────────────────
// Safari can silently drop the file bytes from a fetch()+FormData body when
// the File object hasn't been fully read yet (seen with large/iCloud-backed
// files) — reading it into a Blob first forces materialization and avoids
// the server seeing an empty/missing "file" field.
async function buildFileFormData(file) {
  const buf = await file.arrayBuffer();
  const blob = new Blob([buf], { type: file.type });
  const form = new FormData();
  form.append("file", blob, file.name);
  return form;
}

// Upload straight into the session's directory; the server answers with the
// name the file got there (renamed if the session already has one by that name).
async function uploadFiles(fileList) {
  for (const file of Array.from(fileList)) {
    const form = await buildFileFormData(file);
    form.append("session_id", ensureSessionId());

    const res = await fetch("/upload", { method: "POST", body: form });
    if (!res.ok) {
      const err = await res.json();
      appendError(`Upload failed: ${formatErrorDetail(err.detail)}`);
      continue;
    }

    const { name } = await res.json();
    const previewUrl = file.type.startsWith("image/") ? URL.createObjectURL(file) : null;
    pendingFiles.push({ name, previewUrl });
  }
  renderFilePreviews();
}

async function handleFiles(event) {
  await uploadFiles(event.target.files);
  event.target.value = "";
}

function renderFilePreviews() {
  const el = document.getElementById("file-preview");
  el.innerHTML = pendingFiles.map((f, i) => `
    <div class="file-chip">
      <span>${escapeHtml(f.name)}</span>
      <span class="file-chip-remove" onclick="removeFile(${i})">×</span>
    </div>
  `).join("");
}

function removeFile(i) {
  pendingFiles.splice(i, 1);
  renderFilePreviews();
}

// ── messages ──────────────────────────────────────
function appendMessage(role, content, files = [], msgId = null) {
  const el  = document.getElementById("messages");
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  if (msgId) div.dataset.msgId = msgId;
  div._files = files;

  if (role === "user") {
    const topRow = document.createElement("div");
    topRow.style.cssText = "display: flex; align-items: flex-start; gap: 6px;";

    const text = document.createElement("span");
    text.style.flex = "1";
    text.textContent = content;
    topRow.appendChild(text);

    const editBtn = document.createElement("button");
    editBtn.className = "edit-btn";
    editBtn.textContent = "✎";
    editBtn.title = "Edit message";
    editBtn.onclick = () => {
      if (isLoading) return;
      const id = parseInt(div.dataset.msgId);
      if (!id) return;
      const originalText = div.querySelector("span")?.textContent || "";
      startEdit(div, id, originalText, div._files || []);
    };
    topRow.appendChild(editBtn);
    div.appendChild(topRow);

    if (files.length > 0) {
      const chipsRow = document.createElement("div");
      chipsRow.style.cssText = "display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px;";
      files.forEach(f => {
        if (f.previewUrl) {
          const img = document.createElement("img");
          img.src = f.previewUrl;
          chipsRow.appendChild(img);
        } else {
          const chip = document.createElement("span");
          chip.className = "msg-file-chip";
          chip.textContent = `📎 ${f.name}`;
          chipsRow.appendChild(chip);
        }
      });
      div.appendChild(chipsRow);
    }
  } else {
    renderMd(div, content);
  }

  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
  return div;
}

function startEdit(div, msgId, originalText, originalFiles = []) {
  div.innerHTML = "";

  const ta = document.createElement("textarea");
  ta.className = "edit-textarea";
  ta.value = originalText;

  const actions = document.createElement("div");
  actions.className = "edit-actions";

  const confirmBtn = document.createElement("button");
  confirmBtn.className = "edit-confirm";
  confirmBtn.textContent = "✓ Send";
  confirmBtn.onclick = () => confirmEdit(msgId, ta.value, originalFiles);

  const cancelBtn = document.createElement("button");
  cancelBtn.className = "edit-cancel";
  cancelBtn.textContent = "✕ Cancel";
  cancelBtn.onclick = () => loadSession(sessionId);

  actions.appendChild(cancelBtn);
  actions.appendChild(confirmBtn);
  div.appendChild(ta);

  if (originalFiles.length > 0) {
    const chipsRow = document.createElement("div");
    chipsRow.style.cssText = "display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px;";
    originalFiles.forEach(f => {
      const chip = document.createElement("span");
      chip.className = "msg-file-chip";
      chip.textContent = `📎 ${f.name}`;
      chipsRow.appendChild(chip);
    });
    div.appendChild(chipsRow);
  }

  div.appendChild(actions);
  ta.focus();
  ta.selectionStart = ta.selectionEnd = ta.value.length;
}

async function confirmEdit(msgId, newText, originalFiles = []) {
  newText = newText.trim();
  if (!newText) return;

  await fetch(`/sessions/${sessionId}/messages/${msgId}`, { method: "DELETE" });

  // remove edited message and everything after it from DOM
  const container = document.getElementById("messages");
  const allMsgs = Array.from(container.children);
  const editedIdx = allMsgs.findIndex(el => parseInt(el.dataset.msgId) === msgId);
  if (editedIdx !== -1) allMsgs.slice(editedIdx).forEach(el => el.remove());

  clearInspector();

  document.getElementById("input").value = newText;
  pendingFiles = originalFiles.filter(f => f.name);
  send();
}

function appendError(message) {
  const el  = document.getElementById("messages");
  const div = document.createElement("div");
  div.className = "msg error-msg";
  div.textContent = `⚠ ${message}`;
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
  return div;
}

function showTypingIndicator() {
  const el  = document.getElementById("messages");
  const div = document.createElement("div");
  div.className = "typing-indicator";
  div.id = "typing";
  div.textContent = "typing...";
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
}

function removeTypingIndicator() {
  document.getElementById("typing")?.remove();
}

function showToolIndicator(div, toolName, { elapsed = true } = {}) {
  removeTypingIndicator();
  removeToolIndicator(div);
  const el = document.createElement("span");
  el.className = "tool-indicator";
  el.textContent = toolName + "…";
  div.appendChild(el);
  // a long step — code can run up to 30 s — shouldn't look frozen
  if (elapsed) {
    const started = Date.now();
    const timer = setInterval(() => {
      if (!el.isConnected) return clearInterval(timer);   // removed or re-rendered away
      const s = Math.floor((Date.now() - started) / 1000);
      if (s >= 3) el.textContent = `${toolName}… ${s}s`;
    }, 1000);
  }
  document.getElementById("messages").scrollTop =
    document.getElementById("messages").scrollHeight;
}

function removeToolIndicator(div) {
  div.querySelector(".tool-indicator")?.remove();
}

// ── loop inspector ────────────────────────────────
let inspectorData = { user_msg: null, file_reads: [], events: [], response: null };

function clearInspector() {
  inspectorData = { user_msg: null, file_reads: [], events: [], response: null };
  document.getElementById("isec-dynamic").innerHTML = "";
  ["loop","user","response"].forEach(t => {
    const body = document.getElementById(`isec-${t}`);
    body.innerHTML = "";
    const isLoop = t === "loop";
    body.classList.toggle("open", isLoop);
    const icon = body.previousElementSibling?.querySelector(".toggle-icon");
    if (icon) icon.textContent = isLoop ? "▼" : "▶";
  });
}

function toggleSection(header) {
  const body = header.nextElementSibling;
  const icon = header.querySelector(".toggle-icon");
  const open = body.classList.toggle("open");
  icon.textContent = open ? "▼" : "▶";
}

function openSection(id) {
  const body = document.getElementById(id);
  if (!body) return;
  if (!body.classList.contains("open")) {
    body.classList.add("open");
    const icon = body.previousElementSibling?.querySelector(".toggle-icon");
    if (icon) icon.textContent = "▼";
  }
  body.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function loopRow(type, label, detail, section) {
  const onclick = section ? `onclick="openSection('${section}')"` : "";
  return `<div class="loop-item" data-type="${type}" ${onclick}>
    <span class="loop-dot"></span>
    <span class="loop-label">${escapeHtml(label)}</span>
    <span class="loop-detail">${escapeHtml(detail)}</span>
  </div>`;
}

function renderLoopTab() {
  const el = document.getElementById("isec-loop");
  const items = [];
  if (inspectorData.user_msg) {
    const t = inspectorData.user_msg.text || "";
    items.push(loopRow("user", "User", t.slice(0, 40) + (t.length > 40 ? "…" : ""), "isec-user"));
  }
  for (const fr of inspectorData.file_reads) {
    const detail = fr.kind === "image" ? "[image]"
      : fr.kind === "error" ? "✗ " + (fr.error || "")
      : (fr.chars || 0).toLocaleString() + " ch";
    items.push(loopRow("file", fr.filename, detail, null));
  }
  for (let i = 0; i < inspectorData.events.length; i++) {
    const ev = inspectorData.events[i];
    const secId = `isec-event-${i}`;
    if (ev.step === "llm_call") {
      const stats = llmStats(ev);
      items.push(loopRow("llm", "LLM call", stats || ev.model || "", secId));
    } else if (ev.step === "rate_limit_wait") {
      items.push(loopRow("wait", "Waiting", `rate limit, ${ev.seconds}s`, secId));
    } else if (ev.step === "retry") {
      items.push(loopRow("retry", "Retry", ev.reason || "", secId));
    } else if (ev.step === "error") {
      items.push(loopRow("error", "Error", ev.message || "", secId));
    } else if (ev.step === "reasoning") {
      const preview = (ev.text || "").slice(0, 40);
      items.push(loopRow("reasoning", "Reasoning", preview + (ev.text?.length > 40 ? "…" : ""), secId));
    } else if (ev.step === "tool_call") {
      items.push(loopRow("tool_call", ev.name, JSON.stringify(ev.args || {}), secId));
    } else if (ev.step === "tool_result") {
      const preview = (ev.result || "").slice(0, 40);
      items.push(loopRow("tool_result", "Result", preview + (ev.result?.length > 40 ? "…" : ""), secId));
    }
  }
  if (inspectorData.response) {
    const tok = inspectorData.response.usage?.total_tokens;
    items.push(loopRow("response", "Response", tok ? tok.toLocaleString() + " tok" : "", "isec-response"));
  }
  el.innerHTML = items.join("");
}

function renderUserTab() {
  const el = document.getElementById("isec-user");
  const d  = inspectorData.user_msg;
  if (!d) { el.innerHTML = ""; return; }
  const filesHtml = (d.filenames || []).length
    ? `<div class="itab-chips">${d.filenames.map(f => `<span class="itab-chip">${escapeHtml(f)}</span>`).join("")}</div>`
    : "";
  el.innerHTML = `<pre class="itab-pre">${escapeHtml(d.text || "")}</pre>${filesHtml}`;
}

// "1,234 tok · 0.8s" for an LLM call once its llm_call_done step has arrived
function llmStats(ev) {
  const parts = [];
  if (ev.usage?.total_tokens) parts.push(ev.usage.total_tokens.toLocaleString() + " tok");
  if (ev.ms != null) parts.push((ev.ms / 1000).toFixed(1) + "s");
  return parts.join(" · ");
}

function makeSec(id, label, content) {
  return `<div class="isec">
    <div class="isec-header" onclick="toggleSection(this)"><span>${escapeHtml(label)}</span><span class="toggle-icon">▶</span></div>
    <div class="isec-body" id="${id}">${content}</div>
  </div>`;
}

function renderDynamicSections() {
  const container = document.getElementById("isec-dynamic");
  const events = inspectorData.events;
  const totalLlm = events.filter(e => e.step === "llm_call").length;
  let llmIndex = 0;
  const parts = [];

  for (let i = 0; i < events.length; i++) {
    const ev = events[i];
    const id = `isec-event-${i}`;

    if (ev.step === "llm_call") {
      llmIndex++;
      const label = totalLlm > 1 ? `LLM call ${llmIndex}` : "LLM call";
      const u = ev.usage;
      const usageText = u ? ` · prompt ${u.prompt_tokens} · completion ${u.completion_tokens}` : "";
      const timeText = ev.ms != null ? ` · ${(ev.ms / 1000).toFixed(1)}s` : "";
      const meta = `<div class="itab-meta">${escapeHtml((ev.model || "") + usageText + timeText)}</div>`;
      const bodyHtml = (ev.messages || [])
        .map(m => `<span class="step-role">[${m.role.toUpperCase()}]</span>\n${escapeHtml(m.content)}`)
        .join('\n\n<span class="step-sep">─────────────</span>\n\n');
      parts.push(makeSec(id, label, `${meta}<pre class="itab-pre">${bodyHtml}</pre>`));

    } else if (ev.step === "reasoning") {
      parts.push(makeSec(id, "Reasoning", `<pre class="itab-pre">${escapeHtml(ev.text || "")}</pre>`));

    } else if (ev.step === "tool_call") {
      const args = JSON.stringify(ev.args || {}, null, 2);
      parts.push(makeSec(id, ev.name, `<pre class="itab-pre"><span class="step-role">[ARGS]</span>\n${escapeHtml(args)}</pre>`));

    } else if (ev.step === "tool_result") {
      const detail = ev.detail
        ? `\n\n<span class="step-sep">─────────────</span>\n\n<span class="step-role">[RAW — before condensing]</span>\n${escapeHtml(ev.detail)}`
        : "";
      parts.push(makeSec(id, "Result", `<pre class="itab-pre">${escapeHtml(ev.result || "")}${detail}</pre>`));

    } else if (ev.step === "rate_limit_wait") {
      parts.push(makeSec(id, "Waiting", `<pre class="itab-pre">Rate limit reached — waited ${ev.seconds}s before retrying.</pre>`));

    } else if (ev.step === "retry") {
      parts.push(makeSec(id, "Retry", `<pre class="itab-pre">The model's tool call was rejected; it was asked to try again.\n\n${escapeHtml(ev.reason || "")}</pre>`));

    } else if (ev.step === "error") {
      parts.push(makeSec(id, "Error", `<pre class="itab-pre">${escapeHtml(ev.message || "")}</pre>`));
    }
  }

  container.innerHTML = parts.join("");
}

function renderResponseTab() {
  const el = document.getElementById("isec-response");
  const d  = inspectorData.response;
  if (!d) { el.innerHTML = ""; return; }
  const u    = d.usage;
  const meta = u ? `prompt: ${u.prompt_tokens} · completion: ${u.completion_tokens} · total: ${u.total_tokens}` : "";
  el.innerHTML = `${meta ? `<div class="itab-meta">${meta}</div>` : ""}<pre class="itab-pre">${escapeHtml(d.content || "")}</pre>`;
}

function appendStep(step) {
  if (step.step === "user_msg") {
    inspectorData.user_msg = step;
    renderUserTab();
  } else if (step.step === "file_read") {
    inspectorData.file_reads.push(step);
  } else if (["llm_call", "reasoning", "tool_call", "tool_result", "rate_limit_wait", "retry", "error"].includes(step.step)) {
    inspectorData.events.push(step);
    renderDynamicSections();
  } else if (step.step === "llm_call_done") {
    // tokens and time belong to the LLM call that just finished
    const call = [...inspectorData.events].reverse().find(e => e.step === "llm_call");
    if (call) Object.assign(call, { usage: step.usage, ms: step.ms });
    renderDynamicSections();
  } else if (step.step === "response") {
    inspectorData.response = step;
    renderResponseTab();
  }
  renderLoopTab();
}

// ── send ──────────────────────────────────────────
async function send() {
  if (isLoading) return;

  const input = document.getElementById("input");
  const text  = input.value.trim();
  if (!text && pendingFiles.length === 0) return;

  input.value = "";
  isLoading   = true;
  document.getElementById("send-btn").disabled = true;

  // auto-resize textarea back to single line
  input.style.height = "auto";

  const files  = [...pendingFiles];
  pendingFiles = [];
  renderFilePreviews();

  clearInspector();

  const userDiv = appendMessage("user", text, files);
  showTypingIndicator();

  // assistant bubble is created lazily on first content/indicator so an
  // empty message box isn't visible while only "typing..." is shown
  let assistantDiv = null;
  let turnId = null;   // id of the saved user message, sent in the first event
  function ensureAssistantDiv() {
    if (!assistantDiv) {
      assistantDiv = appendMessage("assistant", "");
      if (turnId) assistantDiv.dataset.turnId = turnId;
    }
    return assistantDiv;
  }
  let accumulated = "";

  // re-parsing the whole answer on every token gets slow for long replies,
  // so render at most once per animation frame
  let renderFrame = 0;
  function renderNow() {
    cancelAnimationFrame(renderFrame);
    renderFrame = 0;
    renderMd(assistantDiv, accumulated);
    addCopyButtons();
    document.getElementById("messages").scrollTop =
      document.getElementById("messages").scrollHeight;
  }
  function scheduleRender() {
    if (!renderFrame) renderFrame = requestAnimationFrame(renderNow);
  }
  function cancelRender() {
    cancelAnimationFrame(renderFrame);
    renderFrame = 0;
  }

  try {
    const res = await fetch("/chat", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({
        message:    text,
        session_id: ensureSessionId(),
        files:      files.map(f => f.name),
      }),
    });

    if (!res.ok) {
      throw new Error(`Server error: ${res.status}`);
    }

    const reader  = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer    = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // split on double newline (SSE event separator)
      const events = buffer.split("\n\n");
      buffer = events.pop(); // last may be incomplete

      for (const event of events) {
        if (!event.trim()) continue;

        const lines     = event.split("\n");
        const eventType = lines.find(l => l.startsWith("event:"))?.slice(6).trim();
        const dataLine  = lines.find(l => l.startsWith("data:"))?.slice(5).trim();
        if (!dataLine) continue;

        let data;
        try {
          data = JSON.parse(dataLine);
        } catch {
          continue;
        }

        if (eventType === "session") {
          sessionId = data.session_id;
          // the user message is saved before the agent runs, so it can be edited
          // even if this turn fails
          turnId = data.user_msg_id;
          userDiv.dataset.msgId = turnId;
          userDiv.dataset.turnId = turnId;
          markInspected(turnId);

        } else if (eventType === "token") {
          removeTypingIndicator();
          ensureAssistantDiv();
          removeToolIndicator(assistantDiv);
          accumulated += data.token;
          scheduleRender();

        } else if (eventType === "step") {
          // a pending render would replace the bubble's HTML and wipe the indicator
          if (renderFrame) renderNow();
          if (data.step === "llm_call" || data.step === "reasoning_start") {
            removeTypingIndicator();
            ensureAssistantDiv();
            showToolIndicator(assistantDiv, "Thinking");
            if (data.step === "llm_call") appendStep(data);
          } else {
            if (data.step === "tool_call") {
              removeTypingIndicator();
              ensureAssistantDiv();
              showToolIndicator(assistantDiv, data.name);
            } else if (data.step === "tool_result") {
              if (assistantDiv) removeToolIndicator(assistantDiv);
            } else if (data.step === "rate_limit_wait") {
              removeTypingIndicator();
              ensureAssistantDiv();
              showToolIndicator(assistantDiv, `Waiting for rate limit (${Math.ceil(data.seconds)}s)`, { elapsed: false });
            }
            appendStep(data);
          }

        } else if (eventType === "error") {
          cancelRender();
          removeTypingIndicator();
          if (assistantDiv) assistantDiv.remove();
          const errDiv = appendError(data.message);
          if (turnId) errDiv.dataset.turnId = turnId;
          appendStep({ step: "error", message: data.message });

        } else if (eventType === "done") {
          ensureAssistantDiv();
          // flush first, or a late frame would wipe the download buttons added below
          if (renderFrame) renderNow();
          const charts = data.charts || [];
          if (charts.length) {
            accumulated += chartMarkdown(charts);
            renderMd(assistantDiv, accumulated);
            addChartButtons(assistantDiv);
            addCopyButtons();
          }
          appendDownloads(assistantDiv, data.files || []);
          document.getElementById("messages").scrollTop =
            document.getElementById("messages").scrollHeight;
          loadSessions();
        }
      }
    }

  } catch (err) {
    cancelRender();
    removeTypingIndicator();
    if (assistantDiv) assistantDiv.remove();
    appendError(`Connection error: ${err.message}`);

  } finally {
    isLoading = false;
    document.getElementById("send-btn").disabled = false;
    removeTypingIndicator();
  }
}

// ── reload prompt ─────────────────────────────────
async function reloadPrompt() {
  const btn = document.getElementById("reload-prompt-btn");
  btn.textContent = "…";
  btn.disabled = true;
  const res = await fetch("/reload-prompt", { method: "POST" });
  const data = await res.json();
  btn.textContent = "↺ Prompt";
  btn.disabled = false;
  if (data.status === "ok") {
    btn.title = data.preview;
  }
}

// ── utils ─────────────────────────────────────────
function formatErrorDetail(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map(d => d?.msg || JSON.stringify(d)).join("; ");
  if (detail && typeof detail === "object") return detail.msg || JSON.stringify(detail);
  return String(detail);
}

function escapeHtml(str) {
  if (!str) return "";
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// auto-resize textarea as user types
document.getElementById("input").addEventListener("input", function () {
  this.style.height = "auto";
  this.style.height = Math.min(this.scrollHeight, 160) + "px";
});

// drag & drop
const chatPanel = document.getElementById("chat-panel");

chatPanel.addEventListener("dragover", (e) => {
  e.preventDefault();
  chatPanel.classList.add("drag-over");
});

chatPanel.addEventListener("dragleave", (e) => {
  if (!chatPanel.contains(e.relatedTarget)) {
    chatPanel.classList.remove("drag-over");
  }
});

chatPanel.addEventListener("drop", async (e) => {
  e.preventDefault();
  chatPanel.classList.remove("drag-over");
  await uploadFiles(e.dataTransfer.files);
});

// clicking a message shows its turn in the inspector
document.getElementById("messages").addEventListener("click", (e) => {
  if (isLoading || e.target.closest("button, a, textarea")) return;
  const bubble = e.target.closest("[data-turn-id]");
  if (bubble) showTurn(bubble.dataset.turnId);
});

// ── init ──────────────────────────────────────────
loadSessions();
