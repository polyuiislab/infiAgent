const $ = (id) => document.getElementById(id);

const STORAGE_PREFIX = "infiagent.deploy.console";

const DEFAULT_ADVANCED_SETTINGS = {
  systemAddPath: "",
  reasoningMode: "",
  reasoningEffort: "",
  thinkingContextMode: "",
  thinkingSteps: "30",
  historyRecentItems: "0",
  autoMode: "",
  sandboxEnabled: "",
  visibleSkills: "",
  contextPlacement: "",
  skillMaterialization: "",
  skillStoreDir: "",
  maxTurns: "",
  autoResumeAttempts: "2",
  autoResumeDelaySec: "1",
  forceNew: false,
  includeTrace: true,
  queueResumeIfNeeded: true,
  sdkOverrides: "{}",
};

// H11: task list paging, compact history and SSE resume tuning.
const TASK_PAGE_SIZE = 100;
const HISTORY_MAX_OUTPUT_CHARS = 4000;
const STREAM_RESUME_MAX_ATTEMPTS = 5;
const STREAM_RESUME_BASE_DELAY_MS = 1000;
const STREAM_STATUS_KEY = "stream-resume";
const QUEUE_STATUS_KEY = "run-queue";

const state = {
  tasks: [],
  // H11: cursor for the next page of GET /api/v1/tasks. null means the loaded
  // prefix is everything the server has — which is also what an old server
  // (no next_cursor in the response) reports, so the list stays as it was.
  taskCursor: null,
  currentTaskId: "",
  streamController: null,
  streamTaskId: "",
  streaming: false,
  // H11: highest per-task SSE `seq` seen, so a dropped connection can resume with
  // after_seq; runTerminal flips on run.finished/run.error so a reader that ends
  // without one is treated as a drop; streamGeneration invalidates an auto-resume
  // that is still waiting out its backoff when the user detaches or starts anew.
  lastEventSeq: 0,
  // 4.5.0 (W10): exact resume cursor — the last SSE id rendered, `<seq>.<k>` (k-th event derived from
  // one persisted record). Deltas carry no id, so they never move it. completedSegments holds the
  // segment_ids whose `.done` is already on screen (a late delta of those is a reconnect overlap);
  // resyncRequested is set by `stream.resync` (the server dropped us for lagging; reconnect at once).
  lastEventId: "",
  completedSegments: {},
  resyncRequested: false,
  resyncStreak: 0,
  runTerminal: true,
  streamGeneration: 0,
  pendingAssistantNode: null,
  activeAssistantNodes: {},
  lastStreamAgentName: "",
  systemAddSources: {},
  lastAgentSystem: "",
  apiPreviewMode: "stream",
  feedbackByRun: {},
  // R14: deferred human confirmations for the current task, grouped by the user
  // message that produced them (one turn == one conversation_id in the framework).
  hcByMessage: {},
  hcLastInputMessage: "",
  // 4.6.0 (N03): shared workspaces from GET /api/v1/workspaces. workspacesSupported stays false on an
  // old server (the endpoint is missing), which hides the workspace choice and keeps the flat task list.
  workspaces: [],
  workspacesById: {},
  workspacesSupported: false,
};

const WORKSPACE_CREATE = "__create__";

const FEEDBACK_VERDICTS = [
  { value: "approved", label: "认可" },
  { value: "partially_approved", label: "部分认可" },
  { value: "usable_after_edit", label: "修改后可采纳" },
  { value: "rejected", label: "不认可" },
];

function verdictLabel(value) {
  const found = FEEDBACK_VERDICTS.find((item) => item.value === value);
  return found ? found.label : String(value || "");
}

// Silently release the current stream: the aborted fetch's finally block
// checks controller ownership, so it will not clear state or reload history.
function detachStream() {
  // H11: bump first — an auto-resume may be waiting out its backoff with no
  // controller in flight, and the generation check is the only thing that
  // cancels it.
  state.streamGeneration = (state.streamGeneration || 0) + 1;
  const controller = state.streamController;
  if (!controller) {
    if (state.streaming) {
      // Detached while a resume was pending: nothing to abort, but the console
      // must stop reporting itself as streaming.
      state.streaming = false;
      state.streamTaskId = "";
      clearTranscriptStatus(STREAM_STATUS_KEY);
    }
    return;
  }
  state.streamController = null;
  state.streamTaskId = "";
  state.streaming = false;
  resetStreamRenderState();
  resetMacLikeStreamState();
  removeTypingIndicator();
  controller.abort();
}

function storageKey(name) {
  return `${STORAGE_PREFIX}.${name}`;
}

function prettyJson(value) {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

function mergeCompletedText(currentText, completedText) {
  const current = String(currentText || "");
  const completed = String(completedText || "");
  if (!completed) return current;
  if (!current) return completed;
  if (completed.startsWith(current)) {
    return current + completed.slice(current.length);
  }
  return completed;
}

// 4.5.0 (W10): SSE cursor helpers. `12.3` -> [12, 3]; a plain `12` (4.4.x servers) means every
// event of seq 12 was delivered -> [12, Infinity]; anything else -> null.
function parseSseCursor(value) {
  const text = String(value ?? "").trim();
  const match = /^(\d+)(?:\.(\d+))?$/.exec(text);
  if (!match) return null;
  return [Number(match[1]), match[2] === undefined ? Infinity : Number(match[2])];
}

function compareSseCursor(a, b) {
  if (a[0] !== b[0]) return a[0] < b[0] ? -1 : 1;
  if (a[1] === b[1]) return 0;
  return a[1] < b[1] ? -1 : 1;
}

function formatSseCursor(cursor) {
  if (!cursor) return "";
  return Number.isFinite(cursor[1]) ? `${cursor[0]}.${cursor[1]}` : String(cursor[0]);
}

function resetStreamCursor() {
  state.lastEventSeq = 0;
  state.lastEventId = "";
  state.completedSegments = {};
  state.resyncRequested = false;
  state.resyncStreak = 0;
}

function hasVisibleText(value) {
  return String(value || "").trim().length > 0;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

// ---- Minimal, dependency-free, XSS-safe Markdown renderer ----
// The runtime ships offline, so no CDN library is available. This covers the
// common shapes agent final output uses: headings, bold/italic, inline code,
// fenced code blocks, lists, links, blockquotes, rules and GFM tables.

function mdSanitizeUrl(url) {
  const raw = String(url || "").trim().replace(/&amp;/g, "&");
  if (/^(https?:|mailto:)/i.test(raw)) return raw;
  if (/^(#|\/|\.\/|\.\.\/)/.test(raw)) return raw;
  if (!/^[a-z][a-z0-9+.\-]*:/i.test(raw)) return raw; // relative path without scheme
  return "";
}

function mdInline(escaped) {
  const codes = [];
  let out = escaped.replace(/`([^`]+)`/g, (_m, code) => {
    codes.push(code);
    return `\u0000C${codes.length - 1}\u0000`;
  });
  out = out.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+&quot;[^&]*&quot;)?\)/g, (_m, label, url) => {
    const safe = mdSanitizeUrl(url);
    if (!safe) return label;
    return `<a href="${escapeHtml(safe)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  });
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  out = out.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  out = out.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
  out = out.replace(/\u0000C(\d+)\u0000/g, (_m, i) => `<code class="md-code">${codes[Number(i)]}</code>`);
  return out;
}

function mdSplitRow(line) {
  return line.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((cell) => cell.trim());
}

function renderMarkdown(source) {
  const lines = String(source == null ? "" : source).replace(/\r\n?/g, "\n").split("\n");
  const blockStart = /^(#{1,6}\s|```|\s*>|\s*[-*+]\s|\s*\d+[.)]\s)/;
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    const fence = line.match(/^```(.*)$/);
    if (fence) {
      const buf = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i])) {
        buf.push(lines[i]);
        i += 1;
      }
      i += 1;
      out.push(`<pre class="md-pre"><code>${escapeHtml(buf.join("\n"))}</code></pre>`);
      continue;
    }

    if (/^\s*$/.test(line)) {
      i += 1;
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      const level = heading[1].length;
      out.push(`<h${level} class="md-h">${mdInline(escapeHtml(heading[2].trim()))}</h${level}>`);
      i += 1;
      continue;
    }

    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      out.push('<hr class="md-hr" />');
      i += 1;
      continue;
    }

    if (
      /\|/.test(line) &&
      i + 1 < lines.length &&
      /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$/.test(lines[i + 1])
    ) {
      const header = mdSplitRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /\|/.test(lines[i]) && !/^\s*$/.test(lines[i])) {
        rows.push(mdSplitRow(lines[i]));
        i += 1;
      }
      let table = '<div class="md-table-wrap"><table class="md-table"><thead><tr>';
      table += header.map((cell) => `<th>${mdInline(escapeHtml(cell))}</th>`).join("");
      table += "</tr></thead><tbody>";
      for (const row of rows) {
        table += "<tr>" + header.map((_c, ci) => `<td>${mdInline(escapeHtml(row[ci] || ""))}</td>`).join("") + "</tr>";
      }
      table += "</tbody></table></div>";
      out.push(table);
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      out.push(`<blockquote class="md-quote">${renderMarkdown(buf.join("\n"))}</blockquote>`);
      continue;
    }

    if (/^\s*[-*+]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*+]\s+/, ""));
        i += 1;
      }
      out.push(`<ul class="md-ul">${items.map((it) => `<li>${mdInline(escapeHtml(it))}</li>`).join("")}</ul>`);
      continue;
    }

    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+[.)]\s+/, ""));
        i += 1;
      }
      out.push(`<ol class="md-ol">${items.map((it) => `<li>${mdInline(escapeHtml(it))}</li>`).join("")}</ol>`);
      continue;
    }

    const buf = [line];
    i += 1;
    while (i < lines.length && !/^\s*$/.test(lines[i]) && !blockStart.test(lines[i])) {
      buf.push(lines[i]);
      i += 1;
    }
    out.push(`<p class="md-p">${buf.map((l) => mdInline(escapeHtml(l))).join("<br />")}</p>`);
  }
  return out.join("\n");
}

function setMarkdown(node, text) {
  if (!node) return;
  node.classList.add("markdown");
  node.innerHTML = renderMarkdown(text);
}

function readSystemSettings(agentSystem) {
  if (!agentSystem) return { ...DEFAULT_ADVANCED_SETTINGS };
  try {
    const raw = localStorage.getItem(storageKey(`advanced.${agentSystem}`));
    if (!raw) return { ...DEFAULT_ADVANCED_SETTINGS };
    const parsed = JSON.parse(raw);
    return {
      ...DEFAULT_ADVANCED_SETTINGS,
      ...(parsed && typeof parsed === "object" ? parsed : {}),
    };
  } catch {
    return { ...DEFAULT_ADVANCED_SETTINGS };
  }
}

function writeSystemSettings(agentSystem, payload) {
  if (!agentSystem) return;
  localStorage.setItem(storageKey(`advanced.${agentSystem}`), JSON.stringify(payload));
}

function readLastAgentSystem() {
  return localStorage.getItem(storageKey("lastAgentSystem")) || "";
}

function writeLastAgentSystem(agentSystem) {
  if (!agentSystem) return;
  localStorage.setItem(storageKey("lastAgentSystem"), agentSystem);
}

function readLastAgentName(agentSystem) {
  if (!agentSystem) return "";
  return localStorage.getItem(storageKey(`agentName.${agentSystem}`)) || "";
}

function writeLastAgentName(agentSystem, agentName) {
  if (!agentSystem || !agentName) return;
  localStorage.setItem(storageKey(`agentName.${agentSystem}`), agentName);
}

async function parseResponsePayload(res) {
  const text = await res.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return { raw: text };
  }
}

function errorMessageFromPayload(payload) {
  if (!payload) return "Unknown error";
  if (typeof payload === "string") return payload;
  if (payload.detail) {
    if (typeof payload.detail === "string") return payload.detail;
    return JSON.stringify(payload.detail, null, 2);
  }
  if (payload.error) return typeof payload.error === "string" ? payload.error : JSON.stringify(payload.error, null, 2);
  if (payload.raw) return payload.raw;
  return JSON.stringify(payload, null, 2);
}

async function jsonFetch(url, method = "GET", body = null) {
  const res = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const payload = await parseResponsePayload(res);
  if (!res.ok) {
    const error = new Error(errorMessageFromPayload(payload));
    // 4.6.5: keep the API error code (SCOPE_DENIED, WORKSPACE_NOT_FOUND …) so callers can explain what to do
    if (payload && typeof payload === "object" && payload.code) error.code = payload.code;
    throw error;
  }
  return payload;
}

async function uploadFetch(url, formData) {
  const res = await fetch(url, {
    method: "POST",
    body: formData,
  });
  const payload = await parseResponsePayload(res);
  if (!res.ok) {
    throw new Error(errorMessageFromPayload(payload));
  }
  return payload;
}

function writePreviewStatus(text) {
  const node = $("apiPreviewStatus");
  if (node) node.textContent = text || "";
}

function prettyWrite(payload) {
  const status = payload && typeof payload === "object" ? payload.status || payload.type || "response" : "response";
  writePreviewStatus(`Last action: ${status}`);
  updateApiPreview();
}

function setUserRootUploadStatus(message, tone = "muted") {
  const node = $("userRootUploadStatus");
  if (!node) return;
  node.className = `small upload-status ${tone}`;
  node.textContent = typeof message === "string" ? message : JSON.stringify(message, null, 2);
}

function setPipUpdateStatus(message, tone = "muted") {
  const node = $("pipUpdateStatus");
  if (!node) return;
  node.className = `small upload-status ${tone}`;
  node.textContent = typeof message === "string" ? message : JSON.stringify(message, null, 2);
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

function formatElapsed(startedAt) {
  const seconds = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${seconds % 60}s`;
}

async function uploadFetchWithProgress(url, formData, { onProgress, onServerWait } = {}) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);

    xhr.upload.onprogress = (event) => {
      if (!onProgress) return;
      onProgress({
        loaded: event.loaded,
        total: event.lengthComputable ? event.total : 0,
        lengthComputable: event.lengthComputable,
      });
    };

    xhr.upload.onload = () => {
      if (onServerWait) onServerWait();
    };

    xhr.onerror = () => reject(new Error("Network error while uploading user_root."));
    xhr.onabort = () => reject(new Error("Upload was aborted."));
    xhr.ontimeout = () => reject(new Error("Upload timed out."));
    xhr.onload = async () => {
      const payload = await parseResponsePayload({
        ok: xhr.status >= 200 && xhr.status < 300,
        text: async () => xhr.responseText || "",
      });
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(new Error(errorMessageFromPayload(payload)));
        return;
      }
      resolve(payload);
    };

    xhr.send(formData);
  });
}

// ha12: the topbar stays one row, so a long agent system / agent name can end in an ellipsis —
// keep the full name in the tooltip.
function syncAgentSelectTitles() {
  for (const id of ["agentSystem", "agentName"]) {
    const select = $(id);
    if (select) select.title = select.value || "";
  }
}

function fillSelect(selectEl, items) {
  selectEl.innerHTML = "";
  for (const item of items) {
    const option = document.createElement("option");
    option.value = item.value;
    option.textContent = item.label;
    selectEl.appendChild(option);
  }
}

// D-7: a fork carries its own task directory, so without a mark it reads as a
// duplicate of the run it came from. Label it; never hide it.
function forkBadge(task) {
  if (!task || !task.is_fork) return "";
  const origin = task.fork_of || {};
  const parent = taskBaseName(origin.task_id || "");
  const seq = origin.run_action_seq;
  const detail = [
    parent ? `源会话 ${parent}` : "",
    origin.run_id ? `源 run ${origin.run_id}` : "",
    Number.isFinite(seq) ? `分叉步 ${seq}` : "",
  ].filter(Boolean).join(" · ");
  return `<span class="task-fork" title="${escapeHtml(detail || "由轨迹分叉创建")}">分叉</span>`;
}

function taskBaseName(value) {
  const text = String(value || "").replace(/[\\/]+$/, "");
  const parts = text.split(/[\\/]/);
  return parts[parts.length - 1] || text;
}

// ----- Task Files browser (right panel) -----

// 4.6.5: `area` = "business" (the workspace of a workspace session, the session directory otherwise) or "session"
// (a workspace session's own private directory: session.json, logs, runs, experience_catalog …; read-only).
function fileAreaParam(area) {
  return area === "session" ? "&area=session" : "";
}

function artifactUrl(relativePath, area = "business") {
  return `/api/v1/tasks/artifact?task_id=${encodeURIComponent(state.currentTaskId)}&path=${encodeURIComponent(relativePath)}${fileAreaParam(area)}`;
}

function artifactDeleteUrl(relativePath) {
  return `/api/v1/tasks/artifact?task_id=${encodeURIComponent(state.currentTaskId)}&path=${encodeURIComponent(relativePath)}`;
}

function formatFileSize(size) {
  if (size == null) return "";
  return formatBytes(size);
}

async function fetchTaskFileEntries(path, area = "business") {
  const data = await jsonFetch(
    `/api/v1/tasks/files?task_id=${encodeURIComponent(state.currentTaskId)}&path=${encodeURIComponent(path || "")}${fileAreaParam(area)}`,
  );
  return data.entries || [];
}

async function deleteTaskFile(entry, button) {
  const taskId = state.currentTaskId;
  if (!taskId || !entry?.path) return;
  const confirmed = window.confirm(`确定删除这个文件吗？\n\n${entry.path}\n\n此操作不可恢复。`);
  if (!confirmed) return;

  button.disabled = true;
  button.textContent = "删除中…";
  try {
    const data = await jsonFetch(artifactDeleteUrl(entry.path), "DELETE");
    writePreviewStatus(`Deleted: ${data.path || entry.path}`);
    await refreshTaskFiles();
  } catch (error) {
    button.disabled = false;
    button.textContent = "删除";
    const message = String(error.message || error);
    writePreviewStatus(`Delete failed: ${message}`);
    window.alert(`删除失败：${message}`);
  }
}

function renderFileEntryList(container, entries, area = "business") {
  container.innerHTML = "";
  if (!entries.length) {
    const empty = document.createElement("div");
    empty.className = "files-empty";
    empty.textContent = "（空目录）";
    container.appendChild(empty);
    return;
  }
  for (const entry of entries) {
    if (entry.type === "dir") {
      const details = document.createElement("details");
      details.className = "files-dir";
      const summary = document.createElement("summary");
      summary.textContent = `📁 ${entry.name}`;
      details.appendChild(summary);
      const child = document.createElement("div");
      child.className = "files-children";
      details.appendChild(child);
      let loaded = false;
      details.addEventListener("toggle", async () => {
        if (!details.open || loaded) return;
        loaded = true;
        child.textContent = "加载中…";
        try {
          renderFileEntryList(child, await fetchTaskFileEntries(entry.path, area), area);
        } catch (error) {
          child.textContent = `加载失败: ${String(error.message || error)}`;
          loaded = false;
        }
      });
      container.appendChild(details);
      continue;
    }
    const row = document.createElement("div");
    row.className = "files-file-row";

    const download = document.createElement("a");
    download.className = "files-file";
    download.href = artifactUrl(entry.path, area);
    download.download = entry.name;
    download.title = `${entry.path}\n${entry.modified_at || ""}`;
    download.innerHTML = `<span class="files-name">📄 ${escapeHtml(entry.name)}</span><span class="files-size">${formatFileSize(entry.size)}</span>`;
    if (area === "session") {
      // the private directory is the session's own record: download only
      row.append(download);
      container.appendChild(row);
      continue;
    }

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "files-delete";
    deleteButton.textContent = "删除";
    deleteButton.title = `删除 ${entry.path}`;
    deleteButton.addEventListener("click", () => deleteTaskFile(entry, deleteButton));

    row.append(download, deleteButton);
    container.appendChild(row);
  }
}

async function refreshTaskFiles() {
  const tree = $("taskFilesTree");
  const title = $("taskFilesTitle");
  if (!state.currentTaskId) {
    title.textContent = "";
    tree.innerHTML = '<div class="files-empty">请先选择一个 task</div>';
    return;
  }
  title.textContent = taskBaseName(state.currentTaskId);
  tree.innerHTML = '<div class="files-empty">加载中…</div>';
  try {
    const workspaceId = await taskWorkspaceId(state.currentTaskId);
    if (!workspaceId) {
      renderFileEntryList(tree, await fetchTaskFileEntries(""));
      return;
    }
    // 4.6.5: a workspace session works in two places — the workspace every session in it shares (file tools,
    // commands) and its own private directory (records, logs, the experience catalog). Show both.
    tree.innerHTML = "";
    const shared = createFileSection(`工作区文件（共享）`, workspaceLabel(workspaceId), workspaceId);
    const own = createFileSection("本会话私有文件", "只读", "会话记录、日志、运行轨迹、经验全集等");
    tree.append(shared.section, own.section);
    await Promise.all([loadFileSection(shared.body, "business"), loadFileSection(own.body, "session")]);
  } catch (error) {
    tree.innerHTML = `<div class="files-empty">加载失败: ${escapeHtml(String(error.message || error))}</div>`;
  }
}

