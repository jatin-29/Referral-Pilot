/* ReferralPilot browser edition - the Web Worker that runs the Python app.
 *
 * Loads Pyodide and the referralpilot package, keeps the SQLite database and
 * generated files on an IndexedDB-backed file system, serves the
 * page's requests through referralpilot.web.handle() and runs the timers that
 * a local install gets from APScheduler. Every call into Python runs to
 * completion before the next one starts.
 */

// The IndexedDB database is named after this mount point. All project pages of one GitHub
// user share an origin, so it must not be a generic name another app could also use.
const DATA_DIR = "/referralpilot-data";
const DB_FILE = `${DATA_DIR}/db/referralpilot.db`;
const SEND_EVERY_MS = 30_000;
const PERIODIC_EVERY_MS = 60_000;

let pyodide = null;
let web = null;
let chain = Promise.resolve();
let syncQueued = false;
let backgroundQueued = false;

function post(message, transfer) {
  self.postMessage(message, transfer || []);
}

function progress(text, pct) {
  post({ type: "progress", text, pct });
}

/** Run `task` after every earlier task has finished (Python is single-threaded and not re-entrant). */
function serial(task) {
  const run = chain.then(task);
  chain = run.catch(() => {});
  return run;
}

function toJs(value) {
  if (value && typeof value.toJs === "function") {
    const converted = value.toJs({ dict_converter: Object.fromEntries });
    if (typeof value.destroy === "function") value.destroy();
    return converted;
  }
  return value;
}

function syncfs(populate) {
  return new Promise((resolve, reject) => {
    pyodide.FS.syncfs(populate, (error) => (error ? reject(error) : resolve()));
  });
}

function dbStamp() {
  try {
    const stat = pyodide.FS.stat(DB_FILE);
    return `${stat.mtime.getTime()}:${stat.size}`;
  } catch (_) {
    return "";
  }
}

/** Persist the data directory to IndexedDB soon (coalesced, never while Python is running). */
function scheduleSync() {
  if (syncQueued) return;
  syncQueued = true;
  setTimeout(() => {
    serial(async () => {
      syncQueued = false;
      await syncfs(false);
    }).catch((error) => post({ type: "log", text: `saving failed: ${error}` }));
  }, 250);
}

function scheduleBackground() {
  if (backgroundQueued) return;
  backgroundQueued = true;
  setTimeout(() => {
    backgroundQueued = false;
    runTask("background");
  }, 0);
}

function afterCall(before, wrote) {
  const changed = dbStamp() !== before;
  if (wrote || changed) scheduleSync();
  if (web.background_pending()) scheduleBackground();
  return changed;
}

function runTask(name) {
  return serial(() => {
    const before = dbStamp();
    const result = toJs(web.run_task(name));
    if (afterCall(before, false)) post({ type: "changed", task: name });
    return result;
  }).catch((error) => post({ type: "log", text: `${name} failed: ${error}` }));
}

async function boot({ base, manifest, env }) {
  progress("Downloading Python (WebAssembly)…", 10);
  const indexURL = new URL(manifest.pyodide.indexURL, base).href;
  const { loadPyodide } = await import(`${indexURL}pyodide.mjs`);
  pyodide = await loadPyodide({
    indexURL,
    lockFileURL: new URL(manifest.pyodide.lockFile, base).href,
    stdout: (text) => post({ type: "log", text }),
    stderr: (text) => post({ type: "log", text }),
  });

  progress("Loading libraries…", 35);
  let loaded = 0;
  const total = manifest.pyodide.packages.length;
  await pyodide.loadPackage(manifest.pyodide.packages, {
    messageCallback: (message) => {
      if (/^Loaded /.test(message)) loaded = total;
      progress(message.length > 90 ? "Loading libraries…" : message, 35 + Math.round((loaded / total) * 30));
    },
    errorCallback: (message) => post({ type: "log", text: message }),
  });

  progress("Loading ReferralPilot…", 70);
  const appUrl = new URL(`${manifest.app.file}?v=${manifest.app.sha256.slice(0, 16)}`, base);
  const response = await fetch(appUrl);
  if (!response.ok) throw new Error(`${manifest.app.file}: HTTP ${response.status}`);
  pyodide.unpackArchive(await response.arrayBuffer(), "zip", { extractDir: "/app" });

  progress("Opening your saved data…", 80);
  const FS = pyodide.FS;
  FS.mkdirTree(DATA_DIR);
  FS.mount(FS.filesystems.IDBFS, {}, DATA_DIR);
  await syncfs(true);
  FS.mkdirTree(`${DATA_DIR}/db`);
  FS.mkdirTree(`${DATA_DIR}/exports`);

  progress("Starting the dashboard…", 90);
  pyodide.runPython("import sys\nif '/app' not in sys.path:\n    sys.path.insert(0, '/app')");
  web = pyodide.pyimport("referralpilot.web");
  const settings = pyodide.toPy({
    ...env,
    DATA_DIR: `${DATA_DIR}/db`,
    EXPORTS_DIR: `${DATA_DIR}/exports`,
    SITE_URL: base,
  });
  let info;
  try {
    info = toJs(web.bootstrap(settings));
  } finally {
    settings.destroy();
  }
  await syncfs(false);

  setTimeout(() => runTask("periodic"), 3000);
  setInterval(() => runTask("send"), SEND_EVERY_MS);
  setInterval(() => runTask("periodic"), PERIODIC_EVERY_MS);
  return info;
}

async function handle({ method, url, headers, body }) {
  const before = dbStamp();
  const pyHeaders = pyodide.toPy(headers || []);
  const bytes = body ? new Uint8Array(body) : undefined; // undefined arrives in Python as None
  let result;
  try {
    result = await web.handle(method, url, pyHeaders, bytes);
  } finally {
    pyHeaders.destroy();
  }
  const [status, outHeaders, content] = toJs(result);
  afterCall(before, method !== "GET" && method !== "HEAD");
  const view = content instanceof Uint8Array ? content : new Uint8Array(content || []);
  const buffer = view.buffer.slice(view.byteOffset, view.byteOffset + view.byteLength);
  return { status, headers: outHeaders, body: buffer };
}

function call(fn, args) {
  if (fn === "client_config") return toJs(web.client_config());
  if (fn === "set_gmail_token") {
    const [token, expiresIn, email] = args;
    return toJs(web.set_gmail_token(token || undefined, Number(expiresIn) || 0, email || undefined));
  }
  throw new Error(`unknown function ${fn}`);
}

self.onmessage = async (event) => {
  const { id, type } = event.data || {};
  try {
    if (type === "boot") {
      post({ id, ok: true, result: await boot(event.data) });
    } else if (type === "request") {
      const response = await serial(() => handle(event.data));
      post({ id, ok: true, ...response }, [response.body]);
    } else if (type === "call") {
      post({ id, ok: true, result: await serial(() => call(event.data.fn, event.data.args || [])) });
    } else if (type === "flush") {
      await serial(() => syncfs(false));
      post({ id, ok: true });
    } else {
      throw new Error(`unknown message ${type}`);
    }
  } catch (error) {
    post({ id, ok: false, error: String((error && (error.stack || error.message)) || error) });
  }
};
