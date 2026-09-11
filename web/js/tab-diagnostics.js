/* Diagnostics tab: doctor, GPU, tools, disk, queue/workers, smoke test, residency. */
"use strict";

document.addEventListener("alpine:init", () => {
  Alpine.data("diagnosticsTab", () => ({
    diag: null,
    diagLoading: false,
    residencyForm: { residency: "on-demand", release_idle_minutes: 10 },

    get store() { return this.$store.ui; },

    init() {
      window.addEventListener("tab:diagnostics", () => this.ensureLoaded());
      this.$watch("$store.ui.config", (config) => this.syncResidencyForm(config));
      this.syncResidencyForm(this.store.config);
    },

    ensureLoaded() {
      if (!this.diag) this.run();
    },

    async run() {
      this.diagLoading = true;
      try {
        this.diag = await api.get("/api/diagnostics");
        this.syncResidencyForm(this.diag.config);
      } catch (error) {
        this.store.notify("Diagnostics failed: " + error.message, "err");
      } finally {
        this.diagLoading = false;
      }
    },

    syncResidencyForm(config) {
      if (!config) return;
      this.residencyForm.residency = config.residency || "on-demand";
      this.residencyForm.release_idle_minutes = config.release_idle_minutes ?? 10;
    },

    async saveResidency() {
      const fields = { residency: this.residencyForm.residency };
      if (this.residencyForm.release_idle_minutes !== null
          && this.residencyForm.release_idle_minutes !== "") {
        fields.release_idle_minutes = Number(this.residencyForm.release_idle_minutes);
      }
      try {
        const data = await api.post("/api/config", fields);
        this.store.config = data.config;
        this.store.notify("Config saved to config.yaml", "ok");
      } catch (error) {
        this.store.notify("Could not save config: " + error.message, "err");
      }
    },

    doctorLabel() {
      const doctor = this.diag && this.diag.doctor;
      if (!doctor) return "doctor unavailable";
      if (doctor.unavailable) return doctor.unavailable;
      return doctor.dependencies_ready ? "dependencies ready" : "dependencies incomplete";
    },
    doctorOk() {
      const doctor = this.diag && this.diag.doctor;
      return !!(doctor && !doctor.unavailable && doctor.dependencies_ready);
    },
  }));
});