// The workspace a task works in ("" = none): from the loaded task list, else from the task's metadata (the list is paged).
async function taskWorkspaceId(taskId) {
  if (!state.workspacesSupported || !taskId) return "";
  const task = state.tasks.find((item) => item.task_id === taskId);
  if (task) return task.workspace_id || "";
  const meta = await jsonFetch(`/api/v1/tasks/meta?task_id=${encodeURIComponent(taskId)}`);
  return (meta.workspace && meta.workspace.workspace_id) || "";
}

function createFileSection(title, note, tooltip = "") {
  const section = document.createElement("div");
  section.className = "files-section";
  const head = document.createElement("div");
  head.className = "files-section-head";
  const name = document.createElement("span");
  name.textContent = title;
  const detail = document.createElement("span");
  detail.className = "files-section-note";
  detail.textContent = note || "";
  if (tooltip) detail.title = tooltip;
  head.append(name, detail);
  const body = document.createElement("div");
  body.innerHTML = '<div class="files-empty">加载中…</div>';
  section.append(head, body);
  return { section, body };
}

async function loadFileSection(body, area) {
  try {
    renderFileEntryList(body, await fetchTaskFileEntries("", area), area);
  } catch (error) {
    body.innerHTML = `<div class="files-empty">加载失败: ${escapeHtml(String(error.message || error))}</div>`;
  }
}

function sidePanelOpen(panelId) {
  const panel = $(panelId);
  return Boolean(panel && panel.classList.contains("open"));
}

function toggleSidePanel(panelId) {
  const panel = $(panelId);
  if (!panel) return false;
  panel.classList.toggle("open");
  const open = panel.classList.contains("open");
  const caret = panel.querySelector(".panel-caret");
  if (caret) caret.textContent = open ? "▾" : "▸";
  return open;
}

function resetTaskFilesPanel({ pending = false } = {}) {
  if (!$("taskFilesPanel")) return;
  if (sidePanelOpen("taskFilesPanel") && !pending) {
    refreshTaskFiles();
  } else {
    $("taskFilesTree").innerHTML = "";
    $("taskFilesTitle").textContent = "";
  }
}

function formatTaskLabel(task) {
  return taskBaseName(task.conversation_name || task.task_id) || "task";
}

// `pending`: the task does not exist yet (New Task before its ensure call) — skip the files request,
// which would only 404; ensureTask() selects the task again once the session is created.
function setCurrentTask(taskId, { pending = false } = {}) {
  // H11: `seq` is monotonic per task, so a different task must not inherit the
  // old high-water mark — a resume would silently skip its first events.
  if (taskId !== state.currentTaskId) {
    resetStreamCursor();
  }
  state.currentTaskId = taskId;
  $("taskId").value = taskId || "";
  renderTaskList();
  resetTaskFilesPanel({ pending });
}

// 4.6.5: local time (toISOString() is UTC — a session created at 09:30 in UTC+8 used to be named …_013000)
function generateTaskId() {
  const now = new Date();
  const pad = (value) => String(value).padStart(2, "0");
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`
    + `_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  return `task_${stamp}`;
}

function ensureCurrentTaskId() {
  const value = $("taskId").value.trim();
  if (value) {
    state.currentTaskId = value;
    return value;
  }
  const nextId = generateTaskId();
  setCurrentTask(nextId);
  return nextId;
}

function messageBubble(role, header, text = "") {
  const wrapper = document.createElement("div");
  wrapper.className = `message ${role}`;

  const headerNode = document.createElement("div");
  headerNode.className = "message-header";
  headerNode.textContent = header;
  wrapper.appendChild(headerNode);

  const bodyNode = document.createElement("div");
  bodyNode.className = "message-body";
  bodyNode.textContent = text;
  wrapper.appendChild(bodyNode);

  return { wrapper, bodyNode };
}

function createProcessCard(title, content = "", { open = false, className = "", markdown = false } = {}) {
  const details = document.createElement("details");
  details.className = `process-card ${className}`.trim();
  details.open = open;

  const summary = document.createElement("summary");
  summary.className = "process-summary";
  summary.textContent = title;
  details.appendChild(summary);

  const body = document.createElement("div");
  body.className = "process-body";
  details.appendChild(body);

  // Building text nodes for hundreds of collapsed cards is the main history
  // rendering cost; defer until the card is actually expanded. Live-stream
  // code appends to card.body directly, so never overwrite existing text.
  let rendered = false;
  const render = () => {
    if (rendered) return;
    rendered = true;
    if (body.textContent || body.innerHTML) return;
    if (markdown) setMarkdown(body, content);
    else body.textContent = content;
  };
  if (open) {
    render();
  } else {
    details.addEventListener("toggle", () => {
      if (details.open) render();
    });
  }

  return { details, body, summary };
}

function createJsonDetails(title, payload, { open = false } = {}) {
  const details = document.createElement("details");
  details.className = "json-details";
  details.open = open;

  const summary = document.createElement("summary");
  summary.textContent = title;
  details.appendChild(summary);

  const pre = document.createElement("pre");
  pre.className = "json-block";
  details.appendChild(pre);

  let rendered = false;
  const render = () => {
    if (rendered) return;
    rendered = true;
    pre.textContent = prettyJson(payload);
  };
  if (open) {
    render();
  } else {
    details.addEventListener("toggle", () => {
      if (details.open) render();
    });
  }

  return details;
}

// H5: with `compact=1` the history endpoint replaces an oversized tool output
// with `output_preview` (the first max_output_chars) plus
// `output_ref {run_id, seq, bytes}`; the full text is fetched on demand from
// /api/v1/tasks/tool-output. Without a ref nothing below changes what renders.
function toolOutputRefOf(result) {
  if (!result || typeof result !== "object" || Array.isArray(result)) return null;
  const ref = result.output_ref;
  if (!ref || typeof ref !== "object" || Array.isArray(ref)) return null;
  if (!Number.isFinite(Number(ref.seq))) return null;
  return ref;
}

// Accept the reference either inside the result (the documented shape) or hung
// off the process entry itself, so one renderer covers both.
function foldToolOutputRef(item, result) {
  if (!item || !item.output_ref) return result;
  const base = result && typeof result === "object" && !Array.isArray(result) ? { ...result } : {};
  base.output_ref = item.output_ref;
  if (item.output_preview !== undefined && base.output_preview === undefined) {
    base.output_preview = item.output_preview;
  }
  return base;
}

// 4.3.23: image_read 读到的图直接画。服务端（4.3.21+）在 tool.finished 事件与 history 工具条目上带
// `images: [{index, path, info, data_uri, bytes}]`，与 image_paths 同序；更老的服务只有
// result._image_base64_list，说明文字从 output 文案里按「路径 (宽x高, …)」逐张取，取不到只给序号。
const TOOL_IMAGE_CAPTION_RE = /^(.*) \((\d+x\d+, .*)\)$/;

// 工具输出文本里夹带的整段 base64（image_read 把返回值 dumps 进了 output）只在显示时换成占位，数据不动。
const INLINE_DATA_URI_RE = /data:image\/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]{64,}/gi;

function redactDataUris(text) {
  return String(text == null ? "" : text).replace(INLINE_DATA_URI_RE, (m) => `[image · ${Math.max(1, Math.round(m.length / 1024))} KB]`);
}

function isSafeImageDataUri(uri) {
  return typeof uri === "string" && /^data:image\/[a-z0-9.+-]+;base64,/i.test(uri);
}

function toolImagesOf(result, images) {
  const out = [];
  if (Array.isArray(images)) {
    images.forEach((raw, i) => {
      if (!raw || !isSafeImageDataUri(raw.data_uri)) return;
      const index = Number(raw.index);
      out.push({
        index: Number.isFinite(index) ? index : i,
        path: String(raw.path || ""),
        info: String(raw.info || ""),
        dataUri: raw.data_uri,
      });
    });
    if (out.length) return out;
  }
  if (!result || typeof result !== "object" || Array.isArray(result)) return out;
  let uris = result._image_base64_list;
  if (!Array.isArray(uris) && typeof result._image_base64 === "string") uris = [result._image_base64];
  if (!Array.isArray(uris)) return out;
  const captions = [];
  const output = typeof result.output === "string" ? result.output : "";
  const marker = "mode: ";
  const at = output.indexOf(marker);
  if (at >= 0) {
    const body = output.slice(at + marker.length).split(". Images are embedded")[0];
    for (const part of body.split("; ")) {
      const text = part.trim();
      if (!text) continue;
      const match = TOOL_IMAGE_CAPTION_RE.exec(text);
      captions.push(match ? [match[1], match[2]] : [text, ""]);
    }
  }
  uris.forEach((uri, index) => {
    if (!isSafeImageDataUri(uri)) return;
    const caption = captions[index] || ["", ""];
    out.push({ index, path: caption[0], info: caption[1], dataUri: uri });
  });
  return out;
}

// JSON 视图不再塞几十 KB 的 base64：换成占位文字，其它字段原样。
function stripToolImageData(result) {
  if (!result || typeof result !== "object" || Array.isArray(result)) return result;
  const placeholder = (uri, i, n) => `[image ${i + 1}/${n} · ${Math.max(1, Math.round(String(uri).length / 1024))} KB]`;
  const copy = { ...result };
  if (Array.isArray(copy._image_base64_list)) {
    const list = copy._image_base64_list;
    copy._image_base64_list = list.map((uri, i) => placeholder(uri, i, list.length));
  }
  if (typeof copy._image_base64 === "string") copy._image_base64 = placeholder(copy._image_base64, 0, 1);
  if (typeof copy.output === "string") copy.output = redactDataUris(copy.output);
  if (typeof copy.output_preview === "string") copy.output_preview = redactDataUris(copy.output_preview);
  return copy;
}

function createToolImagesBlock(images) {
  const block = document.createElement("div");
  block.className = "tool-images";
  for (const image of images) {
    const label = image.path || `图 ${image.index + 1}`;
    const figure = document.createElement("figure");
    figure.className = "tool-image";
    figure.title = image.info ? `${label} (${image.info})` : label;
    const img = document.createElement("img");
    img.src = image.dataUri;
    img.alt = label;
    img.loading = "lazy";
    img.addEventListener("click", () => figure.classList.toggle("zoom"));
    const caption = document.createElement("figcaption");
    caption.textContent = image.info ? `${label} · ${image.info.split(",")[0]}` : label;
    figure.appendChild(img);
    figure.appendChild(caption);
    block.appendChild(figure);
  }
  return block;
}

function toolOutputRefLabel(ref) {
  const bytes = Number(ref && ref.bytes);
  if (!Number.isFinite(bytes) || bytes <= 0) return "展开完整输出";
  return `展开完整输出 (${Math.max(1, Math.round(bytes / 1024))} KB)`;
}

function createToolOutputRefBlock(result, ref, taskId) {
  const details = document.createElement("details");
  details.className = "json-details tool-output-ref";
  details.open = true;

  const summary = document.createElement("summary");
  summary.textContent = "Result · 输出已截断";
  details.appendChild(summary);

  const pre = document.createElement("pre");
  pre.className = "json-block tool-output-preview";
  // Tool output is untrusted text: textContent only, never innerHTML.
  pre.textContent = redactDataUris(String(result.output_preview == null ? "" : result.output_preview));
  details.appendChild(pre);

  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary tool-output-expand";
  button.textContent = toolOutputRefLabel(ref);
  button.addEventListener("click", async () => {
    button.disabled = true;
    button.textContent = "加载中…";
    try {
      const params = new URLSearchParams({
        task_id: String(taskId || state.currentTaskId || ""),
        run_id: String(ref.run_id == null ? "" : ref.run_id),
        seq: String(ref.seq),
      });
      const data = await jsonFetch(`/api/v1/tasks/tool-output?${params.toString()}`);
      pre.textContent = redactDataUris(String(data && data.output != null ? data.output : ""));
      button.remove();
    } catch (error) {
      button.disabled = false;
      button.textContent = `${toolOutputRefLabel(ref)} · 重试`;
      writePreviewStatus(`读取完整工具输出失败：${String(error.message || error)}`);
    }
  });
  details.appendChild(button);

  return details;
}

// The preview block already shows the output; the remaining keys of a compact
// result (status, error, …) still deserve the normal JSON view.
function toolResultWithoutOutputRef(result) {
  const rest = {};
  for (const [key, value] of Object.entries(result)) {
    if (key === "output_ref" || key === "output_preview") continue;
    rest[key] = value;
  }
  return rest;
}

function appendToolResultNodes(detailsHost, result, taskId, { skipEmpty = true, images = null } = {}) {
  const toolImages = toolImagesOf(result, images);
  if (toolImages.length) {
    const previous = detailsHost.querySelector(":scope > .tool-images");
    if (previous) previous.remove();
    detailsHost.appendChild(createToolImagesBlock(toolImages));
    result = stripToolImageData(result);
  }
  const ref = toolOutputRefOf(result);
  if (ref) {
    detailsHost.appendChild(createToolOutputRefBlock(result, ref, taskId));
    const rest = toolResultWithoutOutputRef(result);
    if (Object.keys(rest).length) {
      detailsHost.appendChild(createJsonDetails("Result", rest));
    }
    return;
  }
  if (!skipEmpty || (result && (typeof result !== "object" || Object.keys(result).length))) {
    detailsHost.appendChild(createJsonDetails("Result", result));
  }
}

function createToolCard(toolName, status, args = null, result = null, timestamp = "", images = null, queue = null) {
  // Captured at build time: an on-demand output fetch must target the task this
  // card was rendered for, even if the user switches tasks afterwards.
  const cardTaskId = String(state.currentTaskId || "");
  const card = document.createElement("div");
  card.className = "tool-card-block";

  const header = document.createElement("div");
  header.className = "tool-card-header";
  header.innerHTML = `
    <span class="tool-name">${toolName || "unknown"}</span>
    <span class="tool-status ${status || "running"}">${status || "running"}</span>
  `;
  card.appendChild(header);

  if (timestamp) {
    const meta = document.createElement("div");
    meta.className = "tool-meta";
    meta.textContent = timestamp;
    card.appendChild(meta);
  }

  // 4.4.1：并发闸门排队信息（tool.finished / history 的 queue 字段）
  const queueMeta = document.createElement("div");
  queueMeta.className = "tool-queue-meta";
  queueMeta.hidden = true;
  card.appendChild(queueMeta);
  const renderQueue = (info) => {
    if (!info || typeof info !== "object") return;
    const wait = Number(info.wait_sec || 0);
    const parts = [];
    if (info.timed_out) parts.push(`排队超时 ${wait.toFixed(1)}s`);
    else if (info.cancelled) parts.push("排队中被取消");
    else parts.push(wait >= 0.5 ? `排队 ${wait.toFixed(1)}s` : "未排队");
    if (info.pool) parts.push(`池 ${info.pool}${info.max ? ` (上限 ${info.max})` : ""}`);
    queueMeta.textContent = parts.join(" · ");
    queueMeta.hidden = false;
  };
  renderQueue(queue);

  const detailsHost = document.createElement("div");
  detailsHost.className = "tool-details-host";
  card.appendChild(detailsHost);

  if (args && typeof args === "object" && Object.keys(args).length) {
    detailsHost.appendChild(createJsonDetails("Arguments", args));
  }
  appendToolResultNodes(detailsHost, result, cardTaskId, { images });

  return {
    card,
    detailsHost,
    updateStatus(nextStatus) {
      const statusNode = header.querySelector(".tool-status");
      if (statusNode) {
        statusNode.textContent = nextStatus || "done";
        statusNode.className = `tool-status ${nextStatus || "done"}`;
      }
    },
    updateResult(nextResult, nextImages = null, nextQueue = null) {
      appendToolResultNodes(detailsHost, nextResult, cardTaskId, { skipEmpty: false, images: nextImages });
      renderQueue(nextQueue);
    },
  };
}

function normalizeHistoryProcess(process = []) {
  const normalized = [];
  const pendingByName = new Map();

  for (const item of process) {
    if (item.type !== "tool") {
      normalized.push(item);
      continue;
    }

    const toolName = String(item.name || "unknown");
    // 4.1.1: pair start/end per (instance, tool) so parallel same-name tools
    // from different sub-agent instances never cross-match.
    const pairKey = `${String(item.agent_id || "")}|${toolName}`;
    if (String(item.status || "") === "running") {
      const entry = {
        type: "tool",
        name: toolName,
        status: "running",
        arguments: item.arguments || {},
        result: null,
        timestamp: item.timestamp || "",
        agent_name: item.agent_name || "",
        agent_id: item.agent_id || "",
      };
      const queue = pendingByName.get(pairKey) || [];
      queue.push(entry);
      pendingByName.set(pairKey, queue);
      normalized.push(entry);
      continue;
    }

    const queue = pendingByName.get(pairKey) || [];
    const pending = queue.shift();
    if (pending) {
      pending.status = String(item.status || "done");
      pending.result = foldToolOutputRef(item, item.result || {});
      pending.images = Array.isArray(item.images) ? item.images : null;
      pending.queue = item.queue && typeof item.queue === "object" ? item.queue : null;
      pending.completed_at = item.timestamp || "";
      if (!queue.length) pendingByName.delete(pairKey);
      else pendingByName.set(pairKey, queue);
    } else {
      normalized.push({
        type: "tool",
        name: toolName,
        status: String(item.status || "done"),
        arguments: item.arguments || {},
        result: foldToolOutputRef(item, item.result || {}),
        images: Array.isArray(item.images) ? item.images : null,
        queue: item.queue && typeof item.queue === "object" ? item.queue : null,
        timestamp: item.timestamp || "",
        agent_name: item.agent_name || "",
        agent_id: item.agent_id || "",
      });
    }
  }

  return normalized;
}

