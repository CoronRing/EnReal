/* EnReal game: builds a playable stage from the engine's output.
   Matter-based platformer with pixel terrain editing and full mechanics:
   - Status bar + Inventory panel (pauses game)
   - NPC (chat UI on J nearby, pauses game)
   - Enemy AI: pursues, HP, damage from bullets/grenades/bombs, contact damage with immunity
   - Item pickup (Fast Gun -> autofire when equipped)
   - Bomb (K) place/detonate; Grenade (G) throw/bounce/detonate; explosions clear red pixels + damage enemies
   - Terrain paint (H) beneath player
   - Movement: sprint (Shift), double jump, drop-through (S), sprint-through walls

   Input: the payload app.js stores in sessionStorage (key enreal_payload) and window.name:
   { skeleton, bgDataUrl }, where bgDataUrl is the engine overlay. Pure red pixels in that
   image are the collision terrain.
*/

(function () {
  // -------------------- Payload handling --------------------
  function getPayload() {
    try {
      const s = sessionStorage.getItem('enreal_payload');
      if (s) return JSON.parse(s);
    } catch (e) {
      console.error('[getPayload] Error parsing enreal_payload:', e);
    }

    // Fallback for browsers that block sessionStorage: app.js also writes window.name.
    try {
      if (window.name && window.name.startsWith('{')) {
        const obj = JSON.parse(window.name);
        if (obj && obj.skeleton && obj.bgDataUrl) return obj;
      }
    } catch (e) {
      console.error('[getPayload] Error parsing window.name:', e);
    }

    return null;
  }

  function boot() {
    const payload = getPayload();
    if (!payload || !payload.skeleton) {
      const root = document.getElementById('game-root');
      root.innerHTML =
        '<div class="empty">No stage yet. <a href="index.html">Make one from an image</a>, then press Play.</div>';
      console.error('[boot] No stage data found.');
      return;
    }
    bootPhaser(payload);
  }

  // -------------------- Platformer --------------------

  function bootPhaser(payload) {
    // Tunables
    const RED_THRESH = { r: 200, g: 40, b: 40 };
    const PROBE_DEPTH = 6; // pixels below feet to search for ground
    const FOOT_FRAC = 0.45; // half width fraction for foot span

    // Movement
    const WALK_SPEED = 5;
    const SPRINT_SPEED = 8;
    const JUMP_VELOCITY = -8.5;
    const DOUBLE_JUMPS = 2;

    // Horizontal collision probe against red "walls"
    const H_PROBE = 6;
    const WALL_SAMPLE_STEP_Y = 2;
    const VERT_MARGIN = 2;

    // Shooting
    const SHOOT_COOLDOWN_MS_NORMAL = 200;
    const SHOOT_COOLDOWN_MS_FAST = 80;
    const BULLET_SPEED = 600;
    const BULLET_LIFETIME_MS = 2000;
    const BULLET_DAMAGE = 25;
    const MAX_BULLETS = 220; // safety cap

    // Drop-through platforms
    const DROP_THROUGH_MS = 220;

    // Bombs and grenades
    const BOMB_RADIUS = 60;
    const GRENADE_RADIUS = Math.floor(BOMB_RADIUS * 2 / 3);
    const EXPLOSION_AUTO_MS = 6000;
    const BOMB_DAMAGE = 100;
    const GRENADE_DAMAGE = 50;

    // Grenade physics (custom, pixel-aware)
    const GRENADE_SIZE = 6;
    const GRENADE_THROW_VX = 300;
    const GRENADE_THROW_VY = -300;
    const GRENADE_GRAVITY = 900;
    const GRENADE_BOUNCE = 0.55;
    const GRENADE_FRICTION = 0.98;
    const GRENADE_SLEEP_SPEED = 15;

    // Terrain painting (H)
    const PAINT_BRUSH_RADIUS = 3;
    const PAINT_OFFSET_Y = 2;

    // UI/Status
    const INITIAL_HEALTH = 100;
    const CONTACT_DAMAGE = 25;
    const CONTACT_IMMUNITY_MS = 3000;

    // NPC/Enemy/Object sizes
    const BLOCK_SIZE = 24;

    const skeleton = payload.skeleton;
    const bgDataUrl = payload.bgDataUrl || payload.overlay_image_b64; // support either key
    const worldW = skeleton.bounds.width;
    const worldH = skeleton.bounds.height;

    const config = {
      type: Phaser.AUTO,
      parent: 'game-root',
      backgroundColor: '#000000',
      scale: {
        mode: Phaser.Scale.FIT,
        autoCenter: Phaser.Scale.CENTER_BOTH,
        width: worldW,
        height: worldH,
      },
      physics: {
        default: 'matter',
        matter: {
          gravity: { y: 1 },
          enableSleeping: false,
          debug: false,
        },
      },
      fps: { target: 60, forceSetTimeOut: true },
      dom: { createContainer: true },
      scene: { preload, create, update },
    };

    let player;
    let cursors;
    let keys;
    let grounded = false;
    let jumpsRemaining = DOUBLE_JUMPS;
    const bgImageKey = 'bg';
    const bgCanvasKey = 'bgCanvas';
  let lastShotAt = 0;
  let facing = 1; // 1 right, -1 left
  let debugText;
  let bullets = []; // simple non-physics bullets
  let dropThroughUntil = 0;

    // Pause + UI
    let gamePaused = false;
    let health = INITIAL_HEALTH;
    let lastDamagedAt = -Infinity;
    let statusBar;        // container
    let healthText;       // text object
    let inventoryButton;  // button
    let inventoryUI;      // container overlay

    // Inventory data
    let inventory = []; // array of strings, e.g., ['fast_gun']
    let equipped = { weapon: 'normal' }; // 'normal' | 'fast'

    // Explosives
    let activeBomb = null;      // { sprite, placedAt }
    let grenades = [];          // [{ sprite, x, y, vx, vy, bornAt, exploded, sleeping }]

    // NPC / Enemy / Item data
    let npcs = [];     // [{ sprite, x, y }]
    let enemies = [];  // [{ sprite, x, y, vx, vy, hp, alive }]
    let items = [];    // [{ sprite, x, y, type }]
    let chatUI = null; // { container, messagesBox, inputDom, sendBtnDom, overlay }
    let chatActive = false;

    // Create the Phaser game
    const game = new Phaser.Game(config);

    // Pause/resume on window blur/focus
    window.addEventListener('blur', () => { if (!gamePaused) setPaused(game.scene.scenes[0], true); });
    window.addEventListener('focus', () => { if (gamePaused && !inventoryUI?.visible && !chatActive) setPaused(game.scene.scenes[0], false); });

    function preload() {
      this.load.image(bgImageKey, bgDataUrl);

      const g = this.make.graphics({ x: 0, y: 0, add: false });
      // Player texture
      g.fillStyle(0xffffff, 1.0);
      g.fillRect(0, 0, 24, 32);
      g.generateTexture('playerTex', 24, 32); g.clear();
      // Bullet texture
      g.fillStyle(0xffe066, 1.0);
      g.fillRect(0, 0, 8, 4);
      g.generateTexture('bulletTex', 8, 4); g.clear();
      // Bomb texture
      g.fillStyle(0x333333, 1.0);
      g.fillCircle(8, 8, 8);
      g.lineStyle(2, 0xffffff, 1.0);
      g.strokeCircle(8, 8, 8);
      g.generateTexture('bombTex', 16, 16); g.clear();
      // Grenade texture
      g.fillStyle(0x447744, 1.0);
      g.fillCircle(6, 6, 6);
      g.lineStyle(1, 0x88cc88, 1.0);
      g.strokeCircle(6, 6, 6);
      g.generateTexture('grenadeTex', 12, 12); g.clear();
      // Inventory button texture
      g.fillStyle(0x222222, 1.0);
      g.fillRoundedRect(0, 0, 80, 24, 6);
      g.lineStyle(2, 0xffffff, 1.0);
      g.strokeRoundedRect(0, 0, 80, 24, 6);
      g.generateTexture('invBtnTex', 80, 24); g.clear();
      // NPC texture (green block)
      g.fillStyle(0x2ecc71, 1.0);
      g.fillRect(0, 0, 24, 24);
      g.generateTexture('npcTex', 24, 24); g.clear();
      // Enemy texture (red block)
      g.fillStyle(0xe74c3c, 1.0);
      g.fillRect(0, 0, 24, 24);
      g.generateTexture('enemyTex', 24, 24); g.clear();
      // Item texture (blue square)
      g.fillStyle(0x3498db, 1.0);
      g.fillRect(0, 0, 24, 24);
      g.generateTexture('itemTex', 24, 24);
      g.destroy();
    }

    function create() {
      // Convert background to canvas texture for runtime editability
      let bgTextureKeyInUse = bgImageKey;
      try {
        const srcImg = this.textures.get(bgImageKey).getSourceImage();
        const canvasTex = this.textures.createCanvas(bgCanvasKey, worldW, worldH);
        const ctx = canvasTex.getContext();
        ctx.clearRect(0, 0, worldW, worldH);
        ctx.drawImage(srcImg, 0, 0, srcImg.width, srcImg.height, 0, 0, worldW, worldH);
        canvasTex.refresh();
        bgTextureKeyInUse = bgCanvasKey;
        console.log('[game] Background converted to editable canvas texture.');
      } catch (e) {
        console.warn('[game] Failed to create canvas texture; terrain edits disabled.', e);
      }

      // Background image
      const bg = this.add.image(0, 0, bgTextureKeyInUse).setOrigin(0, 0);
      bg.setDisplaySize(worldW, worldH);

      // World bounds for matter
      this.matter.world.setBounds(0, 0, worldW, worldH);

      // Player body
      player = this.matter.add.sprite(skeleton.spawn.x, skeleton.spawn.y, 'playerTex');
      player.setFixedRotation();
      player.setFrictionAir(0.02);
      player.setFriction(0.1);
      player.setFrictionStatic(0.2);

      // Input
      cursors = this.input.keyboard.createCursorKeys();
      keys = this.input.keyboard.addKeys({
        A: Phaser.Input.Keyboard.KeyCodes.A,
        D: Phaser.Input.Keyboard.KeyCodes.D,
        W: Phaser.Input.Keyboard.KeyCodes.W,
        SPACE: Phaser.Input.Keyboard.KeyCodes.SPACE,
        S: Phaser.Input.Keyboard.KeyCodes.S,
        J: Phaser.Input.Keyboard.KeyCodes.J, // shoot or interact
        K: Phaser.Input.Keyboard.KeyCodes.K, // bomb place/detonate
        G: Phaser.Input.Keyboard.KeyCodes.G, // grenade throw/detonate
        H: Phaser.Input.Keyboard.KeyCodes.H, // draw terrain
        SHIFT: Phaser.Input.Keyboard.KeyCodes.SHIFT,
        ESC: Phaser.Input.Keyboard.KeyCodes.ESC,
      });

      // Camera
      this.cameras.main.setBounds(0, 0, worldW, worldH);
      this.cameras.main.startFollow(player, false, 0.1, 0.1);
      this.input.keyboard.addCapture(['SPACE', 'LEFT', 'RIGHT', 'UP', 'DOWN']);

      // Debug HUD
      debugText = this.add.text(8, 40, '', { font: '12px monospace', fill: '#0f0' }).setScrollFactor(0).setDepth(1000);

      // Status bar + Inventory
      createStatusBar.call(this);
      createInventoryUI.call(this);

      // Spawn world objects
      spawnWorldObjects.call(this);

      console.log('[game] Scene created. World:', worldW, 'x', worldH, 'Spawn:', skeleton.spawn);
    }

    // ---------------- UI creation ----------------
    function createStatusBar() {
      statusBar = this.add.container(8, 8).setScrollFactor(0).setDepth(2000);
      const bg = this.add.rectangle(0, 0, 420, 28, 0x000000, 0.5).setOrigin(0, 0);
      bg.setStrokeStyle(1, 0xffffff, 0.6);
      healthText = this.add.text(8, 6, `HP: ${health}`, { font: '14px monospace', color: '#ffffff' });

      // Inventory button
      const btn = this.add.image(100, 4, 'invBtnTex').setOrigin(0, 0).setInteractive({ useHandCursor: true });
      const btnLabel = this.add.text(120, 8, 'Inventory', { font: '12px monospace', color: '#ffffff' });

  btn.on('pointerup', () => toggleInventory(this, true));
  statusBar.add([bg, healthText, btn, btnLabel]);
    }

    function createInventoryUI() {
      const width = Math.floor(worldW * 0.7);
      const height = Math.floor(worldH * 0.7);
      const x = Math.floor((worldW - width) / 2);
      const y = Math.floor((worldH - height) / 2);

      inventoryUI = this.add.container(x, y).setScrollFactor(0).setDepth(3000);
      inventoryUI.visible = false;

      // Dim overlay
      const overlay = this.add.rectangle(-x, -y, worldW, worldH, 0x000000, 0.5)
        .setScrollFactor(0).setInteractive();
      overlay.on('pointerup', () => toggleInventory(this, false));

      // Panel
      const panel = this.add.rectangle(0, 0, width, height, 0x101010, 0.95).setOrigin(0, 0);
      panel.setStrokeStyle(2, 0xffffff, 1);

      const leftW = Math.floor(width * 0.4);
      const rightW = width - leftW - 16;
      const leftPanel = this.add.rectangle(8, 8, leftW - 16, height - 16, 0x181818, 1).setOrigin(0, 0);
      leftPanel.setStrokeStyle(1, 0xaaaaaa, 1);
      const charTitle = this.add.text(16, 12, 'Character', { font: '16px monospace', color: '#ffffff' });
      const mannequin = this.add.rectangle(16 + (leftW - 16) / 2, height / 2, 80, 120, 0x333333, 1)
        .setStrokeStyle(1, 0xffffff)
        .setOrigin(0.5);
      const equipTitle = this.add.text(16, height - 60, 'Equipment', { font: '14px monospace', color: '#ffffff' });
      const weaponText = this.add.text(16, height - 40, `Weapon: ${equipped.weapon}`, { font: '12px monospace', color: '#bbbbbb' });

      const equipBtn = this.add.text(16, height - 24, '[Equip Fast Gun]', { font: '12px monospace', color: '#aaddff' })
        .setInteractive({ useHandCursor: true });
      equipBtn.on('pointerup', () => {
        if (!inventory.includes('fast_gun')) return;
        equipped.weapon = 'fast';
        weaponText.setText(`Weapon: ${equipped.weapon}`);
      });
      const unequipBtn = this.add.text(140, height - 24, '[Unequip]', { font: '12px monospace', color: '#ffccaa' })
        .setInteractive({ useHandCursor: true });
      unequipBtn.on('pointerup', () => {
        equipped.weapon = 'normal';
        weaponText.setText(`Weapon: ${equipped.weapon}`);
      });

      const rightPanelX = 8 + leftW + 8;
      const rightPanel = this.add.rectangle(rightPanelX, 8, rightW, height - 16, 0x181818, 1).setOrigin(0, 0);
      rightPanel.setStrokeStyle(1, 0xaaaaaa, 1);
      const invTitle = this.add.text(rightPanelX + 8, 12, 'Inventory', { font: '16px monospace', color: '#ffffff' });
      const invList = this.add.text(rightPanelX + 8, 34, '', { font: '12px monospace', color: '#dddddd', wordWrap: { width: rightW - 16 } });

      const closeBtn = this.add.text(x + width - 60, y + 8, '[Close]', { font: '14px monospace', color: '#ffdddd' })
        .setScrollFactor(0).setDepth(3100).setInteractive({ useHandCursor: true });
      closeBtn.on('pointerup', () => toggleInventory(this, false));

      function refreshInv() {
        invList.setText(inventory.length ? ('- ' + inventory.join('\n- ')) : '(empty)');
        equipBtn.setColor(inventory.includes('fast_gun') ? '#aaddff' : '#666666');
      }
      inventoryUI.refreshInv = refreshInv;

      inventoryUI.add([
        overlay, panel, leftPanel, charTitle, mannequin, equipTitle, weaponText,
        equipBtn, unequipBtn, rightPanel, invTitle, invList, closeBtn
      ]);
    }

    function toggleInventory(scene, open) {
      const desired = open !== undefined ? open : !inventoryUI.visible;
      inventoryUI.visible = desired;
      if (desired && typeof inventoryUI.refreshInv === 'function') inventoryUI.refreshInv();
      setPaused(scene, desired);
      console.log('[ui] Inventory', desired ? 'opened' : 'closed');
    }

    function setPaused(scene, paused) {
      gamePaused = paused;
      scene.matter.world.enabled = !paused;
    }

    // Chat UI
    function openChat(scene, npc) {
      if (chatActive) return;
      chatActive = true;
      setPaused(scene, true);

      const width = Math.floor(worldW * 0.5);
      const height = Math.floor(worldH * 0.5);
      const x = Math.floor((worldW - width) / 2);
      const y = Math.floor((worldH - height) / 2);

      const container = scene.add.container(x, y).setScrollFactor(0).setDepth(4000);

      const overlay = scene.add.rectangle(-x, -y, worldW, worldH, 0x000000, 0.5)
        .setScrollFactor(0).setInteractive();
      overlay.on('pointerup', () => closeChat(scene));

      const panel = scene.add.rectangle(0, 0, width, height, 0xffffff, 1).setOrigin(0, 0);
      panel.setStrokeStyle(2, 0x333333, 1);

      const header = scene.add.rectangle(0, 0, width, 40, 0x1877f2, 1).setOrigin(0, 0);
      const headerText = scene.add.text(12, 10, 'Chat with NPC', { font: '16px system-ui', color: '#ffffff' });

      const messagesBg = scene.add.rectangle(8, 48, width - 16, height - 100, 0xeeeeee, 1).setOrigin(0, 0);
      messagesBg.setStrokeStyle(1, 0xcccccc, 1);

      const messagesBox = scene.add.text(16, 56, '', {
        font: '14px system-ui',
        color: '#000000',
        wordWrap: { width: width - 32 }
      }).setOrigin(0, 0);

      const msgs = [];
      function addMsg(sender, text) {
        msgs.push({ sender, text });
        const formatted = msgs.map(m => (m.sender === 'npc' ? 'NPC: ' : 'You: ') + m.text).join('\n');
        messagesBox.setText(formatted);
      }
      addMsg('npc', 'Hello!');

      const inputDiv = scene.add.dom(12, height - 44, 'div', 'width:' + (width - 24) + 'px;height:36px;');
      const input = document.createElement('input');
      input.type = 'text';
      input.placeholder = 'Type a message...';
      input.style.width = (width - 110) + 'px';
      input.style.height = '32px';
      input.style.fontSize = '16px';
      input.style.padding = '4px 8px';
      const sendBtn = document.createElement('button');
      sendBtn.textContent = 'Send';
      sendBtn.style.marginLeft = '8px';
      sendBtn.style.height = '32px';
      sendBtn.style.fontSize = '14px';
      inputDiv.node.appendChild(input);
      inputDiv.node.appendChild(sendBtn);

      const closeBtn = scene.add.text(x + width - 60, y + 8, '[Close]', { font: '14px monospace', color: '#ffdddd' })
        .setScrollFactor(0).setDepth(4100).setInteractive({ useHandCursor: true });
      closeBtn.on('pointerup', () => closeChat(scene));

      function sendCurrent() {
        const text = (input.value || '').trim();
        if (!text) return;
        addMsg('you', text);
        input.value = '';
        mockChatEndpoint(text).then(reply => {
          addMsg('npc', reply);
        }).catch(err => {
          console.warn('Chat error:', err);
          addMsg('npc', '(no response)');
        });
      }
      sendBtn.addEventListener('click', sendCurrent);
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') sendCurrent();
        e.stopPropagation();
      });
      setTimeout(() => input.focus(), 0);

      container.add([overlay, panel, header, headerText, messagesBg, messagesBox, inputDiv, closeBtn]);
      chatUI = { container, messagesBox, overlay, inputDiv, closeBtn };
    }

    function closeChat(scene) {
      if (!chatActive) return;
      if (chatUI && chatUI.container) chatUI.container.destroy(true);
      chatUI = null;
      chatActive = false;
      setPaused(scene, false);
    }

    function mockChatEndpoint(userText) {
      return new Promise((resolve) => {
        setTimeout(() => {
          resolve('Mock: Nice to meet you! You said: "' + userText + '"');
        }, 400);
      });
    }

    // Terrain helpers
    function activeBgKey(scene) {
      return scene.textures.exists(bgCanvasKey) ? bgCanvasKey : bgImageKey;
    }

    function isEdgePixel(scene, x, y) {
      const c = scene.textures.getPixel(Math.floor(x), Math.floor(y), activeBgKey(scene));
      return c && c.r >= RED_THRESH.r && c.g <= RED_THRESH.g && c.b <= RED_THRESH.b;
    }

    function clearRedCircle(scene, cx, cy, radius) {
      const key = scene.textures.exists(bgCanvasKey) ? bgCanvasKey : null;
      if (!key) return;

      const tex = scene.textures.get(key);
      const ctx = tex.getContext();
      if (!ctx) return;

      const x0 = Math.max(0, Math.floor(cx - radius));
      const y0 = Math.max(0, Math.floor(cy - radius));
      const x1 = Math.min(worldW - 1, Math.floor(cx + radius));
      const y1 = Math.min(worldH - 1, Math.floor(cy + radius));

      try {
        const w = x1 - x0 + 1;
        const h = y1 - y0 + 1;
        const img = ctx.getImageData(x0, y0, w, h);
        const data = img.data;
        const r2 = radius * radius;

        for (let j = 0; j < h; j++) {
          for (let i = 0; i < w; i++) {
            const dx = (x0 + i) - cx;
            const dy = (y0 + j) - cy;
            if (dx * dx + dy * dy > r2) continue;
            const idx = (j * w + i) * 4;
            const r = data[idx];
            const g = data[idx + 1];
            const b = data[idx + 2];
            if (r >= RED_THRESH.r && g <= RED_THRESH.g && b <= RED_THRESH.b) {
              data[idx] = 0; data[idx + 1] = 0; data[idx + 2] = 0; data[idx + 3] = 0;
            }
          }
        }
        ctx.putImageData(img, x0, y0);
        tex.refresh();
      } catch (e) {
        console.warn('[terrain] clearRedCircle failed:', e);
      }
    }

    function paintRedCircle(scene, cx, cy, radius) {
      const key = scene.textures.exists(bgCanvasKey) ? bgCanvasKey : null;
      if (!key) return;

      const tex = scene.textures.get(key);
      const ctx = tex.getContext();
      if (!ctx) return;

      try {
        ctx.save();
        ctx.fillStyle = '#ff0000';
        ctx.beginPath();
        ctx.arc(cx, cy, radius, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
        tex.refresh();
      } catch (e) {
        console.warn('[terrain] paintRedCircle failed:', e);
      }
    }

    function findGroundY(scene, x1, x2, yStart, maxProbe) {
      const stepX = 1;
      for (let dy = 0; dy <= maxProbe; dy++) {
        const y = Math.floor(yStart + dy);
        for (let x = Math.floor(x1); x <= Math.floor(x2); x += stepX) {
          const c = scene.textures.getPixel(x, y, activeBgKey(scene));
          if (c && c.r >= RED_THRESH.r && c.g <= RED_THRESH.g && c.b <= RED_THRESH.b) {
            return y;
          }
        }
      }
      return -1;
    }

    // Explosives
    function placeOrDetonateBomb(scene, time) {
      if (gamePaused) return;

      if (activeBomb) {
        explodeAt(scene, activeBomb.sprite.x, activeBomb.sprite.y, BOMB_RADIUS, BOMB_DAMAGE);
        activeBomb.sprite.destroy();
        activeBomb = null;
        console.log('[bomb] Detonated manually.');
        return;
      }

      const bomb = scene.add.image(player.x, player.y, 'bombTex').setDepth(20);
      activeBomb = { sprite: bomb, placedAt: time };
      console.log('[bomb] Placed at', bomb.x, bomb.y);
    }

    function findRandomGroundPosition(scene, opts = {}) {
      const minX = opts.minX ?? 24;
      const maxX = opts.maxX ?? (worldW - 24);
      const maxAttempts = opts.maxAttempts ?? 80;
      const clearance = opts.clearance ?? BLOCK_SIZE;

      for (let attempt = 0; attempt < maxAttempts; attempt++) {
        const x = Phaser.Math.Between(minX, maxX);
        const gy = findGroundY(scene, x - 2, x + 2, 0, worldH - 2);
        if (gy >= 0) {
          let clearAbove = true;
          const topY = gy - clearance;
          for (let y = gy - 2; y >= Math.max(0, topY); y -= 2) {
            if (isEdgePixel(scene, x, y)) { clearAbove = false; break; }
          }
          if (!clearAbove) continue;

          const y = gy - BLOCK_SIZE / 2 - 1;
          return { x, y };
        }
      }
      return {
        x: Math.max(24, Math.min(worldW - 24, skeleton.spawn.x)),
        y: Math.max(24, Math.min(worldH - 24, skeleton.spawn.y))
      };
    }

    function updateBomb(scene, time) {
      if (activeBomb && (time - activeBomb.placedAt) >= EXPLOSION_AUTO_MS) {
        explodeAt(scene, activeBomb.sprite.x, activeBomb.sprite.y, BOMB_RADIUS, BOMB_DAMAGE);
        activeBomb.sprite.destroy();
        activeBomb = null;
        console.log('[bomb] Auto-detonated.');
      }
    }

    function throwOrDetonateGrenade(scene, time) {
      if (gamePaused) return;

      const idxLive = grenades.findIndex(g => !g.exploded);
      if (idxLive >= 0) {
        const g = grenades[idxLive];
        explodeAt(scene, g.x, g.y, GRENADE_RADIUS, GRENADE_DAMAGE);
        if (g.sprite) g.sprite.destroy();
        g.exploded = true;
        grenades.splice(idxLive, 1);
        console.log('[grenade] Detonated manually.');
        return;
      }

      const vx = facing * GRENADE_THROW_VX;
      const vy = GRENADE_THROW_VY;
      const sprite = scene.add.image(player.x, player.y, 'grenadeTex').setDepth(20);
      const grenade = { sprite, x: player.x, y: player.y, vx, vy, bornAt: time, exploded: false, sleeping: false };
      grenades.push(grenade);
      console.log('[grenade] Thrown. v=', vx, vy);
    }

    function updateGrenades(scene, time, dt) {
      const toRemove = [];
      for (let i = 0; i < grenades.length; i++) {
        const g = grenades[i];
        if (g.exploded) { toRemove.push(i); continue; }

        if ((time - g.bornAt) >= EXPLOSION_AUTO_MS) {
          explodeAt(scene, g.x, g.y, GRENADE_RADIUS, GRENADE_DAMAGE);
          if (g.sprite) g.sprite.destroy();
          g.exploded = true;
          toRemove.push(i);
          console.log('[grenade] Auto-detonated.');
          continue;
        }

        if (g.sleeping) continue;

        let nx = g.x + g.vx * dt;
        let ny = g.y + g.vy * dt + 0.5 * GRENADE_GRAVITY * dt * dt;
        let nvy = g.vy + GRENADE_GRAVITY * dt;

        if (nx < GRENADE_SIZE) { nx = GRENADE_SIZE; g.vx = -g.vx * GRENADE_BOUNCE; }
        if (nx > worldW - GRENADE_SIZE) { nx = worldW - GRENADE_SIZE; g.vx = -g.vx * GRENADE_BOUNCE; }
        if (ny < GRENADE_SIZE) { ny = GRENADE_SIZE; nvy = -nvy * GRENADE_BOUNCE; }

        const hit = circleHitsRed(scene, nx, ny, GRENADE_SIZE);
        if (hit) {
          const hitX = circleHitsRed(scene, nx, g.y, GRENADE_SIZE);
          const hitY = circleHitsRed(scene, g.x, ny, GRENADE_SIZE);

          if (hitX && !hitY) {
            g.vx = -g.vx * GRENADE_BOUNCE;
            nx = g.x + g.vx * dt;
          } else if (!hitX && hitY) {
            nvy = -nvy * GRENADE_BOUNCE;
            ny = g.y + nvy * dt;
          } else {
            g.vx = -g.vx * GRENADE_BOUNCE;
            nvy = -nvy * GRENADE_BOUNCE;
            nx = g.x + g.vx * dt;
            ny = g.y + nvy * dt;
          }
        }

        if (circleHitsRed(scene, nx, ny + 1, GRENADE_SIZE + 1)) {
          g.vx *= GRENADE_FRICTION;
          if (Math.abs(g.vx) < GRENADE_SLEEP_SPEED && Math.abs(nvy) < GRENADE_SLEEP_SPEED) {
            g.vx = 0; nvy = 0; g.sleeping = true;
          }
        }

        g.x = nx; g.y = ny; g.vy = nvy;
        if (g.sprite) { g.sprite.x = g.x; g.sprite.y = g.y; }
      }
      for (let i = toRemove.length - 1; i >= 0; i--) {
        const idx = toRemove[i];
        grenades.splice(idx, 1);
      }
    }

    function circleHitsRed(scene, cx, cy, radius) {
      const step = Math.max(1, Math.floor(radius / 2));
      for (let y = Math.max(0, Math.floor(cy - radius)); y <= Math.min(worldH - 1, Math.floor(cy + radius)); y += step) {
        for (let x = Math.max(0, Math.floor(cx - radius)); x <= Math.min(worldW - 1, Math.floor(cx + radius)); x += step) {
          const dx = x - cx, dy = y - cy;
          if (dx * dx + dy * dy <= radius * radius && isEdgePixel(scene, x, y)) {
            return true;
          }
        }
      }
      return false;
    }

    function explodeAt(scene, x, y, radius, damage) {
      clearRedCircle(scene, x, y, radius);
      for (const e of enemies) {
        if (!e.alive) continue;
        const dx = e.x - x, dy = e.y - y;
        if (dx * dx + dy * dy <= radius * radius) applyEnemyDamage(e, damage);
      }
      const flash = scene.add.circle(x, y, radius, 0xffaa00, 0.2).setDepth(1000);
      scene.tweens.add({ targets: flash, alpha: 0, duration: 200, ease: 'Cubic.easeOut', onComplete: () => flash.destroy() });
    }

    function shoot(scene, timeMs) {
      const cooldown = equipped.weapon === 'fast' ? SHOOT_COOLDOWN_MS_FAST : SHOOT_COOLDOWN_MS_NORMAL;
      if (timeMs - lastShotAt < cooldown) return;
      const offsetX = (player.width * 0.5) + 8;
      const muzzleX = player.x + facing * offsetX;
      const muzzleY = player.y - player.height * 0.2;
      const bullet = scene.add.image(muzzleX, muzzleY, 'bulletTex').setOrigin(0.5, 0.5).setDepth(10);

      const vx = facing * BULLET_SPEED;
      const bornAt = timeMs;
      bullets.push({ sprite: bullet, vx, bornAt });
      lastShotAt = timeMs;

      // Safety cap
      if (bullets.length > MAX_BULLETS) {
        const old = bullets.splice(0, bullets.length - MAX_BULLETS);
        old.forEach(b => b.sprite.destroy());
      }
    }

    function spawnWorldObjects() {
      const scene = this;
      // NPC
      const npcPos = findRandomGroundPosition(scene);
      const npcSprite = scene.add.image(npcPos.x, npcPos.y, 'npcTex').setDepth(5);
      npcs.push({ sprite: npcSprite, x: npcPos.x, y: npcPos.y });

      // Enemy
      const enemyPos = findRandomGroundPosition(scene);
      const enemySprite = scene.add.image(enemyPos.x, enemyPos.y, 'enemyTex').setDepth(5);
      enemies.push({ sprite: enemySprite, x: enemyPos.x, y: enemyPos.y, vx: 0, vy: 0, hp: 100, alive: true });

      // Item (Fast Gun)
      const itemPos = findRandomGroundPosition(scene);
      const itemSprite = scene.add.image(itemPos.x, itemPos.y, 'itemTex').setDepth(5);
      items.push({ sprite: itemSprite, x: itemPos.x, y: itemPos.y, type: 'fast_gun' });

      console.log('[spawn] NPC @', npcPos.x, npcPos.y, 'Enemy @', enemyPos.x, enemyPos.y, 'Item @', itemPos.x, itemPos.y);
    }

    function interactIfNearby(scene) {
      const px = player.x, py = player.y;
      // NPC within radius
      let nearestNpc = null, minD = 9999;
      for (const n of npcs) {
        const d = Phaser.Math.Distance.Between(px, py, n.x, n.y);
        if (d < 40 && d < minD) { minD = d; nearestNpc = n; }
      }
      if (nearestNpc) { openChat(scene, nearestNpc); return true; }

      // Item within radius
      for (let i = 0; i < items.length; i++) {
        const it = items[i];
        const d = Phaser.Math.Distance.Between(px, py, it.x, it.y);
        if (d < 40) {
          if (it.type === 'fast_gun' && !inventory.includes('fast_gun')) {
            inventory.push('fast_gun');
            if (inventoryUI && typeof inventoryUI.refreshInv === 'function') inventoryUI.refreshInv();
            showToast(scene, 'Picked up: Fast Gun');
          }
          it.sprite.destroy();
          items.splice(i, 1);
          return true;
        }
      }
      return false;
    }

    function showToast(scene, text) {
      const t = scene.add.text(player.x, player.y - 50, text, { font: '14px monospace', color: '#ffffaa', backgroundColor: '#00000088' })
        .setDepth(3000).setScrollFactor(1).setOrigin(0.5);
      scene.tweens.add({ targets: t, y: t.y - 20, alpha: 0, duration: 1200, onComplete: () => t.destroy() });
    }

    // Enemy helpers
    function applyEnemyDamage(enemy, dmg) {
      if (!enemy.alive) return;
      enemy.hp -= dmg;
      enemy.sprite.setTintFill(0xffffff);
      setTimeout(() => enemy.sprite.clearTint(), 80);
      if (enemy.hp <= 0) {
        enemy.alive = false;
        enemy.sprite.setTint(0x555555);
        enemy.sprite.scene.tweens.add({
          targets: enemy.sprite, alpha: 0, duration: 400, onComplete: () => enemy.sprite.destroy()
        });
      }
    }

    function updateEnemies(scene, dt, time) {
      const SPEED = 3;
      for (const e of enemies) {
        if (!e.alive) continue;

        // Ground check
        const bottom = e.y + BLOCK_SIZE * 0.5;
        const xLeft = e.x - BLOCK_SIZE * FOOT_FRAC;
        const xRight = e.x + BLOCK_SIZE * FOOT_FRAC;

        let pixelGrounded = false;
        let groundY = -1;
        for (let dy = 0; dy <= PROBE_DEPTH && groundY < 0; dy++) {
          const y = Math.floor(bottom + dy);
          for (let x = Math.floor(xLeft); x <= Math.floor(xRight); x++) {
            if (isEdgePixel(scene, x, y)) { groundY = y; break; }
          }
        }
        if (groundY >= 0) {
          e.y = groundY - BLOCK_SIZE * 0.5 - 1;
          e.vy = 0;
          pixelGrounded = true;
        } else {
          e.vy += 600 * dt;
          e.y += e.vy * dt;
        }

        // Horizontal toward player
        const dir = Math.sign(player.x - e.x);
        e.vx = dir * SPEED;

        // Horizontal red-wall collision
        const goingRight = e.vx > 0;
        const sideX = Math.floor(goingRight ? e.x + BLOCK_SIZE * 0.5 : e.x - BLOCK_SIZE * 0.5);
        const yTop = Math.floor(e.y - BLOCK_SIZE * 0.5 + VERT_MARGIN);
        const yBottom = Math.floor(e.y + BLOCK_SIZE * 0.5 - VERT_MARGIN);
        let hitX = -1;
        outer: for (let dx = 0; dx <= H_PROBE; dx++) {
          const x = sideX + (goingRight ? dx : -dx);
          for (let y = yTop; y <= yBottom; y += WALL_SAMPLE_STEP_Y) {
            if (isEdgePixel(scene, x, y)) { hitX = x; break outer; }
          }
        }
        if (hitX >= 0) {
          e.vx = 0;
          e.x = goingRight ? (hitX - BLOCK_SIZE * 0.5 - 1) : (hitX + BLOCK_SIZE * 0.5 + 1);
        } else {
          e.x += e.vx * dt * 60 / 60;
        }

        e.sprite.x = e.x; e.sprite.y = e.y;

        // Contact damage with immunity
        if (time - lastDamagedAt > CONTACT_IMMUNITY_MS) {
          if (rectOverlap(player.x, player.y, player.width, player.height, e.x, e.y, BLOCK_SIZE, BLOCK_SIZE)) {
            health = Math.max(0, health - CONTACT_DAMAGE);
            lastDamagedAt = time;
            showToast(scene, `- ${CONTACT_DAMAGE} HP`);
            if (health <= 0) handlePlayerDeath(scene);
          }
        }
      }
    }

    function rectOverlap(x1, y1, w1, h1, x2, y2, w2, h2) {
      const l1 = x1 - w1 / 2, r1 = x1 + w1 / 2, t1 = y1 - h1 / 2, b1 = y1 + h1 / 2;
      const l2 = x2 - w2 / 2, r2 = x2 + w2 / 2, t2 = y2 - h2 / 2, b2 = y2 + h2 / 2;
      return !(l1 > r2 || r1 < l2 || t1 > b2 || b1 < t2);
    }

    // Core update
    function update(time, delta) {
      const scene = this;
      const dt = Math.max(0.001, delta / 1000);

      if (gamePaused) {
        debugText.setText(`PAUSED  HP:${health}`);
        return;
      }

      const leftPressed = keys.A.isDown || cursors.left.isDown;
      const rightPressed = keys.D.isDown || cursors.right.isDown;
      const upPressedOnce = Phaser.Input.Keyboard.JustDown(keys.W) || Phaser.Input.Keyboard.JustDown(keys.SPACE);
      const downHeld = keys.S.isDown || cursors.down.isDown;
      const downPressedOnce = Phaser.Input.Keyboard.JustDown(keys.S) || Phaser.Input.Keyboard.JustDown(cursors.down);
      const jJustDown = Phaser.Input.Keyboard.JustDown(keys.J);
      const jHeld = keys.J.isDown;
      const bombPressed = Phaser.Input.Keyboard.JustDown(keys.K);
      const grenadePressed = Phaser.Input.Keyboard.JustDown(keys.G);
      const drawHeld = keys.H.isDown;
      const sprinting = keys.SHIFT.isDown && (leftPressed || rightPressed);

      if (Phaser.Input.Keyboard.JustDown(keys.ESC)) {
        toggleInventory(scene, !inventoryUI.visible);
        return;
      }

      const speedX = sprinting ? SPRINT_SPEED : WALK_SPEED;
      let intentX = 0;
      if (leftPressed && !rightPressed) { intentX = -speedX; facing = -1; }
      else if (rightPressed && !leftPressed) { intentX = speedX; facing = 1; }
      player.setVelocityX(intentX);

      const body = player.body;
      const bottom = player.y + player.height * 0.5;
      const xLeft = player.x - player.width * FOOT_FRAC;
      const xRight = player.x + player.width * FOOT_FRAC;

      let pixelGrounded = false;
      let groundY = -1;
      const allowGrounding = !(time < dropThroughUntil);

      if (allowGrounding) {
        for (let dy = 0; dy <= PROBE_DEPTH && groundY < 0; dy++) {
          const y = Math.floor(bottom + dy);
          for (let x = Math.floor(xLeft); x <= Math.floor(xRight); x++) {
            const c = scene.textures.getPixel(x, y, activeBgKey(scene));
            if (c && c.r >= RED_THRESH.r && c.g <= RED_THRESH.g && c.b <= RED_THRESH.b) { groundY = y; break; }
          }
        }
        if (groundY >= 0 && body.velocity.y >= 0) {
          const targetY = groundY - player.height * 0.5 - 1;
          player.setY(targetY);
          player.setVelocityY(0);
          pixelGrounded = true;
        }
      }
      const nearWorldBottom = player.body.bounds.max.y >= worldH - 1.5 && body.velocity.y >= 0;
      grounded = pixelGrounded || nearWorldBottom;

      if (grounded) jumpsRemaining = DOUBLE_JUMPS;
      if (upPressedOnce && jumpsRemaining > 0) {
        player.setVelocityY(JUMP_VELOCITY);
        jumpsRemaining -= 1;
      }

      if (downPressedOnce && pixelGrounded) {
        dropThroughUntil = time + DROP_THROUGH_MS;
        if (body.velocity.y > -0.1) player.setVelocityY(1.5);
      }

      if (!sprinting && Math.abs(body.velocity.x) > 0.01) {
        const goingRight = body.velocity.x > 0;
        const sideX = Math.floor(goingRight ? player.x + player.width * 0.5 : player.x - player.width * 0.5);
        const yTop = Math.floor(player.y - player.height * 0.5 + VERT_MARGIN);
        const yBottom = Math.floor(player.y + player.height * 0.5 - VERT_MARGIN);
        let hitX = -1;
        outer: for (let dx = 0; dx <= H_PROBE; dx++) {
          const x = sideX + (goingRight ? dx : -dx);
          for (let y = yTop; y <= yBottom; y += WALL_SAMPLE_STEP_Y) {
            if (isEdgePixel(scene, x, y)) { hitX = x; break outer; }
          }
        }
        if (hitX >= 0) {
          const targetX = goingRight ? (hitX - player.width * 0.5 - 1) : (hitX + player.width * 0.5 + 1);
          player.setX(targetX);
          player.setVelocityX(0);
        }
      }

      // Interactions
      let interacted = false;
      if (jJustDown) interacted = interactIfNearby(scene);

      // Weapons + explosives
      const canShootAuto = (equipped.weapon === 'fast' && jHeld) || jJustDown;
      if (!interacted && canShootAuto) shoot(scene, time);
      if (bombPressed) placeOrDetonateBomb(scene, time);
      updateBomb(scene, time);
      if (grenadePressed) throwOrDetonateGrenade(scene, time);
      updateGrenades(scene, time, dt);

      if (drawHeld) {
        const centerX = Math.floor(player.x);
        const yPaint = Math.floor(bottom + PAINT_OFFSET_Y);
        paintRedCircle(scene, centerX, yPaint, PAINT_BRUSH_RADIUS);
      }

      // Update bullets
      if (bullets.length) {
        const toRemove = [];
        for (let i = 0; i < bullets.length; i++) {
          const b = bullets[i];
          b.sprite.x += b.vx * dt;

          for (const e of enemies) {
            if (!e.alive) continue;
            if (rectOverlap(b.sprite.x, b.sprite.y, 8, 4, e.x, e.y, BLOCK_SIZE, BLOCK_SIZE)) {
              applyEnemyDamage(e, BULLET_DAMAGE);
              toRemove.push(i);
              break;
            }
          }

          if (
            b.sprite.x < -32 || b.sprite.x > worldW + 32 ||
            b.sprite.y < -32 || b.sprite.y > worldH + 32 ||
            (time - b.bornAt) > BULLET_LIFETIME_MS
          ) {
            toRemove.push(i);
          }
        }
        toRemove.sort((a, b) => b - a);
        for (const idx of toRemove) {
          if (bullets[idx]) bullets[idx].sprite.destroy();
          bullets.splice(idx, 1);
        }
      }

      // Enemies
      updateEnemies(scene, dt, time);

      // Debug
      const vx = player.body.velocity.x.toFixed(2);
      const vy = player.body.velocity.y.toFixed(2);
      debugText.setText(
        `vx:${vx} vy:${vy} grounded:${grounded ? '1' : '0'} jumps:${jumpsRemaining} ` +
        `bullets:${bullets.length} bombs:${activeBomb ? 1 : 0} grenades:${grenades.length} HP:${health} inv:[${inventory.join(', ')}]`
      );

      if (healthText) healthText.setText(`HP: ${health}`);
    }

    function handlePlayerDeath(scene) {
      setPaused(scene, true);
      const overlay = scene.add.rectangle(0, 0, worldW, worldH, 0x000000, 0.7).setOrigin(0, 0).setDepth(5000).setScrollFactor(0);
      const txt = scene.add.text(worldW / 2, worldH / 2, 'You Died', { font: '32px monospace', color: '#ff5555' })
        .setOrigin(0.5).setDepth(5001).setScrollFactor(0);
      scene.add.text(worldW / 2, worldH / 2 + 40, 'Reload the page to retry.', { font: '16px monospace', color: '#ffffff' })
        .setOrigin(0.5).setDepth(5001).setScrollFactor(0);
    }
  }

  // Start sequence
  window.addEventListener('DOMContentLoaded', boot);
})();