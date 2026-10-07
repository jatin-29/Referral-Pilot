/* ReferralPilot browser edition - the page side.
 *
 * The dashboard (FastAPI + Jinja templates + htmx) runs in a Web Worker under
 * Pyodide (web/worker.js). This script:
 *   - makes sure only one tab runs the app (two tabs would send emails twice);
 *   - boots the worker and shows progress;
 *   - answers XMLHttpRequest and fetch() calls for app paths ("/outbox",
 *     "/jobs/3/tailor", ...) from the worker instead of the network;
 *   - turns links and GET forms into hash routes (#/outbox), so reloads and the
 *     back button work on a static host;
 *   - opens PDFs and downloads that the worker produces;
 *   - signs in to Gmail with Google Identity Services and hands the
 *     short-lived token to the worker.
 */
(function () {
  "use strict";

  const BASE = new URL("./", document.baseURI).href;
  const LOCK_NAME = "referralpilot-app";
  const GIS_SRC = "https://accounts.google.com/gsi/client";
  const GMAIL_TOKEN_KEY = "rp-gmail-token";
  const HTMX_VERBS = ["hx-get", "hx-post", "hx-put", "hx-patch", "hx-delete", "data-hx-get", "data-hx-post"];
  const STATUS_TEXT = { 200: "OK", 204: "No Content", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
    405: "Method Not Allowed", 422: "Unprocessable Content", 500: "Internal Server Error" };

  const encoder = new TextEncoder();
  const decoder = new TextDecoder();

  function deferred() {
    let resolve, reject;
    const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
    return { promise, resolve, reject };
  }

  const ready = deferred();
  ready.promise.catch(() => {}); // failures are shown on the start-up screen
  let worker = null;
  let nextId = 1;
  const pending = new Map();
  let clientConfig = {};
  let stopped = false;

  // ------------------------------------------------------------ worker RPC ---
  function rpc(type, payload, transfer) {
    if (stopped) return Promise.reject(new Error("ReferralPilot is open in another tab"));
    return new Promise((resolve, reject) => {
      const id = nextId++;
      pending.set(id, { resolve, reject });
      worker.postMessage({ id, type, ...payload }, transfer || []);
    });
  }

  function onWorkerMessage(event) {
    const msg = event.data || {};
    if (msg.type === "progress") return showProgress(msg.text, msg.pct);
    if (msg.type === "log") return console.debug("[python]", msg.text);
    if (msg.type === "changed") return scheduleRefresh();
    const entry = pending.get(msg.id);
    if (!entry) return;
    pending.delete(msg.id);
    if (msg.ok) entry.resolve(msg);
    else entry.reject(new Error(msg.error || "worker error"));
  }

  let refreshTimer = null;
  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => {
      if (!window.htmx) return;
      htmx.trigger(document.body, "refreshStatus");
      htmx.trigger(document.body, "refreshOutbox");
    }, 500);
  }

  // ------------------------------------------------------ app requests -------
  function appPath(url) {
    // Only root-relative strings are app routes; site files are always fetched relatively.
    if (typeof url !== "string") url = url == null ? "" : String(url);
    return url.startsWith("/") && !url.startsWith("//") ? url : null;
  }

  function headerValue(headers, name) {
    const lower = name.toLowerCase();
    const found = (headers || []).filter(([key]) => key.toLowerCase() === lower).map(([, value]) => value);
    return found.length ? found.join(", ") : null;
  }

  async function encodeBody(body, headers) {
    if (body == null) return null;
    if (typeof body === "string") return encoder.encode(body).buffer;
    if (body instanceof ArrayBuffer) return body.slice(0);
    if (ArrayBuffer.isView(body)) return body.buffer.slice(body.byteOffset, body.byteOffset + body.byteLength);
    if (body instanceof Blob) return await body.arrayBuffer();
    if (body instanceof URLSearchParams) return encoder.encode(body.toString()).buffer;
    if (body instanceof FormData) {
      // The dashboard never uploads files through forms: send the fields URL-encoded.
      const params = new URLSearchParams();
      for (const [key, value] of body) if (typeof value === "string") params.append(key, value);
      const index = headers.findIndex(([key]) => key.toLowerCase() === "content-type");
      if (index >= 0) headers.splice(index, 1);
      headers.push(["Content-Type", "application/x-www-form-urlencoded"]);
      return encoder.encode(params.toString()).buffer;
    }
    return encoder.encode(String(body)).buffer;
  }

  async function appRequest(method, url, headers, body) {
    await ready.promise;
    const outgoing = (headers || []).map(([key, value]) => [String(key), String(value)]);
    const buffer = await encodeBody(body, outgoing);
    const msg = await rpc("request", { method: String(method || "GET").toUpperCase(), url, headers: outgoing, body: buffer },
      buffer ? [buffer] : []);
    return { status: msg.status, headers: msg.headers || [], body: msg.body || new ArrayBuffer(0) };
  }

  // XMLHttpRequest: htmx's requests for app paths are answered by the worker. Everything else
  // (other origins) goes to the real network untouched.
  (function patchXHR() {
    const proto = XMLHttpRequest.prototype;
    const native = {
      open: proto.open, send: proto.send, setRequestHeader: proto.setRequestHeader, abort: proto.abort,
      getResponseHeader: proto.getResponseHeader, getAllResponseHeaders: proto.getAllResponseHeaders,
      overrideMimeType: proto.overrideMimeType,
    };
    const FAKED = ["readyState", "status", "statusText", "responseURL", "responseText", "response"];
    const states = new WeakMap();

    function fire(xhr, type) {
      xhr.dispatchEvent(new ProgressEvent(type));
    }

    proto.open = function (method, url, ...rest) {
      const path = appPath(url);
      if (path === null) {
        if (states.has(this)) {
          states.delete(this);
          for (const key of FAKED) delete this[key];
        }
        return native.open.call(this, method, url, ...rest);
      }
      const state = { method, url: path, headers: [], readyState: 1, status: 0, statusText: "", text: "",
        body: null, responseHeaders: [], aborted: false };
      states.set(this, state);
      const xhr = this;
      Object.defineProperties(this, {
        readyState: { configurable: true, get: () => state.readyState },
        status: { configurable: true, get: () => state.status },
        statusText: { configurable: true, get: () => state.statusText },
        responseURL: { configurable: true, get: () => (state.readyState === 4 ? location.origin + state.url : "") },
        responseText: { configurable: true, get: () => state.text },
        response: {
          configurable: true,
          get() {
            if (state.readyState !== 4) return null;
            switch (xhr.responseType) {
              case "arraybuffer": return state.body;
              case "blob": return new Blob([state.body], { type: headerValue(state.responseHeaders, "content-type") || "" });
              case "json": try { return JSON.parse(state.text); } catch (_) { return null; }
              case "document": return new DOMParser().parseFromString(state.text, "text/html");
              default: return state.text;
            }
          },
        },
      });
      fire(this, "readystatechange");
    };

    proto.setRequestHeader = function (name, value) {
      const state = states.get(this);
      if (!state) return native.setRequestHeader.call(this, name, value);
      state.headers.push([name, value]);
    };

    proto.overrideMimeType = function (mime) {
      if (!states.has(this)) return native.overrideMimeType.call(this, mime);
    };

    proto.getResponseHeader = function (name) {
      const state = states.get(this);
      if (!state) return native.getResponseHeader.call(this, name);
      return state.readyState >= 2 ? headerValue(state.responseHeaders, name) : null;
    };

    proto.getAllResponseHeaders = function () {
      const state = states.get(this);
      if (!state) return native.getAllResponseHeaders.call(this);
      return state.responseHeaders.map(([key, value]) => `${key.toLowerCase()}: ${value}`).join("\r\n");
    };

    proto.abort = function () {
      const state = states.get(this);
      if (!state) return native.abort.call(this);
      if (state.aborted || state.readyState === 4) return;
      state.aborted = true;
      state.readyState = 4;
      fire(this, "readystatechange");
      fire(this, "abort");
      fire(this, "loadend");
    };

    proto.send = function (body) {
      const state = states.get(this);
      if (!state) return native.send.call(this, body);
      const xhr = this;
      fire(xhr, "loadstart");
      appRequest(state.method, state.url, state.headers, body).then(
        (response) => {
          if (state.aborted) return;
          state.status = response.status;
          state.statusText = STATUS_TEXT[response.status] || "";
          state.responseHeaders = response.headers;
          state.body = response.body;
          state.text = decoder.decode(response.body);
          state.readyState = 4;
          fire(xhr, "readystatechange");
          fire(xhr, "load");
          fire(xhr, "loadend");
        },
        (error) => {
          if (state.aborted) return;
          console.error("ReferralPilot request failed", state.url, error);
          state.readyState = 4;
          fire(xhr, "readystatechange");
          fire(xhr, "error");
          fire(xhr, "loadend");
        },
      );
    };
  })();

  const nativeFetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    const path = typeof input === "string" ? appPath(input) : null;
    if (path === null) return nativeFetch(input, init);
    init = init || {};
    const headers = [];
    new Headers(init.headers || {}).forEach((value, key) => headers.push([key, value]));
    const response = await appRequest(init.method || "GET", path, headers, init.body == null ? null : init.body);
    const noBody = [101, 204, 205, 304].includes(response.status);
    return new Response(noBody ? null : response.body, { status: response.status, headers: response.headers });
  };

  // ---------------------------------------------------------- pages ----------
  function currentRoute() {
    const hash = location.hash;
    return hash.startsWith("#/") ? hash.slice(1) : "/";
  }

  function navigate(path) {
    if (currentRoute() === path) loadRoute();
    else location.hash = "#" + path;
  }

  let routeSeq = 0;
  async function loadRoute() {
    const seq = ++routeSeq;
    const path = currentRoute();
    let response;
    try {
      response = await appRequest("GET", path, [["Accept", "text/html"]], null);
    } catch (error) {
      if (!stopped) showMessage("Something went wrong", String(error), true);
      return;
    }
    if (seq !== routeSeq) return; // a newer navigation won
    const type = headerValue(response.headers, "content-type") || "";
    if (response.status === 404) {
      showMessage("Page not found", `There is nothing at ${path}.`, false);
      return;
    }
    if (response.status >= 400 || !type.includes("text/html")) {
      showMessage("Something went wrong", `${path} answered ${response.status}.`, true);
      return;
    }
    renderPage(decoder.decode(response.body));
  }

  function renderPage(html) {
    const doc = new DOMParser().parseFromString(html, "text/html");
    if (window.RPApp) window.RPApp.stop();
    const toasts = Array.from((document.getElementById("toasts") || { children: [] }).children);
    document.title = doc.title || "ReferralPilot";
    document.body.className = doc.body.className;
    document.body.replaceChildren(...Array.from(doc.body.childNodes, (node) => document.importNode(node, true)));
    const toastHost = document.getElementById("toasts");
    if (toastHost) toasts.forEach((toast) => toastHost.appendChild(toast)); // keep messages shown just before
    window.scrollTo(0, 0);
    if (window.htmx) htmx.process(document.body);
    if (window.RPApp) window.RPApp.init(document.body);
    updateGmailStatus();
    if (pendingNotice) {
      notify(...pendingNotice);
      pendingNotice = null;
    }
  }

  function showMessage(title, detail, offerReload) {
    const screen = document.createElement("div");
    screen.className = "rp-screen";
    screen.innerHTML = `<div class="rp-card"><div class="rp-logo" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"
      stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 17l5-10 3 6 2-3 4 7"/></svg></div>
      <h1></h1><p class="rp-sub"></p><div class="rp-actions"></div></div>`;
    screen.querySelector("h1").textContent = title;
    screen.querySelector(".rp-sub").textContent = detail;
    const actions = screen.querySelector(".rp-actions");
    const home = document.createElement("a");
    home.className = "rp-btn rp-btn-primary";
    home.href = "#/";
    home.textContent = "Back to the board";
    actions.appendChild(home);
    if (offerReload) {
      const reload = document.createElement("button");
      reload.className = "rp-btn";
      reload.textContent = "Reload";
      reload.onclick = () => location.reload();
      actions.appendChild(reload);
    }
    if (window.RPApp) window.RPApp.stop();
    document.body.replaceChildren(screen);
  }

  window.addEventListener("hashchange", () => { if (!stopped) loadRoute(); });

  // Links to app paths become hash routes; target=_blank / download links are served from the worker.
  document.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.button !== 0) return;
    const link = event.target.closest && event.target.closest("a[href]");
    if (!link) return;
    const path = appPath(link.getAttribute("href"));
    if (path === null) return;
    event.preventDefault();
    if (link.hasAttribute("download")) saveFile(path, link.getAttribute("download"));
    else if (link.target === "_blank" || event.ctrlKey || event.metaKey || event.shiftKey) openFile(path);
    else navigate(path);
  });

  // Forms: htmx handles its own; plain GET forms (e.g. the activity-log filter) become routes.
  document.addEventListener("submit", (event) => {
    if (event.defaultPrevented) return;
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (HTMX_VERBS.some((attr) => form.hasAttribute(attr))) {
      event.preventDefault(); // htmx drives these forms through their own triggers
      return;
    }
    const action = form.getAttribute("action");
    if (action && appPath(action) === null) return; // a real cross-site form, e.g. "Open in Overleaf"
    event.preventDefault();
    let data;
    try { data = new FormData(form, event.submitter || null); } catch (_) { data = new FormData(form); }
    const params = new URLSearchParams();
    for (const [key, value] of data) if (typeof value === "string") params.append(key, value);
    const target = action || currentRoute().split("?")[0];
    if ((form.getAttribute("method") || "get").toLowerCase() === "get") {
      const query = params.toString();
      navigate(target + (query ? "?" + query : ""));
    } else {
      appRequest("POST", target, [["Content-Type", "application/x-www-form-urlencoded"], ["HX-Request", "true"]], params)
        .then(() => loadRoute());
    }
  });

  function fileName(path, headers, suggested) {
    if (suggested) return suggested;
    const disposition = headerValue(headers, "content-disposition") || "";
    const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
    if (match) {
      try { return decodeURIComponent(match[1]); } catch (_) { return match[1]; }
    }
    return path.split("?")[0].split("/").pop() || "download";
  }

  function blobUrl(response) {
    const type = headerValue(response.headers, "content-type") || "application/octet-stream";
    return URL.createObjectURL(new Blob([response.body], { type }));
  }

  async function saveFile(path, suggested) {
    try {
      const response = await appRequest("GET", path, [], null);
      if (response.status !== 200) throw new Error(`HTTP ${response.status}`);
      const url = blobUrl(response);
      const link = document.createElement("a");
      link.href = url;
      link.download = fileName(path, response.headers, suggested);
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch (error) {
      notify(`Download failed: ${error.message}`, "error");
    }
  }

  async function openFile(path) {
    const tab = window.open("about:blank", "_blank"); // opened during the click so pop-up blockers allow it
    try {
      const response = await appRequest("GET", path, [], null);
      if (response.status !== 200) throw new Error(`HTTP ${response.status}`);
      const type = headerValue(response.headers, "content-type") || "";
      const url = type.includes("text/html") ? BASE + "#" + path : blobUrl(response);
      if (tab) tab.location.href = url;
      else window.location.assign(url);
      if (url.startsWith("blob:")) setTimeout(() => URL.revokeObjectURL(url), 10 * 60000);
    } catch (error) {
      if (tab) tab.close();
      notify(`Could not open the file: ${error.message}`, "error");
    }
  }

  function notify(message, kind) {
    if (window.toast) window.toast(message, kind);
    else console.warn(message);
  }

  // Show a message on the page we are about to open (rendering replaces the toast area).
  let pendingNotice = null;
  function notifyAndGo(path, message, kind) {
    if (currentRoute().split("?")[0] === path) {
      notify(message, kind);
      return;
    }
    pendingNotice = [message, kind];
    navigate(path);
  }

  // ----------------------------------------------------------- Gmail ---------
  let tokenClient = null;
  let gisLoading = null;
  let expiryTimer = null;

  function loadGis() {
    if (window.google && google.accounts && google.accounts.oauth2) return Promise.resolve();
    if (!gisLoading) {
      gisLoading = new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = GIS_SRC;
        script.async = true;
        script.onload = () => resolve();
        script.onerror = () => { gisLoading = null; reject(new Error("could not load Google sign-in")); };
        document.head.appendChild(script);
      });
    }
    return gisLoading;
  }

  async function prepareGmail() {
    tokenClient = null;
    if (clientConfig.email_backend !== "gmail_web" || !clientConfig.gmail_client_id) return;
    try {
      await loadGis();
      tokenClient = google.accounts.oauth2.initTokenClient({
        client_id: clientConfig.gmail_client_id,
        scope: clientConfig.gmail_scopes,
        callback: onGmailToken,
        error_callback: (error) => notify(`Google sign-in closed: ${error && error.type ? error.type : "cancelled"}`, "info"),
      });
    } catch (error) {
      notify(`Google sign-in unavailable: ${error.message}`, "error");
    }
  }

  async function refreshClientConfig() {
    const msg = await rpc("call", { fn: "client_config", args: [] });
    clientConfig = msg.result || {};
    await prepareGmail();
    updateGmailStatus();
  }

  function connectGmail() {
    if (clientConfig.email_backend !== "gmail_web") {
      notifyAndGo("/settings", "Choose gmail_web as the sending mode in Settings first", "info");
      return;
    }
    if (!clientConfig.gmail_client_id) {
      notifyAndGo("/settings", "Paste your Google OAuth client ID in Settings first (see the README)", "info");
      return;
    }
    if (!tokenClient) {
      notify("Google sign-in is still loading - try again in a moment", "info");
      prepareGmail();
      return;
    }
    tokenClient.requestAccessToken({ prompt: "" }); // must run inside the click for the pop-up
  }

  async function onGmailToken(response) {
    if (!response || response.error) {
      notify(`Google sign-in failed: ${response ? response.error : "no response"}`, "error");
      return;
    }
    const scopes = String(clientConfig.gmail_scopes || "").split(" ").filter(Boolean);
    if (google.accounts.oauth2.hasGrantedAnyScope && !google.accounts.oauth2.hasGrantedAnyScope(response, scopes[0])) {
      notify("Gmail send permission was not granted", "error");
      return;
    }
    let email = null;
    try {
      const profile = await nativeFetch("https://gmail.googleapis.com/gmail/v1/users/me/profile", {
        headers: { Authorization: `Bearer ${response.access_token}` },
      });
      if (profile.ok) email = (await profile.json()).emailAddress || null;
    } catch (_) { /* the address is only cosmetic */ }
    const expiresIn = Number(response.expires_in) || 3600;
    await setGmailToken(response.access_token, expiresIn, email);
    try {
      sessionStorage.setItem(GMAIL_TOKEN_KEY, JSON.stringify({
        token: response.access_token, email, expiresAt: Date.now() + expiresIn * 1000,
      }));
    } catch (_) { /* private mode: the token simply is not kept across reloads */ }
    notify(`Gmail connected${email ? " as " + email : ""} - queued emails go out while this tab is open`, "success");
  }

  async function setGmailToken(token, expiresIn, email) {
    const msg = await rpc("call", { fn: "set_gmail_token", args: [token, expiresIn, email] });
    clientConfig.gmail = msg.result || {};
    clearTimeout(expiryTimer);
    if (token) {
      expiryTimer = setTimeout(() => {
        clientConfig.gmail = { connected: false };
        updateGmailStatus();
        scheduleRefresh();
        notify("Gmail sign-in expired - click Connect Gmail to keep sending", "info");
      }, Math.max(0, expiresIn - 60) * 1000);
    }
    updateGmailStatus();
    scheduleRefresh();
  }

  async function disconnectGmail() {
    const token = (() => {
      try { return JSON.parse(sessionStorage.getItem(GMAIL_TOKEN_KEY) || "{}").token; } catch (_) { return null; }
    })();
    try { sessionStorage.removeItem(GMAIL_TOKEN_KEY); } catch (_) { /* ignore */ }
    if (token && window.google && google.accounts && google.accounts.oauth2) google.accounts.oauth2.revoke(token, () => {});
    await setGmailToken(null, 0, null);
    notify("Gmail disconnected", "success");
  }

  async function restoreGmailToken() {
    let saved = null;
    try { saved = JSON.parse(sessionStorage.getItem(GMAIL_TOKEN_KEY) || "null"); } catch (_) { saved = null; }
    if (!saved || !saved.token) return;
    const left = Math.floor((saved.expiresAt - Date.now()) / 1000);
    if (left > 120) await setGmailToken(saved.token, left, saved.email);
    else sessionStorage.removeItem(GMAIL_TOKEN_KEY);
  }

  function updateGmailStatus() {
    const gmail = clientConfig.gmail || {};
    document.querySelectorAll("[data-gmail-status]").forEach((el) => {
      if (gmail.connected) {
        const minutes = Math.max(1, Math.round((gmail.expires_in || 0) / 60));
        el.textContent = `Connected${gmail.email ? " as " + gmail.email : ""} (sign-in valid for ~${minutes} min).`;
      } else if (clientConfig.email_backend !== "gmail_web") {
        el.textContent = "Not in use: the sending mode is dry run.";
      } else {
        el.textContent = clientConfig.gmail_client_id ? "Not connected." : "Add your OAuth client ID below, save, then connect.";
      }
    });
  }

  document.addEventListener("click", (event) => {
    const target = event.target.closest && event.target.closest("[data-connect-gmail], [data-disconnect-gmail]");
    if (!target) return;
    event.preventDefault();
    if (target.hasAttribute("data-connect-gmail")) connectGmail();
    else disconnectGmail();
  });
  document.addEventListener("settingsChanged", () => { refreshClientConfig(); });

  // ------------------------------------------------------ boot screen --------
  function showProgress(text, pct) {
    const message = document.getElementById("rp-boot-msg");
    const bar = document.getElementById("rp-bar-fill");
    if (message && text) message.textContent = text;
    if (bar && typeof pct === "number") bar.style.width = Math.max(4, Math.min(100, pct)) + "%";
  }

  function bootFailed(error) {
    stopped = true;
    const card = document.querySelector("#rp-boot .rp-card");
    if (!card) {
      showMessage("ReferralPilot stopped", String(error), true);
      return;
    }
    showProgress("ReferralPilot could not start.", 100);
    const detail = document.createElement("p");
    detail.className = "rp-error";
    detail.textContent = `${error && error.message ? error.message : error}\n\nIt needs a recent Chrome, Edge, Firefox or Safari with site storage enabled.`;
    const actions = document.createElement("div");
    actions.className = "rp-actions";
    const reload = document.createElement("button");
    reload.className = "rp-btn rp-btn-primary";
    reload.textContent = "Try again";
    reload.onclick = () => location.reload();
    actions.appendChild(reload);
    card.append(detail, actions);
  }

  // --------------------------------------------------- one tab at a time -----
  const channel = "BroadcastChannel" in window ? new BroadcastChannel("referralpilot") : null;
  let releaseLock = null;

  function holdLock() {
    return new Promise((resolve) => { releaseLock = resolve; });
  }

  function claimLock(steal) {
    return new Promise((resolve, reject) => {
      const options = steal ? { steal: true } : { ifAvailable: true };
      navigator.locks.request(LOCK_NAME, options, (lock) => {
        resolve(Boolean(lock));
        return lock ? holdLock() : undefined;
      }).catch((error) => {
        // Another tab took the app over (it "stole" the lock from this one).
        if (error && error.name === "AbortError") handOver();
        else reject(error);
      });
    });
  }

  async function handOver() {
    if (stopped) return;
    try { if (worker) await rpc("flush", {}); } catch (_) { /* best effort */ }
    stopped = true;
    if (worker) worker.terminate();
    if (releaseLock) releaseLock();
    showMessage("ReferralPilot moved to another tab",
      "It runs in one tab at a time so emails are never sent twice. Use it there, or bring it back here.", false);
    const actions = document.querySelector(".rp-actions");
    if (actions) {
      actions.replaceChildren();
      const back = document.createElement("button");
      back.className = "rp-btn rp-btn-primary";
      back.textContent = "Use it in this tab";
      back.onclick = () => location.reload();
      actions.appendChild(back);
    }
  }

  async function ensureSingleTab() {
    if (!navigator.locks) return; // very old browsers: no protection, still works
    if (await claimLock(false)) return;
    await new Promise((resolve) => {
      showProgress("ReferralPilot is already open in another tab.", 100);
      const card = document.querySelector("#rp-boot .rp-card");
      const actions = document.createElement("div");
      actions.className = "rp-actions";
      const take = document.createElement("button");
      take.className = "rp-btn rp-btn-primary";
      take.textContent = "Use it here instead";
      take.onclick = async () => {
        take.disabled = true;
        showProgress("Taking over from the other tab…", 10);
        // Ask the other tab to save and stop, give it a moment, then take the lock regardless.
        if (channel) channel.postMessage({ type: "takeover" });
        await new Promise((done) => setTimeout(done, 1500));
        await claimLock(true);
        actions.remove();
        resolve();
      };
      actions.appendChild(take);
      if (card) card.appendChild(actions);
    });
  }

  if (channel) {
    channel.onmessage = (event) => {
      if (event.data && event.data.type === "takeover" && releaseLock && !stopped) handOver();
    };
  }

  // ------------------------------------------------------------ boot ---------
  async function boot() {
    await ensureSingleTab();
    showProgress("Checking for updates…", 6);
    const manifest = await nativeFetch(new URL("manifest.json", BASE), { cache: "no-store" }).then((response) => {
      if (!response.ok) throw new Error(`manifest.json: HTTP ${response.status}`);
      return response.json();
    });
    worker = new Worker(new URL(`web/worker.js?v=${encodeURIComponent(manifest.build)}`, BASE), { type: "module" });
    worker.onmessage = onWorkerMessage;
    worker.onerror = (event) => bootFailed(event.message || "the background worker crashed");
    const env = {};
    try {
      const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
      if (zone) env.APP_TIMEZONE = zone;
    } catch (_) { /* UTC */ }
    const msg = await rpc("boot", { base: BASE, manifest, env });
    clientConfig = msg.result || {};
    ready.resolve();
    showProgress("Ready", 100);
    await restoreGmailToken();
    prepareGmail();
    if (navigator.storage && navigator.storage.persist) {
      navigator.storage.persisted().then((done) => { if (!done) navigator.storage.persist(); }).catch(() => {});
    }
    await loadRoute();
  }

  window.RPWeb = {
    reload: () => loadRoute(),
    navigate,
    connectGmail,
    request: appRequest,
  };

  document.addEventListener("DOMContentLoaded", () => {
    if (window.htmx) htmx.config.historyEnabled = false;
    boot().catch((error) => {
      console.error(error);
      ready.reject(error);
      bootFailed(error);
    });
  });
})();
