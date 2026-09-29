/*!
  vn_player.js
  Reusable Visual Novel (VN) overlay engine that renders into a given container element.

  Stable version:
  - Background cues apply silently and never block progression.
  - First click while typing reveals full text; next click advances.
  - Space/Enter/Click to advance; Next button always advances.
  - No re-entrancy stalls; non-blocking audio playback.
  - Shows only current speaker + those who spoke in the last N lines (gray).
*/

(function (global) {
  'use strict';

  const STYLE_ID = 'vn-player-styles-stable';

  function injectStyles() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = `
      .vnRoot { position: absolute; inset: 0; display: none; overflow: hidden; }
      .vnRoot .vnCanvas { position: absolute; inset: 0; display: grid; grid-template-rows: 1fr auto; }
      .vnRoot .vnLayer { position: relative; overflow: hidden; }
      .vnRoot .vnBackground {
        position: absolute; inset: 0;
        width: 100%; height: 100%;
        object-fit: cover;
        filter: brightness(0.9) contrast(1.05);
        z-index: 1;
      }
      .vnRoot .vnStage {
        position: relative; z-index: 2;
        display: flex; justify-content: center; align-items: flex-end; padding: 16px; gap: 12px;
        height: 100%;
        pointer-events: none; /* allow clicks to pass to overlay/dialogue */
      }
      .vnRoot .vnPortrait {
        width: 128px; height: 128px; object-fit: contain;
        filter: grayscale(60%); opacity: 0.7;
        transition: filter 0.2s, opacity 0.2s, transform 0.2s;
        transform-origin: bottom center;
      }
      .vnRoot .vnPortrait.active { filter: none; opacity: 1; transform: scale(1.05); }
      .vnRoot .vnDialogue {
        position: relative; z-index: 3;
        background: rgba(0,0,0,0.7); border-top: 1px solid #2a3145; padding: 12px;
        font-family: system-ui, Arial, sans-serif;
      }
      .vnRoot .vnName { font-weight: 700; color: #a1c1ff; margin-bottom: 6px; min-height: 1em; }
      .vnRoot .vnText { min-height: 56px; line-height: 1.5; }
      .vnRoot .vnControls { display: flex; justify-content: space-between; align-items: center; margin-top: 8px; }
      .vnRoot .vnHint { font-size: 12px; color: #94a3b8; }
      .vnRoot .vnNext { padding: 6px 10px; border-radius: 6px; border: 1px solid #2a3145; background: #3452a5; color: #fff; cursor: pointer; }
      .vnFadeIn { animation: vnFadeIn 240ms ease-in forwards; }
      .vnFadeOut { animation: vnFadeOut 180ms ease-out forwards; }
      @keyframes vnFadeIn { from { opacity: 0; } to { opacity: 1; } }
      @keyframes vnFadeOut { from { opacity: 1; } to { opacity: 0; } }
    `;
    document.head.appendChild(style);
  }

  function createOverlay(rootEl, hintText) {
    const overlay = document.createElement('div');
    overlay.className = 'vnRoot';

    const canvas = document.createElement('div');
    canvas.className = 'vnCanvas';

    const layer = document.createElement('div');
    layer.className = 'vnLayer';

    const bgImg = document.createElement('img');
    bgImg.className = 'vnBackground';
    bgImg.alt = 'background';
    bgImg.src = '';

    const stage = document.createElement('div');
    stage.className = 'vnStage';

    const dialogue = document.createElement('div');
    dialogue.className = 'vnDialogue';

    const name = document.createElement('div');
    name.className = 'vnName';
    name.textContent = '';

    const text = document.createElement('div');
    text.className = 'vnText';
    text.textContent = '';

    const controls = document.createElement('div');
    controls.className = 'vnControls';

    const hint = document.createElement('div');
    hint.className = 'vnHint';
    hint.textContent = hintText || 'Space/Enter or click Next ▶';

    const next = document.createElement('button');
    next.className = 'vnNext';
    next.textContent = 'Next ▶';
    next.type = 'button';

    const audio = document.createElement('audio');
    audio.preload = 'auto';
    audio.controls = false;

    controls.appendChild(hint);
    controls.appendChild(next);

    dialogue.appendChild(name);
    dialogue.appendChild(text);
    dialogue.appendChild(controls);
    dialogue.appendChild(audio);

    layer.appendChild(bgImg);
    layer.appendChild(stage);

    canvas.appendChild(layer);
    canvas.appendChild(dialogue);
    overlay.appendChild(canvas);

    const currentPos = getComputedStyle(rootEl).position;
    if (currentPos === 'static' || !currentPos) rootEl.style.position = 'relative';
    rootEl.appendChild(overlay);
    return { overlay, bgImg, stage, name, text, next, audio };
  }

  function VNPlayerInstance(opts) {
    injectStyles();

    const rootEl = opts.rootEl;
    const typeSpeed = typeof opts.typeSpeed === 'number' ? Math.max(0, opts.typeSpeed) : 16;
    const onEnd = typeof opts.onEnd === 'function' ? opts.onEnd : () => {};
    const hintText = opts.hintText || undefined;
    const recentWindow = Number.isFinite(opts.recentWindow) ? Math.max(1, Math.floor(opts.recentWindow)) : 10;

    const dom = createOverlay(rootEl, hintText);
    const overlayEl = dom.overlay;
    const stageEl = dom.stage;
    const nameEl = dom.name;
    const textEl = dom.text;
    const nextBtn = dom.next;
    const audioEl = dom.audio;
    const bgImgEl = dom.bgImg;

    let manifest = null;
    let script = [];
    let portraitsByName = new Map(); // name -> full URL
    let idx = -1;                   // last processed index; starts before the script
    let currentSpeaker = null;
    let active = false;
    const spokenCounts = new Map(); // name -> spoken line count
    const spokenHistory = [];       // names of speakers in shown lines (for recency)

    // Typewriter state
    let isTyping = false;
    let currentFullText = '';

    function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

    async function typewriter(el, text) {
      currentFullText = text || '';
      if (typeSpeed === 0 || !currentFullText) {
        el.textContent = currentFullText;
        isTyping = false;
        return;
      }
      isTyping = true;
      el.textContent = '';
      for (let i = 0; i < currentFullText.length; i++) {
        if (!isTyping) { el.textContent = currentFullText; return; }
        el.textContent += currentFullText[i];
        // eslint-disable-next-line no-await-in-loop
        await sleep(typeSpeed);
      }
      isTyping = false;
    }

    function setBackground(file_name) {
      if (!manifest) return;
      const base = manifest.base_url || '';
      let rel = null;
      const list = (manifest.assets && manifest.assets.backgrounds) || [];
      const entry = list.find(b => b.file_name === file_name);
      if (entry && entry.path) rel = entry.path;
      if (!rel) rel = 'backgrounds/' + file_name;
      const url = base ? (base + '/' + rel) : rel;
      bgImgEl.src = url;
    }

    function buildPortraitMap() {
      portraitsByName.clear();
      const base = manifest.base_url || '';
      const arr = (manifest.assets && manifest.assets.portraits) || [];
      arr.forEach(p => {
        portraitsByName.set(p.character_name, base ? (base + '/' + p.path) : p.path);
      });
    }

    function recentSpeakers(windowSize) {
      const recent = [];
      for (let i = spokenHistory.length - 1; i >= 0 && recent.length < windowSize; i--) {
        const nm = spokenHistory[i];
        if (!recent.includes(nm)) recent.push(nm);
      }
      return recent.reverse();
    }

    function renderStageCharacters() {
      stageEl.innerHTML = '';
      const recents = recentSpeakers(recentWindow);
      const visibleSet = new Set(recents);
      if (currentSpeaker) visibleSet.add(currentSpeaker);
      const visible = Array.from(visibleSet).filter(n => portraitsByName.has(n));
      const ordered = visible.filter(n => n !== currentSpeaker).concat(currentSpeaker ? [currentSpeaker] : []);
      ordered.forEach(name => {
        const img = document.createElement('img');
        img.className = 'vnPortrait' + (name === currentSpeaker ? ' active' : '');
        img.src = portraitsByName.get(name);
        img.alt = name;
        img.title = name;
        stageEl.appendChild(img);
      });
    }

    function voicePathFor(speaker, nth) {
      const c = (manifest.characters || []).find(x => x.character_name === speaker);
      if (!c || !Array.isArray(c.voices)) return null;
      return c.voices[nth] || null;
    }

    function playVoiceFor(speaker) {
      if (!speaker) return;
      const n = spokenCounts.get(speaker) || 0;
      const rel = voicePathFor(speaker, n);
      spokenCounts.set(speaker, n + 1);
      if (!rel) return;
      const base = manifest.base_url || '';
      const url = rel.startsWith('http') ? rel : (base ? (base + '/' + rel) : rel);
      try {
        audioEl.src = url;
        const p = audioEl.play();
        if (p && typeof p.then === 'function') p.catch(() => {});
      } catch (_) { /* ignore */ }
    }

    // Advance to the next dialogue line.
    // Applies background(s) silently while skipping them.
    async function showNextLine() {
      // If a line is still typing: reveal text immediately
      if (isTyping) { isTyping = false; textEl.textContent = currentFullText; return; }

      const n = script.length;
      let i = idx + 1;
      while (i < n) {
        const item = script[i];

        // Guard against malformed entries
        const t = item && item.type ? String(item.type) : '';
        if (t === 'background') {
          const fname = item.file_name || 'bg_000.png';
          setBackground(fname);
          i += 1;
          continue; // do not pause on background
        }
        if (t === 'line') {
          idx = i; // commit index
          currentSpeaker = item.name || '';
          if (currentSpeaker) spokenHistory.push(currentSpeaker);
          renderStageCharacters();
          nameEl.textContent = currentSpeaker || '';
          await typewriter(textEl, item.text || '');
          playVoiceFor(currentSpeaker);
          return; // stop on this line
        }
        // Unknown -> skip
        i += 1;
      }

      // End of script
      idx = n;
      await end();
    }

    function onKey(ev) {
      if (!active) return;
      const tag = (ev.target && ev.target.tagName) ? ev.target.tagName.toLowerCase() : '';
      if (tag === 'input' || tag === 'textarea') return;

      const key = ev.key || '';
      const code = ev.code || '';
      const isNext =
        code === 'Enter' || code === 'Space' || key === 'Enter' || key === ' ' || key === 'Spacebar';
      if (isNext) { ev.preventDefault(); showNextLine(); }
    }

    function onOverlayClick(ev) {
      if (!active) return;
      const target = ev.target;
      if (target && (target.closest('a') || target.closest('audio') || target.closest('button'))) return;
      ev.preventDefault();
      showNextLine();
    }

    function onNextClick(ev) {
      if (!active) return;
      ev.preventDefault();
      ev.stopPropagation();
      showNextLine();
    }

    async function start(m) {
      manifest = m || {};
      script = Array.isArray(manifest.script) ? manifest.script : [];

      buildPortraitMap();

      // Initialize background from the first background cue (if any)
      const firstBg = script.find(it => it && it.type === 'background' && it.file_name);
      if (firstBg && firstBg.file_name) setBackground(firstBg.file_name);
      else {
        const bgAssets = (manifest.assets && manifest.assets.backgrounds) || [];
        if (bgAssets.length > 0) setBackground(bgAssets[0].file_name || 'bg_000.png');
      }

      // Reset state
      overlayEl.style.display = 'block';
      overlayEl.classList.remove('vnFadeOut');
      overlayEl.classList.add('vnFadeIn');

      active = true;
      idx = -1;
      currentSpeaker = null;
      spokenCounts.clear();
      spokenHistory.length = 0;
      isTyping = false;
      currentFullText = '';

      window.addEventListener('keydown', onKey);
      overlayEl.addEventListener('click', onOverlayClick);
      nextBtn.addEventListener('click', onNextClick);

      await sleep(50);
      // Jump immediately to first line (skip any leading backgrounds)
      await showNextLine();
    }

    async function end() {
      active = false;
      overlayEl.classList.remove('vnFadeIn');
      overlayEl.classList.add('vnFadeOut');
      await sleep(150);
      overlayEl.style.display = 'none';

      window.removeEventListener('keydown', onKey);
      overlayEl.removeEventListener('click', onOverlayClick);
      nextBtn.removeEventListener('click', onNextClick);

      try { onEnd(); } catch (_) {}
    }

    async function loadAndStart(manifestUrl) {
      const res = await fetch(manifestUrl);
      if (!res.ok) throw new Error(`Failed to load manifest: ${res.status}`);
      const m = await res.json();
      return start(m);
    }

    function dispose() {
      try {
        window.removeEventListener('keydown', onKey);
        overlayEl.removeEventListener('click', onOverlayClick);
        nextBtn.removeEventListener('click', onNextClick);
      } catch (_) {}
      if (overlayEl && overlayEl.parentElement === rootEl) {
        rootEl.removeChild(overlayEl);
      }
    }

    return { start, loadAndStart, dispose };
  }

  const VNPlayer = {
    create(opts) {
      if (!opts || !opts.rootEl) throw new Error('VNPlayer.create requires a { rootEl }');
      return VNPlayerInstance(opts);
    },
  };

  global.VNPlayer = VNPlayer;
})(window);