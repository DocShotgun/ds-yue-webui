/* ds-yue-webui shared helpers: fetch wrappers, SSE, formatting, ABC rendering. */
"use strict";

async function errorDetail(response) {
  try {
    const data = await response.json();
    const detail = data && (data.detail || data.error);
    if (!detail) return "";
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail.map((item) => `${(item.loc || []).join(".")}: ${item.msg}`).join("; ");
    }
    return JSON.stringify(detail);
  } catch (_) {
    return "";
  }
}

const api = {
  async get(url) {
    const response = await fetch(url);
    if (!response.ok) throw new Error((await errorDetail(response)) || response.statusText);
    return response.json();
  },
  async post(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body === undefined ? {} : body),
    });
    if (!response.ok) throw new Error((await errorDetail(response)) || response.statusText);
    return response.json();
  },
  async upload(url, formData) {
    const response = await fetch(url, { method: "POST", body: formData });
    if (!response.ok) throw new Error((await errorDetail(response)) || response.statusText);
    return response.json();
  },
  async del(url) {
    const response = await fetch(url, { method: "DELETE" });
    if (!response.ok) throw new Error((await errorDetail(response)) || response.statusText);
    return response.json();
  },
};

/* Subscribe to a job's event stream; callback receives every job snapshot.
   Returns the EventSource (call .close() to detach). */
function watchJob(jobId, onJob) {
  const source = new EventSource(`/api/jobs/${jobId}/events`);
  source.onmessage = (event) => {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (_) {
      return;
    }
    if (data.missing) {
      onJob(null);
      source.close();
      return;
    }
    onJob(data);
  };
  source.onerror = () => {
    /* Reconnect is automatic; the final event closes the stream server-side. */
  };
  return source;
}

function fmtDate(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.max(0, Math.round(seconds));
  const m = Math.floor(s / 60);
  return m > 0 ? `${m}m ${String(s % 60).padStart(2, "0")}s` : `${s}s`;
}

/* Debounce helper for the ABC previews. */
function debounce(fn, ms) {
  let timer = null;
  return function (...args) {
    clearTimeout(timer);
    timer = setTimeout(() => fn.apply(this, args), ms);
  };
}

/* Render ABC text into an element with abcjs (vendored). */
function renderAbc(element, text) {
  if (!window.ABCJS || !element) return;
  element.innerHTML = "";
  try {
    window.ABCJS.renderAbc(element, text, { responsive: "resize", paddingtop: 0, paddingbottom: 0 });
  } catch (error) {
    element.innerHTML = "";
    const note = document.createElement("div");
    note.className = "abc-error";
    note.textContent = "abcjs cannot preview this score: " + error.message;
    element.appendChild(note);
  }
}

function renderAbcById(id, text) {
  renderAbc(document.getElementById(id), text);
}

function jsonString(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch (_) {
    return String(value);
  }
}

/* Insert a structure tag ([Chorus] etc.) on its own line at the caret. */
function insertTagAtCursor(area, tag, setValue) {
  if (!area) {
    setValue(tag + "\n");
    return;
  }
  const start = area.selectionStart ?? area.value.length;
  const end = area.selectionEnd ?? start;
  const lineStart = area.value.lastIndexOf("\n", start - 1) + 1;
  let lineEnd = area.value.indexOf("\n", start);
  if (lineEnd === -1) lineEnd = area.value.length;
  const beforeCursorBlank = area.value.slice(lineStart, start).trim() === "";
  const atLineStart = start === lineStart;
  const nextIsNewline = start === area.value.length || area.value[start] === "\n";
  const text = (beforeCursorBlank && start === end ? (atLineStart ? "" : "\n") : "\n")
             + tag + (nextIsNewline ? "" : "\n");
  area.setRangeText(text, start, end, "end");
  setValue(area.value);
  const caret = start + text.length;
  area.focus();
  area.setSelectionRange(caret, caret);
}

/* Submit the enclosing form when Enter is pressed outside textareas/buttons. */
function enterSubmits(event, submit) {
  const tag = (event.target && event.target.tagName || "").toLowerCase();
  if (tag === "textarea" || event.target.closest("button, a, select, input[type='file']")) return;
  event.preventDefault();
  submit();
}
