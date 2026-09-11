/* Generate tab: full YuE2 songs from style + lyrics, with optional ABC conditioning. */
"use strict";

document.addEventListener("alpine:init", () => {
  Alpine.data("generateTab", () => ({
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
    g: {
      name: "", style: "", lyrics: "", cot: "full", seed: null, cfgScale: null,
      abcMode: "plan", abcText: "", abcPlanName: "", advOpen: false,
      abcAdv: {}, semAdv: {},
    },

    get store() { return this.$store.ui; },

    init() {
      for (const [key] of this.samplingFields) {
        this.g.abcAdv[key] = null;
        this.g.semAdv[key] = null;
      }
      this.$watch("g.abcText", debounce((value) => renderAbcById("gen-abc-preview", value), 300));
    },

    rerenderAbc() {
      renderAbcById("gen-abc-preview", this.g.abcText);
    },

    async loadExample() {
      const example = this.store.config && this.store.config.example_request;
      if (!example) {
        this.store.notify("No example request available", "err");
        return;
      }
      this.g.name = example.id || "";
      this.g.style = example.style || "";
      this.g.lyrics = example.lyrics || "";
      this.g.cot = example.cot || "full";
      this.g.seed = example.seed ?? null;
      this.store.notify("Example loaded", "ok");
    },

    async loadPlan() {
      const name = this.g.abcPlanName;
      if (!name) return;
      try {
        const plan = await api.get(`/api/library/plans/${encodeURIComponent(name)}`);
        this.g.abcText = plan.abc || "";
        this.g.abcMode = "paste";
        this.store.notify(`Plan "${name}" loaded`, "ok");
      } catch (error) {
        this.store.notify("Could not load plan: " + error.message, "err");
      }
    },

    cleanedSampling(section) {
      const source = section === "abc" ? this.g.abcAdv : this.g.semAdv;
      const cleaned = {};
      for (const [key] of this.samplingFields) {
        const value = source[key];
        if (value !== null && value !== undefined && value !== "") cleaned[key] = Number(value);
      }
      return Object.keys(cleaned).length ? cleaned : null;
    },

    async submitGenerate(planOnly) {
      const form = this.g;
      if (!form.style.trim()) {
        this.store.notify("Style is required (genre, instruments, vocal, tempo)", "err");
        return;
      }
      const usingScore = !planOnly && form.abcMode !== "plan";
      if (usingScore && !form.abcText.trim()) {
        this.store.notify("ABC score is empty", "err");
        return;
      }
      const params = { style: form.style, lyrics: form.lyrics, cot: form.cot };
      if (form.seed !== null && form.seed !== undefined && form.seed !== "") params.seed = Number(form.seed);
      if (form.cfgScale !== null && form.cfgScale !== undefined && form.cfgScale !== "") {
        params.cfg_scale = Number(form.cfgScale);
      }
      if (usingScore) {
        params.abc = form.abcText;
        const sampling = this.cleanedSampling("abc");
        if (sampling) params.sampling = { abc: sampling };
      } else {
        const abc = this.cleanedSampling("abc");
        const semantic = this.cleanedSampling("semantic");
        if (abc || semantic) params.sampling = { abc, semantic };
      }
      await this.store.submit(planOnly ? "plan" : "generate", form.name || null, params);
    },

    insertTag(tag) {
      insertTagAtCursor(document.getElementById("lyrics-text"), tag, (value) => { this.g.lyrics = value; });
    },

    onKeydown(event) {
      enterSubmits(event, () => this.submitGenerate(false));
    },
  }));
});