function createFeedbackBar(runId, existing) {
  const wrap = document.createElement("div");
  wrap.className = "feedback-bar";

  const summary = document.createElement("div");
  summary.className = "feedback-existing";
  const renderExisting = (record) => {
    if (!record) {
      summary.textContent = "";
      summary.style.display = "none";
      return;
    }
    summary.style.display = "";
    const fileCount = (record.attachments || []).length;
    const commentPart = record.comment ? ` · ${record.comment.slice(0, 80)}` : "";
    const filePart = fileCount ? ` · 附件×${fileCount}` : "";
    summary.textContent = `已评：${verdictLabel(record.verdict)} · ${record.created_at || ""}${commentPart}${filePart}`;
    summary.dataset.verdict = record.verdict || "";
  };
  renderExisting(existing);
  wrap.appendChild(summary);

  const details = document.createElement("details");
  details.className = "feedback-form";
  const summaryEl = document.createElement("summary");
  summaryEl.textContent = existing ? "重新评价" : "评价本轮";
  details.appendChild(summaryEl);

  const body = document.createElement("div");
  body.className = "feedback-body";

  let selected = "";
  const hint = document.createElement("div");
  hint.className = "feedback-hint";

  const btnRow = document.createElement("div");
  btnRow.className = "feedback-verdicts";
  const buttons = FEEDBACK_VERDICTS.map((v) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `feedback-verdict v-${v.value}`;
    b.textContent = v.label;
    b.addEventListener("click", () => {
      selected = v.value;
      buttons.forEach((x) => x.classList.remove("selected"));
      b.classList.add("selected");
      hint.textContent = v.value === "approved" ? "" : "该评价需填写意见或添加附件";
    });
    btnRow.appendChild(b);
    return b;
  });
  body.appendChild(btnRow);

  const textarea = document.createElement("textarea");
  textarea.className = "feedback-comment";
  textarea.rows = 2;
  textarea.placeholder = "具体意见…（文件可直接拖入此区域作为附件）";
  body.appendChild(textarea);

  let pendingFiles = [];
  const fileRow = document.createElement("div");
  fileRow.className = "feedback-files";
  const fileInput = document.createElement("input");
  fileInput.type = "file";
  fileInput.multiple = true;
  const fileList = document.createElement("div");
  fileList.className = "feedback-file-list";
  const renderFiles = () => {
    fileList.innerHTML = "";
    pendingFiles.forEach((f, i) => {
      const chip = document.createElement("span");
      chip.className = "feedback-file-chip";
      chip.textContent = `${f.name} ✕`;
      chip.title = "点击移除";
      chip.addEventListener("click", () => {
        pendingFiles.splice(i, 1);
        renderFiles();
      });
      fileList.appendChild(chip);
    });
  };
  fileInput.addEventListener("change", () => {
    pendingFiles.push(...Array.from(fileInput.files || []));
    fileInput.value = "";
    renderFiles();
  });
  fileRow.appendChild(fileInput);
  fileRow.appendChild(fileList);
  body.appendChild(fileRow);

  body.addEventListener("dragover", (event) => {
    event.preventDefault();
    body.classList.add("dragging");
  });
  body.addEventListener("dragleave", () => body.classList.remove("dragging"));
  body.addEventListener("drop", (event) => {
    event.preventDefault();
    body.classList.remove("dragging");
    if (event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files.length) {
      pendingFiles.push(...Array.from(event.dataTransfer.files));
      renderFiles();
    }
  });

  const submit = document.createElement("button");
  submit.type = "button";
  submit.className = "feedback-submit";
  submit.textContent = "提交评价";
  submit.addEventListener("click", async () => {
    if (!selected) {
      hint.textContent = "请先选择评价结论";
      return;
    }
    const comment = textarea.value.trim();
    if (selected !== "approved" && !comment && !pendingFiles.length) {
      hint.textContent = "部分认可 / 修改后可采纳 / 不认可 必须填写意见或添加附件";
      return;
    }
    submit.disabled = true;
    submit.textContent = "提交中...";
    try {
      const form = new FormData();
      form.append("task_id", state.currentTaskId);
      form.append("run_id", runId || "");
      form.append("verdict", selected);
      form.append("comment", comment);
      for (const f of pendingFiles) {
        form.append("files", f, f.name);
      }
      const res = await fetch("/api/v1/tasks/feedback", { method: "POST", body: form });
      const payload = await parseResponsePayload(res);
      if (!res.ok) {
        throw new Error(errorMessageFromPayload(payload) || `HTTP ${res.status}`);
      }
      state.feedbackByRun[runId || ""] = payload.feedback;
      renderExisting(payload.feedback);
      summaryEl.textContent = "重新评价";
      details.open = false;
      textarea.value = "";
      pendingFiles = [];
      renderFiles();
      hint.textContent = "";
      prettyWrite(payload);
    } catch (error) {
      hint.textContent = `提交失败: ${String(error.message || error)}`;
    } finally {
      submit.disabled = false;
      submit.textContent = "提交评价";
    }
  });
  body.appendChild(submit);
  body.appendChild(hint);

  details.appendChild(body);
  wrap.appendChild(details);
  return wrap;
}

function renderAgentTurn(item, inputMessage = "") {
  const assistant = assistantBubble(item.agent_name ? `assistant · ${item.agent_name}` : "assistant");
  const altOutput = item.output_2
    || (item.process || []).find((p) => p && p.type === "final_output" && p.output_2)?.output_2
    || "";
  if (altOutput) {
    renderTwoVersions(assistant, item.text || "", altOutput, item.run_id);
  } else {
    setMarkdown(assistant.bodyNode, item.text || "");
    attachFinalOutputExport(assistant.wrapper, item.text || "", finalOutputExportName(item.run_id));
  }
  // R15: download links for this turn's declared files, at the end of the body.
  attachDownloadLinks(assistant.bodyNode, downloadPathsOf(item), state.currentTaskId);
  // R14: one decision badge per turn (never per version — both versions share them).
  assistant.wrapper.dataset.hcInputMessage = String(inputMessage || "");
  attachConfirmationBadge(assistant.wrapper, inputMessage);

  const normalized = normalizeHistoryProcess(item.process || []);
  // 4.1.1: group sub-agent instance entries under per-instance sections.
  // The first non-empty agent_id in the timeline is the main instance.
  const mainAgentId = String((normalized.find((p) => p && p.agent_id) || {}).agent_id || "");
  const subGroups = {};
  const targetFor = (processItem) => {
    const aid = String(processItem?.agent_id || "");
    if (!aid || aid === mainAgentId) return assistant.processNode;
    if (!subGroups[aid]) {
      const label = `\u21b3 \u5b50\u667a\u80fd\u4f53 \u00b7 ${processItem.agent_name || "agent"} \u00b7 #${aid.slice(-6)}`;
      // 默认收起；用户展开过的组在流式重渲后保持展开（按实例记忆）
      state.subGroupOpen = state.subGroupOpen || {};
      const group = createProcessCard(label, "", { className: "subagent-card", open: !!state.subGroupOpen[aid] });
      group.details.dataset.agentId = aid;
      group.details.addEventListener("toggle", () => { state.subGroupOpen[aid] = group.details.open; });
      group.details.style.marginLeft = "14px";
      group.details.style.borderLeft = "3px solid #b8c4e0";
      group.details.style.paddingLeft = "8px";
      group.details.style.background = "rgba(184,196,224,0.08)";
      assistant.processNode.appendChild(group.details);
      subGroups[aid] = group;
    }
    return subGroups[aid].body;
  };
  for (const processItem of normalized) {
    if (processItem.type === "thinking" && processItem.content) {
      const card = createProcessCard(
        processItem.agent_name ? `Thinking · ${processItem.agent_name}` : "Thinking",
        processItem.content,
        { className: "thinking-card" },
      );
      targetFor(processItem).appendChild(card.details);
      continue;
    }

    if (processItem.type === "assistant_reasoning" && processItem.content) {
      const card = createProcessCard(
        processItem.agent_name ? `Reasoning · ${processItem.agent_name}` : "Reasoning",
        processItem.content,
        { className: "reasoning-card" },
      );
      targetFor(processItem).appendChild(card.details);
      continue;
    }

    if (processItem.type === "assistant_content" && processItem.content) {
      const card = createProcessCard(
        processItem.agent_name ? `Content · ${processItem.agent_name}` : "Content",
        processItem.content,
        { className: "content-card" },
      );
      targetFor(processItem).appendChild(card.details);
      continue;
    }

    if (processItem.type === "final_output" && processItem.content) {
      const card = createProcessCard(
        processItem.agent_name ? `Final Output · ${processItem.agent_name}` : "Final Output",
        processItem.content,
        { className: "content-card", open: true, markdown: true },
      );
      targetFor(processItem).appendChild(card.details);
      continue;
    }

    if (processItem.type === "tool") {
      const tool = createToolCard(
        processItem.name,
        processItem.status,
        processItem.arguments || {},
        processItem.result || {},
        processItem.completed_at || processItem.timestamp || "",
        processItem.images || null,
        processItem.queue || null,
      );
      targetFor(processItem).appendChild(tool.card);
    }
  }

  const runId = String(item.run_id || "");
  assistant.wrapper.appendChild(createFeedbackBar(runId, state.feedbackByRun[runId]));

  return assistant.wrapper;
}

function assistantBubble(headerText = "assistant") {
  const { wrapper, bodyNode } = messageBubble("assistant", headerText, "");
  const processNode = document.createElement("div");
  processNode.className = "turn-process";
  wrapper.insertBefore(processNode, bodyNode);
  bodyNode.classList.add("final-output");

  return {
    wrapper,
    bodyNode,
    processNode,
    content: "",
    toolCards: [],
    activeSegments: {
      thinking: null,
      reasoning: null,
      content: null,
    },
  };
}

function resetStreamRenderState() {
  state.pendingAssistantNode = null;
  state.activeAssistantNodes = {};
  state.lastStreamAgentName = "";
}

function streamEventAgentName(event) {
  return String(event?.agent_name || event?.agent || "").trim();
}

function ensureAssistantForEvent(event) {
  return ensurePendingAssistant();
}

// 4.1.5-console: structured live stream — every parallel sub-agent gets its own
// collapsed <details> inside the turn; expand to watch that agent live.
function scrollMessagesIfNearBottom() {
  const m = $("messages");
  if (!m) return;
  const nearBottom = m.scrollHeight - m.scrollTop - m.clientHeight < 120;
  if (nearBottom) m.scrollTop = m.scrollHeight;
}

function structEventAgentId(event) {
  return String(event?.agent_id || "").trim();
}

function structIsSub(aid) {
  return !!(aid && state.streamMainAgentId && aid !== state.streamMainAgentId);
}

function structRootFor(assistant, event) {
  const aid = structEventAgentId(event);
  if (!structIsSub(aid)) return assistant.processNode;
  assistant.subGroups = assistant.subGroups || {};
  let g = assistant.subGroups[aid];
  if (!g) {
    state.subGroupOpen = state.subGroupOpen || {};
    const name = streamEventAgentName(event) || "agent";
    const label = `\u21b3 \u5b50\u667a\u80fd\u4f53 \u00b7 ${name} \u00b7 #${aid.slice(-6)}`;
    const group = createProcessCard(label, "", { className: "subagent-card", open: !!state.subGroupOpen[aid] });
    group.details.dataset.agentId = aid;
    group.details.addEventListener("toggle", () => { state.subGroupOpen[aid] = group.details.open; });
    assistant.processNode.appendChild(group.details);
    g = assistant.subGroups[aid] = group;
  }
  return g.body;
}

// 每类流式段最近一次 .delta 的实例 id。4.3.7 之前的镜像里，由 run.llm.end 派生的
// .done 事件不带 agent_id，而对应的 .delta 带 —— done 会去清另一个 key，delta 累积
// 的那段永远清不掉，下一轮继续往上拼（「新块开头永远是上一轮的内容」）。缺 id 时按
// 「最近一次同类 delta 的实例」回退：子 agent 的 done 才不会被误归到主 agent。
const lastDeltaAgentByKind = {};

function structSegKey(kind, event) {
  let aid = structEventAgentId(event);
  const type = String(event?.type || "");
  if (aid) {
    if (type.endsWith(".delta")) lastDeltaAgentByKind[kind] = aid;
  } else {
    aid = String(lastDeltaAgentByKind[kind] || state.streamMainAgentId || "");
  }
  return aid ? `${kind}:${aid}` : kind;
}

function segmentTitle(baseTitle, event) {
  const agentName = streamEventAgentName(event);
  return agentName ? `${baseTitle} · ${agentName}` : baseTitle;
}

function startOrderedSegment(assistant, key, title, className, rootNode) {
  const card = createProcessCard(title, "", { open: true, className });
  (rootNode || assistant.processNode).appendChild(card.details);
  assistant.activeSegments[key] = card;
  return card;
}

function ensureOrderedSegment(assistant, key, title, className, rootNode) {
  if (!assistant.activeSegments[key]) {
    return startOrderedSegment(assistant, key, title, className, rootNode);
  }
  return assistant.activeSegments[key];
}

function resetOrderedSegment(assistant, key) {
  assistant.activeSegments[key] = null;
}

function appendMessageNode(node) {
  $("messages").appendChild(node);
  $("messages").scrollTop = $("messages").scrollHeight;
}

function insertQueuedUserMessage(text) {
  const { wrapper } = messageBubble("user", "user", text || "");
  const container = $("messages");
  const assistantWrapper = state.pendingAssistantNode?.wrapper;
  if (assistantWrapper && assistantWrapper.parentNode === container) {
    container.insertBefore(wrapper, assistantWrapper);
  } else {
    container.appendChild(wrapper);
  }
  container.dataset.taskId = state.currentTaskId || "";
  container.scrollTop = container.scrollHeight;
}

let currentAgentBubble = null;
let currentAgentContentDiv = null;
let currentStreamText = "";
let currentAgentBubbleOwner = "";
let thinkingAgentOwner = "";
let reasoningOwner = "";
// 4.1.5-console: live per-sub-agent collapsed groups. Every parallel sub-agent
// streams into its own <details> (collapsed by default); expand to watch live.
let subLiveGroups = {};

function isSubAgentId(agentId) {
  const aid = String(agentId || "");
  return !!(aid && state.streamMainAgentId && aid !== state.streamMainAgentId);
}

function streamRootFor(agentId, agentName = "") {
  const aid = String(agentId || "");
  if (!isSubAgentId(aid)) return $("messages");
  let g = subLiveGroups[aid];
  if (!g) {
    const details = document.createElement("details");
    details.className = "subagent-live";
    details.open = false;
    details.style.margin = "6px 0 6px 14px";
    details.style.borderLeft = "3px solid #b8c4e0";
    details.style.paddingLeft = "8px";
    details.style.background = "rgba(184,196,224,0.08)";
    details.style.borderRadius = "6px";
    const summary = document.createElement("summary");
    summary.style.cursor = "pointer";
    summary.style.padding = "6px 4px";
    summary.style.fontSize = "13px";
    summary.style.color = "#5b6b93";
    const label = `\u21b3 \u5b50\u667a\u80fd\u4f53 \u00b7 ${agentName || "agent"} \u00b7 #${aid.slice(-6)}`;
    summary.dataset.label = label;
    summary.textContent = `${label} \u00b7 running...`;
    const body = document.createElement("div");
    body.className = "subagent-live-body";
    details.appendChild(summary);
    details.appendChild(body);
    $("messages").appendChild(details);
    g = subLiveGroups[aid] = { details, summary, body };
    $("messages").scrollTop = $("messages").scrollHeight;
  }
  return g.body;
}

function markSubAgentDone(agentId, status = "done") {
  const g = subLiveGroups[String(agentId || "")];
  if (!g) return;
  g.summary.textContent = `${g.summary.dataset.label} \u00b7 ${status}`;
  if (status === "error") g.summary.style.color = "#b3423a";
}
let reasoningBubble = null;
let reasoningContentDiv = null;
let reasoningStreamText = "";
let thinkingAgentBubble = null;
let thinkingAgentContentDiv = null;
let thinkingAgentText = "";

function resetMacLikeStreamState() {
  state.streamMainAgentId = "";
  currentAgentBubbleOwner = "";
  thinkingAgentOwner = "";
  reasoningOwner = "";
  subLiveGroups = {};
  currentAgentBubble = null;
  currentAgentContentDiv = null;
  currentStreamText = "";
  reasoningBubble = null;
  reasoningContentDiv = null;
  reasoningStreamText = "";
  thinkingAgentBubble = null;
  thinkingAgentContentDiv = null;
  thinkingAgentText = "";
}

function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

// H4/H11: single-line notes inside the transcript (queue position, stream
// reconnect). Keyed so a repeated event updates the same line in place instead
// of stacking copies; loadHistory() clears them with the rest of the container.
function setTranscriptStatus(key, text) {
  const container = $("messages");
  if (!container) return null;
  let node = container.querySelector(`[data-status-key="${key}"]`);
  if (!node) {
    node = document.createElement("div");
    node.className = "message-system stream-status";
    node.dataset.statusKey = key;
    container.appendChild(node);
  }
  node.textContent = `— ${text} —`;
  container.scrollTop = container.scrollHeight;
  return node;
}

function clearTranscriptStatus(key) {
  const container = $("messages");
  if (!container) return;
  const node = container.querySelector(`[data-status-key="${key}"]`);
  if (node) node.remove();
}

function addSystemMessage(text) {
  const div = document.createElement("div");
  div.className = "message-system";
  div.textContent = `— ${text} —`;
  $("messages").appendChild(div);
}

function addErrorMessage(text) {
  const div = document.createElement("div");
  div.className = "message-error";
  div.innerHTML = `<div class="error-card">${escapeHtml(text)}</div>`;
  $("messages").appendChild(div);
}

function addToolMessageMac(toolName, status, args, agentName = "", agentId = "") {
  const div = document.createElement("div");
  div.className = "message-tool";
  div.dataset.agentId = String(agentId || "");
  let argsHtml = "";
  if (args && typeof args === "object" && Object.keys(args).length > 0) {
    const argsStr = JSON.stringify(args, null, 2);
    argsHtml = `
      <details class="tool-details">
        <summary class="tool-params-toggle">Parameters</summary>
        <pre class="tool-params-content">${escapeHtml(argsStr)}</pre>
      </details>
    `;
  }
  div.innerHTML = `
    <div class="tool-card tool-${status}">
      <div class="tool-header">
        <span class="tool-dot tool-dot-${status}"></span>
        <span class="tool-name">${escapeHtml(toolName)}</span>
        ${agentId && state.streamMainAgentId && agentId !== state.streamMainAgentId
          ? `<span class="tool-agent-tag" style="font-size:11px;color:#7a86a8;">\u21b3 ${escapeHtml(agentName || "agent")} \u00b7 #${escapeHtml(String(agentId).slice(-6))}</span>`
          : ""}
        <span class="tool-status-label">${status === "running" ? "running..." : escapeHtml(status)}</span>
      </div>
      ${argsHtml}
      <div class="tool-result-area" style="display:none;"></div>
    </div>
  `;
  div.dataset.toolMessage = "true";
  streamRootFor(agentId, agentName).appendChild(div);
  if (!isSubAgentId(agentId)) $("messages").scrollTop = $("messages").scrollHeight;
}

function updateLastToolStatusMac(status, resultPayload, agentId = "") {
  const tools = $("messages").querySelectorAll("[data-tool-message]");
  if (!tools.length) return;
  // 4.1.1: with parallel sub-agents, match the newest RUNNING card of the same
  // instance instead of blindly taking the last card.
  let last = null;
  const want = String(agentId || "");
  for (let i = tools.length - 1; i >= 0; i -= 1) {
    const el = tools[i];
    const running = el.querySelector(".tool-card.tool-running");
    if (!running) continue;
    if (String(el.dataset.agentId || "") === want) { last = el; break; }
    if (last === null) last = el; // fallback: newest running card
  }
  if (!last) last = tools[tools.length - 1];
  const card = last.querySelector(".tool-card");
  const dot = last.querySelector(".tool-dot");
  const label = last.querySelector(".tool-status-label");
  if (card) card.className = `tool-card tool-${status}`;
  if (dot) dot.className = `tool-dot tool-dot-${status}`;
  if (label) label.textContent = status === "success" ? "done" : status;
  if (resultPayload !== undefined) {
    const resultArea = last.querySelector(".tool-result-area");
    if (resultArea) {
      resultArea.style.display = "block";
      resultArea.innerHTML = `
        <details class="tool-details">
          <summary class="tool-params-toggle">Result</summary>
          <pre class="tool-params-content">${escapeHtml(prettyJson(resultPayload))}</pre>
        </details>
      `;
    }
  }
}

function streamTokenMac(text, agentName = "Agent", agentId = "") {
  if (currentAgentBubble && String(agentId || "") !== currentAgentBubbleOwner) {
    finalizeCurrentStreamMac();
  }
  if (!currentAgentBubble) {
    currentAgentBubbleOwner = String(agentId || "");
    const wrapper = document.createElement("div");
    wrapper.className = "message-agent";
    wrapper.dataset.agentName = agentName;

    const bubble = document.createElement("div");
    bubble.className = "bubble";

    const label = document.createElement("div");
    label.className = "agent-label";
    label.textContent = (agentId && state.streamMainAgentId && agentId !== state.streamMainAgentId)
      ? `\u21b3 ${agentName} \u00b7 #${String(agentId).slice(-6)}`
      : agentName;

    const content = document.createElement("div");
    content.className = "agent-content";

    bubble.appendChild(label);
    bubble.appendChild(content);
    wrapper.appendChild(bubble);
    streamRootFor(agentId, agentName).appendChild(wrapper);

    currentAgentBubble = wrapper;
    currentAgentContentDiv = content;
    currentStreamText = "";
  }

  currentStreamText += text;
  currentAgentContentDiv.innerText = currentStreamText;
  $("messages").scrollTop = $("messages").scrollHeight;
}

function finalizeCurrentStreamMac() {
  currentAgentBubble = null;
  currentAgentContentDiv = null;
  currentStreamText = "";
}

function streamReasoningTokenMac(text, agentId = "") {
  if (reasoningBubble && String(agentId || "") !== reasoningOwner) {
    finalizeReasoningStreamMac();
  }
  if (!reasoningBubble) {
    reasoningOwner = String(agentId || "");
    const div = document.createElement("div");
    div.className = "message-thinking";
    div.innerHTML = `
      <div class="thinking-card">
        <details open>
          <summary>Model Reasoning</summary>
          <div class="thinking-content"></div>
        </details>
      </div>
    `;
    streamRootFor(agentId).appendChild(div);
    reasoningBubble = div;
    reasoningContentDiv = div.querySelector(".thinking-content");
    reasoningStreamText = "";
  }

  reasoningStreamText += text;
  reasoningContentDiv.innerText = reasoningStreamText;
  $("messages").scrollTop = $("messages").scrollHeight;
}

