/* EnReal page logic: pick an engine, parse an image, hand the stage to the game.

   The engine comes from backend.js (window.EnRealBackend). The stage reaches game.html
   through sessionStorage (key enreal_payload), with window.name as a fallback, in the
   shape game.js expects: { skeleton, bgDataUrl }.
*/
(function () {
  'use strict';

  const MAX_UPLOAD_BYTES = 30 * 1024 * 1024; // matches server/app.py
  const PAYLOAD_KEY = 'enreal_payload';

  const $ = (id) => document.getElementById(id);

  let selection = null; // { backend, choice, reason } once resolved
  let pendingSelection = null;

  function setStatus(text, kind) {
    const el = $('status');
    el.textContent = text;
    el.dataset.kind = kind || 'info';
  }

  function intInput(id, fallback, min, max) {
    const n = parseInt($(id).value, 10);
    if (!Number.isFinite(n)) return fallback;
    return Math.min(max, Math.max(min, n));
  }

  function readParams() {
    return {
      max_dim: intInput('max_dim', 1280, 64, 4096),
      grid_size: intInput('grid_size', 8, 1, 64),
      scene: $('scene').value === 'ui' ? 'ui' : 'art',
    };
  }

  function describe(sel) {
    const where = sel.backend.describe();
    return sel.choice === 'auto' ? `${where} (auto: ${sel.reason})` : where;
  }

  async function activate(choice) {
    $('run').disabled = true;
    if (selection) selection.backend.dispose();
    selection = null;
    setStatus('Choosing an engine...');

    pendingSelection = window.EnRealBackend.select(choice);
    const sel = await pendingSelection;
    selection = sel;

    if (sel.backend.mode === 'local') {
      setStatus('Loading the in-browser engine. The first visit downloads about 22 MB; later visits use the cache.');
      $('run').disabled = false; // processing waits for the load to finish
      try {
        await sel.backend.ready((p) => {
          if (selection === sel) setStatus(`${p.message}...`);
        });
        if (selection === sel) setStatus(`Ready: ${describe(sel)}.`, 'ok');
      } catch (err) {
        if (selection === sel) setStatus(`The in-browser engine failed to load: ${err.message}`, 'error');
      }
    } else {
      setStatus(`Ready: ${describe(sel)}.`, 'ok');
      $('run').disabled = false;
    }
  }

  function storePayload(payload) {
    const json = JSON.stringify(payload);
    let stored = false;
    try {
      sessionStorage.setItem(PAYLOAD_KEY, json);
      stored = true;
    } catch (e) {
      // Too large or blocked. Clear any older stage so the game cannot load it by mistake.
      try { sessionStorage.removeItem(PAYLOAD_KEY); } catch (_) { /* blocked entirely */ }
      console.warn('[app] sessionStorage unavailable, using window.name:', e);
    }
    try {
      window.name = json;
      stored = true;
    } catch (e) {
      console.warn('[app] window.name unavailable:', e);
    }
    if (!stored) throw new Error('the browser refused to store the stage; try a smaller max size');
  }

  async function processImage() {
    const input = $('file');
    if (!input.files || input.files.length === 0) {
      setStatus('Choose an image first.', 'error');
      return;
    }
    const file = input.files[0];
    if (file.size > MAX_UPLOAD_BYTES) {
      setStatus('That image is over 30 MB. Pick a smaller one.', 'error');
      return;
    }
    if (!selection) await pendingSelection;
    const sel = selection;

    $('run').disabled = true;
    $('play').disabled = true;
    $('overlay').removeAttribute('src');
    $('json').textContent = '';
    setStatus(sel.backend.mode === 'local' ? 'Parsing in your browser...' : 'Parsing on the server...');

    const started = performance.now();
    try {
      const data = await sel.backend.process(file, readParams());
      const seconds = ((performance.now() - started) / 1000).toFixed(1);
      $('overlay').src = data.overlay_image_b64;
      $('json').textContent = JSON.stringify(data.skeleton, null, 2);
      storePayload({ skeleton: data.skeleton, bgDataUrl: data.overlay_image_b64 });
      $('play').disabled = false;
      const curves = data.skeleton.curves.length;
      setStatus(`Parsed in ${seconds}s: ${curves} terrain ${curves === 1 ? 'curve' : 'curves'}. Press Play.`, 'ok');
    } catch (err) {
      console.error(err);
      setStatus(`Could not parse the image: ${err.message}`, 'error');
    } finally {
      $('run').disabled = false;
    }
  }

  window.addEventListener('DOMContentLoaded', () => {
    const picker = $('engine');
    picker.value = window.EnRealBackend.initialChoice();
    picker.addEventListener('change', () => {
      window.EnRealBackend.savePreference(picker.value);
      activate(picker.value);
    });
    $('run').addEventListener('click', processImage);
    $('play').addEventListener('click', () => {
      window.location.href = 'game.html';
    });
    activate();
  });
})();
