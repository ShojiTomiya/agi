// ── state ─────────────────────────────────────────
let sessionId   = null;
let pendingFiles = [];   // [{path, filename, previewUrl}]
let isLoading   = false;

// ── sessions ──────────────────────────────────────
async function loadSessions() {
  const res      = await fetch("/sessions");
  const sessions = await res.json();
  const el       = document.getElementById("sessions");

  el.innerHTML = sessions.map(s => `
    <div class="session-item ${s.id === sessionId ? "active" : ""}"
         onclick="loadSession('${s.id}')">
      <span class="session-title">${escapeHtml(s.title || "Conversation")}</span>
      <span class="session-rename"
            onclick="event.stopPropagation(); renameSession('${s.id}', '${escapeHtml(s.title || 'Conversation')}')">✎</span>
      <span class="session-delete"
            onclick="event.stopPropagation(); deleteSession('${s.id}')">×</span>
    </div>
  `).join("");
}

async function loadSession(id) {
  sessionId = id;
  document.getElementById("messages").innerHTML = "";
  clearInspector();
  loadSessions();

  const res  = await fetch(`/sessions/${id}/messages`);
  const rows = await res.json();

  for (const row of rows) {
    if (row.role === "user" || row.role === "assistant") {
      let previews = [];
      if (row.role === "user") {
        const filePaths = (row.metadata?.file_paths || []).map(p => typeof p === "string" ? p : p.path);
        previews = filePaths.map(p => {
          const filename = p.split("_").slice(1).join("_") || p;
          if (p.match(/\.(png|jpg|jpeg|webp|gif)$/i)) return { path: p, previewUrl: "/" + p, filename };
          return { path: p, filename };
        });
      }

      let content = row.content;
      for (const path of (row.metadata?.chart_paths || [])) {
        content += `\n\n![chart](/${path})`;
      }

      const msgDiv = appendMessage(row.role, content, previews, row.id);
      if (row.role === "assistant") {
        const nonImgFiles = (row.metadata?.file_paths || []).filter(f => {
          const p = typeof f === "string" ? f : f.path;
          return !p.match(/\.(png|jpg|jpeg|webp|gif)$/i);
        });
        if (nonImgFiles.length > 0) {
          const fileRow = document.createElement("div");
          fileRow.style.cssText = "display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px;";
          for (const f of nonImgFiles) {
            const p = typeof f === "string" ? f : f.path;
            const name = typeof f === "string" ? p.split("_").slice(1).join("_") : f.name;
            const a = document.createElement("a");
            a.href = `/${p}`;
            a.download = name;
            a.className = "file-download-btn";
            a.textContent = `⬇ ${name}`;
            fileRow.appendChild(a);
          }
          msgDiv.appendChild(fileRow);
        }
      }
    }
  }
  addCopyButtons();
  addChartButtons(document.getElementById("messages"));
  buildInspectorFromHistory(rows);
}