function finalizeReasoningStreamMac() {
  if (reasoningBubble) {
    const details = reasoningBubble.querySelector("details");
    if (details) {
      details.open = false;
      const summary = details.querySelector("summary");
      if (summary) summary.textContent = "Model Reasoning (click to expand)";
    }
  }
  reasoningBubble = null;
  reasoningContentDiv = null;
  reasoningStreamText = "";
}

function startThinkingAgentStreamMac(agentName = "Thinking Agent", agentId = "") {
  thinkingAgentOwner = String(agentId || "");
  const subTag = (agentId && state.streamMainAgentId && agentId !== state.streamMainAgentId)
    ? ` \u00b7 #${String(agentId).slice(-6)}`
    : "";
  const div = document.createElement("div");
  div.className = "message-thinking";
  div.innerHTML = `
    <div class="thinking-card">
      <details open>
        <summary>${escapeHtml(agentName)}${subTag} Thinking...</summary>
        <div class="thinking-content"></div>
      </details>
    </div>
  `;
  streamRootFor(agentId, agentName).appendChild(div);
  thinkingAgentBubble = div;
  thinkingAgentContentDiv = div.querySelector(".thinking-content");
  thinkingAgentText = "";
  $("messages").scrollTop = $("messages").scrollHeight;
}

function streamThinkingAgentTokenMac(text, agentName = "Thinking Agent", agentId = "") {
  if (thinkingAgentBubble && String(agentId || "") !== thinkingAgentOwner) {
    finalizeThinkingAgentStreamMac();
  }
  if (!thinkingAgentBubble) startThinkingAgentStreamMac(agentName, agentId);
  thinkingAgentText += text;
  thinkingAgentContentDiv.innerText = thinkingAgentText;
  $("messages").scrollTop = $("messages").scrollHeight;
}

function finalizeThinkingAgentStreamMac() {
  if (thinkingAgentBubble) {
    const details = thinkingAgentBubble.querySelector("details");
    if (details) {
      details.open = false;
      const summary = details.querySelector("summary");
      if (summary) summary.textContent = "Thinking (click to expand)";
    }
  }
  thinkingAgentBubble = null;
  thinkingAgentContentDiv = null;
  thinkingAgentText = "";
}

function showTypingIndicator() {
  if ($("messages").querySelector(".typing-indicator")) return;
  const div = document.createElement("div");
  div.className = "typing-indicator";
  div.innerHTML = '<div class="typing-dot"></div><div class="typing-dot"></div><div class="typing-dot"></div>';
  $("messages").appendChild(div);
}

function removeTypingIndicator() {
  const indicator = $("messages").querySelector(".typing-indicator");
  if (indicator) indicator.remove();
}

function renderTaskList() {
  const list = $("taskList");
  list.innerHTML = "";
  // H11: build every card off-DOM and attach once — the old per-card append
  // forced one reflow per task, which is what made long lists crawl.
  const fragment = document.createDocumentFragment();
  // 4.6.0 (N03): tasks in shared workspaces are grouped under their workspace (groups keep the list's
  // updated_at order; tasks outside any workspace come last). No task in a workspace = the old flat list.
  if (state.tasks.some((task) => task.workspace_id)) {
    const groups = new Map();
    for (const task of state.tasks) {
      const key = task.workspace_id || "";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(task);
    }
    const keys = [...groups.keys()].filter((key) => key);
    if (groups.has("")) keys.push("");
    for (const key of keys) {
      fragment.appendChild(createTaskGroupHead(key, groups.get(key).length));
      for (const task of groups.get(key)) {
        fragment.appendChild(createTaskCard(task));
      }
    }
  } else {
    for (const task of state.tasks) {
      fragment.appendChild(createTaskCard(task));
    }
  }
  list.appendChild(fragment);
  renderTaskListFooter();
}

function workspaceLabel(workspaceId) {
  const ws = state.workspacesById[workspaceId];
  if (ws && ws.name) return ws.name;
  return workspaceId ? `${String(workspaceId).slice(0, 8)}…` : "";
}

function createTaskGroupHead(workspaceId, count) {
  const head = document.createElement("div");
  head.className = "task-group-head";
  const name = document.createElement("span");
  name.className = "ws-name";
  if (workspaceId) {
    const ws = state.workspacesById[workspaceId];
    name.textContent = `工作区 · ${workspaceLabel(workspaceId)}`;
    name.title = `${workspaceId}${ws && ws.path ? `\n${ws.path}` : ""}`;
    if (ws && ws.concurrency === "file_lock") name.textContent += "（多会话并行）";
  } else {
    name.textContent = "未在工作区";
  }
  const size = document.createElement("span");
  size.className = "ws-count";
  size.textContent = String(count);
  head.append(name, size);
  return head;
}

async function refreshWorkspaces() {
  try {
    const data = await jsonFetch("/api/v1/workspaces?limit=200");
    state.workspaces = data.items || [];
    state.workspacesSupported = true;
  } catch (error) {
    state.workspaces = [];
    state.workspacesSupported = false;
  }
  state.workspacesById = {};
  for (const ws of state.workspaces) {
    state.workspacesById[ws.workspace_id] = ws;
  }
  renderWorkspaceChoice();
}

function renderWorkspaceChoice(selected = null) {
  const panel = $("newTaskWorkspacePanel");
  const select = $("newTaskWorkspace");
  if (!panel || !select) return;
  panel.hidden = !state.workspacesSupported;
  const wanted = selected === null ? select.value : selected;
  select.innerHTML = "";
  const addOption = (parent, value, label) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    parent.appendChild(option);
  };
  addOption(select, "", "不用工作区");
  addOption(select, WORKSPACE_CREATE, "新建工作区…");
  if (state.workspaces.length) {
    const group = document.createElement("optgroup");
    group.label = "已有工作区";
    for (const ws of state.workspaces) {
      const parallel = ws.concurrency === "file_lock" ? "（多会话并行）" : "";
      addOption(group, ws.workspace_id, `${ws.name || ws.workspace_id}${parallel}`);
    }
    select.appendChild(group);
  }
  select.value = [...select.options].some((option) => option.value === wanted) ? wanted : "";
  syncWorkspaceChoice();
}

function syncWorkspaceChoice() {
  const fields = $("newWorkspaceFields");
  if (fields) fields.hidden = $("newTaskWorkspace").value !== WORKSPACE_CREATE;
}

// The `workspace` field of the ensure call for a new task: an existing workspace_id, the create form
// {"create": {...}}, or undefined (no workspace — the request is then exactly what it was before 4.6.0).
function workspaceArgForNewTask() {
  if (!state.workspacesSupported) return undefined;
  const value = $("newTaskWorkspace").value;
  if (!value) return undefined;
  if (value !== WORKSPACE_CREATE) return value;
  const create = {};
  const name = $("newWorkspaceName").value.trim();
  if (name) create.name = name;
  if ($("newWorkspaceParallel").checked) create.concurrency = "file_lock";
  return { create };
}

// H11: the server's next_cursor decides whether there is a next page. An old
// server sends no such field, readNextCursor() returns null, and no button is
// rendered — the list then behaves exactly as it did before.
function readNextCursor(data) {
  if (!data || typeof data !== "object") return null;
  if (!Object.prototype.hasOwnProperty.call(data, "next_cursor")) return null;
  const cursor = data.next_cursor;
  if (cursor === null || cursor === undefined || cursor === "") return null;
  return String(cursor);
}

function renderTaskListFooter() {
  const list = $("taskList");
  if (!list) return;
  const prior = list.querySelector(":scope > .task-load-more");
  if (prior) prior.remove();
  if (!state.taskCursor) return;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary task-load-more";
  button.textContent = "加载更多";
  button.addEventListener("click", () => loadMoreTasks(button));
  list.appendChild(button);
}

// Appends the next page to the loaded prefix: state.tasks grows, and only the
// new cards are built and inserted ahead of the button.
async function loadMoreTasks(button) {
  const cursor = state.taskCursor;
  if (!cursor) return;
  button.disabled = true;
  button.textContent = "加载中…";
  try {
    const data = await jsonFetch(
      `/api/v1/tasks?limit=${TASK_PAGE_SIZE}&cursor=${encodeURIComponent(cursor)}`,
    );
    const items = data.items || [];
    state.tasks = state.tasks.concat(items);
    state.taskCursor = readNextCursor(data);
    // 4.6.0: a later page can add tasks to a workspace group shown above, so rebuild the whole list.
    renderTaskList();
  } catch (error) {
    button.disabled = false;
    button.textContent = "加载更多（重试）";
    writePreviewStatus(`加载更多会话失败：${String(error.message || error)}`);
  }
}

// 4.6.5: a name typed into Task ID (Ensure / Send on a new name) is current before the field holds the full id the list uses.
function isCurrentTask(task) {
  const current = state.currentTaskId;
  if (!current) return false;
  if (task.task_id === current) return true;
  return !/[\\/]/.test(current) && taskBaseName(task.task_id) === current;
}

function createTaskCard(task) {
  const card = document.createElement("button");
  const classes = ["task-card"];
  if (isCurrentTask(task)) classes.push("active");
  if (task.running) classes.push("running");
  card.className = classes.join(" ");
  card.type = "button";
  card.innerHTML = `
    <span class="task-delete" title="删除该会话及其全部关联文件" role="button" aria-label="delete">🗑</span>
    <div class="title">${forkBadge(task)}${formatTaskLabel(task)}</div>
    <div class="meta mono" title="${task.task_id}">${taskBaseName(task.task_id)}</div>
    <div class="meta">${task.agent_system || ""} / ${task.agent_name || ""}</div>
    <div class="meta">${task.user_id != null ? `用户 ${escapeHtml(String(task.user_id))} · ` : ""}${task.updated_at || ""}</div>
  `;
  const delBtn = card.querySelector(".task-delete");
  if (delBtn) {
    delBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      event.preventDefault();
      deleteTaskById(task);
    });
  }
  card.addEventListener("click", async () => {
    const stayingOnLiveStream = state.streaming && state.streamTaskId === task.task_id;
    if (state.streamController && !stayingOnLiveStream) {
      detachStream();
    }
    setCurrentTask(task.task_id);
    if (task.agent_system) {
      if (state.lastAgentSystem && state.lastAgentSystem !== task.agent_system) {
        persistCurrentSystemSettings(state.lastAgentSystem);
      }
      $("agentSystem").value = task.agent_system;
      state.lastAgentSystem = task.agent_system;
      writeLastAgentSystem(task.agent_system);
      await refreshAgents();
      if (task.agent_name) {
        $("agentName").value = task.agent_name;
        writeLastAgentName(task.agent_system, task.agent_name);
        syncAgentSelectTitles();
      }
      loadCurrentSystemSettings();
    }
    const info = await loadHistory();
    // Snapshot rendered; if the task has a live run, replay it from the
    // start and keep following (live-stream replays + follows by task).
    if (info && info.live_run_id && !stayingOnLiveStream) {
      attachLiveStream(info.live_run_id);
    }
  });
  return card;
}

async function deleteTaskById(task) {
  const label = task.conversation_name || taskBaseName(task.task_id);
  const ok = window.confirm(
    `确认删除会话「${label}」？\n\n将移除该会话，独立文件移入 user_root/.trash；共享工作区的文件保留。`,
  );
  if (!ok) return;
  try {
    const res = await fetch(`/api/v1/tasks?task_id=${encodeURIComponent(task.task_id)}`, { method: "DELETE" });
    const payload = await parseResponsePayload(res);
    if (!res.ok) throw new Error(errorMessageFromPayload(payload) || `HTTP ${res.status}`);
    prettyWrite(payload);
    if (state.currentTaskId === task.task_id) {
      detachStream();
      setCurrentTask("");
      $("messages").innerHTML = "";
      $("messages").dataset.taskId = "";
    }
    await refreshTasks();
    writePreviewStatus(`已删除会话「${label}」`);
  } catch (error) {
    prettyWrite({ status: "error", error: String(error.message || error) });
    window.alert(`删除失败: ${String(error.message || error)}`);
  }
}

function collectCurrentAdvancedSettings() {
  return {
    systemAddPath: $("systemAddPath").value.trim(),
    reasoningMode: $("reasoningMode").value,
    reasoningEffort: $("reasoningEffort").value,
    thinkingContextMode: $("thinkingContextMode")?.value || "",
    thinkingSteps: $("thinkingSteps").value.trim(),
    historyRecentItems: $("historyRecentItems").value.trim(),
    autoMode: $("autoMode").value,
    sandboxEnabled: $("sandboxEnabled").value,
    visibleSkills: $("visibleSkills").value,
    contextPlacement: $("contextPlacement").value,
    skillMaterialization: $("skillMaterialization").value,
    skillStoreDir: $("skillStoreDir").value.trim(),
    maxTurns: $("maxTurns").value.trim(),
    autoResumeAttempts: $("autoResumeAttempts").value.trim(),
    autoResumeDelaySec: $("autoResumeDelaySec").value.trim(),
    forceNew: $("forceNew").checked,
    includeTrace: $("includeTrace").checked,
    queueResumeIfNeeded: $("queueResumeIfNeeded").checked,
    sdkOverrides: $("sdkOverrides").value,
  };
}

function persistCurrentSystemSettings(agentSystem = $("agentSystem").value) {
  if (!agentSystem) return;
  writeSystemSettings(agentSystem, collectCurrentAdvancedSettings());
  writeLastAgentSystem(agentSystem);
}

function applyAdvancedSettings(settings) {
  const merged = {
    ...DEFAULT_ADVANCED_SETTINGS,
    ...(settings && typeof settings === "object" ? settings : {}),
  };
  $("systemAddPath").value = merged.systemAddPath || "";
  $("reasoningMode").value = merged.reasoningMode ?? "";
  $("reasoningEffort").value = merged.reasoningEffort || "";
  if ($("thinkingContextMode")) $("thinkingContextMode").value = merged.thinkingContextMode || "";
  $("thinkingSteps").value = merged.thinkingSteps || "30";
  $("historyRecentItems").value = merged.historyRecentItems || "0";
  $("autoMode").value = merged.autoMode || "";
  $("sandboxEnabled").value = merged.sandboxEnabled || "";
  $("visibleSkills").value = merged.visibleSkills || "";
  $("contextPlacement").value = merged.contextPlacement || "";
  $("skillMaterialization").value = merged.skillMaterialization || "";
  $("skillStoreDir").value = merged.skillStoreDir || "";
  $("maxTurns").value = merged.maxTurns || "";
  $("autoResumeAttempts").value = merged.autoResumeAttempts || "2";
  $("autoResumeDelaySec").value = merged.autoResumeDelaySec || "1";
  $("forceNew").checked = Boolean(merged.forceNew);
  $("includeTrace").checked = merged.includeTrace !== false;
  $("queueResumeIfNeeded").checked = merged.queueResumeIfNeeded !== false;
  $("sdkOverrides").value = merged.sdkOverrides || "{}";
  updateApiPreview();
}

function applyDefaultSystemAddPath(force = false) {
  const currentSystem = $("agentSystem").value;
  const candidate = state.systemAddSources[currentSystem] || "";
  if (force || !$("systemAddPath").value.trim()) {
    $("systemAddPath").value = candidate;
    updateApiPreview();
  }
}

function taskIdForPreview() {
  return $("taskId").value.trim() || state.currentTaskId || "<auto-generated-by-ui>";
}

function parseSdkOverridesFromForm({ persist = false } = {}) {
  const raw = $("sdkOverrides").value.trim();
  const base = raw ? JSON.parse(raw) : {};
  if ($("systemAddPath").value.trim()) {
    base.system_add_path = $("systemAddPath").value.trim();
  }
  if ($("thinkingSteps").value.trim()) {
    base.action_window_steps = parseInt($("thinkingSteps").value.trim(), 10);
  }
  if ($("historyRecentItems").value.trim()) {
    base.user_history_recent_items = parseInt($("historyRecentItems").value.trim(), 10);
  }
  if ($("maxTurns").value.trim()) {
    base.max_turns = parseInt($("maxTurns").value.trim(), 10);
  }
  if ($("autoMode").value) {
    base.auto_mode = $("autoMode").value === "true";
  }
  const visibleSkills = $("visibleSkills").value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  if (visibleSkills.length) {
    base.visible_skills = visibleSkills;
  }
  if (persist) {
    persistCurrentSystemSettings();
  }
  return base;
}

function optionalIntFromInput(id) {
  const raw = $(id).value.trim();
  if (!raw) return undefined;
  const value = parseInt(raw, 10);
  return Number.isFinite(value) ? value : undefined;
}

function optionalFloatFromInput(id) {
  const raw = $(id).value.trim();
  if (!raw) return undefined;
  const value = parseFloat(raw);
  return Number.isFinite(value) ? value : undefined;
}

function buildRuntimeOptionsFromForm() {
  const options = {
    // 留空 = 不下发该键，交回 agent yaml / app_config.json（详见 index.html 的说明）
    ...($("reasoningMode").value ? { reasoning_mode: $("reasoningMode").value } : {}),
    // 留空 = 不下发该键，交给 llm_config / 厂商默认
    ...($("reasoningEffort").value ? { reasoning_effort: $("reasoningEffort").value } : {}),
    force_new: $("forceNew").checked,
    include_trace: $("includeTrace").checked,
  };
  const autoResumeAttempts = optionalIntFromInput("autoResumeAttempts");
  const autoResumeDelaySec = optionalFloatFromInput("autoResumeDelaySec");
  if (autoResumeAttempts !== undefined) {
    options.auto_resume_attempts = Math.max(0, autoResumeAttempts);
  }
  if (autoResumeDelaySec !== undefined) {
    options.auto_resume_delay_sec = Math.max(0, autoResumeDelaySec);
  }
  return options;
}

function buildEnsurePayload(taskId = taskIdForPreview()) {
  return {
    task_id: taskId,
    conversation_name: taskId,
    agent_system: $("agentSystem").value,
    agent_name: $("agentName").value,
  };
}

function buildMessagePayload(taskId = taskIdForPreview(), { stream = false } = {}) {
  const payload = {
    task_id: taskId,
    agent_system: $("agentSystem").value,
    agent_name: $("agentName").value,
    new_message: $("messageInput").value,
    sdk_overrides: parseSdkOverridesFromForm({ persist: false }),
    ...buildRuntimeOptionsFromForm(),
  };
  if (stream) {
    payload.stream_mode = "normalized";
  }
  return payload;
}

function buildQueuePayload(taskId = taskIdForPreview()) {
  return {
    task_id: taskId,
    new_message: $("messageInput").value,
    source: "user",
    resume_if_needed: $("queueResumeIfNeeded").checked,
    agent_system: $("agentSystem").value,
  };
}

function requestPreviewForMode(mode = state.apiPreviewMode) {
  const taskId = taskIdForPreview();
  const ensureRequest = {
    method: "POST",
    url: "/api/v1/tasks/ensure",
    headers: { "Content-Type": "application/json" },
    body: buildEnsurePayload(taskId),
  };
  const common = {
    note: "SDK overrides are shown as the UI sends them. The server may normalize direct tool-name hidden params into tool_runtime_defaults before calling InfiAgent.run().",
  };

  if (mode === "ensure") {
    return {
      preview_mode: "Ensure",
      requests: [ensureRequest],
      ...common,
    };
  }
  if (mode === "run") {
    return {
      preview_mode: "Run Once",
      requests: [
        ensureRequest,
        {
          method: "POST",
          url: "/api/v1/tasks/message",
          headers: { "Content-Type": "application/json" },
          body: buildMessagePayload(taskId),
        },
      ],
      ...common,
    };
  }
  if (mode === "queue") {
    return {
      preview_mode: "Queue Message",
      requests: [
        ensureRequest,
        {
          method: "POST",
          url: "/api/v1/tasks/add-message",
          headers: { "Content-Type": "application/json" },
          body: buildQueuePayload(taskId),
        },
      ],
      ...common,
    };
  }
  return {
    preview_mode: "Stream Send",
    requests: [
      ensureRequest,
      {
        method: "POST",
        url: "/api/v1/tasks/message/stream",
        headers: { "Content-Type": "application/json" },
        body: buildMessagePayload(taskId, { stream: true }),
        response: "text/event-stream",
      },
    ],
    ...common,
  };
}

