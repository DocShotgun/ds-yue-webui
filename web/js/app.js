/* ds-yue-webui: Alpine root component. */
"use strict";

document.addEventListener("alpine:init", () => {
  Alpine.data("app", () => ({
    tab: "generate",
    config: null,
    now: Date.now(),
    toast: null,
    toastTimer: null,

    /* --- jobs --- */
    jobs: [],
    counts: {},
    activeJobId: null,
    jobSource: null,
    jobDetail: null,

    /* --- generate form --- */
    g: {
      name: "", style: "", lyrics: "", cot: "full", seed: null, cfgScale: null,
      abcMode: "plan", abcText: "", abcPlanName: "",
      advOpen: false,
      abcAdv: {}, semAdv: {},
    },
    structureTags: ["[Intro]", "[Verse]", "[Pre-Chorus]", "[Chorus]", "[Bridge]", "[Outro]"],

    /* --- cover wizard --- */
    cover: {
      step: 1, file: null, task: "full", maxSeconds: null, name: "",
      transcripts: [], transcriptName: "", transcript: null,
      abcText: "", keepChords: false,
      style: "", lyrics: "", planName: "",
    },

    /* --- edit --- */
    edit: {
      songs: [], songName: "", song: null,
      abcOriginal: "", abcText: "",
      compareResult: null, compareError: null, allowTempoChange: false,
      style: "", lyrics: "", cot: "full", name: "",
    },

    /* --- library --- */
    library: { songs: [], transcripts: [], plans: [], loaded: false },
    detail: null, detailKind: null, detailOpen: false,
    audioFormat: "flac",
    playingName: null,

    /* --- diagnostics --- */
    diag: null, diagLoading: false,
    residencyForm: { residency: "on-demand", release_idle_minutes: 10 },

    samplingSections: ["abc", "semantic"],
    samplingFields: [
      ["temperature", "number", "temperature", "0–5, default 1.0"],
      ["top_p", "number", "top-p", "0–1, default 0.95"],
      ["top_k", "number", "top-k", "integer, default 100"],
      ["repetition_penalty", "number", "rep penalty", "> 0, default 1.2"],
      ["penalty_window", "number", "rep window", "1–100, default 50"],
      ["min_tokens", "number", "min tokens", "integer, default 0"],
      ["max_tokens", "number", "max tokens", "integer"],
    ],

    async init() {
      setInterval(() => { this.now = Date.now(); }, 1000);
      for (const [key] of this.samplingFields) {
        this.g.abcAdv[key] = null;
        this.g.semAdv[key] = null;
      }
      this.$watch("g.abcText", debounce((value) => { const el = document.getElementById("gen-abc-preview"); if (el) renderAbc(el, value); }, 300));
      this.$watch("cover.abcText", debounce((value) => { const el = document.getElementById("cover-abc-preview"); if (el) renderAbc(el, value); }, 300));
      this.$watch("edit.abcText", debounce((value) => { const el = document.getElementById("edit-abc-preview"); if (el) renderAbc(el, value); }, 300));
      try {
        this.config = await api.get("/api/config");
        this.residencyForm.residency = this.config.residency || "on-demand";
        this.residencyForm.release_idle_minutes = this.config.release_idle_minutes ?? 10;
      } catch (error) {
        this.notify("Server config unavailable: " + error.message, "err");
      }
      await Promise.all([this.refreshJobs(), this.refreshLibrary()]);
    },

    /* formatting helpers for Alpine expressions */
    fmtTime: (ts) => fmtTime(ts),
    fmtDate: (ts) => fmtDate(ts),
    fmtDuration: (s) => fmtDuration(s),
    fmtElapsed: (ts) => fmtElapsed(ts),
    jsonString: (v) => jsonString(v),

    notify(message, kind = "ok") {
      this.toast = { message, kind };
      clearTimeout(this.toastTimer);
      this.toastTimer = setTimeout(() => { this.toast = null; }, kind === "err" ? 8000 : 4000);
    },

    switchTab(tab) {
      this.tab = tab;
      if (tab === "library") this.refreshLibrary();
      if (tab === "diagnostics" && !this.diag) this.runDiagnostics();
    },

    async refreshJobs() {
      try {
        const data = await api.get("/api/jobs?limit=100");
        this.jobs = data.jobs;
        this.counts = data.counts;
        if (this.jobDetail && this.activeJobId) {
          const current = this.jobs.find((j) => j.id === this.activeJobId);
          if (current && current.status !== this.jobDetail.status) {
            this.jobDetail = current;
            if (["done", "failed", "cancelled"].includes(current.status)) {
              this.onWatchedJobFinished(current);
            }
          }
        }
      } catch (error) {
        this.notify("Could not refresh jobs: " + error.message, "err");
      }
    },

    runningCount() {
      return (this.counts.running || 0);
    },

    /* --- job watching --- */
    async watch(jobId) {
      this.activeJobId = jobId;
      this.jobDetail = null;
      if (this.jobSource) this.jobSource.close();
      this.jobSource = watchJob(jobId, async (job) => {
        if (!job) {
          this.jobDetail = null;
          return;
        }
        this.jobDetail = job;
        this.refreshJobsQuiet();
        if (["done", "failed", "cancelled"].includes(job.status)) {
          this.jobSource.close();
          this.jobSource = null;
          await this.refreshLibrary();
        }
      });
    },

    refreshJobsQuiet() {
      api.get("/api/jobs?limit=100").then((data) => {
        this.jobs = data.jobs;
        this.counts = data.counts;
      }).catch(() => {});
    },

    onWatchedJobFinished(job) {
      if (job.status === "done") this.notify("Job finished — view it in the Library", "ok");
      else if (job.status === "failed") this.notify("Job failed: " + (job.error || "unknown error"), "err");
      else this.notify("Job cancelled", "ok");
    },

    jobStageLabel() {
      const job = this.jobDetail;
      if (!job) return "";
      if (job.status === "pending") return "queued";
      if (["done", "failed", "cancelled"].includes(job.status)) return job.status;
      const progress = job.progress || {};
      if (job.kind === "generate" || job.kind === "plan") {
        const order = ["abc", "semantic", "nar", "vae"];
        const stage = progress.stage;
        if (stage && order.includes(stage)) return { abc: "planning score", semantic: "generating song", nar: "synthesizing audio", vae: "decoding audio" }[stage];
        return "starting worker";
      }
      if (job.kind === "transcribe") return progress.stage === "decoding" ? "decoding tokens" : (progress.stage || "transcribing");
      if (job.kind === "decode") return "decoding latents";
      return "running";
    },

    jobPercent() {
      const job = this.jobDetail;
      if (!job) return 0;
      if (["done"].includes(job.status)) return 100;
      if (job.status !== "running") return 5;
      const progress = job.progress || {};
      const stage = progress.stage || "";
      if (job.kind === "generate") {
        const tokens = Number(progress.tokens || 0);
        if (stage === "abc") return 5 + Math.min(tokens / 4096, 1) * 10;
        if (stage === "semantic") return 15 + Math.min(tokens / 9000, 1) * 45;
        if (stage === "nar") return 65;
        if (stage === "vae") return 85;
        return 3;
      }
      if (job.kind === "plan") return stage === "abc" ? 50 : 3;
      if (job.kind === "transcribe") {
        const window = Number(progress.window || 0);
        const windows = Number(progress.windows || 0);
        if (windows > 0 && window > 0) return 5 + Math.min((window - 1) / windows, 1) * 85;
        return 3;
      }
      if (job.kind === "decode") return 50;
      return 3;
    },

    jobActive() {
      return this.jobDetail && !["done", "failed", "cancelled"].includes(this.jobDetail.status);
    },

    async cancelWatchedJob() {
      const job = this.jobDetail;
      if (!job) return;
      try {
        await api.del(`/api/jobs/${job.id}`);
        this.notify("Cancel requested", "ok");
        this.refreshJobsQuiet();
      } catch (error) {
        this.notify("Cancel failed: " + error.message, "err");
      }
    },

    closeJobPanel() {
      if (this.jobSource) { this.jobSource.close(); this.jobSource = null; }
      this.activeJobId = null;
      this.jobDetail = null;
    },

    async clearJobs() {
      if (!confirm("Clear the entire jobs history? Files on disk are untouched; only the records are hidden.")) return;
      try {
        const data = await api.del("/api/jobs");
        const noun = data.cleared === 1 ? "job" : "jobs";
        this.notify(`Cleared ${data.cleared} ${noun} from the history`, "ok");
        this.refreshJobsQuiet();
      } catch (error) {
        this.notify("Could not clear history: " + error.message, "err");
      }
    },

    /* --- generate --- */
    cleanedSampling(section) {
      const source = section === "abc" ? this.g.abcAdv : this.g.semAdv;
      const cleaned = {};
      for (const [key] of this.samplingFields) {
        const value = source[key];
        if (value !== null && value !== undefined && value !== "") cleaned[key] = Number(value);
      }
      return Object.keys(cleaned).length ? cleaned : null;
    },

    insertTag(tag) {
      this.g.lyrics = (this.g.lyrics ? this.g.lyrics.trimEnd() + "\n" : "") + tag + "\n";
    },

    async loadExample() {
      const example = this.config && this.config.example_request;
      if (!example) { this.notify("No example request available", "err"); return; }
      this.g.name = example.id || "";
      this.g.style = example.style || "";
      this.g.lyrics = example.lyrics || "";
      this.g.cot = example.cot || "full";
      this.g.seed = example.seed ?? null;
      this.notify("Example loaded", "ok");
    },

    async loadPlanInto(name, target) {
      try {
        const plan = await api.get(`/api/library/plans/${encodeURIComponent(name)}`);
        if (target === "generate") {
          this.g.abcText = plan.abc || "";
          this.g.abcMode = "paste";
          this.g.abcPlanName = name;
        } else {
          this.cover.abcText = plan.abc || "";
          this.cover.planName = name;
        }
        this.notify(`Plan ${name} loaded`, "ok");
      } catch (error) {
        this.notify("Could not load plan: " + error.message, "err");
      }
    },

    async submitGenerate(planOnly) {
      const form = this.g;
      if (!planOnly && this.g.abcMode !== "plan") {
        if (!this.g.abcText.trim()) { this.notify("ABC score is empty", "err"); return; }
      }
      const params = {
        style: form.style,
        lyrics: form.lyrics,
        cot: form.cot,
      };
      if (form.seed !== null && form.seed !== undefined && form.seed !== "") params.seed = Number(form.seed);
      if (form.cfgScale !== null && form.cfgScale !== undefined && form.cfgScale !== "") params.cfg_scale = Number(form.cfgScale);
      if (!planOnly && this.g.abcMode !== "plan") {
        params.abc = this.g.abcText;
        const sampling = this.cleanedSampling("abc");
        if (sampling) params.sampling = { abc: sampling };
      } else {
        const sampling = this.cleanedSampling("abc");
        const semantic = this.cleanedSampling("semantic");
        if (sampling || semantic) {
          params.sampling = { abc: sampling, semantic: semantic };
        }
      }
      await this.submit(form.name || null, planOnly ? "plan" : "generate", params);
    },

    insertTag(tag) {
      const area = document.getElementById("lyrics-text");
      if (!area) { this.g.lyrics = (this.g.lyrics ? this.g.lyrics.trimEnd() + "\n" : "") + tag + "\n"; return; }
      const start = area.selectionStart ?? area.value.length;
      const end = area.selectionEnd ?? start;
      const lineStart = area.value.lastIndexOf("\n", start - 1) + 1;
      const lineEnd = area.value.indexOf("\n", start);
      if (lineEnd === -1) lineEnd = area.value.length;
      const isBlankLine = area.value.slice(lineStart, end).trim() === "";
      const text = isBlankLine && start === lineStart ? tag + "\n" : "\n" + tag + "\n";
      area.setRangeText(text, start, end, "end");
      this.g.lyrics = area.value;
      const caret = start + text.length;
      area.focus();
      area.setSelectionRange(caret, caret);
    },

    async submit(name, kind, params) {
      try {
        const data = await api.post("/api/jobs", { kind, name, params });
        const warnings = data.warnings || [];
        if (warnings.length) this.notify(warnings[0], "err");
        else this.notify(`Job #${data.job.id} queued (${kind})`, "ok");
        this.refreshJobsQuiet();
        this.watch(data.job.id);
      } catch (error) {
        this.notify(`Could not submit job: ${error.message}`, "err");
      }
    },

    /* --- ABC helpers --- */
    async validateAbc(text) {
      try {
        const result = await api.post("/api/abc/inspect", { text });
        if (result.ok) this.notify(`ABC valid: ${result.report.voices.Vocal.sounding_notes} vocal notes, ${result.has_chords ? "has chords" : "chord-free"}`, "ok");
        else this.notify("ABC check failed: " + result.error, "err");
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

    async compareAbc(before, after) {
      this.edit.compareError = null;
      try {
        const result = await api.post("/api/abc/compare", {
          before, after, allow_tempo_change: this.edit.allowTempoChange, voices: "both",
        });
        if (result.ok) {
          this.edit.compareResult = result.compare;
          this.notify(result.compare.match ? "Edits match invariants (unchanged notes are identical)" : "Edits changed notes — inspect the differences", result.compare.match ? "ok" : "err");
        } else {
          this.edit.compareResult = null;
          this.edit.compareError = result.error;
        }
      } catch (error) {
        this.edit.compareResult = null;
        this.edit.compareError = error.message;
        this.notify("Compare failed: " + error.message, "err");
      }
    },

    /* --- cover wizard --- */
    onCoverFile(event) {
      const files = event.target.files;
      this.cover.file = files && files.length ? files[0] : null;
    },

    async coverUpload() {
      if (!this.cover.file) { this.notify("Choose an audio file first", "err"); return; }
      try {
        const form = new FormData();
        form.append("file", this.cover.file);
        const upload = await api.upload("/api/uploads", form);
        const name = this.cover.name || null;
        const audioName = upload.file;
        this.notify(`Uploaded ${audioName}; queuing transcription…`, "ok");
        await new Promise((resolve) => setTimeout(resolve, 300));
        const params = { audio: audioName, task: this.cover.task };
        if (this.cover.maxSeconds !== null && this.cover.maxSeconds !== undefined && this.cover.maxSeconds !== "") {
          params.max_seconds = Number(this.cover.maxSeconds);
        }
        const data = await api.post("/api/jobs", { kind: "transcribe", name: this.cover.name || null, params });
        this.cover.transcriptName = "";
        this.refreshJobsQuiet();
        this.watch(data.job.id);
        const checkDone = setInterval(async () => {
          const job = await api.get(`/api/jobs/${data.job.id}`);
          if (["done", "failed", "cancelled"].includes(job.status)) {
            clearInterval(checkDone);
            await this.refreshLibrary();
            if (job.status === "done") {
              await this.refreshLibrary();
              this.cover.transcripts = this.library.transcripts;
              const latest = this.cover.transcripts.find((t) => t.output_dir === job.output_dir);
              this.cover.step = 2;
              if (latest) {
                this.cover.transcriptName = this.nameOf(latest.output_dir);
                await this.coverLoadTranscript();
              }
            } else {
              this.notify("Transcription failed: " + (job.error || "unknown error"), "err");
            }
          }
        }, 2000);
      } catch (error) {
        this.notify("Upload failed: " + error.message, "err");
      }
    },

    nameOf(outputDir) {
      if (!outputDir) return "";
      const parts = outputDir.replace(/\\/g, "/").split("/");
      return parts[parts.length - 1];
    },

    async coverLoadTranscript() {
      const name = this.cover.transcriptName;
      if (!name) { this.cover.transcript = null; return; }
      try {
        this.cover.transcript = await api.get(`/api/library/transcripts/${encodeURIComponent(name)}`);
        this.cover.abcText = this.cover.transcript.abc || "";
      } catch (error) {
        this.notify("Could not load transcript: " + error.message, "err");
      }
    },

    async coverContinueToStyle() {
      if (!this.cover.abcText.trim()) { this.notify("Score is empty", "err"); return; }
      this.cover.step = 3;
    },

    async coverPrepare() {
      if (this.cover.keepChords) return true;
      const stripped = await this.stripChords(this.cover.abcText);
      if (stripped === null) return false;
      this.cover.abcText = stripped;
      return true;
    },

    async coverSubmit() {
      const okPrepared = await this.coverPrepare();
      if (!okPrepared) return;
      const params = {
        style: this.cover.style,
        lyrics: this.cover.lyrics,
        cot: this.cover.keepChords ? "full" : "melody",
        abc: this.cover.abcText,
      };
      await this.submit(this.cover.name || null, "generate", params);
    },

    /* --- edit --- */
    async refreshEditSongs() {
      await this.refreshLibrary();
      this.edit.songs = this.library.songs.filter((s) => s.status === "done");
    },

    async editLoadSong() {
      const name = this.edit.songName;
      if (!name) { this.edit.song = null; return; }
      try {
        this.edit.song = await api.get(`/api/library/songs/${encodeURIComponent(name)}`);
        this.edit.abcOriginal = this.edit.song.abc || "";
        this.edit.abcText = this.edit.abcOriginal;
        const request = this.edit.song.request || {};
        this.edit.style = request.style || "";
        this.edit.lyrics = request.lyrics || "";
        this.edit.cot = request.cot === "melody" ? "melody" : "full";
        this.edit.compareResult = null;
      } catch (error) {
        this.notify("Could not load song: " + error.message, "err");
      }
    },

    async editSubmit() {
      if (!this.edit.abcText.trim()) { this.notify("Score is empty", "err"); return; }
      const params = {
        style: this.edit.style,
        lyrics: this.edit.lyrics,
        cot: this.edit.cot,
        abc: this.edit.abcText,
      };
      const name = this.edit.name ? `${this.edit.name}` : null;
      await this.submit(name, "generate", params);
    },

    /* --- library --- */
    async refreshLibrary() {
      try {
        const data = await api.get("/api/library");
        this.library.songs = data.songs;
        this.library.transcripts = data.transcripts;
        this.library.plans = data.plans;
        this.library.loaded = true;
      } catch (error) {
        this.notify("Could not refresh library: " + error.message, "err");
      }
    },

    async openDetail(kind, name) {
      try {
        this.detailKind = kind;
        this.detailName = name;
        const endpoint = kind === "song" ? "songs" : kind === "transcript" ? "transcripts" : "plans";
        this.detail = await api.get(`/api/library/${endpoint}/${encodeURIComponent(name)}`);
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

    togglePlay(name) {
      if (this.playingName === name) {
        const el = document.getElementById(`audio-${name}`);
        if (el) el.pause();
        this.playingName = null;
        return;
      }
      if (this.playingName) {
        const previous = document.getElementById(`audio-${this.playingName}`);
        if (previous) previous.pause();
      }
      this.playingName = name;
      this.$nextTick(() => {
        const el = document.getElementById(`audio-${name}`);
        if (el) { el.play().catch(() => {}); el.onended = () => { this.playingName = null; }; }
      });
    },

    artifactLinks() {
      if (!this.detail || this.detailKind !== "song") return [];
      const artifacts = (this.detail.result && this.detail.result.artifacts) || {};
      return Object.keys(artifacts).map((key) => ({
        key,
        url: `/api/library/songs/${encodeURIComponent(this.detailName)}/artifacts/${key.split("/").map(encodeURIComponent).join("/")}`,
      }));
    },

    async redecode(name, vae) {
      await this.submit(null, "decode", { source: name, vae });
    },

    async deleteItem(kind, name) {
      if (!confirm(`Delete ${kind} ${name}? This cannot be undone.`)) return;
      const endpoint = kind === "song" ? "songs" : kind === "transcript" ? "transcripts" : "plans";
      try {
        await api.del(`/api/library/${endpoint}/${encodeURIComponent(name)}`);
        this.notify(`Deleted ${name}`, "ok");
        await this.refreshLibrary();
      } catch (error) {
        this.notify("Delete failed: " + error.message, "err");
      }
    },

    /* --- diagnostics --- */
    async runDiagnostics() {
      this.diagLoading = true;
      try {
        this.diag = await api.get("/api/diagnostics");
        this.residencyForm.residency = this.diag.config.residency;
        this.residencyForm.release_idle_minutes = this.diag.config.release_idle_minutes;
      } catch (error) {
        this.notify("Diagnostics failed: " + error.message, "err");
      } finally {
        this.diagLoading = false;
      }
    },

    async saveResidency() {
      try {
        const fields = { residency: this.residencyForm.residency };
        if (this.residencyForm.release_idle_minutes !== null && this.residencyForm.release_idle_minutes !== "") {
          fields.release_idle_minutes = Number(this.residencyForm.release_idle_minutes);
        }
        const data = await api.post("/api/config", fields);
        this.config = data.config;
        this.notify("Config saved to config.yaml", "ok");
      } catch (error) {
        this.notify("Could not save config: " + error.message, "err");
      }
    },
  }));
});
