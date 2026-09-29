// src/frontend/app.js
function getInputValue(id, fallback) {
  const el = document.getElementById(id);
  if (el && typeof el.value !== 'undefined' && el.value !== '') return el.value;
  return fallback;
}

async function processImage() {
  const fileInput = document.getElementById('file');
  if (!fileInput.files || fileInput.files.length === 0) {
    alert('Please select an image file first.');
    return;
  }
  const file = fileInput.files[0];

  const params = new URLSearchParams({
    max_dim: getInputValue('max_dim', '1280'),
    grid_size: getInputValue('grid_size', '8'),
    min_platform_len_pct: getInputValue('min_platform_len_pct', '0.22'),
    max_platforms: getInputValue('max_platforms', '200'),
    scene: getInputValue('scene', 'art'),
  });

  const formData = new FormData();
  formData.append('file', file);

  const overlayImg = document.getElementById('overlay');
  const jsonPre = document.getElementById('json');
  const playBtn = document.getElementById('play');

  overlayImg.src = '';
  jsonPre.textContent = 'Processing...';
  playBtn.disabled = true;

  try {
    const response = await fetch(`/api/process?${params.toString()}`, {
      method: 'POST',
      body: formData,
    });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(`Server error: ${response.status} ${text}`);
    }

    const data = await response.json();

    // Show overlay preview
    overlayImg.src = data.overlay_image_b64;
    jsonPre.textContent = JSON.stringify(data.skeleton, null, 2);

    // Prepare payload for the game page
    const payload = {
      skeleton: data.skeleton,
      bgDataUrl: data.overlay_image_b64,
      packId: data.pack_id || null,
      packBaseUrl: data.pack_base_url || null,
      manifestUrl: data.manifest_url || (data.pack_base_url ? (data.pack_base_url + '/manifest.json') : null),
    };

    // Primary channel: window.name
    try {
      window.name = JSON.stringify(payload);
    } catch (e) {
      console.warn('Failed to store payload in window.name:', e);
    }

    // Secondary: sessionStorage
    try {
      sessionStorage.setItem('enreal_payload', JSON.stringify(payload));
    } catch (e) {
      console.warn('Failed to store payload in sessionStorage:', e);
    }

    playBtn.disabled = false;
  } catch (err) {
    console.error(err);
    alert('Failed to process image: ' + err.message);
    jsonPre.textContent = 'Error.';
  }
}

window.addEventListener('DOMContentLoaded', () => {
  const runBtn = document.getElementById('run');
  const playBtn = document.getElementById('play');
  if (runBtn) runBtn.addEventListener('click', processImage);
  if (playBtn) playBtn.addEventListener('click', () => {
    window.location.href = 'game.html';
  });
});