function updateApiPreview(mode = state.apiPreviewMode) {
  state.apiPreviewMode = mode || "stream";
  try {
    $("output").textContent = JSON.stringify(requestPreviewForMode(state.apiPreviewMode), null, 2);
  } catch (error) {
    $("output").textContent = JSON.stringify(
      {
        preview_mode: state.apiPreviewMode,
        status: "invalid_form_payload",
        error: String(error),
      },
      null,
      2,
    );
  }
}

function loadCurrentSystemSettings() {
  const currentSystem = $("agentSystem").value;
  const settings = readSystemSettings(currentSystem);
  applyAdvancedSettings(settings);
  applyDefaultSystemAddPath(false);
}

async function loadHistory() {
  if (!state.currentTaskId) return null;
  const container = $("messages");
  if (state.streaming && state.streamTaskId === state.currentTaskId) {
    writePreviewStatus("Task is streaming; keeping live messages instead of reloading history.");
    updateApiPreview();
    return null;
  }
  const [data, feedback] = await Promise.all([
    jsonFetch(
      `/api/v1/tasks/history?task_id=${encodeURIComponent(state.currentTaskId)}` +
        `&compact=1&max_output_chars=${HISTORY_MAX_OUTPUT_CHARS}`,
    ),
    jsonFetch(`/api/v1/tasks/feedback?task_id=${encodeURIComponent(state.currentTaskId)}`).catch(() => null),
    refreshConfirmations(state.currentTaskId),
  ]);
  state.feedbackByRun = (feedback && feedback.latest_by_run) || {};
  const items = data.items || [];
  if (!items.length) {
    if (container.dataset.taskId !== state.currentTaskId) {
      container.innerHTML = "";
      container.dataset.taskId = state.currentTaskId;
    }
    writePreviewStatus("History is empty; keeping current visible messages.");
    updateApiPreview();
    return data;
  }
  const nextNodes = [];
  // Track the user message each turn answers: the framework keys confirmation
  // requests by (task_id + input_message), which is how a badge finds its own turn.
  let lastUserMessage = "";
  for (const item of items) {
    if (item.type === "user_message") {
      lastUserMessage = String(item.text || "");
      const { wrapper } = messageBubble("user", "user", item.text || "");
      nextNodes.push(wrapper);
      continue;
    }
    if (item.type === "agent_turn") {
      nextNodes.push(renderAgentTurn(item, lastUserMessage));
    }
  }
  if (!nextNodes.length) {
    writePreviewStatus("History contains no renderable messages; keeping current visible messages.");
    updateApiPreview();
    return data;
  }
  container.innerHTML = "";
  container.dataset.taskId = state.currentTaskId;
  for (const node of nextNodes) {
    container.appendChild(node);
  }
  container.scrollTop = container.scrollHeight;
  loadPendingHil();
  return data;
}

async function refreshRuntime() {
  const runtime = await jsonFetch("/api/v1/runtime");
  state.systemAddSources = runtime.system_add_sources || {};
  $("userRootText").textContent = runtime.active_user_root || "";
  const versionPrefix = runtime.infiagent_version ? `InfiAgent ${runtime.infiagent_version}; ` : "";
  $("runtimeSummary").textContent = `${versionPrefix}${(runtime.agent_systems || []).length} agent systems`;

  const systems = (runtime.agent_systems || []).map((item) => ({ value: item.name, label: item.name }));
  fillSelect($("agentSystem"), systems);

  const preferredSystem =
    $("agentSystem").value ||
    state.lastAgentSystem ||
    readLastAgentSystem() ||
    (systems[0] ? systems[0].value : "");
  if (systems.some((item) => item.value === preferredSystem)) {
    $("agentSystem").value = preferredSystem;
  }

  state.lastAgentSystem = $("agentSystem").value;
  await refreshAgents();
  loadCurrentSystemSettings();
  writePreviewStatus("Runtime refreshed");
  updateApiPreview();
}

async function refreshAgents() {
  const agentSystem = $("agentSystem").value;
  if (!agentSystem) return;
  const data = await jsonFetch(`/api/v1/agents?agent_system=${encodeURIComponent(agentSystem)}`);
  const items = (data.agents || []).map((item) => ({ value: item.name, label: item.name }));
  fillSelect($("agentName"), items);
  const remembered = readLastAgentName(agentSystem);
  if (items.some((item) => item.value === remembered)) {
    $("agentName").value = remembered;
  } else if (items[0]) {
    $("agentName").value = items[0].value;
  }
  syncAgentSelectTitles();
  updateApiPreview();
}

async function refreshTasks(selectNewest = false) {
  // H11: ask for one page; an old server ignores `limit` and returns everything.
  const data = await jsonFetch(`/api/v1/tasks?limit=${TASK_PAGE_SIZE}`);
  state.tasks = data.items || [];
  state.taskCursor = readNextCursor(data);
  if (selectNewest && state.tasks.length && !state.currentTaskId) {
    setCurrentTask(state.tasks[0].task_id);
  }
  renderTaskList();
}

// ----- 4.6.5: who new sessions belong to, and a visible error when creating one fails -----

function readConsoleUserId() {
  return localStorage.getItem(storageKey("consoleUserId")) || "";
}

function writeConsoleUserId(value) {
  if (value) localStorage.setItem(storageKey("consoleUserId"), value);
  else localStorage.removeItem(storageKey("consoleUserId"));
}

// The "用户 ID" box: {value: null} when empty (a session without identity), {value: text}, or {error} when the text
// breaks the server's rule (4.6.6: an integer, or 1–128 letters, digits and . _ @ : + -, case-sensitive). The text is sent
// as typed; the server turns "7" into the number 7 and keeps "alice" / "007" as strings.
const USER_ID_RE = /^[A-Za-z0-9._@:+-]{1,128}$/;

function consoleUserId() {
  const input = $("consoleUserId");
  const raw = input ? input.value.trim() : "";
  if (input) input.removeAttribute("aria-invalid");
  if (!raw) return { value: null };
  if (!USER_ID_RE.test(raw)) {
    if (input) input.setAttribute("aria-invalid", "true");
    return { error: `用户 ID 只能是整数，或 1–128 个字母、数字、. _ @ : + - 组成的字符串（现在是「${raw}」）；留空 = 无身份会话` };
  }
  return { value: raw };
}

function hideTaskNotice() {
  const node = $("newTaskNotice");
  if (!node) return;
  node.hidden = true;
  node.innerHTML = "";
}

function showTaskNotice(message, hint = "") {
  const node = $("newTaskNotice");
  if (!node) return;
  node.innerHTML = `<button type="button" class="task-notice-close" aria-label="关闭">×</button>`
    + `<div>新建会话失败：${escapeHtml(message)}</div>`
    + (hint ? `<div class="task-notice-hint">${escapeHtml(hint)}</div>` : "");
  node.querySelector(".task-notice-close").addEventListener("click", hideTaskNotice);
  node.hidden = false;
}

// What to do in this console; the server's message already says who may join the workspace.
function newTaskErrorHint(error, payload) {
  if (error.code === "SCOPE_DENIED" && payload.user_id == null) {
    return "在上方「用户 ID」填你的 ID 再点 New Task。";
  }
  if (error.code === "WORKSPACE_NOT_FOUND") {
    return "这个工作区已不存在：点会话列表上方的刷新按钮，重新选择工作区。";
  }
  return "";
}

// Whether `taskId` names a session that does not exist yet. A task in the loaded list exists; anything else is
// asked of the server (the list is paged, and a typed name may be an older session).
async function taskIsNew(taskId) {
  if (state.tasks.some((task) => task.task_id === taskId)) return false;
  try {
    const meta = await jsonFetch(`/api/v1/tasks/meta?task_id=${encodeURIComponent(taskId)}`);
    return meta.exists === false;
  } catch (error) {
    return false;  // let the ensure call report whatever is wrong with this task id
  }
}

// `fresh: true` = the caller just generated the id (New Task). Otherwise the session is new only if the server
// has never seen it — then, whichever button creates it (Ensure, Run Once, Send, Queue, Upload …), the request
// carries the user ID and the "新会话的工作区" choice. An existing session is ensured with the old payload:
// sending another user_id would rewrite its owner, and its workspace cannot be changed this way.
async function ensureTask({ fresh = false } = {}) {
  const taskId = ensureCurrentTaskId();
  const payload = {
    task_id: taskId,
    conversation_name: taskId,
    agent_system: $("agentSystem").value,
    agent_name: $("agentName").value,
  };
  const isNew = fresh === true || await taskIsNew(taskId);
  if (isNew) {
    const user = consoleUserId();
    if (user.error) {
      showTaskNotice(user.error);
      throw new Error(user.error);
    }
    if (user.value !== null) payload.user_id = user.value;
    const workspace = workspaceArgForNewTask();
    if (workspace !== undefined) payload.workspace = workspace;
  }
  let data;
  try {
    data = await jsonFetch("/api/v1/tasks/ensure", "POST", payload);
  } catch (error) {
    if (isNew) showTaskNotice(String(error.message || error), newTaskErrorHint(error, payload));
    throw error;
  }
  if (isNew) hideTaskNotice();
  prettyWrite(data);
  if (payload.workspace !== undefined) {
    await refreshWorkspaces();
    if (data.workspace && data.workspace.workspace_id) {
      // keep the new/joined workspace selected so the next New Task lands next to this one
      renderWorkspaceChoice(data.workspace.workspace_id);
      $("newWorkspaceName").value = "";
    }
  }
  await refreshTasks();
  setCurrentTask(taskId);
  return data;
}

function parseOverrides() {
  return parseSdkOverridesFromForm({ persist: true });
}

async function runOnce() {
  const taskId = ensureCurrentTaskId();
  await ensureTask();
  const payload = {
    task_id: taskId,
    agent_system: $("agentSystem").value,
    agent_name: $("agentName").value,
    new_message: $("messageInput").value,
    sdk_overrides: parseOverrides(),
    ...buildRuntimeOptionsFromForm(),
  };
  const data = await jsonFetch("/api/v1/tasks/message", "POST", payload);
  prettyWrite(data);
  await refreshTasks();
  await loadHistory();
}

function consumeStreamChunk(text) {
  const blocks = text.split("\n\n").filter(Boolean);
  for (const block of blocks) {
    const lines = block.split("\n");
    const line = lines.find((part) => part.startsWith("data: "));
    if (!line) continue;
    const payload = JSON.parse(line.slice(6));
    // H2: frames also carry `id: <seq>.<k>` (4.5.0; plain `<seq>` before); fall back to it when the
    // event body itself has no seq, so resume never loses its high-water mark.
    const idLine = lines.find((part) => part.startsWith("id:"));
    const frameId = idLine ? idLine.slice(3).trim() : "";
    if (payload && typeof payload === "object" && payload.seq === undefined && frameId) {
      const parsed = parseSseCursor(frameId);
      if (parsed) payload.seq = parsed[0];
    }
    applyStreamEvent(payload, frameId);
  }
}

function ensurePendingAssistant() {
  if (state.pendingAssistantNode) return state.pendingAssistantNode;
  state.pendingAssistantNode = assistantBubble();
  appendMessageNode(state.pendingAssistantNode.wrapper);
  return state.pendingAssistantNode;
}

function findRunningToolCard(assistant, toolName, agentId = "", toolCallId = "") {
  const want = String(agentId || "");
  let fallback = null;
  for (let index = assistant.toolCards.length - 1; index >= 0; index -= 1) {
    const item = assistant.toolCards[index];
    if (item.toolName !== toolName || item.status !== "running") continue;
    if (toolCallId && item.toolCallId && String(item.toolCallId) !== String(toolCallId)) continue;
    if (String(item.agentId || "") === want) return item;
    if (fallback === null) fallback = item;
  }
  return fallback;
}

function markHilResolved(card, text) {
  card.classList.add("resolved");
  const form = card.querySelector(".hil-form");
  if (form) form.remove();
  const done = document.createElement("div");
  done.className = "hil-resolved";
  done.textContent = text;
  card.appendChild(done);
}

function hilChoiceSpec(ui) {
  // 4.5.0: ui = {type: "choice", options: [...], multi_select, allow_other}; anything else is a plain text question.
  if (!ui || ui.type !== "choice" || !Array.isArray(ui.options)) return null;
  const options = ui.options.map((o) => String(o)).filter((o) => o.trim() !== "");
  if (options.length === 0) return null;
  return { options, multi: Boolean(ui.multi_select), allowOther: ui.allow_other !== false };
}

function renderHilCard(event) {
  const hilId = String(event.hil_id || "");
  if (!hilId) return;
  // 4.5.0: hil_id is only the model's alias; one card per occurrence (same alias may be asked by two agents).
  const occurrenceId = String(event.occurrence_id || "");
  const cardKey = occurrenceId || hilId;
  const container = $("messages");
  if (container.querySelector(`[data-hil-id="${CSS.escape(cardKey)}"]`)) return; // already shown
  const choice = hilChoiceSpec(event.ui);
  const card = document.createElement("div");
  card.className = "hil-card";
  card.dataset.hilId = cardKey;
  const title = document.createElement("div");
  title.className = "hil-title";
  title.textContent = `🔔 ${event.title || "Human-in-Loop"}${event.agent_name ? ` · ${event.agent_name}` : ""}`;
  const msg = document.createElement("div");
  msg.className = "hil-message";
  msg.textContent = event.message || "";
  const form = document.createElement("div");
  form.className = "hil-form";
  const hint = document.createElement("div");
  hint.className = "hil-hint";
  const buttons = [];
  // The answer is always one string: an option's text, or (multi-select) the chosen options one per line.
  const send = async (response, busyBtn) => {
    buttons.forEach((b) => { b.disabled = true; });
    const label = busyBtn ? busyBtn.textContent : "";
    if (busyBtn) busyBtn.textContent = "发送中…";
    hint.textContent = "";
    try {
      const res = await fetch("/api/v1/tasks/hil-respond", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task_id: state.currentTaskId, hil_id: hilId, response, ...(occurrenceId ? { occurrence_id: occurrenceId } : {}) }),
      });
      const payload = await parseResponsePayload(res);
      if (!res.ok) throw new Error(errorMessageFromPayload(payload) || `HTTP ${res.status}`);
      markHilResolved(card, `✅ 已回答：${response ? response.split("\n").join("、") : "(空)"}`);
    } catch (error) {
      hint.textContent = `发送失败：${String(error.message || error)}`;
      buttons.forEach((b) => { b.disabled = false; });
      if (busyBtn) busyBtn.textContent = label;
    }
  };
  const input = document.createElement("textarea");
  input.className = "hil-input";
  input.rows = 2;
  if (!choice) {
    input.placeholder = "输入你的回复…";
    const submit = document.createElement("button");
    submit.type = "button";
    submit.className = "hil-submit";
    submit.textContent = "回复";
    buttons.push(submit);
    submit.addEventListener("click", () => send(input.value.trim(), submit));
    form.append(input, submit, hint);
  } else if (!choice.multi) {
    const row = document.createElement("div");
    row.className = "hil-options";
    choice.options.forEach((option) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "hil-option";
      btn.textContent = option;
      btn.addEventListener("click", () => send(option, btn));
      buttons.push(btn);
      row.appendChild(btn);
    });
    form.appendChild(row);
    if (choice.allowOther) {
      input.placeholder = "都不合适？在这里写你的回复…";
      const submit = document.createElement("button");
      submit.type = "button";
      submit.className = "hil-submit";
      submit.textContent = "回复其他";
      buttons.push(submit);
      submit.addEventListener("click", () => {
        const text = input.value.trim();
        if (!text) { hint.textContent = "请先写回复，或直接点上面的选项"; return; }
        send(text, submit);
      });
      form.append(input, submit);
    }
    form.appendChild(hint);
  } else {
    const list = document.createElement("div");
    list.className = "hil-checks";
    const boxes = choice.options.map((option, i) => {
      const label = document.createElement("label");
      label.className = "hil-check";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = option;
      box.id = `hil-${cardKey}-${i}`;
      label.htmlFor = box.id;
      const text = document.createElement("span");
      text.textContent = option;
      label.append(box, text);
      list.appendChild(label);
      return box;
    });
    form.appendChild(list);
    if (choice.allowOther) {
      input.placeholder = "其他（可选）…";
      form.appendChild(input);
    }
    const submit = document.createElement("button");
    submit.type = "button";
    submit.className = "hil-submit";
    submit.textContent = "提交";
    buttons.push(submit);
    submit.addEventListener("click", () => {
      const lines = boxes.filter((b) => b.checked).map((b) => b.value);
      const other = choice.allowOther ? input.value.trim() : "";
      if (other) lines.push(...other.split("\n").map((l) => l.trim()).filter(Boolean));
      if (lines.length === 0) { hint.textContent = "至少选一项"; return; }
      send(lines.join("\n"), submit);
    });
    form.append(submit, hint);
  }
  card.append(title, msg, form);
  container.appendChild(card);
}

function renderToolConfirmCard(event) {
  const confirmId = String(event.confirm_id || "");
  if (!confirmId) return;
  const occurrenceId = String(event.occurrence_id || "");
  const cardKey = occurrenceId || confirmId;
  const container = $("messages");
  if (container.querySelector(`[data-confirm-id="${CSS.escape(cardKey)}"]`)) return;
  const card = document.createElement("div");
  card.className = "hil-card confirm";
  card.dataset.confirmId = cardKey;
  const title = document.createElement("div");
  title.className = "hil-title";
  title.textContent = `🛡 工具执行确认：${event.tool_name || ""}`;
  const args = createJsonDetails("Arguments", event.arguments || {}, { open: true });
  const form = document.createElement("div");
  form.className = "hil-form";
  const hint = document.createElement("div");
  hint.className = "hil-hint";
  const respond = async (approved, btn) => {
    Array.from(form.querySelectorAll("button")).forEach((b) => (b.disabled = true));
    if (btn) btn.textContent = "发送中…";
    try {
      const res = await fetch("/api/v1/tasks/tool-confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task_id: state.currentTaskId, confirm_id: confirmId, approved, ...(occurrenceId ? { occurrence_id: occurrenceId } : {}) }),
      });
      const payload = await parseResponsePayload(res);
      if (!res.ok) throw new Error(errorMessageFromPayload(payload) || `HTTP ${res.status}`);
      // 4.5.0: approval is recorded here; the tool itself runs afterwards inside the agent turn.
      markHilResolved(card, approved ? "✅ 已批准（工具随后在本轮执行）" : "🚫 已拒绝");
    } catch (error) {
      hint.textContent = `发送失败：${String(error.message || error)}`;
      Array.from(form.querySelectorAll("button")).forEach((b) => (b.disabled = false));
    }
  };
  const approve = document.createElement("button");
  approve.type = "button";
  approve.className = "hil-submit approve";
  approve.textContent = "批准";
  approve.addEventListener("click", () => respond(true, approve));
  const reject = document.createElement("button");
  reject.type = "button";
  reject.className = "hil-submit reject";
  reject.textContent = "拒绝";
  reject.addEventListener("click", () => respond(false, reject));
  form.append(approve, reject, hint);
  card.append(title, args, form);
  container.appendChild(card);
}

async function loadPendingHil() {
  if (!state.currentTaskId) return;
  try {
    const data = await jsonFetch(`/api/v1/tasks/pending-hil?task_id=${encodeURIComponent(state.currentTaskId)}`);
    for (const item of data.items || []) {
      if (item.type === "tool_confirmation") renderToolConfirmCard(item);
      else renderHilCard(item);
    }
  } catch (_error) {
    /* pending HIL is best-effort; ignore fetch errors */
  }
}

// H2: every SSE event carries a per-task monotonic `seq`. Remember the highest
// one so a dropped connection can resume with after_seq, and track whether the
// current run already reached a terminal event.
function noteStreamEventSeq(event) {
  if (!event || typeof event !== "object") return;
  const raw = event.seq;
  if (raw !== undefined && raw !== null && raw !== "") {
    const seq = Number(raw);
    if (Number.isFinite(seq)) {
      state.lastEventSeq = Math.max(state.lastEventSeq || 0, seq);
    }
  }
  const type = event.type;
  if (type === "run.finished" || type === "run.error") {
    state.runTerminal = true;
  } else if (type === "run.started" || type === "run.queued") {
    state.runTerminal = false;
  }
}

// 4.5.0 (W10): persisted events carry an exact id; one at or below the cursor is already on screen
// (overlap between the POST stream, a reconnect's replay and live) and is dropped. Deltas and
// synthetic events carry none and always pass.
function admitStreamCursor(event, frameId) {
  const cursor = parseSseCursor(frameId || (event && event.sse_id) || "");
  if (!cursor) return true;
  const current = parseSseCursor(state.lastEventId);
  if (current && compareSseCursor(cursor, current) <= 0) return false;
  state.lastEventId = formatSseCursor(cursor);
  state.resyncStreak = 0;
  return true;
}