function renderMd(el, text) {
  el.innerHTML = marked.parse(text);
  if (window._katexReady) {
    renderMathInElement(el, {
      delimiters: [
        {left: "$$", right: "$$", display: true},
        {left: "$", right: "$", display: false},
        {left: "\\[", right: "\\]", display: true},
        {left: "\\(", right: "\\)", display: false},
      ],
      throwOnError: false,
    });
  }
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
  document.getElementById("session-tokens").textContent = "Session: 0 tokens";
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

async function handleFiles(event) {
  const files = Array.from(event.target.files);

  for (const file of files) {
    const form = await buildFileFormData(file);

    const res = await fetch("/upload", { method: "POST", body: form });

    if (!res.ok) {
      const err = await res.json();
      appendError(`Upload failed: ${formatErrorDetail(err.detail)}`);
      continue;
    }

    const data = await res.json();
    const previewUrl = file.type.startsWith("image/")
      ? URL.createObjectURL(file)
      : null;

    pendingFiles.push({ path: data.path, filename: data.filename, previewUrl });
  }

  renderFilePreviews();
  event.target.value = "";
}

function renderFilePreviews() {
  const el = document.getElementById("file-preview");
  el.innerHTML = pendingFiles.map((f, i) => `
    <div class="file-chip">
      <span>${escapeHtml(f.filename)}</span>
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
          chip.textContent = `📎 ${f.filename}`;
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
      chip.textContent = `📎 ${f.filename}`;
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
  document.querySelector(".regenerate-btn")?.remove();

  document.getElementById("input").value = newText;
  pendingFiles = originalFiles.filter(f => f.path);
  send();
}

function appendError(message) {
  const el  = document.getElementById("messages");
  const div = document.createElement("div");
  div.className = "msg error-msg";
  div.textContent = `⚠ ${message}`;
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
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

function showToolIndicator(div, toolName) {
  removeTypingIndicator();
  removeToolIndicator(div);
  const el = document.createElement("span");
  el.className = "tool-indicator";
  el.textContent = toolName + "…";
  div.appendChild(el);
  document.getElementById("messages").scrollTop =
    document.getElementById("messages").scrollHeight;
}

function removeToolIndicator(div) {
  div.querySelector(".tool-indicator")?.remove();
}

function addRegenerateButton() {
  document.querySelector(".regenerate-btn")?.remove();

  const messages = document.getElementById("messages");
  const btn = document.createElement("button");
  btn.className = "regenerate-btn";
  btn.textContent = "↺ Regenerate";
  btn.onclick = regenerate;
  messages.appendChild(btn);
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
      items.push(loopRow("llm", "LLM call", ev.model || "", secId));
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
      const meta = `<div class="itab-meta">${escapeHtml(ev.model || "")}</div>`;
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
      parts.push(makeSec(id, "Result", `<pre class="itab-pre">${escapeHtml(ev.result || "")}</pre>`));
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
  } else if (step.step === "llm_call" || step.step === "reasoning" || step.step === "tool_call" || step.step === "tool_result") {
    inspectorData.events.push(step);
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
  function ensureAssistantDiv() {
    if (!assistantDiv) assistantDiv = appendMessage("assistant", "");
    return assistantDiv;
  }
  let accumulated = "";

  try {
    const res = await fetch("/chat", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({
        message:    text,
        session_id: sessionId,
        file_paths: files.map(f => f.path),
        filenames:  files.map(f => f.filename),
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

        } else if (eventType === "token") {
          removeTypingIndicator();
          ensureAssistantDiv();
          removeToolIndicator(assistantDiv);
          accumulated += data.token;
          renderMd(assistantDiv, accumulated);
          addCopyButtons();
          document.getElementById("messages").scrollTop =
            document.getElementById("messages").scrollHeight;

        } else if (eventType === "step") {
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
            }
            appendStep(data);
          }

        } else if (eventType === "error") {
          removeTypingIndicator();
          if (assistantDiv) assistantDiv.remove();
          appendError(data.message);

        } else if (eventType === "done") {
          ensureAssistantDiv();
          for (const path of (data.chart_paths || [])) {
            accumulated += `\n\n![chart](/${path})`;
          }
          if ((data.chart_paths || []).length > 0) {
            renderMd(assistantDiv, accumulated);
            addChartButtons(assistantDiv);
            addCopyButtons();
          }
          if ((data.file_paths || []).length > 0) {
            const fileRow = document.createElement("div");
            fileRow.style.cssText = "display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px;";
            for (const f of data.file_paths) {
              const a = document.createElement("a");
              a.href = `/${f.path}`;
              a.download = f.name;
              a.className = "file-download-btn";
              a.textContent = `⬇ ${f.name}`;
              fileRow.appendChild(a);
            }
            assistantDiv.appendChild(fileRow);
          }
          if (data.user_msg_id) userDiv.dataset.msgId = data.user_msg_id;
          document.getElementById("messages").scrollTop =
            document.getElementById("messages").scrollHeight;
          loadSessions();
        }
      }
    }

  } catch (err) {
    removeTypingIndicator();
    if (assistantDiv) assistantDiv.remove();
    appendError(`Connection error: ${err.message}`);

  } finally {
    isLoading = false;
    document.getElementById("send-btn").disabled = false;
    removeTypingIndicator();
  }
}

// ── inspector from history ────────────────────────
function buildInspectorFromHistory(rows) {
  clearInspector();
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

  const files = Array.from(e.dataTransfer.files);
  for (const file of files) {
    const form = await buildFileFormData(file);

    const res = await fetch("/upload", { method: "POST", body: form });

    if (!res.ok) {
      const err = await res.json();
      appendError(`Upload failed: ${formatErrorDetail(err.detail)}`);
      continue;
    }

    const data = await res.json();
    const previewUrl = file.type.startsWith("image/")
      ? URL.createObjectURL(file)
      : null;

    pendingFiles.push({ path: data.path, filename: data.filename, previewUrl });
  }

  renderFilePreviews();
});

// ── init ──────────────────────────────────────────
loadSessions();
