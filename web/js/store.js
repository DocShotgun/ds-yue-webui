/* ds-yue-webui global store: tabs, toasts, job watching, library, detail modal.

   The job panel follows the newest submission; every other active job is
   tracked by a light background poll and announces its completion via a
   clickable toast. The queue is strictly serial on the server, so pending
   jobs surface their queue position from the API. */
"use strict";

const JOB_TERMINAL = ["done", "failed", "cancelled"];

document.addEventListener("alpine:init", () => {
  Alpine.store("ui", {
    tab: "generate",
    config: null,
    now: Date.now(),

    /* --- jobs --- */
    jobs: [],
    counts: {},
    activeJobId: null,
    jobDetail: null,

    /* --- library (shared across tabs) --- */
    library: { songs: [], transcripts: [], plans: [] },
    audioFormat: "flac",

    /* --- theme (per-browser preference; system follows the OS) --- */
    theme: "system",

    /* --- detail modal --- */
    detail: null,
    detailKind: null,
    detailName: null,
    detailOpen: false,

    /* --- cross-tab requests --- */
    editRequest: null,

    /* --- toasts --- */
    toasts: [],
    _toastSeq: 1,

    /* --- private plumbing (not reactive state) --- */
    _jobSource: null,
    _watchFinished: null,
    _pollTimer: null,
    _statuses: {},
    _mql: null,

    bootstrap() {
      setInterval(() => { this.now = Date.now(); }, 1000);
      this.initTheme();
      this.loadConfig();
      this.refreshJobs();
      this.refreshLibrary();
    },

    /* ---------- theme ---------- */
    initTheme() {
      const stored = localStorage.getItem("yue-theme");
      if (stored === "system" || stored === "light" || stored === "dark") this.theme = stored;
      this._mql = window.matchMedia("(prefers-color-scheme: dark)");
      const onChange = () => { if (this.theme === "system") this.applyTheme(); };
      if (this._mql.addEventListener) this._mql.addEventListener("change", onChange);
      else if (this._mql.addListener) this._mql.addListener(onChange);
      this.applyTheme();
    },
    applyTheme() {
      const effective = this.theme === "system"
        ? (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
        : this.theme;
      document.documentElement.classList.toggle("light", effective === "light");
    },
    cycleTheme() {
      const order = ["system", "light", "dark"];
      this.theme = order[(order.indexOf(this.theme) + 1) % order.length];
      localStorage.setItem("yue-theme", this.theme);
      this.applyTheme();
    },

    async loadConfig() {
      try {
        this.config = await api.get("/api/config");
      } catch (error) {
        this.notify("Server config unavailable: " + error.message, "err");
      }
    },

    /* ---------- formatting helpers for templates ---------- */
    fmtDate: (ts) => fmtDate(ts),
    fmtDuration: (s) => fmtDuration(s),
    jsonString: (v) => jsonString(v),
    /* Elapsed time for the job panel: live while running, frozen at the total once terminal. */
    jobElapsed(job) {
      if (!job || !job.started_at) return "—";
      const active = !JOB_TERMINAL.includes(job.status);
      const end = active ? this.now : (job.finished_at || job.started_at);
      return fmtDuration((end * 1000 - job.started_at * 1000) / 1000);
    },
    nameOf(outputDir) {
      if (!outputDir) return "";
      const parts = String(outputDir).replace(/\\/g, "/").split("/");
      return parts[parts.length - 1];
    },

    /* ---------- toasts ---------- */
    notify(message, kind = "ok", opts = {}) {
      const id = this._toastSeq++;
      this.toasts.push({ id, message, kind, action: opts.action || null });
      setTimeout(() => this.dismissToast(id), kind === "err" ? 9000 : 5000);
    },
    dismissToast(id) {
      this.toasts = this.toasts.filter((toast) => toast.id !== id);
    },
    runToastAction(toast) {
      if (toast.action) {
        toast.action.run();
        this.dismissToast(toast.id);
      }
    },

    /* ---------- tabs ---------- */
    switchTab(tab) {
      this.tab = tab;
      if (tab === "library" || tab === "edit") this.refreshLibrary();
      if (tab === "edit") window.dispatchEvent(new CustomEvent("tab:edit"));
      if (tab === "diagnostics") window.dispatchEvent(new CustomEvent("tab:diagnostics"));
    },
    requestEdit(name) {
      const seq = this.editRequest ? this.editRequest.seq + 1 : 1;
      this.editRequest = { name, seq };
      this.switchTab("edit");
    },

    /* ---------- jobs ---------- */
    activeCount() {
      return (this.counts.pending || 0) + (this.counts.running || 0);
    },

    async refreshJobs() {
      try {
        await this._applyJobs(await api.get("/api/jobs?limit=100"));
      } catch (error) {
        this.notify("Could not refresh jobs: " + error.message, "err");
      }
    },
    refreshJobsQuiet() {
      api.get("/api/jobs?limit=100").then((data) => this._applyJobs(data)).catch(() => {});
    },
    _applyJobs(data) {
      this.jobs = data.jobs;
      this.counts = data.counts;
      for (const job of data.jobs) {
        const previous = this._statuses[job.id];
        if (previous && previous !== job.status && JOB_TERMINAL.includes(job.status)
            && job.id !== this.activeJobId) {
          this._announce(job);
        }
        this._statuses[job.id] = job.status;
      }
      if (this.activeCount() > 0) this._ensurePolling(); else this._stopPolling();
    },
    _announce(job) {
      const label = job.name ? `"${job.name}"` : `${job.kind} job`;
      if (job.status === "done") {
        this.notify(`Job #${job.id} (${label}) finished`, "ok",
                     { action: { label: "View", run: () => this.watch(job.id) } });
      } else if (job.status === "failed") {
        this.notify(`Job #${job.id} (${label}) failed: ${job.error || "unknown error"}`, "err",
                     { action: { label: "View", run: () => this.watch(job.id) } });
      } else {
        this.notify(`Job #${job.id} (${label}) was cancelled`, "ok");
      }
    },
    _ensurePolling() {
      if (this._pollTimer) return;
      this._pollTimer = setInterval(() => this.refreshJobsQuiet(), 8000);
    },
    _stopPolling() {
      if (this._pollTimer) {
        clearInterval(this._pollTimer);
        this._pollTimer = null;
      }
    },

    watch(jobId, opts = {}) {
      this.closeJobPanel();
      this.activeJobId = jobId;
      this.jobDetail = null;
      this._watchFinished = opts.onFinished || null;
      this._ensurePolling();
      this._jobSource = watchJob(jobId, (job) => {
        if (!job) {
          this.notify(`Job #${jobId} is no longer in the history`, "err");
          this.closeJobPanel();
          return;
        }
        this.jobDetail = job;
        this._statuses[job.id] = job.status;
        this.refreshJobsQuiet();
        if (JOB_TERMINAL.includes(job.status)) {
          const finished = this._watchFinished;
          this._jobSource.close();
          this._jobSource = null;
          this._watchFinished = null;
          this._announce(job);
          this.refreshLibrary();
          this.refreshJobs();
          if (finished) finished(job);
        }
      });
    },
    closeJobPanel() {
      if (this._jobSource) {
        this._jobSource.close();
        this._jobSource = null;
      }
      this._watchFinished = null;
      this.activeJobId = null;
      this.jobDetail = null;
    },

    jobActive(job) {
      return !!job && !JOB_TERMINAL.includes(job.status);
    },
    jobStageLabel(job) {
      if (!job) return "";
      if (job.status === "pending") {
        const queue = job.queue;
        return queue && queue.total > 1
          ? `queued · position ${queue.position} of ${queue.total}` : "queued";
      }
      /* terminal states are shown by the status pill; avoid echoing them here */
      if (JOB_TERMINAL.includes(job.status)) return "";
      const progress = job.progress || {};
      if (job.kind === "generate" || job.kind === "plan") {
        const labels = { abc: "planning score", semantic: "generating song",
                         nar: "synthesizing audio", vae: "decoding audio" };
        return labels[progress.stage] || "starting worker";
      }
      if (job.kind === "transcribe") {
        return progress.stage === "decoding" ? "decoding tokens" : (progress.stage || "transcribing");
      }
      if (job.kind === "decode") return "decoding latents";
      return "running";
    },
    /* A number when progress is quantifiable, null for indeterminate stages. */
    jobPercent(job) {
      if (!job) return null;
      if (job.status === "done") return 100;
      if (job.status !== "running") return null;
      const progress = job.progress || {};
      const stage = progress.stage || "";
      const tokens = Number(progress.tokens || 0);
      if (job.kind === "generate" || job.kind === "plan") {
        if (stage === "abc") return 5 + Math.min(tokens / 4096, 1) * (job.kind === "plan" ? 90 : 10);
        if (stage === "semantic") return 15 + Math.min(tokens / 9000, 1) * 45;
        return null;
      }
      if (job.kind === "transcribe") {
        const windows = Number(progress.windows || 0);
        if (windows > 0) return 5 + Math.min((Number(progress.window || 0) - 1) / windows, 1) * 85;
        return null;
      }
      return null;
    },

    async cancelJob(jobId) {
      try {
        await api.del(`/api/jobs/${jobId}`);
        this.notify("Cancel requested", "ok");
        this.refreshJobsQuiet();
      } catch (error) {
        this.notify("Cancel failed: " + error.message, "err");
      }
    },

    /* Submit a job; the panel follows it. opts.onFinished(job) fires at the end. */
    async submit(kind, name, params, opts = {}) {
      let data;
      try {
        data = await api.post("/api/jobs", { kind, name, params });
      } catch (error) {
        this.notify(`Could not submit job: ${error.message}`, "err");
        return null;
      }
      for (const warning of data.warnings || []) this.notify(warning, "warn");
      this.notify(`Job #${data.job.id} queued (${kind})`, "ok");
      this.refreshJobsQuiet();
      if (opts.watch !== false) this.watch(data.job.id, { onFinished: opts.onFinished || null });
      return data.job;
    },

    async clearJobs() {
      if (!confirm("Clear the entire jobs history, worker logs, and uploaded audio? Files in the Library are untouched; job records are deleted and IDs restart at #1.")) return;
      try {
        const data = await api.del("/api/jobs");
        const logs = (data.logs || []).length;
        const uploads = data.uploads || 0;
        this.notify(`Cleared ${data.cleared} job${data.cleared === 1 ? "" : "s"}, ${logs} log${logs === 1 ? "" : "s"}, ` +
                    `and ${uploads} upload${uploads === 1 ? "" : "s"}`, "ok");
        this.closeJobPanel();
        this._statuses = {};
        await this.refreshJobs();
      } catch (error) {
        this.notify("Could not clear history: " + error.message, "err");
      }
    },

    /* ---------- shared ABC services ---------- */
    async validateAbc(text) {
      try {
        const result = await api.post("/api/abc/inspect", { text });
        if (result.ok) {
          const voices = (result.report && result.report.voices) || {};
          const notes = voices.Vocal ? voices.Vocal.sounding_notes : "?";
          this.notify(`ABC valid: ${notes} vocal notes, ${result.has_chords ? "has chords" : "chord-free"}`, "ok");
        } else {
          this.notify("ABC check failed: " + result.error, "err");
        }
        return result;
      } catch (error) {
        this.notify("Validation failed: " + error.message, "err");
        return null;
      }
    },
    async stripChords(text) {
      try {
        const result = await api.post("/api/abc/strip", { text, keep_voice: "both" });
        this.notify("Chords stripped (melody preserved)", "ok");
        return result.abc;
      } catch (error) {
        this.notify("Strip failed: " + error.message, "err");
        return null;
      }
    },

    /* ---------- library ---------- */
    async refreshLibrary() {
      try {
        const data = await api.get("/api/library");
        this.library.songs = data.songs;
        this.library.transcripts = data.transcripts;
        this.library.plans = data.plans;
      } catch (error) {
        this.notify("Could not refresh library: " + error.message, "err");
      }
    },
    async openDetail(kind, name) {
      const endpoint = kind === "song" ? "songs" : kind === "transcript" ? "transcripts" : "plans";
      try {
        this.detail = await api.get(`/api/library/${endpoint}/${encodeURIComponent(name)}`);
        this.detailKind = kind;
        this.detailName = name;
        this.detailOpen = true;
      } catch (error) {
        this.notify("Could not load details: " + error.message, "err");
      }
    },
    closeDetail() {
      this.detailOpen = false;
      this.detail = null;
      this.detailKind = null;
    },
    audioUrl(name) {
      return `/api/library/songs/${encodeURIComponent(name)}/audio?format=${this.audioFormat}`;
    },
    artifactLinks() {
      if (!this.detail || this.detailKind !== "song") return [];
      const artifacts = (this.detail.result && this.detail.result.artifacts) || {};
      return Object.keys(artifacts).map((key) => ({
        key,
        url: `/api/library/songs/${encodeURIComponent(this.detailName)}/artifacts/` +
             key.split("/").map(encodeURIComponent).join("/"),
      }));
    },
    async deleteItem(kind, name) {
      if (!confirm(`Delete ${kind} "${name}"? This cannot be undone.`)) return;
      const endpoint = kind === "song" ? "songs" : kind === "transcript" ? "transcripts" : "plans";
      try {
        await api.del(`/api/library/${endpoint}/${encodeURIComponent(name)}`);
        this.notify(`Deleted ${name}`, "ok");
        await this.refreshLibrary();
        this.refreshJobsQuiet();
      } catch (error) {
        this.notify("Delete failed: " + error.message, "err");
      }
    },
  });
});