// 4.5.0 (W10): a delta that belongs to a different LLM call than the card it would land in means the
// previous call's `.done` never reached us (it fell into a reconnect gap): close that card instead of
// appending the new call's text to it.
function switchStreamSegment(assistant, key, segmentId) {
  if (!segmentId) return false;
  assistant.segmentIdByKey = assistant.segmentIdByKey || {};
  const current = assistant.segmentIdByKey[key];
  assistant.segmentIdByKey[key] = segmentId;
  if (current && current !== segmentId) {
    resetOrderedSegment(assistant, key);
    return true;
  }
  return false;
}

function finishStreamSegment(assistant, key, segmentId) {
  if (segmentId) state.completedSegments[segmentId] = true;
  if (assistant.segmentIdByKey) assistant.segmentIdByKey[key] = "";
}

function applyStreamEvent(event, frameId = "") {
  if (!admitStreamCursor(event, frameId)) return;
  noteStreamEventSeq(event);

  // 4.5.0 (W10): this connection fell behind the server's live window and was closed. Reconnect at
  // once with our cursor: persisted events are replayed from disk, then live resumes.
  if (event.type === "stream.resync") {
    if (!state.lastEventId && event.from_id) state.lastEventId = String(event.from_id);
    state.resyncRequested = true;
    setTranscriptStatus(STREAM_STATUS_KEY, "正在重新同步…");
    return;
  }
  const segmentId = String(event.segment_id || "");
  if (segmentId && state.completedSegments[segmentId] && String(event.type || "").endsWith(".delta")) {
    return; // late delta of a segment whose complete text is already rendered
  }

  // H4: the node is at capacity and this run is waiting its turn. One line,
  // updated in place, dropped as soon as the run actually starts.
  if (event.type === "run.queued") {
    const position = Number(event.position);
    const maxRuns = Number(event.max_runs);
    setTranscriptStatus(
      QUEUE_STATUS_KEY,
      `排队中：第 ${Number.isFinite(position) ? position : "?"} 位（本节点上限 ${
        Number.isFinite(maxRuns) ? maxRuns : "?"
      }）`,
    );
    return;
  }
  if (event.type === "run.started") {
    clearTranscriptStatus(QUEUE_STATUS_KEY);
  }

  // Anchor the main instance on the FIRST event that carries an agent_id —
  // agent.started itself has no id (backend emits it before push_agent), but
  // the main agent's thinking/tool events always precede any sub-agent's.
  {
    const anchorId = structEventAgentId(event);
    if (!state.streamMainAgentId && anchorId) state.streamMainAgentId = anchorId;
  }
  if (event.type === "agent.started") {
    const aid = structEventAgentId(event);
    if (!state.streamMainAgentId && aid) state.streamMainAgentId = aid;
    const assistant = ensureAssistantForEvent(event);
    if (structIsSub(aid)) structRootFor(assistant, event);
    scrollMessagesIfNearBottom();
    return;
  }

  if (event.type === "agent.finished") {
    const agentName = streamEventAgentName(event);
    if (agentName) {
      state.lastStreamAgentName = agentName;
    }
    const aid = structEventAgentId(event);
    const assistant = state.pendingAssistantNode;
    const g = assistant?.subGroups?.[aid];
    if (g && g.summary && !/done|error|stopped/.test(g.summary.textContent)) {
      g.summary.textContent += " \u00b7 done";
    }
    return;
  }

  if (event.type === "human_in_loop") {
    renderHilCard(event);
    $("messages").scrollTop = $("messages").scrollHeight;
    return;
  }

  if (event.type === "tool_confirmation") {
    renderToolConfirmCard(event);
    $("messages").scrollTop = $("messages").scrollHeight;
    return;
  }

  const assistant = ensureAssistantForEvent(event);
  assistant.contentByKey = assistant.contentByKey || {};
  if (event.type === "assistant.content.delta") {
    const key = structSegKey("content", event);
    if (switchStreamSegment(assistant, key, segmentId)) assistant.contentByKey[key] = "";
    const card = ensureOrderedSegment(assistant, key, segmentTitle("Content", event), "content-card", structRootFor(assistant, event));
    assistant.contentByKey[key] = (assistant.contentByKey[key] || "") + (event.text || "");
    card.body.textContent = assistant.contentByKey[key];
  } else if (event.type === "assistant.content.reset") {
    const key = structSegKey("content", event);
    assistant.contentByKey[key] = "";
    resetOrderedSegment(assistant, key);
  } else if (event.type === "assistant.content.done") {
    const key = structSegKey("content", event);
    if (switchStreamSegment(assistant, key, segmentId)) assistant.contentByKey[key] = "";
    const merged = mergeCompletedText(assistant.contentByKey[key] || "", event.text);
    if (hasVisibleText(merged) || assistant.activeSegments[key]) {
      const card = ensureOrderedSegment(assistant, key, segmentTitle("Content", event), "content-card", structRootFor(assistant, event));
      setMarkdown(card.body, merged);
    }
    // 主实例的最终 content 仍供 message.done 兜底展示
    if (!structIsSub(structEventAgentId(event))) assistant.content = merged;
    assistant.contentByKey[key] = "";
    resetOrderedSegment(assistant, key);
    finishStreamSegment(assistant, key, segmentId);
  } else if (event.type === "assistant.reasoning.started") {
    resetOrderedSegment(assistant, structSegKey("reasoning", event));
  } else if (event.type === "assistant.reasoning.reset") {
    resetOrderedSegment(assistant, structSegKey("reasoning", event));
  } else if (event.type === "assistant.reasoning.delta") {
    switchStreamSegment(assistant, structSegKey("reasoning", event), segmentId);
    const card = ensureOrderedSegment(assistant, structSegKey("reasoning", event), segmentTitle("Reasoning", event), "reasoning-card", structRootFor(assistant, event));
    card.body.textContent += event.text || "";
  } else if (event.type === "assistant.reasoning.done") {
    switchStreamSegment(assistant, structSegKey("reasoning", event), segmentId);
    const existing = assistant.activeSegments[structSegKey("reasoning", event)];
    const mergedText = mergeCompletedText(existing?.body?.textContent || "", event.text);
    if (hasVisibleText(mergedText) || existing) {
      const card = existing || ensureOrderedSegment(assistant, structSegKey("reasoning", event), segmentTitle("Reasoning", event), "reasoning-card", structRootFor(assistant, event));
      card.body.textContent = mergedText;
    }
    resetOrderedSegment(assistant, structSegKey("reasoning", event));
    finishStreamSegment(assistant, structSegKey("reasoning", event), segmentId);
  } else if (event.type === "thinking.started") {
    resetOrderedSegment(assistant, structSegKey("thinking", event));
  } else if (event.type === "thinking.reset") {
    resetOrderedSegment(assistant, structSegKey("thinking", event));
  } else if (event.type === "thinking.delta") {
    switchStreamSegment(assistant, structSegKey("thinking", event), segmentId);
    const card = ensureOrderedSegment(assistant, structSegKey("thinking", event), segmentTitle("Thinking", event), "thinking-card", structRootFor(assistant, event));
    card.body.textContent += event.text || "";
  } else if (event.type === "thinking.done") {
    switchStreamSegment(assistant, structSegKey("thinking", event), segmentId);
    const existing = assistant.activeSegments[structSegKey("thinking", event)];
    const mergedText = mergeCompletedText(existing?.body?.textContent || "", event.text);
    if (hasVisibleText(mergedText) || existing) {
      const card = existing || ensureOrderedSegment(assistant, structSegKey("thinking", event), segmentTitle("Thinking", event), "thinking-card", structRootFor(assistant, event));
      card.body.textContent = mergedText;
    }
    resetOrderedSegment(assistant, structSegKey("thinking", event));
    finishStreamSegment(assistant, structSegKey("thinking", event), segmentId);
  } else if (event.type === "tool.started") {
    const tool = createToolCard(event.tool_name, "running", event.arguments || {}, null, event.timestamp || "");
    resetOrderedSegment(assistant, structSegKey("thinking", event));
    resetOrderedSegment(assistant, structSegKey("reasoning", event));
    resetOrderedSegment(assistant, structSegKey("content", event));
    structRootFor(assistant, event).appendChild(tool.card);
    assistant.toolCards.push({ toolName: event.tool_name, status: "running", agentId: structEventAgentId(event), toolCallId: event.tool_call_id || "", ...tool });
  } else if (event.type === "tool.finished") {
    const existing = findRunningToolCard(assistant, event.tool_name, structEventAgentId(event), event.tool_call_id || "");
    if (existing) {
      existing.status = event.status || "done";
      existing.updateStatus(event.status || "done");
      existing.updateResult(event.result || {}, event.images || null, event.queue || null);
    } else {
      const tool = createToolCard(event.tool_name, event.status || "done", {}, event.result || {}, event.timestamp || "", event.images || null, event.queue || null);
      structRootFor(assistant, event).appendChild(tool.card);
      assistant.toolCards.push({ toolName: event.tool_name, status: event.status || "done", ...tool });
    }
  } else if (event.type === "message.done") {
    const finalText = event.text || assistant.bodyNode.textContent || "";
    if (event.output_2) {
      renderTwoVersions(assistant, finalText, event.output_2, event.run_id);
    } else {
      setMarkdown(assistant.bodyNode, finalText);
      attachFinalOutputExport(assistant.wrapper, finalText, finalOutputExportName(event.run_id));
    }
    // R15: files this final_output declared (message.done carries download_paths)
    attachDownloadLinks(assistant.bodyNode, event.download_paths, state.currentTaskId);
    // R14: the run may have registered decisions that need a human; surface the badge
    // as soon as the answer lands instead of only after a manual history reload.
    const liveInput = state.hcLastInputMessage || "";
    assistant.wrapper.dataset.hcInputMessage = liveInput;
    refreshConfirmations(state.currentTaskId)
      .then(() => attachConfirmationBadge(assistant.wrapper, liveInput))
      .catch(() => {});
  } else if (event.type === "run.error") {
    assistant.bodyNode.textContent += `\n[error] ${event.error || ""}`;
  }
  scrollMessagesIfNearBottom();
}

function applyRawStreamEvent(event) {
  const type = event?.type;
  const agentName = event?.agent || event?.agent_name || "Agent";

  switch (type) {
    case "token":
      removeTypingIndicator();
      finalizeReasoningStreamMac();
      streamTokenMac(event.text || "", agentName, String(event.agent_id || ""));
      break;
    case "reasoning_token":
      removeTypingIndicator();
      finalizeCurrentStreamMac();
      streamReasoningTokenMac(event.text || "", String(event.agent_id || ""));
      break;
    case "thinking_token":
      removeTypingIndicator();
      finalizeCurrentStreamMac();
      streamThinkingAgentTokenMac(event.text || "", agentName, String(event.agent_id || ""));
      break;
    case "tool_call":
      finalizeCurrentStreamMac();
      finalizeReasoningStreamMac();
      finalizeThinkingAgentStreamMac();
      removeTypingIndicator();
      addToolMessageMac(event.tool || event.tool_name || event.name || "unknown", "running", event.arguments, agentName, String(event.agent_id || ""));
      showTypingIndicator();
      break;
    case "tool_result":
      removeTypingIndicator();
      updateLastToolStatusMac(event.status === "error" ? "error" : "success", event.result || event.output_preview || "", String(event.agent_id || ""));
      break;
    case "agent_start": {
      if (!state.streamMainAgentId && event.agent_id) {
        state.streamMainAgentId = String(event.agent_id);
      }
      const aid = String(event.agent_id || "");
      // 只收自己实例的流段——并行子实例的启动不打断主实例正在输出的段
      if (currentAgentBubbleOwner === aid) finalizeCurrentStreamMac();
      if (reasoningOwner === aid) finalizeReasoningStreamMac();
      if (thinkingAgentOwner === aid) finalizeThinkingAgentStreamMac();
      removeTypingIndicator();
      if (isSubAgentId(aid)) {
        streamRootFor(aid, agentName);   // 建收起气泡，子实例内容都进这里
      } else {
        addSystemMessage(`${agentName || "Agent"} started`);
      }
      showTypingIndicator();
      break;
    }
    case "agent_end": {
      const aid = String(event.agent_id || "");
      if (currentAgentBubbleOwner === aid) finalizeCurrentStreamMac();
      if (reasoningOwner === aid) finalizeReasoningStreamMac();
      if (thinkingAgentOwner === aid) finalizeThinkingAgentStreamMac();
      if (isSubAgentId(aid)) {
        markSubAgentDone(aid, String(event.status || "") === "error" ? "error" : "done");
      }
      removeTypingIndicator();
      break;
    }
    case "thinking_start":
      finalizeCurrentStreamMac();
      finalizeReasoningStreamMac();
      removeTypingIndicator();
      startThinkingAgentStreamMac(agentName, String(event.agent_id || ""));
      break;
    case "thinking_end":
      removeTypingIndicator();
      finalizeThinkingAgentStreamMac();
      break;
    case "result":
      finalizeCurrentStreamMac();
      finalizeReasoningStreamMac();
      finalizeThinkingAgentStreamMac();
      removeTypingIndicator();
      if (event.summary) {
        streamTokenMac(event.summary, agentName);
        finalizeCurrentStreamMac();
      }
      break;
    case "error":
      finalizeCurrentStreamMac();
      finalizeReasoningStreamMac();
      finalizeThinkingAgentStreamMac();
      removeTypingIndicator();
      addErrorMessage(event.message || event.error || event.text || "Unknown error");
      break;
    default:
      if (event?.text) {
        removeTypingIndicator();
        streamTokenMac(event.text, agentName);
      }
      break;
  }

  $("messages").scrollTop = $("messages").scrollHeight;
}

// H11: `resume` reconnects a dropped stream. It deliberately keeps the render
// state (pending assistant bubble, per-agent segments, sub-agent groups) so the
// replayed tail lands in the same bubbles instead of opening a second turn.
async function attachLiveStream(runId = "", { resume = false, attempt = 0 } = {}) {
  if (!state.currentTaskId) return;
  if (!resume) {
    detachStream();
    resetStreamCursor();
    state.runTerminal = false;
  }

  state.streaming = true;
  const controller = new AbortController();
  state.streamController = controller;
  state.streamTaskId = state.currentTaskId;
  const generation = state.streamGeneration;
  if (!resume) {
    resetStreamRenderState();
    resetMacLikeStreamState();
    removeTypingIndicator();
  }
  $("messages").dataset.taskId = state.currentTaskId;

  try {
    const params = new URLSearchParams({
      task_id: state.currentTaskId,
      stream_mode: "normalized",
    });
    if (runId) {
      params.set("run_id", runId);
    }
    // H2: only events past the high-water mark, so the replay cannot duplicate
    // what is already on screen. 4.5.0: the exact `<seq>.<k>` cursor (a record's second
    // derived event — e.g. content.done after reasoning.done — is no longer skipped).
    const headers = {};
    if (state.lastEventId) {
      params.set("after_id", state.lastEventId);
      headers["Last-Event-ID"] = state.lastEventId;
    } else if (state.lastEventSeq > 0) {
      params.set("after_seq", String(state.lastEventSeq));
    }
    const res = await fetch(`/api/v1/tasks/live-stream?${params.toString()}`, {
      method: "GET",
      headers,
      signal: controller.signal,
    });
    if (!res.ok) {
      throw new Error(await res.text());
    }
    clearTranscriptStatus(STREAM_STATUS_KEY);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop() || "";
      for (const part of parts) {
        if (part.trim()) {
          consumeStreamChunk(part + "\n\n");
        }
      }
    }
  } catch (error) {
    if (error.name !== "AbortError") {
      prettyWrite({ status: "error", error: String(error) });
    }
  } finally {
    // Only the stream that still owns the controller may clear shared state
    // and reload history; a detached stream must exit silently.
    if (state.streamController === controller) {
      const streamedTaskId = state.streamTaskId;
      state.streamController = null;
      if (consumeResyncRequest(controller, generation)) {
        await attachLiveStream(runId, { resume: true, attempt: 0 });
      } else if (streamShouldResume(controller, generation, attempt)) {
        await resumeLiveStream(runId, attempt + 1, generation, streamedTaskId);
      } else {
        state.streaming = false;
        state.streamTaskId = "";
        clearTranscriptStatus(STREAM_STATUS_KEY);
        clearTranscriptStatus(QUEUE_STATUS_KEY);
        resetStreamRenderState();
        resetMacLikeStreamState();
        removeTypingIndicator();
        await refreshTasks();
        await loadHistory();
      }
    }
  }
}

// 4.5.0 (W10): `stream.resync` is not a failure — reconnect immediately without spending the resume
// budget. A streak of resyncs with no persisted progress in between falls back to the backoff path.
function consumeResyncRequest(controller, generation) {
  if (!state.resyncRequested) return false;
  state.resyncRequested = false;
  if (!controller || controller.signal.aborted) return false;
  if (generation !== state.streamGeneration) return false;
  if (!state.currentTaskId) return false;
  if (state.streamTaskId && state.streamTaskId !== state.currentTaskId) return false;
  state.resyncStreak = (state.resyncStreak || 0) + 1;
  return state.resyncStreak <= STREAM_RESUME_MAX_ATTEMPTS;
}

// A reader that ends (or a network error) before run.finished/run.error means
// the connection dropped, not that the run is over — reconnect through
// live-stream with after_seq. An explicit abort (detach / stop) never resumes,
// and neither does a stream whose task is no longer the selected one.
function streamShouldResume(controller, generation, attempt) {
  if (!controller || controller.signal.aborted) return false;
  if (state.runTerminal) return false;
  if (generation !== state.streamGeneration) return false;
  if (attempt >= STREAM_RESUME_MAX_ATTEMPTS) return false;
  if (!state.currentTaskId) return false;
  if (state.streamTaskId && state.streamTaskId !== state.currentTaskId) return false;
  return true;
}

async function resumeLiveStream(runId, attempt, generation, taskId) {
  setTranscriptStatus(
    STREAM_STATUS_KEY,
    `连接中断，正在续流…(${attempt}/${STREAM_RESUME_MAX_ATTEMPTS})`,
  );
  await sleep(STREAM_RESUME_BASE_DELAY_MS * Math.pow(2, attempt - 1));
  // The user may have detached, stopped, or switched task during the backoff.
  // Only a generation bump means somebody else already took ownership of the
  // stream state; in every other give-up case this call must release it.
  const generationStale = generation !== state.streamGeneration;
  const taskStale = Boolean(taskId) && taskId !== state.currentTaskId;
  if (generationStale || taskStale || state.runTerminal) {
    if (!generationStale) {
      state.streaming = false;
      state.streamTaskId = "";
    }
    clearTranscriptStatus(STREAM_STATUS_KEY);
    return;
  }
  await attachLiveStream(runId, { resume: true, attempt });
}

async function runStream() {
  if (state.streaming) {
    writePreviewStatus("A stream is already active; use Queue Message to append, or wait for the current run.");
    prettyWrite({ status: "busy", error: "当前已有流式任务在执行；请用 Queue Message 追加消息，或等待当前 run 结束。" });
    return;
  }
  const taskId = ensureCurrentTaskId();
  await ensureTask();
  persistCurrentSystemSettings();

  const { wrapper } = messageBubble("user", "user", $("messageInput").value);
  appendMessageNode(wrapper);
  resetStreamRenderState();
  resetMacLikeStreamState();
  removeTypingIndicator();
  $("messages").dataset.taskId = taskId;
  // R14: remember which message this run answers, so message.done can look up the
  // decisions the run registered without waiting for a history reload.
  state.hcLastInputMessage = $("messageInput").value || "";
  // H11: a new run opens a fresh seq window and a fresh resume budget.
  resetStreamCursor();
  state.runTerminal = false;
  clearTranscriptStatus(STREAM_STATUS_KEY);
  clearTranscriptStatus(QUEUE_STATUS_KEY);

  const payload = {
    task_id: taskId,
    agent_system: $("agentSystem").value,
    agent_name: $("agentName").value,
    new_message: $("messageInput").value,
    sdk_overrides: parseOverrides(),
    stream_mode: "normalized",
    ...buildRuntimeOptionsFromForm(),
  };

  state.streaming = true;
  state.streamGeneration = (state.streamGeneration || 0) + 1;
  const controller = new AbortController();
  state.streamController = controller;
  state.streamTaskId = taskId;
  const generation = state.streamGeneration;
  try {
    const res = await fetch("/api/v1/tasks/message/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    if (!res.ok) {
      throw new Error(await res.text());
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop() || "";
      for (const part of parts) {
        if (part.trim()) {
          consumeStreamChunk(part + "\n\n");
        }
      }
    }
  } catch (error) {
    if (error.name !== "AbortError") {
      prettyWrite({ status: "error", error: String(error) });
    }
  } finally {
    if (state.streamController === controller) {
      state.streamController = null;
      // The POST stream dropped mid-run: follow the same run through
      // live-stream (by task) starting after the last seq we rendered.
      if (consumeResyncRequest(controller, generation)) {
        state.streamTaskId = taskId;
        await attachLiveStream("", { resume: true, attempt: 0 });
      } else if (streamShouldResume(controller, generation, 0)) {
        await resumeLiveStream("", 1, generation, taskId);
      } else {
        state.streaming = false;
        state.streamTaskId = "";
        clearTranscriptStatus(STREAM_STATUS_KEY);
        clearTranscriptStatus(QUEUE_STATUS_KEY);
        resetStreamRenderState();
        resetMacLikeStreamState();
        removeTypingIndicator();
        await refreshTasks();
        await loadHistory();
      }
    }
  }
}

