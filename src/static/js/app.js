// Global UI behaviours shared across pages:
//   1. A top loading bar that appears the instant a request starts — HTMX
//      fragment loads, form submits, AND plain full-page link clicks — so the
//      UI never looks "frozen" while the server works.
//   2. A single poll of /api/active that feeds an Alpine store ($store.job),
//      which the dashboard, session list and session detail read to show
//      "currently updating" indicators. Polling here (once) avoids every view
//      running its own loop, and uses fetch() so it never trips the loading bar.
//
// Guarded against double-initialisation in case the script is ever re-run.
(function () {
  if (window.__optvInit) return;
  window.__optvInit = true;

  // --- Top loading bar -----------------------------------------------------
  let inflight = 0;
  let trickle = null;

  function bar() {
    return document.getElementById("global-progress");
  }

  function start() {
    const el = bar();
    if (!el) return;
    el.style.transition = "none";
    el.style.width = "0%";
    el.style.opacity = "1";
    // Force a reflow so the reset width applies before we animate.
    void el.offsetWidth;
    el.style.transition = "width 0.2s ease-out, opacity 0.3s ease";
    let w = 8;
    el.style.width = w + "%";
    clearInterval(trickle);
    trickle = setInterval(function () {
      w = Math.min(90, w + (90 - w) * 0.12);
      el.style.width = w + "%";
    }, 200);
  }

  function finish() {
    const el = bar();
    if (!el) return;
    clearInterval(trickle);
    el.style.width = "100%";
    setTimeout(function () {
      el.style.opacity = "0";
      setTimeout(function () {
        el.style.transition = "none";
        el.style.width = "0%";
      }, 300);
    }, 200);
  }

  // Background pollers (hx-trigger="… every Ns") should not flash the bar.
  // Identify them by their trigger attribute.
  function isBackground(elt) {
    if (!elt || !elt.getAttribute) return false;
    const trig = elt.getAttribute("hx-trigger") || "";
    return trig.indexOf("every") !== -1;
  }

  document.addEventListener("htmx:beforeRequest", function (e) {
    if (isBackground(e.detail && e.detail.elt)) return;
    if (inflight++ === 0) start();
  });
  function settle(e) {
    if (isBackground(e.detail && e.detail.elt)) return;
    if (inflight > 0 && --inflight === 0) finish();
  }
  document.addEventListener("htmx:afterRequest", settle);
  document.addEventListener("htmx:responseError", settle);
  document.addEventListener("htmx:sendError", settle);

  // Full-page navigations: show the bar the moment an internal link is clicked.
  // The new document load discards this bar, so there is nothing to reset.
  document.addEventListener("click", function (e) {
    const a = e.target.closest && e.target.closest("a[href]");
    if (!a) return;
    if (a.target === "_blank" || a.hasAttribute("download")) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
    const href = a.getAttribute("href");
    if (!href || href.charAt(0) === "#" || href.indexOf("javascript:") === 0) return;
    // Only same-origin navigations.
    if (a.origin && a.origin !== window.location.origin) return;
    start();
  });
  // If a navigation is aborted (e.g. the user hits back), tidy the bar up.
  window.addEventListener("pageshow", finish);

  // --- Shared active-job store ---------------------------------------------
  const EMPTY = {
    active: false,
    parliament: null,
    status: null,
    stage: null,
    current_session: null,
    sessions_completed: 0,
    sessions_total: 0,
  };

  document.addEventListener("alpine:init", function () {
    if (window.Alpine) window.Alpine.store("job", Object.assign({}, EMPTY));
  });

  async function pollActive() {
    try {
      const r = await fetch("/api/active", {
        headers: { "X-Requested-With": "fetch" },
        credentials: "same-origin",
      });
      if (!r.ok) return;
      const d = await r.json();
      const s = window.Alpine && window.Alpine.store("job");
      if (!s) return;
      if (d && d.active) {
        Object.assign(s, {
          active: true,
          parliament: d.parliament || null,
          status: d.status || null,
          stage: d.stage || null,
          current_session: d.current_session || null,
          sessions_completed: d.sessions_completed || 0,
          sessions_total: d.sessions_total || 0,
        });
      } else {
        Object.assign(s, EMPTY);
      }
    } catch (err) {
      /* transient network error — keep last known state */
    }
  }

  setInterval(function () {
    if (!document.hidden) pollActive();
  }, 2000);
  // Kick off as soon as Alpine has the store ready.
  document.addEventListener("alpine:initialized", pollActive);
  if (document.readyState !== "loading") setTimeout(pollActive, 0);
})();
