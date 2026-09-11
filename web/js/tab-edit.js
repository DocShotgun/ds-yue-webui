/* Edit tab: pick a completed song, edit its ABC score, compare, regenerate. */
"use strict";

document.addEventListener("alpine:init", () => {
  Alpine.data("editTab", () => ({
    samplingFields: [
      ["temperature", "number", "temperature", "0–5, default 1.0"],
      ["top_p", "number", "top-p", "0–1, default 0.95"],
      ["top_k", "number", "top-k", "integer, default 100"],
      ["repetition_penalty", "number", "rep penalty", "> 0, default 1.2"],
      ["penalty_window", "number", "rep window", "1–100, default 50"],
      ["min_tokens", "number", "min tokens", "integer, default 0"],
      ["max_tokens", "number", "max tokens", "integer"],
    ],
    edit: {
      songName: "", song: null,
      abcOriginal: "", abcText: "",
      compareResult: null, compareError: null, allowTempoChange: false,
      style: "", lyrics: "", cot: "full", name: "",
      seed: null, cfgScale: null, advOpen: false, abcAdv: {},
    },

    get store() { return this.$store.ui; },

    init() {
      for (const [key] of this.samplingFields) this.edit.abcAdv[key] = null;
      this.$watch("edit.abcText", debounce((value) => renderAbcById("edit-abc-preview", value), 300));
      this.$watch("$store.ui.editRequest", (request) => {
        if (request) this.loadSong(request.name);
      });
      window.addEventListener("tab:edit", () => this.store.refreshLibrary());
    },

    rerenderAbc() {
      renderAbcById("edit-abc-preview", this.edit.abcText);
    },

    get doneSongs() {
      return this.store.library.songs.filter((song) => song.status === "done");
    },

    async loadSong(name) {
      if (!name) {
        this.edit.song = null;
        this.edit.songName = "";
        return;
      }
      try {
        const song = await api.get(`/api/library/songs/${encodeURIComponent(name)}`);
        this.edit.song = song;
        this.edit.songName = name;
        this.edit.abcOriginal = song.abc || "";
        this.edit.abcText = this.edit.abcOriginal;
        const request = song.request || {};
        this.edit.style = request.style || "";
        this.edit.lyrics = request.lyrics || "";
        this.edit.cot = request.cot === "melody" ? "melody" : "full";
        this.edit.seed = request.seed ?? null;
        this.edit.cfgScale = request.cfg_scale ?? null;
        this.edit.compareResult = null;
        this.edit.compareError = null;
      } catch (error) {
        this.store.notify("Could not load song: " + error.message, "err");
      }
    },

    onSelectSong() {
      this.loadSong(this.edit.songName);
    },

    revert() {
      this.edit.abcText = this.edit.abcOriginal;
      this.edit.compareResult = null;
      this.edit.compareError = null;
    },

    async compare() {
      this.edit.compareError = null;
      try {
        const result = await api.post("/api/abc/compare", {
          before: this.edit.abcOriginal, after: this.edit.abcText,
          allow_tempo_change: this.edit.allowTempoChange, voices: "both",
        });
        if (result.ok) {
          this.edit.compareResult = result.compare;
          this.store.notify(
            result.compare.match
              ? "Edits match invariants (unchanged notes are identical)"
              : "Edits changed notes — inspect the differences",
            result.compare.match ? "ok" : "warn");
        } else {
          this.edit.compareResult = null;
          this.edit.compareError = result.error;
        }
      } catch (error) {
        this.edit.compareResult = null;
        this.edit.compareError = error.message;
      }
    },

    cleanedSampling() {
      const cleaned = {};
      for (const [key] of this.samplingFields) {
        const value = this.edit.abcAdv[key];
        if (value !== null && value !== undefined && value !== "") cleaned[key] = Number(value);
      }
      return Object.keys(cleaned).length ? { abc: cleaned } : null;
    },

    async submitEdit() {
      if (!this.edit.abcText.trim()) {
        this.store.notify("Score is empty", "err");
        return;
      }
      if (!this.edit.style.trim()) {
        this.store.notify("Style is required (genre, instruments, vocal, tempo)", "err");
        return;
      }
      const params = {
        style: this.edit.style,
        lyrics: this.edit.lyrics,
        cot: this.edit.cot,
        abc: this.edit.abcText,
      };
      if (this.edit.seed !== null && this.edit.seed !== undefined && this.edit.seed !== "") {
        params.seed = Number(this.edit.seed);
      }
      if (this.edit.cfgScale !== null && this.edit.cfgScale !== undefined && this.edit.cfgScale !== "") {
        params.cfg_scale = Number(this.edit.cfgScale);
      }
      const sampling = this.cleanedSampling();
      if (sampling) params.sampling = sampling;
      await this.store.submit("generate", this.edit.name || null, params);
    },

    onKeydown(event) {
      enterSubmits(event, () => this.submitEdit());
    },
  }));
});