async function queueMessage() {
  const taskId = ensureCurrentTaskId();
  const queuedText = $("messageInput").value;
  const wasStreaming = state.streaming;
  await ensureTask();
  persistCurrentSystemSettings();
  const payload = {
    task_id: taskId,
    new_message: queuedText,
    source: "user",
    resume_if_needed: $("queueResumeIfNeeded").checked,
    agent_system: $("agentSystem").value,
  };
  const data = await jsonFetch("/api/v1/tasks/add-message", "POST", payload);
  prettyWrite(data);
  if (data.status === "success" && (wasStreaming || data.running)) {
    insertQueuedUserMessage(queuedText);
  }
  await refreshTasks();
  if (data.status === "success" && data.run_id && !wasStreaming) {
    attachLiveStream(data.run_id);
    return;
  }
  if (wasStreaming || data.running) {
    return;
  }
  await loadHistory();
}

async function stopStream() {
  // H11: a manual stop must also cancel a pending auto-resume — it may be
  // waiting out its backoff with no controller left to abort.
  state.streamGeneration = (state.streamGeneration || 0) + 1;
  state.runTerminal = true;
  if (state.streamController) {
    state.streamController.abort();
  } else if (state.streaming) {
    state.streaming = false;
    state.streamTaskId = "";
    clearTranscriptStatus(STREAM_STATUS_KEY);
  }
  removeTypingIndicator();
  if (state.currentTaskId) {
    const data = await jsonFetch("/api/v1/tasks/pause", "POST", {
      task_id: state.currentTaskId,
      reason: "manual stop from web console",
    });
    prettyWrite(data);
    await refreshTasks();
    await loadHistory();
    // Worker was killed: no result events will arrive for in-flight tools,
    // so flip leftover running markers to a terminal stopped state.
    document.querySelectorAll('#messages .tool-status.running').forEach((el) => {
      el.textContent = 'stopped';
      el.classList.remove('running');
    });
    document.querySelectorAll('#messages .tool-card.tool-running').forEach((c) => {
      c.classList.remove('tool-running');
      const label = c.querySelector('.tool-status-label');
      if (label) label.textContent = 'stopped';
    });
    document.querySelectorAll('#messages .subagent-card > summary, #messages .subagent-live > summary').forEach((sm) => {
      if (/running/.test(sm.textContent)) sm.textContent = sm.textContent.replace(/running.*$/, 'stopped');
    });
  }
}

async function taskAction(endpoint, body) {
  if (!state.currentTaskId) return;
  const data = await jsonFetch(
    endpoint,
    "POST",
    body || {
      task_id: state.currentTaskId,
      reason: "manual action from web console",
    },
  );
  prettyWrite(data);
  await refreshTasks();
  await loadHistory();
}

async function resumeTask() {
  if (!state.currentTaskId) return;
  const data = await jsonFetch("/api/v1/tasks/resume", "POST", {
    task_id: state.currentTaskId,
    reason: "manual resume from web console",
  });
  prettyWrite(data);
  await refreshTasks();
  if (data.status === "success" && data.run_id) {
    attachLiveStream(data.run_id);
    return;
  }
  await loadHistory();
}

async function fetchInspector(endpoint) {
  if (!state.currentTaskId) return;
  const data = await jsonFetch(`${endpoint}?task_id=${encodeURIComponent(state.currentTaskId)}`);
  prettyWrite(data);
}

// R8: detect whether a chunk of Markdown holds at least one pipe table (a row
// containing "|" followed by a |---|---| separator). Decides whether a given
// final_output gets its own "导出表格" button.
function hasMarkdownTable(text) {
  if (!text) return false;
  const lines = String(text).split(/\r?\n/);
  for (let i = 0; i < lines.length - 1; i += 1) {
    if (
      lines[i].includes("|") &&
      /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/.test(lines[i + 1])
    ) {
      return true;
    }
  }
  return false;
}

