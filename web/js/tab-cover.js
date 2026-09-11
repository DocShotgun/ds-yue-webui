/* Cover tab: 3-step wizard — transcribe a recording, review the melody, generate. */
"use strict";

document.addEventListener("alpine:init", () => {
  Alpine.data("coverTab", () => ({
    structureTags: ["[Intro]", "[Verse]", "[Pre-Chorus]", "[Chorus]", "[Bridge]", "[Outro]"],
    samplingFields: [
      ["temperature", "number", "temperature", "0–5, default 1.0"],
      ["top_p", "number", "top-p", "0–1, default 0.95"],
      ["top_k", "number", "top-k", "integer, default 100"],
      ["repetition_penalty", "number", "rep penalty", "> 0, default 1.2"],
      ["penalty_window", "number", "rep window", "1–100, default 50"],
      ["min_tokens", "number", "min tokens", "integer, default 0"],
      ["max_tokens", "number", "max tokens", "integer"],
    ],
    cover: {
      step: 1, file: null, task: "full", maxSeconds: null, name: "",
      transcriptName: "", transcript: null, abcText: "",
      keepChords: false, style: "", lyrics: "",
      seed: null, cfgScale: null, advOpen: false, abcAdv: {},
    },
    transcribing: false,

    get store() { return this.$store.ui; },

    init() {
      for (const [key] of this.samplingFields) this.cover.abcAdv[key] = null;
      this.$watch("cover.abcText", debounce((value) => renderAbcById("cover-abc-preview", value), 300));
    },

    rerenderAbc() {
      renderAbcById("cover-abc-preview", this.cover.abcText);
    },

    onCoverFile(event) {
      const files = event.target.files;
      this.cover.file = files && files.length ? files[0] : null;
    },

    async upload() {
      if (this.transcribing) return;
      if (!this.cover.file) {
        this.store.notify("Choose an audio file first", "err");
        return;
      }
      let upload;
      try {
        const form = new FormData();
        form.append("file", this.cover.file);
        upload = await api.upload("/api/uploads", form);
      } catch (error) {
        this.store.notify("Upload failed: " + error.message, "err");
        return;
      }
      this.store.notify(upload.deduped
        ? `${upload.file} was already uploaded — reusing it`
        : `Uploaded ${upload.file}`, "ok");

      const params = { audio: upload.file, task: this.cover.task };
      if (this.cover.maxSeconds !== null && this.cover.maxSeconds !== undefined && this.cover.maxSeconds !== "") {
        params.max_seconds = Number(this.cover.maxSeconds);
      }
      this.transcribing = true;
      // the transcription job is named after the uploaded file — extension and
      // dedupe digest stripped — not a stale form field
      const jobName = upload.file.replace(/\.[^/.]+$/, "").replace(/-[0-9a-f]{16}$/, "");
      const job = await this.store.submit("transcribe", jobName, params, {
        onFinished: async (finished) => {
          this.transcribing = false;
          if (finished.status !== "done") return;
          await this.store.refreshLibrary();
          const name = this.store.nameOf(finished.output_dir);
          const transcript = this.store.library.transcripts.find(
            (t) => this.store.nameOf(t.output_dir) === name);
          if (transcript) {
            this.cover.transcriptName = name;
            await this.loadTranscript();
            this.cover.step = Math.max(this.cover.step, 2);
          }
        },
      });
      if (!job) this.transcribing = false;
    },

    async loadTranscript() {
      const name = this.cover.transcriptName;
      if (!name) {
        this.cover.transcript = null;
        return;
      }
      try {
        this.cover.transcript = await api.get(`/api/library/transcripts/${encodeURIComponent(name)}`);
        this.cover.abcText = this.cover.transcript.abc || "";
      } catch (error) {
        this.store.notify("Could not load transcript: " + error.message, "err");
      }
    },

    async onSelectTranscript() {
      await this.loadTranscript();
      if (this.cover.transcript) this.cover.step = Math.max(this.cover.step, 2);
    },

    continueToStyle() {
      if (!this.cover.abcText.trim()) {
        this.store.notify("Score is empty", "err");
        return;
      }
      this.cover.step = 3;
    },

    cleanedSampling() {
      const cleaned = {};
      for (const [key] of this.samplingFields) {
        const value = this.cover.abcAdv[key];
        if (value !== null && value !== undefined && value !== "") cleaned[key] = Number(value);
      }
      return Object.keys(cleaned).length ? { abc: cleaned } : null;
    },

    async submitCover() {
      if (this.transcribing) return;
      if (!this.cover.style.trim()) {
        this.store.notify("Style is required (genre, instruments, vocal, tempo)", "err");
        return;
      }
      if (!this.cover.keepChords) {
        const stripped = await this.store.stripChords(this.cover.abcText);
        if (stripped === null) return;
        this.cover.abcText = stripped;
      }
      const params = {
        style: this.cover.style,
        lyrics: this.cover.lyrics,
        cot: this.cover.keepChords ? "full" : "melody",
        abc: this.cover.abcText,
      };
      if (this.cover.seed !== null && this.cover.seed !== undefined && this.cover.seed !== "") {
        params.seed = Number(this.cover.seed);
      }
      if (this.cover.cfgScale !== null && this.cover.cfgScale !== undefined && this.cover.cfgScale !== "") {
        params.cfg_scale = Number(this.cover.cfgScale);
      }
      const sampling = this.cleanedSampling();
      if (sampling) params.sampling = sampling;
      await this.store.submit("generate", this.cover.name || null, params);
    },

    insertTag(tag) {
      insertTagAtCursor(document.getElementById("cover-lyrics-text"), tag,
                        (value) => { this.cover.lyrics = value; });
    },

    onKeydown(event) {
      enterSubmits(event, () => this.submitCover());
    },
  }));
});
