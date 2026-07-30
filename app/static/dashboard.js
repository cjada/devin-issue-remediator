// Ticks the duration of in-flight remediations between server-side refreshes.
(function () {
  function format(seconds) {
    if (seconds < 60) return seconds + "s";
    if (seconds < 3600) {
      return Math.floor(seconds / 60) + "m " + String(seconds % 60).padStart(2, "0") + "s";
    }
    const minutes = Math.floor((seconds % 3600) / 60);
    return Math.floor(seconds / 3600) + "h " + String(minutes).padStart(2, "0") + "m";
  }

  const cells = Array.from(document.querySelectorAll(".ticking"))
    .map((el) => [el, Date.parse(el.dataset.started)])
    .filter(([, started]) => !Number.isNaN(started));

  if (!cells.length) return;

  function tick() {
    const now = Date.now();
    for (const [el, started] of cells) {
      el.textContent = format(Math.max(0, Math.floor((now - started) / 1000)));
    }
  }

  tick();
  setInterval(tick, 1000);
})();