// POST one final_output's Markdown to the backend, which returns an xlsx (one
// sheet per table). We convert per-turn content instead of refetching the
// task's last output, so every final_output exports its own tables.
async function exportMarkdownTablesToXlsx(markdown, filenameBase) {
  writePreviewStatus("正在导出表格…");
  try {
    const res = await fetch("/api/v1/artifacts/markdown-tables.xlsx", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ markdown, filename: filenameBase || "tables" }),
    });
    if (res.status === 400) {
      writePreviewStatus("该输出没有可导出的表格");
      window.alert("这段最终输出里没有检测到 Markdown 表格。");
      return;
    }
    if (!res.ok) {
      throw new Error((await res.text()) || `HTTP ${res.status}`);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${filenameBase || "tables"}.xlsx`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    writePreviewStatus(`已导出表格为 Excel（${filenameBase || "tables"}.xlsx）`);
  } catch (error) {
    const message = String(error.message || error);
    writePreviewStatus(`导出失败：${message}`);
    window.alert(`导出失败：${message}`);
  }
}

// 3.12.35: when a final_output carries output_2, render both versions as
// selectable options with "采用此版本" (adoption recorded server-side) instead
// of a single answer. mainText = output (主版本), altText = output_2 (第二版本).
function renderTwoVersions(assistant, mainText, altText, runId) {
  const body = assistant.bodyNode;
  body.innerHTML = "";
  body.classList.add("version-pair");
  const taskId = (state.currentTaskId || $("taskId")?.value || "").trim();
  const versions = [
    { id: "main", label: "主版本", text: mainText || "" },
    { id: "alt", label: "第二版本", text: altText || "" },
  ];
  versions.forEach((v) => {
    const card = document.createElement("div");
    card.className = "version-card";
    card.dataset.versionId = v.id;
    const head = document.createElement("div");
    head.className = "version-head";
    const title = document.createElement("span");
    title.className = "version-title";
    title.textContent = v.label;
    const adopt = document.createElement("button");
    adopt.type = "button";
    adopt.className = "secondary version-adopt";
    adopt.textContent = "采用此版本";
    adopt.addEventListener("click", () => adoptVersion(assistant.wrapper, taskId, runId, v.id, adopt));
    head.appendChild(title);
    head.appendChild(adopt);
    const md = document.createElement("div");
    md.className = "version-body";
    setMarkdown(md, v.text);
    card.appendChild(head);
    card.appendChild(md);
    if (hasMarkdownTable(v.text)) {
      const bar = document.createElement("div");
      bar.className = "final-output-export";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "secondary export-tables-btn";
      btn.textContent = "⬇ 导出表格 Excel";
      btn.addEventListener("click", () => exportMarkdownTablesToXlsx(v.text, `${finalOutputExportName(runId)}_${v.id}`));
      bar.appendChild(btn);
      card.appendChild(bar);
    }
    body.appendChild(card);
  });
}

async function adoptVersion(wrapper, taskId, runId, versionId, btn) {
  if (!taskId) { window.alert("缺少 Task ID，无法记录采用"); return; }
  try {
    const res = await fetch("/api/v1/tasks/adopt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_id: taskId, run_id: runId || "", version_id: versionId }),
    });
    if (!res.ok) throw new Error((await res.text()) || `HTTP ${res.status}`);
    wrapper.querySelectorAll(".version-card").forEach((c) => c.classList.toggle("adopted", c.dataset.versionId === versionId));
    wrapper.querySelectorAll(".version-adopt").forEach((b) => { b.textContent = "采用此版本"; b.disabled = false; });
    btn.textContent = "✓ 已采用";
    btn.disabled = true;
    writePreviewStatus(`已采用${versionId === "main" ? "主版本" : "第二版本"}（已记录）`);
  } catch (error) {
    const message = String(error.message || error);
    writePreviewStatus(`采用失败：${message}`);
    window.alert(`采用失败：${message}`);
  }
}

// Build a download filename base for one turn's export: task id (path-stripped)
// plus a short run suffix so multiple turns don't collide.
function finalOutputExportName(runId) {
  const task = String(state.currentTaskId || $("taskId")?.value || "task").trim() || "task";
  const base = task.split("/").pop() || "task";
  const suffix = runId ? `_${String(runId).slice(-6)}` : "";
  return `${base}${suffix}_tables`;
}

// Append a "导出表格 Excel" button after a final_output body when its text has
// Markdown tables. Idempotent: strips a prior button first so a history reload
// doesn't stack duplicates.
function attachFinalOutputExport(container, text, filenameBase) {
  if (!container) return;
  const prior = container.querySelector(":scope > .final-output-export");
  if (prior) prior.remove();
  if (!hasMarkdownTable(text)) return;
  const bar = document.createElement("div");
  bar.className = "final-output-export";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "secondary export-tables-btn";
  btn.textContent = "⬇ 导出表格 Excel";
  btn.title = "把这段最终输出里的 Markdown 表格导出为 Excel（每张表一个 sheet）";
  btn.addEventListener("click", () => exportMarkdownTablesToXlsx(text, filenameBase));
  bar.appendChild(btn);
  container.appendChild(bar);
}

// R15 (infiagent >= 3.12.40): final_output can declare `download_paths` — task-relative
// files the framework already validated. Rendered as plain links at the very end of the
// answer body. One list per turn: both final_output versions come from the same tool
// call, so the files are shared rather than per-version.
function downloadPathsOf(item) {
  const direct = Array.isArray(item?.download_paths) ? item.download_paths : null;
  if (direct && direct.length) return direct;
  const fromProcess = (item?.process || []).find(
    (p) => p && p.type === "final_output" && Array.isArray(p.download_paths) && p.download_paths.length,
  );
  return fromProcess ? fromProcess.download_paths : [];
}

function attachDownloadLinks(container, paths, taskId) {
  if (!container) return;
  const prior = container.querySelector(":scope > .dl-links");
  if (prior) prior.remove();
  const list = (Array.isArray(paths) ? paths : []).map((p) => String(p || "").trim()).filter(Boolean);
  if (!list.length) return;
  const task = String(taskId || state.currentTaskId || $("taskId")?.value || "").trim();
  if (!task) return;

  const bar = document.createElement("div");
  bar.className = "dl-links";
  list.forEach((rel) => {
    const row = document.createElement("div");
    row.className = "dl-row";
    const link = document.createElement("a");
    link.className = "dl-link";
    link.href = `/api/v1/tasks/artifact?task_id=${encodeURIComponent(task)}&path=${encodeURIComponent(rel)}&download=true`;
    link.setAttribute("download", rel.split("/").pop() || "file");
    link.title = `下载 ${rel}`;
    const name = document.createElement("span");
    name.className = "dl-name";
    name.textContent = rel.split("/").pop() || rel;
    const hint = document.createElement("span");
    hint.className = "dl-hint";
    hint.textContent = " · 点击下载";
    link.appendChild(name);
    link.appendChild(hint);
    row.appendChild(link);
    bar.appendChild(row);
  });
  container.appendChild(bar);
}

// ==================== R14: deferred human confirmation (infiagent >= 3.12.39) ====================
// A tool declared with requires_human_confirmation does NOT run during the agent turn:
// it only registers a decision request. The console surfaces one badge button per turn
// (next to the export button) and executes/rejects through the trusted host API.
// Deliberately ONE button per turn even when the turn has two answers — the two
// versions share the same decisions, so a per-version badge would double-count.

async function refreshConfirmations(taskId) {
  const task = String(taskId || state.currentTaskId || "").trim();
  state.hcByMessage = {};
  if (!task) return;
  try {
    const data = await jsonFetch(
      `/api/v1/human-confirmations?status=all&task_id=${encodeURIComponent(task)}&limit=1000`,
    );
    const grouped = {};
    for (const req of data.requests || []) {
      const key = String(req.input_message || "");
      if (!grouped[key]) grouped[key] = [];
      grouped[key].push(req);
    }
    state.hcByMessage = grouped;
  } catch (error) {
    // A user_root on an older infiagent has no such table; the badge just stays hidden.
    state.hcByMessage = {};
  }
}

function confirmationsForMessage(inputMessage) {
  const key = String(inputMessage || "");
  if (key && state.hcByMessage[key]) return state.hcByMessage[key];
  return [];
}

// Attach the badge into the turn's own action row so it sits beside "导出表格 Excel".
function attachConfirmationBadge(wrapper, inputMessage) {
  if (!wrapper) return;
  const prior = wrapper.querySelector(":scope > .hc-badge-bar");
  if (prior) prior.remove();
  const requests = confirmationsForMessage(inputMessage);
  if (!requests.length) return;
  const pending = requests.filter((r) => String(r.status || "") === "pending");

  let bar = wrapper.querySelector(":scope > .final-output-export");
  let ownBar = false;
  if (!bar) {
    bar = document.createElement("div");
    bar.className = "final-output-export hc-badge-bar";
    ownBar = true;
  }
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = pending.length ? "secondary hc-badge hc-badge-pending" : "secondary hc-badge";
  btn.textContent = pending.length
    ? `⚠ 待确认决策 ${pending.length}`
    : `✓ 决策记录 ${requests.length}`;
  btn.title = pending.length
    ? "该轮有需要人工确认后才真正执行的操作，点击查看决策参数并确认或拒绝"
    : "该轮的决策已全部处理，点击查看处理结果";
  btn.addEventListener("click", () => openConfirmationPanel(inputMessage));
  bar.appendChild(btn);
  if (ownBar) wrapper.appendChild(bar);
}

const HC_STATUS_LABEL = {
  pending: "待确认",
  approved: "已批准·未执行",
  executing: "执行中",
  executed: "已执行",
  rejected: "已拒绝",
  failed: "执行失败",
  outcome_unknown: "结果未知·需核对",
  reconciling: "核对中",
};

// 4.5.0 (R01): answered / approved / executed are three separate commit points; show each one.
function buildCommitPoints(req) {
  const points = req.commit_points || {};
  const row = document.createElement("div");
  row.className = "hc-points";
  const items = [
    ["已回答", points.answered ? points.answered.at : null],
    ["已批准", points.approved ? points.approved.at : null],
    ["已执行成功", points.executed ? points.executed.at : null],
  ];
  items.forEach(([label, at]) => {
    const span = document.createElement("span");
    span.className = at ? "hc-point done" : "hc-point";
    span.textContent = at ? `✓ ${label}` : `○ ${label}`;
    if (at) span.title = String(at);
    row.appendChild(span);
  });
  if (points.executed && points.executed.reconciled) {
    const tag = document.createElement("span");
    tag.className = "hc-point done";
    tag.textContent = "（人工核对确认）";
    row.appendChild(tag);
  }
  if (req.attempt) {
    const tag = document.createElement("span");
    tag.className = "hc-point";
    tag.textContent = `第 ${req.attempt} 次执行`;
    row.appendChild(tag);
  }
  return row;
}

// Heuristic: which argument values should offer preview/download. Only strings that
// look like a file path AND carry a file extension, so ordinary text isn't decorated.
function looksLikeFilePath(value) {
  const text = String(value || "").trim();
  if (!text || text.length > 500 || /[\n\r]/.test(text)) return false;
  if (!/[\/\\]/.test(text) && !/^[\w.\-]+\.[A-Za-z0-9]{1,8}$/.test(text)) return false;
  return /\.[A-Za-z0-9]{1,8}$/.test(text);
}

function collectPathArguments(node, prefix, out) {
  if (node === null || node === undefined) return out;
  if (typeof node === "string") {
    if (looksLikeFilePath(node)) out.push({ key: prefix || "(值)", value: node });
    return out;
  }
  if (Array.isArray(node)) {
    node.forEach((child, index) => collectPathArguments(child, `${prefix}[${index}]`, out));
    return out;
  }
  if (typeof node === "object") {
    Object.entries(node).forEach(([key, child]) =>
      collectPathArguments(child, prefix ? `${prefix}.${key}` : key, out),
    );
  }
  return out;
}

function openConfirmationPanel(inputMessage) {
  const requests = confirmationsForMessage(inputMessage);
  const mask = document.createElement("div");
  mask.className = "hc-mask";
  const modal = document.createElement("div");
  modal.className = "hc-modal";
  const pendingCount = requests.filter((r) => String(r.status || "") === "pending").length;
  const head = document.createElement("div");
  head.className = "hc-head";
  head.innerHTML = `<h3>待确认决策</h3><span class="hc-sub">共 ${requests.length} 条，其中待确认 ${pendingCount} 条。确认后才会真正执行该操作。</span>`;
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "secondary hc-close";
  closeBtn.textContent = "关闭";
  closeBtn.addEventListener("click", () => mask.remove());
  head.appendChild(closeBtn);
  modal.appendChild(head);

  const list = document.createElement("div");
  list.className = "hc-list";
  requests.forEach((req) => list.appendChild(buildConfirmationCard(req, inputMessage)));
  modal.appendChild(list);

  mask.appendChild(modal);
  mask.addEventListener("click", (event) => {
    if (event.target === mask) mask.remove();
  });
  document.body.appendChild(mask);
}

function buildConfirmationCard(req, inputMessage) {
  const card = document.createElement("div");
  card.className = "hc-card";
  card.dataset.requestId = String(req.request_id || "");
  const status = String(req.status || "");

  const top = document.createElement("div");
  top.className = "hc-card-head";
  const name = document.createElement("span");
  name.className = "hc-tool";
  name.textContent = String(req.tool_name || "(未知工具)");
  const chip = document.createElement("span");
  chip.className = `hc-chip hc-chip-${status || "unknown"}`;
  chip.textContent = HC_STATUS_LABEL[status] || status || "未知";
  top.appendChild(name);
  top.appendChild(chip);
  card.appendChild(top);

  if (req.tool_description) {
    const desc = document.createElement("div");
    desc.className = "hc-desc";
    desc.textContent = String(req.tool_description);
    card.appendChild(desc);
  }

  const metaLine = document.createElement("div");
  metaLine.className = "hc-meta";
  metaLine.textContent = `request_id: ${req.request_id || ""} · 登记于 ${req.created_at || ""}`;
  card.appendChild(metaLine);
  card.appendChild(buildCommitPoints(req));

  card.appendChild(buildJsonBlock("决策参数（工具执行参数）", req.arguments));
  if (req.evidence && Object.keys(req.evidence).length) {
    card.appendChild(buildJsonBlock("支持证据 evidence", req.evidence));
  }

  // File-path arguments get preview/download through the server, which re-validates
  // the value against the stored record before touching the filesystem.
  const paths = collectPathArguments(req.arguments, "", []);
  collectPathArguments(req.evidence, "", paths);
  if (paths.length) {
    const files = document.createElement("div");
    files.className = "hc-files";
    const label = document.createElement("div");
    label.className = "hc-block-title";
    label.textContent = "参数中的文件";
    files.appendChild(label);
    paths.forEach((entry) => {
      const row = document.createElement("div");
      row.className = "hc-file-row";
      const nameEl = document.createElement("code");
      nameEl.className = "hc-file-path";
      nameEl.textContent = `${entry.key}: ${entry.value}`;
      const base = `/api/v1/human-confirmations/file?request_id=${encodeURIComponent(req.request_id || "")}&value=${encodeURIComponent(entry.value)}`;
      const preview = document.createElement("a");
      preview.className = "hc-file-btn";
      preview.textContent = "预览";
      preview.href = base;
      preview.target = "_blank";
      preview.rel = "noopener";
      const download = document.createElement("a");
      download.className = "hc-file-btn";
      download.textContent = "下载";
      download.href = `${base}&download=true`;
      row.appendChild(nameEl);
      row.appendChild(preview);
      row.appendChild(download);
      files.appendChild(row);
    });
    card.appendChild(files);
  }

  if (req.decision) {
    card.appendChild(buildJsonBlock("处理结果 decision", req.decision));
  }
  if (req.execution_result) {
    card.appendChild(buildJsonBlock("真实执行结果 execution_result", req.execution_result));
  }
  if (req.error) {
    const err = document.createElement("div");
    err.className = "hc-error";
    err.textContent = String(req.error);
    card.appendChild(err);
  }

  const note = document.createElement("div");
  note.className = "hc-note";
  card.appendChild(note);

  if (status === "outcome_unknown" || status === "reconciling") {
    // The executor vanished mid-run: the side effect may or may not have happened. Never re-run
    // blindly — the operator checks the downstream system and records the verdict (host-only reconcile).
    const warn = document.createElement("div");
    warn.className = "hc-error";
    warn.textContent = "执行中途失联，外部操作是否已经发生未知。请到下游系统核对后选择结论；系统不会自动重试。";
    card.appendChild(warn);
    const evidence = document.createElement("input");
    evidence.className = "hc-reason";
    evidence.placeholder = "核对依据（如下游单号，可留空）";
    card.appendChild(evidence);
    const acts = document.createElement("div");
    acts.className = "hc-acts";
    const buttons = [];
    [
      ["executed", "已发生（记为已执行）", "hc-confirm"],
      ["retry", "未发生，重新执行", "secondary"],
      ["failed", "未成功（记为失败）", "secondary hc-reject"],
    ].forEach(([decision, label, cls]) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = cls;
      btn.textContent = label;
      btn.addEventListener("click", () =>
        reconcileConfirmation(card, req, decision, evidence.value || "", inputMessage, buttons, note),
      );
      buttons.push(btn);
      acts.appendChild(btn);
    });
    card.appendChild(acts);
  }

  if (status === "pending" || status === "approved") {
    const reason = document.createElement("textarea");
    reason.className = "hc-reason";
    reason.rows = 2;
    reason.placeholder = "拒绝原因（可留空，不强制填写）";
    card.appendChild(reason);

    const acts = document.createElement("div");
    acts.className = "hc-acts";
    const ok = document.createElement("button");
    ok.type = "button";
    ok.className = "hc-confirm";
    ok.textContent = "确认并执行";
    const no = document.createElement("button");
    no.type = "button";
    no.className = "secondary hc-reject";
    no.textContent = "拒绝";
    ok.addEventListener("click", () =>
      decideConfirmation(card, req, "execute", "", inputMessage, [ok, no], note),
    );
    no.addEventListener("click", () =>
      decideConfirmation(card, req, "reject", reason.value || "", inputMessage, [ok, no], note),
    );
    acts.appendChild(ok);
    acts.appendChild(no);
    card.appendChild(acts);
  }
  return card;
}

function buildJsonBlock(title, value) {
  const block = document.createElement("div");
  block.className = "hc-block";
  const label = document.createElement("div");
  label.className = "hc-block-title";
  label.textContent = title;
  const pre = document.createElement("pre");
  pre.className = "hc-json";
  try {
    pre.textContent = JSON.stringify(value, null, 2);
  } catch (error) {
    pre.textContent = String(value);
  }
  block.appendChild(label);
  block.appendChild(pre);
  return block;
}

async function decideConfirmation(card, req, action, reason, inputMessage, buttons, note) {
  const confirmText =
    action === "execute"
      ? `确认执行 ${req.tool_name}？这会真正产生副作用（调用真实工具）。`
      : `拒绝 ${req.tool_name}？不会执行任何操作。`;
  if (!window.confirm(confirmText)) return;
  buttons.forEach((b) => (b.disabled = true));
  note.textContent = action === "execute" ? "正在执行…" : "正在拒绝…";
  try {
    const url =
      action === "execute"
        ? "/api/v1/human-confirmations/execute"
        : "/api/v1/human-confirmations/reject";
    const revision = req.revision === undefined || req.revision === null ? {} : { expected_revision: Number(req.revision) };
    const body =
      action === "execute"
        ? { request_id: req.request_id, ...revision }
        : { request_id: req.request_id, reason: reason || "", ...revision };
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const text = await res.text();
    if (!res.ok) throw new Error(text || `HTTP ${res.status}`);
    const data = text ? JSON.parse(text) : {};
    const outcome = String(data.status || "");
    if (data.error_code === "OUTCOME_UNKNOWN") {
      note.textContent = "上一次执行结果未知，不会重复执行：请先核对";
    } else if (outcome === "not_executable" || outcome === "not_rejectable") {
      // Another operator (or tab) won the race for this pending row.
      note.textContent = `该请求已被处理，当前状态：${HC_STATUS_LABEL[data.request?.status] || data.request?.status || "未知"}`;
    } else if (action === "execute") {
      const st = String(data.request?.status || "");
      note.textContent = st === "failed" ? "已执行，但工具报错（见执行结果）" : "已确认并真实执行";
    } else {
      note.textContent = data.reason_provided ? "已拒绝（含原因）" : "已拒绝（未填原因，已记默认标记）";
    }
    await rebuildConfirmationCard(card, req, inputMessage, note);
  } catch (error) {
    const message = String(error.message || error);
    note.textContent = `失败：${message}`;
    buttons.forEach((b) => (b.disabled = false));
  }
}

async function rebuildConfirmationCard(card, req, inputMessage, note) {
  await refreshConfirmations(state.currentTaskId);
  const fresh = confirmationsForMessage(inputMessage).find(
    (r) => String(r.request_id) === String(req.request_id),
  );
  if (fresh) {
    const rebuilt = buildConfirmationCard(fresh, inputMessage);
    const keep = note.textContent;
    const newNote = rebuilt.querySelector(".hc-note");
    if (newNote) newNote.textContent = keep;
    card.replaceWith(rebuilt);
  }
  repaintConfirmationBadges();
}

// 4.5.0 (R01): host-only reconcile of an execution whose outcome is unknown. "retry" first records
// the verdict (back to approved), then executes once more with a fresh attempt / idempotency key.
async function reconcileConfirmation(card, req, decision, evidenceRef, inputMessage, buttons, note) {
  const confirmText =
    decision === "executed"
      ? `确认 ${req.tool_name} 已在下游发生？将记为已执行，不会再次执行。`
      : decision === "retry"
        ? `确认 ${req.tool_name} 在下游没有发生？将重新执行一次（新的幂等键）。`
        : `确认 ${req.tool_name} 没有成功？将记为执行失败。`;
  if (!window.confirm(confirmText)) return;
  buttons.forEach((b) => (b.disabled = true));
  note.textContent = "正在提交核对…";
  try {
    const res = await fetch(`/api/v1/human-confirmations/${encodeURIComponent(req.request_id || "")}/reconcile`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: Number(req.revision || 0), decision, evidence_ref: evidenceRef || null }),
    });
    const payload = await parseResponsePayload(res);
    if (!res.ok) throw new Error(errorMessageFromPayload(payload) || `HTTP ${res.status}`);
    let text = decision === "executed" ? "已核对：记为已执行" : decision === "failed" ? "已核对：记为执行失败" : "已核对：未发生";
    if (decision === "retry") {
      const after = payload.request || {};
      const execRes = await fetch("/api/v1/human-confirmations/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: req.request_id, expected_revision: Number(after.revision || 0) }),
      });
      const execPayload = await parseResponsePayload(execRes);
      if (!execRes.ok) throw new Error(errorMessageFromPayload(execPayload) || `HTTP ${execRes.status}`);
      const st = String(execPayload.request?.status || "");
      text = st === "executed" ? "已重新执行并成功" : `重新执行后状态：${HC_STATUS_LABEL[st] || st || execPayload.status}`;
    }
    note.textContent = text;
    await rebuildConfirmationCard(card, req, inputMessage, note);
  } catch (error) {
    note.textContent = `失败：${String(error.message || error)}`;
    buttons.forEach((b) => (b.disabled = false));
  }
}

// Refresh every rendered turn's badge from the current cache without reloading history.
function repaintConfirmationBadges() {
  const container = $("messages");
  if (!container) return;
  container.querySelectorAll("[data-hc-input-message]").forEach((wrapper) => {
    attachConfirmationBadge(wrapper, wrapper.dataset.hcInputMessage || "");
  });
}

async function uploadFiles() {
  const taskId = ensureCurrentTaskId();
  await ensureTask();
  const files = $("uploadFiles").files;
  if (!files || !files.length) {
    prettyWrite({ status: "error", error: "No files selected" });
    return;
  }
  const form = new FormData();
  form.append("task_id", taskId);
  form.append("dest_subdir", ".upload");
  for (const file of files) {
    form.append("files", file, file.name);
  }
  const data = await uploadFetch("/api/v1/tasks/upload-files", form);
  prettyWrite(data);
}

async function listUploads() {
  if (!state.currentTaskId) return;
  const data = await jsonFetch(`/api/v1/tasks/uploads?task_id=${encodeURIComponent(state.currentTaskId)}`);
  prettyWrite(data);
}

async function importPath() {
  const taskId = ensureCurrentTaskId();
  await ensureTask();
  const localPath = $("importPath").value.trim();
  if (!localPath) {
    prettyWrite({ status: "error", error: "import path is empty" });
    return;
  }
  const data = await jsonFetch("/api/v1/tasks/import-path", "POST", {
    task_id: taskId,
    local_path: localPath,
    dest_subdir: ".upload",
  });
  prettyWrite(data);
}

async function uploadUserRoot() {
  const archiveInput = $("userRootArchive");
  const folderInput = $("userRootFolder");
  const uploadButton = $("uploadUserRootBtn");
  const archive = archiveInput.files && archiveInput.files[0] ? archiveInput.files[0] : null;
  const folderFiles = folderInput.files || [];

  if (!archive && !folderFiles.length) {
    setUserRootUploadStatus("Please select a user_root zip or folder first.", "error");
    return;
  }

  const mode = $("userRootUploadMode") ? $("userRootUploadMode").value : "replace";
  const confirmText =
    mode === "replace"
      ? "Replace mode: this will DELETE every file under the active user_root on the server, then copy the upload. Running tasks must be stopped first. Continue?"
      : mode === "merge_overwrite"
        ? "Merge (overwrite): uploaded files will be added, and same-name files on the server will be overwritten. Other existing files are kept. Continue?"
        : "Merge (skip): only files that do not exist yet on the server will be added; same-name files are kept untouched. Continue?";
  const ok = window.confirm(confirmText);
  if (!ok) return;

  const label = archive ? archive.name : `${folderFiles.length} files`;
  const startedAt = Date.now();
  let phase = `Preparing upload for ${label}`;
  let lastProgress = "";
  const renderStatus = () => {
    const suffix = `Elapsed: ${formatElapsed(startedAt)}.`;
    setUserRootUploadStatus([phase, lastProgress, suffix].filter(Boolean).join("\n"));
  };
  const timer = window.setInterval(renderStatus, 1000);
  archiveInput.disabled = true;
  folderInput.disabled = true;
  if (uploadButton) {
    uploadButton.disabled = true;
    uploadButton.textContent = "Uploading...";
  }
  renderStatus();

  try {
    const form = new FormData();
    if (archive) {
      form.append("archive", archive, archive.name);
    }
    for (const file of folderFiles) {
      form.append("files", file, file.webkitRelativePath || file.name);
    }
    form.append("strip_top_level", $("stripUserRootTopLevel").checked ? "true" : "false");
    form.append("mode", mode);

    const data = await uploadFetchWithProgress("/api/v1/runtime/upload-user-root", form, {
      onProgress: ({ loaded, total, lengthComputable }) => {
        phase = `Uploading ${label} to server`;
        if (lengthComputable && total > 0) {
          const percent = Math.min(100, (loaded / total) * 100);
          lastProgress = `${percent.toFixed(1)}% (${formatBytes(loaded)} / ${formatBytes(total)})`;
        } else {
          lastProgress = `${formatBytes(loaded)} uploaded`;
        }
        renderStatus();
      },
      onServerWait: () => {
        phase =
          mode === "replace"
            ? "Upload complete. Server is extracting and replacing active user_root..."
            : "Upload complete. Server is extracting and merging into active user_root...";
        lastProgress = "Large user_root folders can spend most of their time in this server-side step.";
        renderStatus();
      },
    });
    archiveInput.value = "";
    folderInput.value = "";
    prettyWrite(data);
    const summary =
      data.mode === "replace"
        ? `Replaced ${data.active_user_root || "active user_root"}; copied top-level entries: ${(data.copied_top_level || []).join(", ") || "(none)"}.`
        : `Merged into ${data.active_user_root || "active user_root"} (${data.mode}): wrote ${data.written_file_count ?? 0} files, skipped ${data.skipped_file_count ?? 0} same-name files.`;
    setUserRootUploadStatus(`Upload success in ${formatElapsed(startedAt)}. ${summary}`, "success");
    $("messages").innerHTML = "";
    setCurrentTask("");
    await refreshRuntime();
    await refreshTasks(true);
    if (state.currentTaskId) {
      await loadHistory();
    }
  } catch (error) {
    setUserRootUploadStatus(`Upload failed after ${formatElapsed(startedAt)}: ${String(error.message || error)}`, "error");
  } finally {
    window.clearInterval(timer);
    archiveInput.disabled = false;
    folderInput.disabled = false;
    if (uploadButton) {
      uploadButton.disabled = false;
      uploadButton.textContent = "Upload User Root";
    }
  }
}

async function updateInfiAgentPackage() {
  const button = $("updateInfiAgentBtn");
  const payload = {
    index_url: $("pipIndexUrl").value.trim(),
    trusted_host: $("pipTrustedHost").value.trim(),
    version: $("pipVersion").value.trim(),
    upgrade: $("pipUpgrade").checked,
    timeout_sec: 900,
  };
  if (!payload.index_url) {
    const ok = window.confirm("No pip index URL was provided. Continue with the container's default pip source?");
    if (!ok) return;
  }
  const target = payload.version ? `infiagent==${payload.version}` : "latest infiagent";
  const ok = window.confirm(`Update ${target} inside this running container? user_root data will not be changed.`);
  if (!ok) return;

  if (button) {
    button.disabled = true;
    button.textContent = "Updating...";
  }
  setPipUpdateStatus(`Running pip install for ${target}. This can take a few minutes...`);
  try {
    const data = await jsonFetch("/api/v1/runtime/update-infiagent", "POST", payload);
    setPipUpdateStatus(
      [
        `Update success: ${data.before_version || "(unknown)"} -> ${data.after_version || "(unknown)"}`,
        data.restart_note || "",
        data.stderr_tail ? `stderr tail:\n${data.stderr_tail}` : "",
      ]
        .filter(Boolean)
        .join("\n\n"),
      "success",
    );
    prettyWrite(data);
    await refreshRuntime();
  } catch (error) {
    setPipUpdateStatus(`Update failed: ${String(error.message || error)}`, "error");
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "Update Package";
    }
  }
}

function bindAdvancedPersistence() {
  [
    "systemAddPath",
    "reasoningMode",
    "reasoningEffort",
    "thinkingSteps",
    "historyRecentItems",
    "autoMode",
    "sandboxEnabled",
    "visibleSkills",
    "maxTurns",
    "autoResumeAttempts",
    "autoResumeDelaySec",
    "forceNew",
    "includeTrace",
    "queueResumeIfNeeded",
    "sdkOverrides",
  ].forEach((id) => {
    const node = $(id);
    if (!node) return;
    const eventName = node.tagName === "SELECT" ? "change" : "input";
    node.addEventListener(eventName, () => {
      persistCurrentSystemSettings();
      updateApiPreview();
    });
  });
}

function bindApiPreviewRefresh() {
  ["taskId", "messageInput", "agentSystem", "agentName"].forEach((id) => {
    const node = $(id);
    if (!node) return;
    const eventName = node.tagName === "SELECT" ? "change" : "input";
    node.addEventListener(eventName, () => updateApiPreview());
  });
  const openOutputAnd = (mode) => {
    if (!sidePanelOpen("outputPanel")) toggleSidePanel("outputPanel");
    updateApiPreview(mode);
  };
  $("previewStreamBtn").addEventListener("click", () => openOutputAnd("stream"));
  $("previewRunBtn").addEventListener("click", () => openOutputAnd("run"));
  $("previewQueueBtn").addEventListener("click", () => openOutputAnd("queue"));
  $("previewEnsureBtn").addEventListener("click", () => openOutputAnd("ensure"));
}

$("agentSystem").addEventListener("change", async () => {
  if (state.lastAgentSystem && state.lastAgentSystem !== $("agentSystem").value) {
    persistCurrentSystemSettings(state.lastAgentSystem);
  }
  state.lastAgentSystem = $("agentSystem").value;
  writeLastAgentSystem(state.lastAgentSystem);
  await refreshAgents();
  loadCurrentSystemSettings();
  updateApiPreview();
});

$("agentName").addEventListener("change", () => {
  writeLastAgentName($("agentSystem").value, $("agentName").value);
  syncAgentSelectTitles();
  updateApiPreview();
});

$("refreshRuntimeBtn").addEventListener("click", refreshRuntime);
$("refreshTasksBtn").addEventListener("click", async () => {
  await refreshWorkspaces();
  await refreshTasks();
});
$("newTaskBtn").addEventListener("click", async () => {
  const button = $("newTaskBtn");
  if (button.disabled) return;
  detachStream();
  const nextId = generateTaskId();
  setCurrentTask(nextId, { pending: true });
  button.disabled = true;  // a double click in the same second would ensure the same id twice
  let data;
  try {
    data = await ensureTask({ fresh: true });
  } catch (error) {
    prettyWrite({ status: "error", error: String(error.message || error) });
    writePreviewStatus(`新建会话失败：${String(error.message || error)}`);
    return;
  } finally {
    button.disabled = false;
  }
  // select the session under the id the task list uses, so its card is highlighted
  if (data && data.task_id && data.task_id !== nextId) setCurrentTask(data.task_id);
  $("messages").innerHTML = "";
  $("messages").dataset.taskId = state.currentTaskId;
});
$("newTaskWorkspace").addEventListener("change", syncWorkspaceChoice);
$("consoleUserId").value = readConsoleUserId();
$("consoleUserId").addEventListener("change", () => {
  const user = consoleUserId();
  if (!user.error) writeConsoleUserId(user.value === null ? "" : String(user.value));
});
$("ensureTaskBtn").addEventListener("click", () => ensureTask());
$("runBtn").addEventListener("click", runOnce);
$("streamBtn").addEventListener("click", runStream);
$("queueBtn").addEventListener("click", queueMessage);
$("stopBtn").addEventListener("click", stopStream);
$("historyBtn").addEventListener("click", loadHistory);
$("snapshotBtn").addEventListener("click", () => fetchInspector("/api/v1/tasks/snapshot"));
$("pauseBtn").addEventListener("click", () => taskAction("/api/v1/tasks/pause"));
$("resumeBtn").addEventListener("click", resumeTask);
$("freshBtn").addEventListener("click", () => taskAction("/api/v1/tasks/fresh"));
$("resetBtn").addEventListener("click", () =>
  taskAction("/api/v1/tasks/reset", {
    task_id: state.currentTaskId,
    reason: "manual reset from web console",
    preserve_history: true,
    kill_background_processes: false,
  }),
);
$("uploadBtn").addEventListener("click", uploadFiles);
$("listUploadsBtn").addEventListener("click", listUploads);
$("importPathBtn").addEventListener("click", importPath);
$("uploadUserRootBtn").addEventListener("click", uploadUserRoot);
$("updateInfiAgentBtn").addEventListener("click", updateInfiAgentPackage);
$("taskFilesRefreshBtn").addEventListener("click", (event) => {
  event.stopPropagation();
  refreshTaskFiles();
});
$("taskFilesToggle").addEventListener("click", () => {
  if (toggleSidePanel("taskFilesPanel")) refreshTaskFiles();
});
$("outputToggle").addEventListener("click", () => toggleSidePanel("outputPanel"));

bindAdvancedPersistence();
bindApiPreviewRefresh();

(async function bootstrap() {
  try {
    await refreshRuntime();
    await refreshWorkspaces();
    await refreshTasks(true);
    if (state.currentTaskId) {
      const info = await loadHistory();
      // Reconnect to an in-flight run after a page refresh.
      if (info && info.live_run_id) {
        attachLiveStream(info.live_run_id);
      }
    }
    updateApiPreview();
  } catch (error) {
    prettyWrite({ status: "error", error: String(error) });
  }
})();
