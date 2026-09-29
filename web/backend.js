/* EnReal engine access: one interface, two implementations.

   RemoteBackend  POSTs the image to an EnReal server (server/app.py) at api/process.
   LocalBackend   runs the same Python engine in this browser, under Pyodide in a Web Worker.

   Both resolve process(file, params) to the same StageResult JSON, so the page and the game
   never need to know which one ran. See docs/design.md, "Frontend engine interface".

   Selection, first match wins:
     1. ?backend=local | remote in the page URL
     2. The choice saved from the engine picker (localStorage)
     3. auto: use a server if GET api/health answers as EnReal, otherwise run locally

   ?server=<url> points RemoteBackend at a server on another origin. That server must list
   this page's origin in ENREAL_CORS_ORIGINS.
*/
(function () {
  'use strict';

  const PREF_KEY = 'enreal.backend';
  const HEALTH_TIMEOUT_MS = 1500;

  // Build id stamped onto this script's URL by scripts/build_web.py (?v=...). Reused for the
  // worker so a cached worker.js can never pair with a newer page.
  const BUILD_QUERY = (function () {
    try {
      const src = document.currentScript && document.currentScript.src;
      return src ? new URL(src).search : '';
    } catch (_) {
      return '';
    }
  })();

  function query() {
    const q = new URLSearchParams(window.location.search);
    return { backend: q.get('backend'), server: q.get('server') };
  }

  function serverBase() {
    const { server } = query();
    if (server) return new URL(server.replace(/\/*$/, '/'), window.location.href).href;
    return new URL('./', window.location.href).href;
  }

  function errorDetail(body, fallback) {
    if (!body || body.detail === undefined) return fallback;
    if (typeof body.detail === 'string') return body.detail;
    // FastAPI validation errors: [{loc, msg, ...}]
    if (Array.isArray(body.detail)) return body.detail.map((d) => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join('; ');
    return JSON.stringify(body.detail);
  }

  class RemoteBackend {
    constructor(base) {
      this.mode = 'remote';
      this.base = base;
    }

    describe() {
      return `Server at ${this.base}`;
    }

    ready() {
      return Promise.resolve();
    }

    dispose() {}

    async process(file, params) {
      const url = new URL('api/process', this.base);
      for (const [key, value] of Object.entries(params)) url.searchParams.set(key, String(value));
      const form = new FormData();
      form.append('file', file);
      const res = await fetch(url, { method: 'POST', body: form });
      if (!res.ok) {
        let body = null;
        try { body = await res.json(); } catch (_) { /* not JSON */ }
        throw new Error(`Server error ${res.status}: ${errorDetail(body, res.statusText)}`);
      }
      return res.json();
    }
  }

  class LocalBackend {
    constructor() {
      this.mode = 'local';
      this.worker = null;
      this.nextId = 1;
      this.pending = new Map();
      this.readyPromise = null;
      this.progressListeners = [];
      this.info = null;
    }

    describe() {
      return this.info ? `In your browser (enreal ${this.info.version}, Pyodide ${this.info.pyodide})` : 'In your browser';
    }

    /** Start the worker and load Python, packages and the engine. Safe to call repeatedly. */
    ready(onProgress) {
      if (onProgress) this.progressListeners.push(onProgress);
      if (!this.readyPromise) {
        this.worker = new Worker('worker.js' + BUILD_QUERY, { type: 'module' });
        this.worker.onmessage = (event) => this._receive(event.data);
        this.worker.onerror = (event) => {
          const err = new Error(event.message || 'The engine worker failed to start.');
          for (const { reject } of this.pending.values()) reject(err);
          this.pending.clear();
        };
        this.readyPromise = this._call('init', {}).then((info) => {
          this.info = info;
          return info;
        });
        // Allow a retry after a failed load (for example a dropped connection mid-download).
        this.readyPromise.catch(() => {
          if (this.worker) this.worker.terminate();
          this.worker = null;
          this.readyPromise = null;
        });
      }
      return this.readyPromise;
    }

    /** Stop the worker and fail anything still waiting on it. */
    dispose() {
      if (this.worker) this.worker.terminate();
      this.worker = null;
      this.readyPromise = null;
      const err = new Error('The in-browser engine was stopped.');
      for (const { reject } of this.pending.values()) reject(err);
      this.pending.clear();
    }

    async process(file, params) {
      await this.ready();
      const bytes = new Uint8Array(await file.arrayBuffer());
      return this._call('process', { bytes, params }, [bytes.buffer]);
    }

    _call(type, payload, transfer) {
      const id = this.nextId++;
      return new Promise((resolve, reject) => {
        this.pending.set(id, { resolve, reject });
        this.worker.postMessage({ id, type, payload }, transfer || []);
      });
    }

    _receive(msg) {
      if (msg.type === 'progress') {
        for (const listener of this.progressListeners) {
          try { listener(msg); } catch (e) { console.warn('[backend] progress listener failed', e); }
        }
        return;
      }
      const entry = this.pending.get(msg.id);
      if (!entry) return;
      this.pending.delete(msg.id);
      if (msg.ok) entry.resolve(msg.result);
      else entry.reject(new Error(msg.error));
    }
  }

  async function probeServer(base) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), HEALTH_TIMEOUT_MS);
    try {
      const res = await fetch(new URL('api/health', base), { signal: controller.signal, cache: 'no-store' });
      if (!res.ok) return false;
      const body = await res.json();
      return Boolean(body && body.engine === 'enreal');
    } catch (_) {
      return false;
    } finally {
      clearTimeout(timer);
    }
  }

  function savedPreference() {
    try { return localStorage.getItem(PREF_KEY); } catch (_) { return null; }
  }

  /** Remember the picker choice. 'auto' clears it. */
  function savePreference(choice) {
    try {
      if (choice === 'local' || choice === 'remote') localStorage.setItem(PREF_KEY, choice);
      else localStorage.removeItem(PREF_KEY);
    } catch (_) { /* storage blocked: the choice lasts for this page only */ }
  }

  /** The choice in effect before probing: 'local', 'remote' or 'auto'. */
  function initialChoice() {
    const fromQuery = query().backend;
    if (fromQuery === 'local' || fromQuery === 'remote') return fromQuery;
    const saved = savedPreference();
    return saved === 'local' || saved === 'remote' ? saved : 'auto';
  }

  /** Resolve a backend. Returns { backend, choice, reason }. */
  async function select(choice) {
    const wanted = choice || initialChoice();
    const base = serverBase();
    if (wanted === 'remote') return { backend: new RemoteBackend(base), choice: wanted, reason: 'chosen' };
    if (wanted === 'local') return { backend: new LocalBackend(), choice: wanted, reason: 'chosen' };
    if (await probeServer(base)) return { backend: new RemoteBackend(base), choice: 'auto', reason: 'server found' };
    return { backend: new LocalBackend(), choice: 'auto', reason: 'no server found' };
  }

  window.EnRealBackend = { select, savePreference, initialChoice, RemoteBackend, LocalBackend };
})();
