/* Library tab: songs, transcripts, plans — play, inspect, edit, delete. */
"use strict";

document.addEventListener("alpine:init", () => {
  Alpine.data("libraryTab", () => ({
    playingName: null,

    get store() { return this.$store.ui; },

    togglePlay(name) {
      if (this.playingName === name) {
        this.pause();
        return;
      }
      this.pause();
      this.playingName = name;
      this.$nextTick(() => {
        const element = document.getElementById(`audio-${name}`);
        if (element) {
          element.play().catch(() => {});
          element.onended = () => { this.playingName = null; };
        }
      });
    },
    pause() {
      if (!this.playingName) return;
      const previous = document.getElementById(`audio-${this.playingName}`);
      if (previous) previous.pause();
      this.playingName = null;
    },

    editSong(name) {
      this.store.requestEdit(name);
    },
    redecode(name, vae) {
      this.store.submit("decode", null, { source: name, vae });
    },
  }));
});
