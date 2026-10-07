/* ReferralPilot dashboard behaviour: toasts, modal/drawer, Kanban drag & drop, live logs. */
(function () {
  "use strict";

  // Every htmx request carries HX-Request: true, which the server requires for
  // state-changing calls (cross-site pages cannot add that header).

  // ---------------------------------------------------------------- toasts --
  const TOAST_STYLES = {
    success: "border-emerald-200 bg-white text-slate-800 [&_.dot]:bg-emerald-500",
    error: "border-rose-200 bg-white text-slate-800 [&_.dot]:bg-rose-500",
    info: "border-slate-200 bg-white text-slate-800 [&_.dot]:bg-sky-500",
  };

  function toast(message, kind) {
    const host = document.getElementById("toasts");
    if (!host || !message) return;
    const el = document.createElement("div");
    el.className = "toast pointer-events-auto flex items-start gap-3 rounded-lg border px-4 py-3 text-sm shadow-lg " +
      (TOAST_STYLES[kind] || TOAST_STYLES.info);
    const dot = document.createElement("span");
    dot.className = "dot mt-1.5 h-2 w-2 shrink-0 rounded-full";
    const text = document.createElement("span");
    text.textContent = message;
    el.append(dot, text);
    host.appendChild(el);
    setTimeout(() => el.classList.add("toast-hide"), kind === "error" ? 6500 : 3500);
    setTimeout(() => el.remove(), kind === "error" ? 7000 : 4000);
  }
  window.toast = toast;

  document.addEventListener("toast", (e) => toast(e.detail.message, e.detail.kind));
  document.addEventListener("htmx:responseError", (e) => {
    const status = e.detail.xhr ? e.detail.xhr.status : "?";
    toast(`Request failed (${status})`, "error");
  });
  document.addEventListener("htmx:sendError", () => toast("Server unreachable - is `referralpilot serve` running?", "error"));

  // --------------------------------------------------------- drawer / modal --
  window.closeModal = function () {
    const modal = document.getElementById("modal");
    if (modal) modal.innerHTML = "";
  };
  window.closeDrawer = function () {
    const drawer = document.getElementById("drawer");
    if (drawer) drawer.innerHTML = "";
  };
  document.addEventListener("closeModal", window.closeModal);
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const modal = document.getElementById("modal");
    if (modal && modal.innerHTML.trim()) window.closeModal();
    else window.closeDrawer();
  });

  // -------------------------------------------------------------- kanban ----
  function initBoard(root) {
    if (typeof Sortable === "undefined") return;
    root.querySelectorAll(".kanban-list").forEach((list) => {
      if (list.dataset.sortable) return;
      list.dataset.sortable = "1";
      Sortable.create(list, {
        group: "board",
        animation: 150,
        draggable: ".job-card",
        forceFallback: true, // pointer-event drag: consistent across browsers and touch screens
        fallbackOnBody: true,
        ghostClass: "card-ghost",
        chosenClass: "card-chosen",
        onEnd(evt) {
          if (evt.from === evt.to) return;
          const placeholder = evt.to.querySelector(".empty-col");
          if (placeholder) placeholder.remove();
          htmx.ajax("POST", `/jobs/${evt.item.dataset.jobId}/status`, {
            values: { status: evt.to.dataset.status },
            swap: "none",
          });
        },
      });
    });
  }

  // ------------------------------------------------------- editor counter ---
  function initCounters(root) {
    root.querySelectorAll("textarea[data-count]").forEach((area) => {
      const counter = area.parentElement.querySelector("[data-counter]");
      if (!counter) return;
      const update = () => {
        const intro = area.value.split(/\n\s*\n/).slice(1, 3).join(" ");
        const sentences = intro.trim() ? intro.trim().split(/(?<=[.!?])["')\]]*\s+(?=[A-Z0-9"'(])/).length : 0;
        const words = area.value.trim().split(/\s+/).filter(Boolean).length;
        counter.textContent = `${words} words · ~${sentences} sentences in the pitch (aim for 3-4)`;
      };
      area.addEventListener("input", update);
      update();
    });
  }

  // ------------------------------------------------------------ live log ----
  const REFRESH_SOURCES = new Set(["harvester", "outreach", "tailor", "prospector", "scheduler"]);
  let refreshTimer = null;

  function appendLog(panel, row) {
    const line = document.createElement("div");
    line.className = "log-line log-" + String(row.level || "info").toLowerCase();
    const time = document.createElement("span");
    time.className = "log-time";
    time.textContent = new Date(row.time).toLocaleTimeString();
    const src = document.createElement("span");
    src.className = "log-src";
    src.textContent = row.source;
    const msg = document.createElement("span");
    msg.className = "log-msg";
    msg.textContent = row.message;
    line.append(time, src, msg);
    const nearBottom = panel.scrollTop + panel.clientHeight >= panel.scrollHeight - 30;
    panel.appendChild(line);
    while (panel.childElementCount > 600) panel.firstElementChild.remove();
    if (nearBottom) panel.scrollTop = panel.scrollHeight;
  }

  function initLogStream() {
    const panel = document.getElementById("live-log");
    if (!panel || !window.EventSource) return;
    panel.scrollTop = panel.scrollHeight;
    const params = new URLSearchParams({ backlog: panel.dataset.backlog || "40" });
    if (window.LOG_AFTER_ID) params.set("after", window.LOG_AFTER_ID);
    if (panel.dataset.source) params.set("source", panel.dataset.source);
    const source = new EventSource("/logs/stream?" + params.toString());
    source.addEventListener("log", (event) => {
      const row = JSON.parse(event.data);
      appendLog(panel, row);
      if (REFRESH_SOURCES.has(row.source) && document.getElementById("board")) {
        clearTimeout(refreshTimer);
        refreshTimer = setTimeout(() => {
          htmx.trigger(document.body, "refreshBoard");
          htmx.trigger(document.body, "refreshStatus");
        }, 1500);
      }
    });
  }

  window.toggleLogDock = function () {
    const panel = document.getElementById("live-log");
    const hint = document.getElementById("log-dock-hint");
    if (!panel) return;
    panel.classList.toggle("hidden");
    if (hint) hint.textContent = panel.classList.contains("hidden") ? "show" : "hide";
  };

  document.addEventListener("DOMContentLoaded", () => {
    htmx.onLoad((el) => {
      initBoard(el);
      initCounters(el);
    });
    initLogStream();
  });
})();
