/* EnReal in-browser engine: runs the enreal Python package under Pyodide.

   Loaded by LocalBackend in backend.js as a module worker: Pyodide no longer runs in
   classic workers. Protocol (postMessage):
     in   { id, type: 'init' | 'process', payload }
     out  { id, ok: true, result } | { id, ok: false, error }
          { type: 'progress', stage, message }   (during init, no id)

   engine/engine.json is written by scripts/build_web.py and names the enreal wheel, its
   SHA-256, and the Pyodide release and packages to load, so versions live in one place.
*/

let pyodide = null;
let runEngine = null;
let initPromise = null;

function progress(stage, message) {
  self.postMessage({ type: 'progress', stage, message });
}

function toHex(buffer) {
  return Array.from(new Uint8Array(buffer), (b) => b.toString(16).padStart(2, '0')).join('');
}

/** Python errors carry the whole traceback; the last line holds the useful message. */
function pythonMessage(err) {
  const text = String((err && err.message) || err);
  const lines = text.trim().split('\n').filter((line) => line.trim());
  return lines.length ? lines[lines.length - 1] : text;
}

async function loadEngine() {
  progress('manifest', 'Reading engine manifest');
  const manifestUrl = new URL('engine/engine.json', self.location.href);
  const res = await fetch(manifestUrl, { cache: 'no-cache' });
  if (!res.ok) {
    throw new Error(`Engine manifest not found (HTTP ${res.status}). Build it with: uv run python scripts/build_web.py`);
  }
  const engine = await res.json();

  progress('runtime', `Loading the Python runtime (Pyodide ${engine.pyodide.version})`);
  const { loadPyodide } = await import(engine.pyodide.indexURL + 'pyodide.mjs');
  pyodide = await loadPyodide({ indexURL: engine.pyodide.indexURL });

  progress('packages', `Loading ${engine.pyodide.packages.join(', ')}`);
  await pyodide.loadPackage(engine.pyodide.packages, {
    messageCallback: (msg) => progress('packages', msg),
    errorCallback: (msg) => console.warn('[worker] loadPackage:', msg),
  });

  progress('engine', `Installing enreal ${engine.version}`);
  const wheelRes = await fetch(new URL('engine/' + engine.wheel.file, self.location.href));
  if (!wheelRes.ok) throw new Error(`Engine wheel not found (HTTP ${wheelRes.status}).`);
  const wheel = await wheelRes.arrayBuffer();
  const digest = toHex(await crypto.subtle.digest('SHA-256', wheel));
  if (digest !== engine.wheel.sha256) {
    throw new Error('Engine wheel failed its integrity check; reload the page to fetch a fresh copy.');
  }
  const sitePackages = pyodide.runPython("import sysconfig; sysconfig.get_paths()['purelib']");
  pyodide.unpackArchive(wheel, 'wheel', { extractDir: sitePackages });

  runEngine = pyodide.runPython(`
import importlib
import json

importlib.invalidate_caches()
import enreal


def _run(data, params_json):
    params = json.loads(params_json)
    return enreal.generate_skeleton(data.to_bytes(), **params).model_dump_json()


_run
`);
  const version = pyodide.runPython('enreal.__version__');
  progress('ready', `enreal ${version} ready`);
  return { version, pyodide: engine.pyodide.version };
}

async function handle(type, payload) {
  if (type === 'init') {
    if (!initPromise) {
      initPromise = loadEngine();
      initPromise.catch(() => { initPromise = null; });
    }
    return initPromise;
  }
  if (type === 'process') {
    await handle('init', {});
    try {
      return JSON.parse(runEngine(payload.bytes, JSON.stringify(payload.params || {})));
    } catch (err) {
      throw new Error(pythonMessage(err));
    }
  }
  throw new Error(`Unknown request type: ${type}`);
}

self.onmessage = async (event) => {
  const { id, type, payload } = event.data || {};
  try {
    const result = await handle(type, payload);
    self.postMessage({ id, ok: true, result });
  } catch (err) {
    self.postMessage({ id, ok: false, error: (err && err.message) || String(err) });
  }
};
