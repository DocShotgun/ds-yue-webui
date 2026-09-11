/* ds-yue-webui API helpers: fetch wrappers, SSE, formatting. */
"use strict";

async function errorDetail(response) {
  try {
    const data = await response.json();
    return (data && (data.detail || data.error)) || "";
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

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
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

function fmtElapsed(startedAt) {
  if (!startedAt) return "—";
  return fmtDuration((Date.now() - startedAt * 1000) / 1000);
}

/* Debounce helper for the ABC preview. */
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

function jsonString(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch (_) {
    return String(value);
  }
}
