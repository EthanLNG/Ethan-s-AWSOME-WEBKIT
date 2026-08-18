/* =============================================================================
 * Ethan's AWESOME WEBKIT — feedback overlay (webkit/overlay/overlay.js)
 * =============================================================================
 *
 * The in-browser half of the feedback loop. The kit's preview server injects
 * this file into every HTML page it serves:
 *
 *   <script src="/__wk/overlay.js" defer
 *           data-wk-color="blue" data-wk-emoji="🔵" data-wk-mode="after">
 *
 * The user toggles into "feedback" mode with a single key (default C),
 * draws rectangles over the live site, types, dictates, or records a note per point
 * (optionally requesting A/B/C variants), and sends the batch. The agent picks
 * the batch up from .webkit/feedback/<color>/feedback.json, applies each point
 * as its own git commit, writes review.json, and reopens the tab with
 * ?wk-review=<batchId>. The overlay then walks the user point-by-point with a
 * BEFORE|AFTER toggle (BEFORE = the page served out of git via /__wk/before/)
 * and records accept / redo / delete verdicts, POSTed back as verdicts.json.
 *
 * Design decisions (see the kit plan for the full rationale):
 *  - ISOLATION: everything lives in an open Shadow DOM on a host <div> hung off
 *    documentElement with all:initial + pointer-events:none, so host-page CSS
 *    cannot bleed in and the overlay cannot repaint the site. (Prefixed classes
 *    were rejected — host resets still bleed; an iframe was rejected — the
 *    overlay constantly reads the host document for context capture.)
 *  - CORNER GEOMETRY: the abc variant switcher owns x=14px growing UP
 *    (bottom = 14 + n*46); the webkit owns y=14px growing RIGHT (corner toggle
 *    at left:60px, send at left:106px). The two never collide by construction.
 *  - LOGICAL PATHNAME: on /__wk/before/<page> the overlay strips the prefix for
 *    every storage key, point.page value and navigation computation, so the
 *    BEFORE and AFTER documents share one state world.
 *  - STATE SURVIVES EVERYTHING: the evaluate⇄feedback toggle is just
 *    visibility:hidden on one container (DOM stays alive → mid-word typing and
 *    the caret survive exactly); a full reload restores the same draft from
 *    localStorage (text, rect, caret range, abc panel, mic arm state).
 *  - NO BUILD STEP, NO DEPENDENCIES: one IIFE, stdlib browser APIs only. The
 *    stylesheet is fetched from /__wk/overlay.css into adoptedStyleSheets
 *    (fallback: a <style> node in the shadow root).
 *
 * Server contract (all under /__wk/, JSON in/out):
 *   GET  /__wk/state?known=<rev>  → {changed, rev, color, emoji, phase,
 *                                    batch, review, verdicts}
 *        phase: collecting | awaiting_agent | reviewing | verdicts_sent
 *   POST /__wk/feedback           ← the batch object (schema below)
 *   POST /__wk/voice-note?id=…    ← raw audio stored beside the feedback inbox
 *   POST /__wk/voice-note/delete  ← remove an abandoned/replaced recording
 *   POST /__wk/verdicts           ← the verdicts object (409 on batch mismatch)
 *   GET  /__wk/before/<path>      → the page at review.beforeRef, re-injected
 *                                   with data-wk-mode="before"
 * ========================================================================== */
(() => {
  'use strict';
  if (window.__wkOverlayLoaded) return;   // injection idempotence (belt: server also guards)
  window.__wkOverlayLoaded = true;

  // ===== script dataset ======================================================
  // document.currentScript is null for some defer/timing combinations — fall
  // back to locating our own tag by the attribute the server always sets.
  const scriptEl = document.currentScript && document.currentScript.dataset.wkColor !== undefined
    ? document.currentScript
    : document.querySelector('script[data-wk-color]');
  const DS = (scriptEl && scriptEl.dataset) || {};
  const COLOR = DS.wkColor || 'unknown';
  const EMOJI = DS.wkEmoji || '⬜';
  const IS_BEFORE = DS.wkMode === 'before';   // reduced state: no drawing, ⚗ disabled
  const DICTATION_MODE = DS.wkDictationMode === 'voice-note' ? 'voice-note' : 'speech';
  const INTERACTION_MODE = DS.wkInteractionMode === 'draw-default'
    ? 'draw-default'
    : 'browse-default';

  // Hotkeys are KeyboardEvent.code values (LAYOUT-INDEPENDENT: this site is
  // Hebrew, so `e.key` would be a different character on every layout).
  // Ctrl/Cmd+. — the original primary — never reaches the page on macOS Chrome
  // (the browser eats the combo), hence a single bare key. The server injects
  // overrides from the config's `hotkeys` block; these are the defaults.
  const HOTKEY_TOGGLE = DS.wkHotkeyToggle || 'KeyC';
  const HOTKEY_DICTATE = DS.wkHotkeyDictate || 'KeyV';
  const keyLabel = (code) => code === 'Backquote' ? '`'
    : /^Key[A-Z]$/.test(code) ? code.slice(3)
      : /^Digit[0-9]$/.test(code) ? code.slice(5)
        : code;
  const TOGGLE_LABEL = keyLabel(HOTKEY_TOGGLE);
  const DICTATE_LABEL = keyLabel(HOTKEY_DICTATE);
  const MIC_TITLE = DICTATION_MODE === 'voice-note'
    ? 'Record a voice note for the agent — or press ' + DICTATE_LABEL + ' outside a text field'
    : 'Dictate (Chrome speech-to-text) — or press ' + DICTATE_LABEL + ' outside a text field';

  const BEFORE_PREFIX = '/__wk/before';
  const LETTERS = 'ABCDEFGHIJ';               // abc request letters, count capped at 10

  // ===== tiny utils ==========================================================
  const cssEscape = (s) => (window.CSS && CSS.escape) ? CSS.escape(s)
    : String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => '\\' + c);

  function rand4() {
    let s = '';
    while (s.length < 4) s += Math.random().toString(36).slice(2);
    return s.slice(0, 4);
  }
  const nowISO = () => new Date().toISOString();
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  function newBatchId() {
    const d = new Date(), p = (n, w) => String(n).padStart(w, '0');
    return 'b-' + d.getFullYear() + p(d.getMonth() + 1, 2) + p(d.getDate(), 2) +
      '-' + p(d.getHours(), 2) + p(d.getMinutes(), 2) + '-' + rand4();
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function debounce(fn, ms) {
    let t = 0;
    const d = (...a) => { clearTimeout(t); t = setTimeout(() => { t = 0; fn(...a); }, ms); };
    d.flush = (...a) => { if (t) { clearTimeout(t); t = 0; fn(...a); } };
    d.cancel = () => { clearTimeout(t); t = 0; };
    return d;
  }

  // The one path rule: storage keys, point.page and navigation math all use the
  // LOGICAL pathname (before-prefix stripped) so both sides share one state.
  function logicalPath(p) {
    p = p === undefined ? location.pathname : p;
    if (p === BEFORE_PREFIX) return '/';
    return p.startsWith(BEFORE_PREFIX + '/') ? p.slice(BEFORE_PREFIX.length) : p;
  }
  // logical page → physical URL path for a given side
  const physicalPath = (page, side) => (side === 'before' ? BEFORE_PREFIX : '') + page;

  // ===== storage (quota/private-mode safe) ==================================
  // localStorage can throw on read AND write (private mode, quota). Degrade to
  // an in-memory map that mirrors every write, so the session keeps working.
  function makeStore(backing) {
    const mem = new Map();
    return {
      get(k) {
        try { const v = backing.getItem(k); if (v !== null) return v; } catch (e) { /* fall through */ }
        return mem.has(k) ? mem.get(k) : null;
      },
      set(k, v) {
        mem.set(k, v);
        try { backing.setItem(k, v); } catch (e) { /* memory copy holds */ }
      },
      remove(k) {
        mem.delete(k);
        try { backing.removeItem(k); } catch (e) { /* ignore */ }
      },
      // every live key (mem ∪ backing) — used to GC round-scoped verdict caches
      keys() {
        const out = new Set(mem.keys());
        try { for (let i = 0; i < backing.length; i++) out.add(backing.key(i)); } catch (e) { /* mem-only */ }
        return [...out];
      },
      getJSON(k, fallback) {
        const raw = this.get(k);
        if (raw === null) return fallback;
        try { return JSON.parse(raw); } catch (e) { return fallback; }
      },
      setJSON(k, v) { this.set(k, JSON.stringify(v)); },
    };
  }
  const LS = makeStore(window.localStorage || { getItem() { return null; }, setItem() {}, removeItem() {} });
  const SS = makeStore(window.sessionStorage || { getItem() { return null; }, setItem() {}, removeItem() {} });

  // SpeechRecognition.lang is a real request parameter, not a UI hint. Keep an
  // explicit English/Hebrew choice because automatic language inference turns
  // accented English into Hebrew (and Hebrew into nonsense English) too often.
  const SPEECH_LANGS = new Set(['en-US', 'he-IL']);
  let speechLang = LS.get('wk:speechLang');
  if (!SPEECH_LANGS.has(speechLang)) {
    speechLang = /^he(?:-|$)/i.test(navigator.language || '') ? 'he-IL' : 'en-US';
  }
  LS.set('wk:speechLang', speechLang);

  function speechLanguageSelect() {
    const select = el('select', 'wk-speech-lang');
    select.setAttribute('aria-label', DICTATION_MODE === 'voice-note' ? 'Voice-note language' : 'Dictation language');
    select.title = DICTATION_MODE === 'voice-note' ? 'Language hint for local transcription' : 'Speech recognition language';
    const english = el('option', '', 'English');
    english.value = 'en-US';
    const hebrew = el('option', '', 'עברית');
    hebrew.value = 'he-IL';
    select.append(english, hebrew);
    select.value = speechLang;
    return select;
  }

  let SESSION_ID = LS.get('wk:sessionId');
  if (!SESSION_ID) {
    SESSION_ID = 's-' + Date.now().toString(36) + '-' + rand4();
    LS.set('wk:sessionId', SESSION_ID);
  }

  // ===== mutable state =======================================================
  const S = {
    mode: 'evaluate',           // 'evaluate' | 'feedback'
    phase: null,                // server phase; null until first /state reply
    rev: '',                    // last seen state rev (echoed as ?known=)
    batch: null,                // parsed feedback.json (from /state)
    review: null,               // parsed review.json (from /state)
    points: LS.getJSON('wk:points', []),   // unsent/queued points (all pages)
    drag: null,                 // active rubber/resize/move drag
    card: null,                 // open editor card {draft, els...}
    mini: null,                 // open redo mini-input
    reviewing: false,
    reviewBatchId: null,
    reviewRound: 0,
    verdictsKey: null,          // 'wk:verdicts:<batchId>:<round>' for the active review
    reviewList: [],             // batch points under review, ordered by number
    handledById: new Map(),     // pointId → review.points entry
    verdicts: {},               // pointId → {verdict, chosenLetter?, redoText?}
    deletedIds: new Set(),      // points deleted/sent this session — never resurrect on cross-tab merge
    cursor: 0,
    side: IS_BEFORE ? 'before' : 'after',  // the served document is authoritative
    compareScope: LS.get('wk:compareScope') === 'site' ? 'site' : 'point',
    sentVerdicts: false,
    abcToggled: new Set(),      // scopeIds the user toggled since review entry
    acceptArmed: null,          // pointId armed for "accept without toggling" confirm
    offeredReview: '',          // batchId:round already toasted, don't re-nag
    bootReview: null,           // ?wk-review target during boot — suppresses the offer toast
    pinEls: [],                 // [{node, box}] for the rAF repositioner
    altHeld: false,
  };
  // read-merge-write so a second tab on the same origin can't clobber points:
  // union by id, keeping foreign points this tab never saw and dropping anything
  // we deleted or already sent (S.deletedIds), which the blind last-writer-wins
  // setJSON used to erase.
  const savePoints = debounce(() => {
    const stored = LS.getJSON('wk:points', []);
    const mine = new Set(S.points.map((p) => p.id));
    const merged = stored.filter((p) => !mine.has(p.id) && !S.deletedIds.has(p.id)).concat(S.points);
    LS.setJSON('wk:points', merged);
  }, 150);

  // ===== shadow shell ========================================================
  const host = document.createElement('div');
  host.setAttribute('data-wk-host', '');
  // Inline (not stylesheet) so isolation holds even if the CSS fetch fails.
  host.style.cssText =
    'all:initial;position:fixed;inset:0;z-index:2147483400;pointer-events:none;display:block;';
  const root = host.attachShadow({ mode: 'open' });
  document.documentElement.appendChild(host);

  // A tidy-minded host page (or a framework re-render) may remove foreign
  // nodes from the tree — quietly re-append ourselves.
  new MutationObserver(() => {
    if (!host.isConnected) document.documentElement.appendChild(host);
  }).observe(document.documentElement, { childList: true });

  // If the fetch fails there is no "unstyled but working" fallback: pointer-events
  // is inherited from the host div's inline `pointer-events:none`, so ONLY the
  // stylesheet re-enables clicks — without it every control is dead AND .wk-off
  // stops hiding, dumping raw overlay text over the page. Install a minimal
  // critical stub immediately (clickable controls + hidden wrap) and keep
  // retrying with backoff until the real sheet lands.
  const CRITICAL =
    '.wk-wrap{position:fixed;inset:0;pointer-events:none}.wk-wrap.wk-off{visibility:hidden}' +
    '.wk-draw:not(.pass),.wk-pin,.wk-bar,.wk-card,.wk-toast,.wk-send{pointer-events:auto}';
  let cssSheet = null, cssStyleNode = null;
  function applySheet(css) {
    try {
      if (!cssSheet) { cssSheet = new CSSStyleSheet(); root.adoptedStyleSheets = [...root.adoptedStyleSheets, cssSheet]; }
      cssSheet.replaceSync(css);   // replace, don't append — retries must not stack sheets
    } catch (e) {
      if (!cssStyleNode) { cssStyleNode = document.createElement('style'); root.appendChild(cssStyleNode); }
      cssStyleNode.textContent = css;
    }
  }
  async function installCss(attempt) {
    attempt = attempt || 0;
    let css = '';
    try {
      const r = await fetch('/__wk/overlay.css', { cache: 'no-store' });
      if (r.ok) css = await r.text();
    } catch (e) { /* server briefly down */ }
    if (!css) {
      applySheet(CRITICAL);
      if (attempt < 6) setTimeout(() => installCss(attempt + 1), Math.min(30000, 1000 * 2 ** attempt));
      return;
    }
    applySheet(css);
  }

  // No corner button by design: the ONE way in and out is the toggle key. A
  // permanently-mounted button is chrome on someone's design, and the author
  // would rather tell people "press C" than have them hunt for a target.
  const wrap = el('div', 'wk-wrap wk-off');          // visibility-toggled container
  const drawLayer = IS_BEFORE ? null : el('div', 'wk-draw' +
    (INTERACTION_MODE === 'browse-default' ? ' pass' : ''));
  const pinLayer = el('div', 'wk-pins');
  const interactionHint = INTERACTION_MODE === 'browse-default'
    ? 'hold ⌥ + drag to mark a spot · click normally to use the page'
    : 'drag to mark a spot · hold ⌥ to use the page';
  const hintChip = el('div', 'wk-hint', interactionHint + ' · ' + TOGGLE_LABEL +
    ' to hide · ' + DICTATE_LABEL + ' to dictate (outside text fields)');
  const sendBtn = el('button', 'wk-send');
  sendBtn.type = 'button';
  sendBtn.hidden = true;
  const statusChip = el('div', 'wk-chip');
  statusChip.hidden = true;
  const bar = el('div', 'wk-bar');                   // review bar, built on demand
  bar.hidden = true;
  bar.tabIndex = 0;
  const toasts = el('div', 'wk-toasts');

  if (drawLayer) wrap.appendChild(drawLayer);
  wrap.appendChild(pinLayer);
  if (!IS_BEFORE) { wrap.appendChild(hintChip); wrap.appendChild(sendBtn); }
  wrap.appendChild(statusChip);
  wrap.appendChild(bar);
  root.appendChild(wrap);
  // toasts live OUTSIDE wrap: wrap gets visibility:hidden in evaluate mode, and
  // the review-ready offer + auto-sent notices must be visible while the user is
  // browsing (evaluate mode is the normal waiting state).
  root.appendChild(toasts);

  // ===== toasts ==============================================================
  function toast(msg, opts) {
    opts = opts || {};
    const t = el('div', 'wk-toast' + (opts.kind ? ' ' + opts.kind : ''));
    t.appendChild(el('span', 'wk-toast-msg', msg));
    let closed = false;
    const close = () => { if (closed) return; closed = true; t.remove(); };
    if (opts.action) {
      const b = el('button', 'wk-toast-btn', opts.action.label);
      b.type = 'button';
      b.addEventListener('click', () => { close(); opts.action.fn(); });
      t.appendChild(b);
    }
    const x = el('button', 'wk-toast-x', '×');
    x.type = 'button';
    x.addEventListener('click', close);
    t.appendChild(x);
    toasts.appendChild(t);
    if (opts.ttl !== 0) setTimeout(close, opts.ttl || 4200);
    return close;
  }

  // ===== server API ==========================================================
  async function api(path, body) {
    const res = await fetch(path, body === undefined
      ? { cache: 'no-store' }
      : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    let json = null;
    try { json = await res.json(); } catch (e) { /* non-JSON error body */ }
    if (!res.ok) {
      const err = new Error((json && json.error) || ('HTTP ' + res.status));
      err.status = res.status;
      throw err;
    }
    return json;
  }

  function deleteVoiceNote(note) {
    if (!note || !note.path) return;
    api('/__wk/voice-note/delete', { path: note.path }).catch(() => { /* best-effort orphan cleanup */ });
  }

  // ===== mode toggle =========================================================
  function setMode(mode) {
    S.mode = mode;
    LS.set('wk:mode', mode);
    wrap.classList.toggle('wk-off', mode !== 'feedback');
    if (mode === 'feedback') {
      if (!IS_BEFORE && !S.card) restoreCardDraft();
      if (S.card) {
        // toggle-in restore: the DOM stayed alive, only re-focus + caret
        const c = S.card;
        c.ta.focus();
        try { c.ta.setSelectionRange(c.draft.caret.start, c.draft.caret.end); } catch (e) { /* ok */ }
      }
      renderPins();
      updateHint();
      if (S.reviewing) updateBar();   // re-parks the page's abc switcher for this point
    } else {
      // overlay hidden = the page is the user's again: its own abc switcher is
      // now the only control there is, so un-park it
      restorePageAbc();
    }
    schedulePoll(true);
  }
  const toggleMode = () => setMode(S.mode === 'feedback' ? 'evaluate' : 'feedback');

  function updateHint() {
    if (IS_BEFORE) { hintChip.hidden = true; return; }
    hintChip.hidden = !(S.mode === 'feedback' && !S.card && !S.reviewing &&
      S.points.filter((p) => p.page === logicalPath()).length === 0);
  }

  // ===== rAF reposition engine ===============================================
  // Pins, rects, the frozen rect and the editor card are all positioned in
  // doc-coords and translated to viewport-coords in one coalesced rAF pass.
  let posRaf = 0;
  function schedulePos() { if (!posRaf) posRaf = requestAnimationFrame(repositionAll); }
  // viewport-anchored boxes (pins on a position:fixed/sticky ancestor) are already
  // stored in viewport coords, so they must NOT be scroll-translated — that is what
  // keeps them pinned to the fixed element instead of drifting up the document.
  function place(node, box, fixed) {
    const x = fixed ? box.x : box.x - scrollX;
    const y = fixed ? box.y : box.y - scrollY;
    node.style.transform = 'translate(' + x + 'px,' + y + 'px)';
  }
  function repositionAll() {
    posRaf = 0;
    for (const p of S.pinEls) place(p.node, p.box, p.fixed);
    if (S.card) positionFrozen(), positionCard();
  }
  window.addEventListener('scroll', schedulePos, { passive: true });
  window.addEventListener('resize', () => { renderPins(); schedulePos(); });

  // ===== selector builder + context capture ==================================
  function machineId(id) {
    return !id
      || /[^a-zA-Z0-9_-]/.test(id)               // React ':r3:', jsf ids etc.
      || /^\d/.test(id)
      || /\d{3,}/.test(id)                       // long digit runs
      || /^[0-9a-f]{6,}$/i.test(id)              // bare hex hash
      || /^(?:radix|react|ember|headlessui|mui|ng|aria|wk|abc)[-_]/i.test(id)
      || (/^[a-zA-Z0-9]{10,}$/.test(id) && /\d/.test(id));  // alphanumeric soup
  }
  function usableId(elm) {
    const id = elm.id;
    if (machineId(id)) return null;
    try {
      const m = document.querySelectorAll('#' + cssEscape(id));
      return (m.length === 1 && m[0] === elm) ? id : null;
    } catch (e) { return null; }
  }
  const STATE_CLASS = /^(?:active|inactive|open(?:ed)?|closed|hover|focus(?:ed)?|visible|hidden|show(?:n|ing)?|hide|selected|current|expanded|collapsed|animat|enter|leav|loading|loaded|in-view|is-|has-|js-)/;
  function stableClasses(elm) {
    const out = [];
    for (const c of elm.classList) {
      if (/^(?:wk-|abc-)/.test(c)) continue;             // our own + abc runtime state
      if (STATE_CLASS.test(c)) continue;                 // transient UI state
      if (/^_/.test(c) || /^css-/.test(c)) continue;     // css-modules / emotion
      if (/[0-9a-f]{5,}/i.test(c) && /\d/.test(c)) continue;  // build hashes
      if (/\d{3,}/.test(c)) continue;
      out.push(c);
      if (out.length === 2) break;                        // 2 classes is plenty
    }
    return out;
  }
  function matchesUnique(sel, elm) {
    try {
      const m = document.querySelectorAll(sel);
      return m.length === 1 && m[0] === elm;
    } catch (e) { return false; }
  }
  function nthOfType(elm) {
    let n = 1;
    for (let s = elm.previousElementSibling; s; s = s.previousElementSibling)
      if (s.tagName === elm.tagName) n++;
    return n;
  }
  function buildSelector(elm) {
    const id = usableId(elm);
    if (id) return '#' + cssEscape(id);
    const segs = [];
    let node = elm;
    while (node && node !== document.body && node !== document.documentElement) {
      let seg = node.tagName.toLowerCase();
      const cls = stableClasses(node);
      if (cls.length) seg += '.' + cls.map(cssEscape).join('.');
      let full = seg + (segs.length ? ' > ' + segs.join(' > ') : '');
      if (!matchesUnique(full, elm)) {
        seg += ':nth-of-type(' + nthOfType(node) + ')';
        full = seg + (segs.length ? ' > ' + segs.join(' > ') : '');
      }
      segs.unshift(seg);
      if (matchesUnique(segs.join(' > '), elm)) return segs.join(' > ');
      const parent = node.parentElement;
      if (parent) {
        const pid = usableId(parent);
        if (pid) {
          const withId = '#' + cssEscape(pid) + ' > ' + segs.join(' > ');
          if (matchesUnique(withId, elm)) return withId;
        }
      }
      node = parent;
    }
    // last resort: absolute nth-of-type chain. Anchor by actual containment
    // (elements hung off <html> — fixed headers, portal roots — are not under
    // body, where 'body > …' would match nothing), and VERIFY before returning:
    // unlike every branch above this one wasn't checked, so a non-matching chain
    // could silently strand correctedRect and hand the agent a dead selector.
    const abs = [];
    for (let n = elm; n && n !== document.body && n !== document.documentElement; n = n.parentElement)
      abs.unshift(n.tagName.toLowerCase() + ':nth-of-type(' + nthOfType(n) + ')');
    const anchor = (document.body && document.body.contains(elm)) ? 'body > ' : ':root > ';
    const sel = anchor + abs.join(' > ');
    return matchesUnique(sel, elm) ? sel : null;
  }

  function isOverlayNode(elm) {
    return elm === host || elm.classList && elm.classList.contains('abc-switch');
  }
  const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'LINK', 'META', 'NOSCRIPT', 'TEMPLATE', 'HTML', 'BODY']);

  // rectDoc = {x,y,w,h} in document coords → context[] (≤12, ranked by overlap)
  function captureContext(rectDoc) {
    const vx = rectDoc.x - scrollX, vy = rectDoc.y - scrollY;
    const areas = new Map();   // element → intersection area (viewport px²)
    function overlap(elm) {
      const b = elm.getBoundingClientRect();
      const w = Math.min(vx + rectDoc.w, b.right) - Math.max(vx, b.left);
      const h = Math.min(vy + rectDoc.h, b.bottom) - Math.max(vy, b.top);
      return (w > 0 && h > 0) ? w * h : 0;
    }
    function consider(elm) {
      if (!elm || areas.has(elm) || SKIP_TAGS.has(elm.tagName) || isOverlayNode(elm)) return;
      const a = overlap(elm);
      if (a > 0) areas.set(elm, a);
    }
    // 1) elementsFromPoint over a 3×3 grid — fast, respects stacking, and our
    //    host is pointer-events:none so it self-excludes.
    for (const fx of [0.12, 0.5, 0.88]) {
      for (const fy of [0.12, 0.5, 0.88]) {
        const px = vx + rectDoc.w * fx, py = vy + rectDoc.h * fy;
        if (px < 0 || py < 0 || px >= innerWidth || py >= innerHeight) continue;
        try {
          for (const elm of document.elementsFromPoint(px, py)) consider(elm);
        } catch (e) { /* ignore */ }
      }
    }
    // 2) bbox scan fallback — the rect may sit partly outside the viewport
    //    where elementsFromPoint can't see.
    if (areas.size === 0 && document.body) {
      for (const elm of document.body.querySelectorAll('*')) consider(elm);
    }
    const ranked = [...areas.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12);
    return ranked.map(([elm], i) => {
      const b = elm.getBoundingClientRect();
      return {
        selector: buildSelector(elm),
        tag: elm.tagName.toLowerCase(),
        text: (elm.innerText || elm.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 120),
        box: {
          x: Math.round(b.left + scrollX), y: Math.round(b.top + scrollY),
          w: Math.round(b.width), h: Math.round(b.height),
        },
        role: i === 0 ? 'primary' : 'intersecting',
      };
    });
  }

  // ===== abc interop =========================================================
  // Every window.__abc touch is optional-chained: the manager only exists on
  // pages that carry an abc experiment, and its shape is another script's.
  function snapshotAbc() {
    const ids = window.__abc?.list?.() || [];
    if (!ids.length) return null;
    const out = {};
    for (const id of ids) {
      const inst = window.__abc?.get?.(id);
      if (inst) out[id] = { current: inst.current, letters: inst.letters };
    }
    return Object.keys(out).length ? out : null;
  }
  // While reviewing an abc point the review bar's ⚗ chip IS the switcher, so the
  // page's own bottom-left button for THAT scope is a confusing duplicate — park
  // it behind an inline display:none (the abc manager only ever writes .hidden
  // and .style.bottom, so it never fights us) and restore it on the way out.
  // Scoped by data-abc-for: an unrelated live experiment's button keeps working.
  let hiddenAbcBtns = [];
  function restorePageAbc() {
    for (const h of hiddenAbcBtns) h.el.style.display = h.prev;
    hiddenAbcBtns = [];
  }
  function hidePageAbc(scopeId) {
    if (hiddenAbcBtns.length && hiddenAbcBtns[0].scopeId === scopeId) return;   // already parked
    restorePageAbc();
    if (!scopeId) return;
    let btns = [];
    try {
      btns = [...document.querySelectorAll('.abc-switch[data-abc-for="' + cssEscape(scopeId) + '"]')];
    } catch (e) { /* exotic scope id — leave the page's button alone */ }
    for (const el2 of btns) {
      hiddenAbcBtns.push({ el: el2, prev: el2.style.display, scopeId });
      el2.style.display = 'none';
    }
  }

  window.addEventListener('abc:change', (e) => {
    const id = e.detail?.id;
    if (id) S.abcToggled.add(id);
    if (S.reviewing) updateBar();
  });

  // ===== points ==============================================================
  // Monotonic across sends: sendPoints emptied S.points, so seeding only from it
  // restarted at 1 and drew a second pin "1" over the already-numbered review
  // pins. wk:lastNum (persisted at send, survives reloads) carries the high-water
  // mark so numbers only ever climb — pins stay unique and "point 7" is stable.
  function nextNumber() {
    let n = Number(LS.get('wk:lastNum')) || 0;
    for (const p of S.points) n = Math.max(n, p.number || 0);
    return n + 1;
  }

  // A rect drawn over a position:fixed/sticky ancestor is anchored to the viewport,
  // not the document: it must render fixed-to-viewport (no scroll translate) or it
  // drifts onto unrelated content the moment the user scrolls. detectAnchor runs at
  // capture; pinBox recomputes the live viewport box (tracks a sticky element that
  // is/ isn't currently stuck) at render time.
  function detectAnchor(vx, vy) {
    try {
      for (let n = document.elementFromPoint(vx, vy);
        n && n !== document.body && n !== document.documentElement; n = n.parentElement) {
        if (isOverlayNode(n)) continue;
        const pos = getComputedStyle(n).position;
        if (pos === 'fixed' || pos === 'sticky') return 'viewport';
      }
    } catch (e) { /* ignore */ }
    return 'doc';
  }
  // → {box, fixed:true} in viewport coords for viewport-anchored points, else null
  function pinBox(p) {
    if (p.anchor !== 'viewport') return null;
    const sel = p.context && p.context[0] && p.context[0].selector;
    if (sel) {
      try {
        const m = document.querySelectorAll(sel);
        if (m.length === 1) {
          const b = m[0].getBoundingClientRect();
          // capture-time scroll cancels: offset of rect within its anchor is
          // (rect.doc − context.doc), reusable against the live viewport box
          const ox = p.rect.x - p.context[0].box.x;
          const oy = p.rect.y - p.context[0].box.y;
          return { box: { x: b.left + ox, y: b.top + oy, w: p.rect.w, h: p.rect.h }, fixed: true };
        }
      } catch (e) { /* fall through to capture-time viewport box */ }
    }
    const sc = p.scroll || { x: 0, y: 0 };
    return { box: { x: p.rect.x - sc.x, y: p.rect.y - sc.y, w: p.rect.w, h: p.rect.h }, fixed: true };
  }
  function pointFromDraft(draft) {
    const existing = draft.editId ? S.points.find((p) => p.id === draft.editId) : null;
    return {
      id: existing ? existing.id : 'p-' + Date.now().toString(36) + '-' + rand4(),
      number: existing ? existing.number : nextNumber(),
      page: logicalPath(),
      createdAt: existing ? existing.createdAt : nowISO(),
      rect: {
        x: Math.round(draft.rect.x), y: Math.round(draft.rect.y),
        w: Math.round(draft.rect.w), h: Math.round(draft.rect.h),
      },
      viewport: { w: innerWidth, h: innerHeight, dpr: window.devicePixelRatio || 1 },
      scroll: { x: Math.round(scrollX), y: Math.round(scrollY) },
      anchor: existing ? (existing.anchor || 'doc') : (draft.anchor || 'doc'),
      context: captureContext(draft.rect),
      abcState: snapshotAbc(),
      text: draft.text.trim(),
      voiceNote: draft.voiceNote || null,
      abcRequest: abcRequestFromDraft(draft),
      status: 'new',
    };
  }
  function abcRequestFromDraft(draft) {
    const a = draft.abc;
    if (!a || !a.open) return null;
    if (a.mode === 'model') return { mode: 'model', count: a.count };
    const prompts = {};
    for (let i = 0; i < a.count; i++) {
      const L = LETTERS[i];
      if (a.prompts[L] && a.prompts[L].trim()) prompts[L] = a.prompts[L].trim();
    }
    return Object.keys(prompts).length ? { mode: 'user', prompts } : null;
  }

  // ===== pins layer ==========================================================
  // Unsent points → ink pins; review points → accent pins. Both live in a
  // pointer-events:none layer (pins re-enable themselves) and are translated by
  // the rAF engine — no per-scroll layout reads.
  function renderPins() {
    pinLayer.textContent = '';
    S.pinEls = [];
    S.curPinEls = null;   // never flash a node that just got detached
    const page = logicalPath();
    const addPin = (num, box, cls, onClick, title, fixed) => {
      const rect = el('div', 'wk-pin-rect ' + cls);
      rect.style.width = box.w + 'px';
      rect.style.height = box.h + 'px';
      const pin = el('button', 'wk-pin ' + cls, String(num));
      pin.type = 'button';
      pin.title = title || '';
      pin.addEventListener('click', onClick);
      pinLayer.appendChild(rect);
      pinLayer.appendChild(pin);
      S.pinEls.push({ node: rect, box, fixed: !!fixed });
      S.pinEls.push({ node: pin, box: { x: box.x - 11, y: box.y - 11, w: 22, h: 22 }, fixed: !!fixed });
      return { rect, pin };
    };
    if (S.reviewing) {
      S.reviewList.forEach((p, i) => {
        if (p.page !== page) return;
        const va = pinBox(p);
        const box = va ? va.box : correctedRect(p);
        const v = S.verdicts[p.id];
        const els = addPin(p.number, box,
          'review' + (v ? ' verdicted v-' + v.verdict : '') + (i === S.cursor ? ' current' : ''),
          () => jumpTo(i), 'point ' + p.number + (v ? ' — ' + v.verdict : ''), !!va);
        if (i === S.cursor) { S.curPinEls = els; }
      });
    }
    for (const p of S.points) {
      if (p.page !== page) continue;
      const va = pinBox(p);
      addPin(p.number, va ? va.box : p.rect, 'queued', () => {
        if (IS_BEFORE || S.card) return;
        openCard({ editId: p.id });
      }, S.reviewing ? 'queued for next batch — click to edit' : 'click to edit', !!va);
    }
    repositionAll();
    updateHint();
  }

  // anchor correction: the agent just changed the layout, so shift the stored
  // rect by how far its primary context element moved since capture
  function correctedRect(p) {
    const sel = p.context && p.context[0] && p.context[0].selector;
    if (sel) {
      try {
        const m = document.querySelectorAll(sel);
        if (m.length === 1) {
          const b = m[0].getBoundingClientRect();
          const dx = (b.left + scrollX) - p.context[0].box.x;
          const dy = (b.top + scrollY) - p.context[0].box.y;
          return { x: p.rect.x + dx, y: p.rect.y + dy, w: p.rect.w, h: p.rect.h };
        }
      } catch (e) { /* selector no longer valid — use stored rect */ }
    }
    return p.rect;
  }

  // ===== draw layer ==========================================================
  if (drawLayer) {
    // New default: the site is directly interactive and Alt temporarily arms
    // rectangle drawing. The legacy setting reverses that relationship. Wheel
    // needs no special-casing: the layer isn't scrollable, so scroll chains to
    // the document whenever it owns the pointer.
    const syncDrawLayer = () => {
      const drawingArmed = INTERACTION_MODE === 'browse-default' ? S.altHeld : !S.altHeld;
      drawLayer.classList.toggle('pass', !drawingArmed);
    };
    window.addEventListener('keydown', (e) => {
      if (e.key === 'Alt') { S.altHeld = true; syncDrawLayer(); }
    }, true);
    window.addEventListener('keyup', (e) => {
      if (e.key === 'Alt') { S.altHeld = false; syncDrawLayer(); }
    }, true);
    window.addEventListener('blur', () => { S.altHeld = false; syncDrawLayer(); });
    syncDrawLayer();

    drawLayer.addEventListener('pointerdown', (e) => {
      if (e.button !== 0 || S.drag) return;
      if (S.card) { S.card.node.classList.remove('attn'); void S.card.node.offsetWidth; S.card.node.classList.add('attn'); return; }
      drawLayer.setPointerCapture(e.pointerId);
      const rubber = el('div', 'wk-rubber');
      drawLayer.appendChild(rubber);
      S.drag = {
        kind: 'rubber', sx: e.clientX, sy: e.clientY, node: rubber, pointerId: e.pointerId,
        cancel() { rubber.remove(); S.drag = null; },
      };
    });
    drawLayer.addEventListener('pointermove', (e) => {
      const d = S.drag;
      if (!d || d.kind !== 'rubber') return;
      const x = Math.min(d.sx, e.clientX), y = Math.min(d.sy, e.clientY);
      const w = Math.abs(e.clientX - d.sx), h = Math.abs(e.clientY - d.sy);
      d.node.style.cssText = 'left:' + x + 'px;top:' + y + 'px;width:' + w + 'px;height:' + h + 'px;';
      d.w = w; d.h = h; d.x = x; d.y = y;
    });
    drawLayer.addEventListener('pointerup', (e) => {
      const d = S.drag;
      if (!d || d.kind !== 'rubber') return;
      d.node.remove();
      S.drag = null;
      if (!(d.w >= 8 && d.h >= 8)) return;   // a click is not a rect
      const anchor = detectAnchor(d.x + d.w / 2, d.y + d.h / 2);   // viewport center
      openCard({ rect: { x: d.x + scrollX, y: d.y + scrollY, w: d.w, h: d.h }, anchor });
    });
    drawLayer.addEventListener('pointercancel', () => { if (S.drag?.kind === 'rubber') S.drag.cancel(); });
  }

  // ===== frozen rect (resize handles + move) =================================
  const HANDLES = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];
  let frozenEl = null;

  function buildFrozen() {
    frozenEl = el('div', 'wk-frozen');
    const body = el('div', 'wk-frozen-body');
    body.dataset.h = 'move';
    frozenEl.appendChild(body);
    for (const h of HANDLES) {
      const hd = el('div', 'wk-handle h-' + h);
      hd.dataset.h = h;
      frozenEl.appendChild(hd);
    }
    frozenEl.addEventListener('pointerdown', (e) => {
      const h = e.target.dataset && e.target.dataset.h;
      if (!h || e.button !== 0 || !S.card || S.drag) return;
      e.preventDefault();
      e.target.setPointerCapture(e.pointerId);
      const r0 = { ...S.card.draft.rect };
      S.drag = {
        kind: 'frozen', h, sx: e.clientX, sy: e.clientY, r0, target: e.target,
        cancel() { S.card.draft.rect = r0; S.drag = null; positionFrozen(); positionCard(); },
      };
    });
    frozenEl.addEventListener('pointermove', (e) => {
      const d = S.drag;
      if (!d || d.kind !== 'frozen' || !S.card) return;
      const dx = e.clientX - d.sx, dy = e.clientY - d.sy;
      let { x, y, w, h } = d.r0;
      if (d.h === 'move') { x += dx; y += dy; }
      else {
        if (d.h.includes('w')) { x += dx; w -= dx; }
        if (d.h.includes('e')) { w += dx; }
        if (d.h.includes('n')) { y += dy; h -= dy; }
        if (d.h.includes('s')) { h += dy; }
        if (w < 8) { if (d.h.includes('w')) x += w - 8; w = 8; }
        if (h < 8) { if (d.h.includes('n')) y += h - 8; h = 8; }
      }
      S.card.draft.rect = { x, y, w, h };
      positionFrozen();
      positionCard();
      S.card.saveDraft();
    });
    frozenEl.addEventListener('pointerup', () => { if (S.drag?.kind === 'frozen') S.drag = null; });
    frozenEl.addEventListener('pointercancel', () => { if (S.drag?.kind === 'frozen') S.drag.cancel(); });
    wrap.appendChild(frozenEl);
  }
  function positionFrozen() {
    if (!frozenEl || !S.card) return;
    const r = S.card.draft.rect;
    place(frozenEl, r);
    frozenEl.style.width = r.w + 'px';
    frozenEl.style.height = r.h + 'px';
  }

  // ===== speech (webkitSpeechRecognition) ====================================
  // One factory reused by the editor card and the redo mini-input. Chrome-only;
  // feature-gated so other browsers simply don't get a mic button.
  const SRClass = window.webkitSpeechRecognition || window.SpeechRecognition;
  // Chrome permits exactly one live recognition. Two (editor card + redo mini)
  // would abort each other, and each aborted onend restarts 250ms later — an
  // endless ping-pong where neither transcribes. This registry guarantees one.
  let liveMic = null;

  function makeMic(ta, btn, langSelect, onText) {
    if (!SRClass) {
      btn.hidden = true;
      langSelect.hidden = true;
      return { stop() {}, arm() {}, get on() { return false; } };
    }
    let rec = null, userOn = false, netFails = 0, restartT = 0, interim = '';
    let languageRestart = false;

    function paint() {
      btn.classList.toggle('on', userOn);
      btn.classList.remove('armed');
    }
    function start() {
      rec = new SRClass();
      rec.continuous = true;
      rec.interimResults = true;
      // The Web Speech API sends this BCP 47 tag to the recognition service
      // when the request starts. This is what makes the UI selector functional.
      rec.lang = langSelect.value;
      rec.onresult = (e) => {
        let fin = '';
        interim = '';
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const alt = e.results[i][0].transcript;
          if (e.results[i].isFinal) fin += alt; else interim += alt;
        }
        if (fin) {
          netFails = 0;
          insertAtCaret(ta, fin);
          onText && onText();
        }
        onText && onText(interim);
      };
      rec.onerror = (e) => {
        if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
          userOn = false;
          paint();                // drop the red 'on' pulse — dictation is dead
          btn.classList.add('error');
          btn.title = 'Microphone blocked — allow the mic for this site in Chrome, then click again';
        } else if (e.error === 'network') {
          netFails++;
          btn.classList.add('error');
          btn.title = 'Speech service network error (' + netFails + '/3)';
          if (netFails >= 3) {
            userOn = false;
            paint();
            btn.classList.add('error');
            btn.title = 'Speech service unreachable — dictation stopped after 3 network errors';
          }
        } else if (e.error !== 'no-speech' && !(languageRestart && e.error === 'aborted')) {
          // audio-capture / language-not-supported / 'aborted' are terminal: make
          // them stop userOn so onend's 250ms restart loop ends (and the two-mic
          // ping-pong breaks — a preempted recognition lands here and must not
          // resurrect itself). 'no-speech' stays routine; onend re-arms it.
          userOn = false;
          paint();
          btn.classList.add('error');
          btn.title = 'Dictation stopped (' + e.error + ') — click to retry';
        }
      };
      rec.onend = () => {
        interim = '';
        onText && onText('');
        if (languageRestart) {
          languageRestart = false;
          if (userOn) restartT = setTimeout(start, 0);
          return;
        }
        // Chrome ends recognition on every silence — quietly re-arm unless the
        // user toggled off or errors made restarting pointless.
        if (userOn) restartT = setTimeout(() => { try { rec && start(); } catch (e) { /* ok */ } }, 250);
      };
      try { rec.start(); } catch (e) { /* double-start race — ignore */ }
    }
    function stop() {
      userOn = false;
      languageRestart = false;
      clearTimeout(restartT);
      try { rec && rec.stop(); } catch (e) { /* ok */ }
      rec = null;
      interim = '';
      onText && onText('');
      if (liveMic === self) liveMic = null;
      paint();
    }
    btn.addEventListener('click', () => {
      if (userOn) { stop(); return; }
      if (liveMic && liveMic !== self) liveMic.stop();   // one live recognition at a time
      liveMic = self;
      userOn = true;
      netFails = 0;
      btn.classList.remove('error');
      btn.title = 'Dictating — click, or press ' + DICTATE_LABEL + ' outside a text field, to stop';
      paint();
      start();
      ta.focus();
    });
    langSelect.addEventListener('change', () => {
      if (!SPEECH_LANGS.has(langSelect.value)) langSelect.value = 'en-US';
      speechLang = langSelect.value;
      LS.set('wk:speechLang', speechLang);
      langSelect.title = 'Speech recognition language: ' + langSelect.options[langSelect.selectedIndex].text;
      if (!userOn) return;
      clearTimeout(restartT);
      languageRestart = true;
      try { rec && rec.stop(); } catch (e) {
        languageRestart = false;
        restartT = setTimeout(start, 0);
      }
    });
    const self = {
      stop,
      // after a reload we can't auto-start (browser gesture rule) — show the
      // armed look so the user knows one click resumes dictation
      arm() {
        if (!userOn) {
          btn.classList.add('armed');
          btn.title = 'Click (or press ' + DICTATE_LABEL + ' outside a text field) to resume dictation';
        }
      },
      get on() { return userOn; },
    };
    return self;
  }

  // Voice-note mode deliberately avoids browser speech recognition. It keeps
  // the original audio, uploads it into this color's private feedback inbox,
  // and lets the background agent run the bundled local Whisper helper.
  function makeVoiceRecorder(btn, langSelect, onSaved, onState) {
    if (!window.MediaRecorder || !navigator.mediaDevices?.getUserMedia) {
      btn.hidden = true;
      langSelect.hidden = true;
      return { stop() {}, arm() {}, get on() { return false; }, get uploading() { return false; } };
    }
    let recorder = null, stream = null, chunks = [], startedAt = 0;
    let recording = false, uploading = false, saveOnStop = false;

    function paint() {
      btn.classList.toggle('on', recording);
      btn.classList.toggle('uploading', uploading);
      if (recording) btn.title = 'Recording voice note — click to stop and attach';
      else if (uploading) btn.title = 'Saving voice note…';
      else btn.title = MIC_TITLE;
      onState && onState({ recording, uploading });
    }
    function closeStream() {
      for (const track of stream?.getTracks?.() || []) track.stop();
      stream = null;
    }
    async function upload(blob, durationMs) {
      uploading = true;
      paint();
      const id = 'voice-' + Date.now().toString(36) + '-' + rand4();
      try {
        const response = await fetch('/__wk/voice-note?id=' + encodeURIComponent(id), {
          method: 'POST', headers: { 'Content-Type': blob.type || 'audio/webm' }, body: blob,
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.error || ('HTTP ' + response.status));
        const note = result.voiceNote;
        note.durationMs = durationMs;
        note.language = langSelect.value === 'he-IL' ? 'he' : 'en';
        onSaved(note);
        toast('Voice note attached — the agent will transcribe it locally.');
      } catch (error) {
        toast('Voice note failed to save: ' + error.message, { kind: 'error' });
      } finally {
        uploading = false;
        paint();
      }
    }
    async function start() {
      if (liveMic && liveMic !== self) liveMic.stop();
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const preferred = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4']
          .find((type) => !MediaRecorder.isTypeSupported || MediaRecorder.isTypeSupported(type));
        recorder = preferred ? new MediaRecorder(stream, { mimeType: preferred }) : new MediaRecorder(stream);
        chunks = [];
        saveOnStop = false;
        recorder.ondataavailable = (event) => { if (event.data?.size) chunks.push(event.data); };
        recorder.onstop = () => {
          const shouldSave = saveOnStop;
          const durationMs = Math.max(0, Date.now() - startedAt);
          const type = recorder?.mimeType || chunks[0]?.type || 'audio/webm';
          recording = false;
          closeStream();
          paint();
          if (shouldSave && chunks.length) upload(new Blob(chunks, { type }), durationMs);
          chunks = [];
        };
        recorder.start(250);
        startedAt = Date.now();
        recording = true;
        liveMic = self;
        paint();
      } catch (error) {
        closeStream();
        recording = false;
        btn.classList.add('error');
        btn.title = 'Microphone blocked or unavailable — click to retry';
        toast('Could not start voice recording: ' + error.message, { kind: 'error' });
      }
    }
    function finish() {
      if (!recording || !recorder) return;
      saveOnStop = true;
      recorder.stop();
      if (liveMic === self) liveMic = null;
    }
    function stop() {
      saveOnStop = false;
      if (recording && recorder) recorder.stop();
      else closeStream();
      recording = false;
      if (liveMic === self) liveMic = null;
      paint();
    }
    btn.addEventListener('click', () => recording ? finish() : start());
    langSelect.addEventListener('change', () => {
      if (!SPEECH_LANGS.has(langSelect.value)) langSelect.value = 'en-US';
      speechLang = langSelect.value;
      LS.set('wk:speechLang', speechLang);
    });
    const self = {
      stop,
      arm() { btn.classList.add('armed'); },
      get on() { return recording; },
      get uploading() { return uploading; },
    };
    return self;
  }

  function insertAtCaret(ta, text) {
    const s = ta.selectionStart ?? ta.value.length;
    const before = ta.value.slice(0, s);
    // smart spacing: dictated chunks arrive without a leading space
    if (before && !/\s$/.test(before) && !/^[\s.,!?;:]/.test(text)) text = ' ' + text;
    ta.setRangeText(text, s, ta.selectionEnd ?? s, 'end');
    ta.dispatchEvent(new Event('input', { bubbles: false }));
  }

  // ===== editor card =========================================================
  const micGlyph =
    '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor"' +
    ' stroke-width="1.5" stroke-linecap="round" aria-hidden="true">' +
    '<rect x="5.5" y="1.75" width="5" height="8" rx="2.5"/>' +
    '<path d="M3 7.5a5 5 0 0 0 10 0M8 12.5v2"/></svg>';

  function openCard(init) {
    if (S.card || IS_BEFORE) return;
    const editing = init.editId ? S.points.find((p) => p.id === init.editId) : null;
    const originalVoiceNote = editing && editing.voiceNote ? editing.voiceNote : null;
    const draft = init.draft || {
      page: logicalPath(),
      rect: editing ? { ...editing.rect } : init.rect,
      anchor: editing ? (editing.anchor || 'doc') : (init.anchor || 'doc'),
      text: editing ? editing.text : '',
      caret: { start: (editing ? editing.text.length : 0), end: (editing ? editing.text.length : 0) },
      abc: editing && editing.abcRequest
        ? {
          open: true,
          mode: editing.abcRequest.mode,
          count: editing.abcRequest.mode === 'model'
            ? editing.abcRequest.count
            : Math.max(2, Object.keys(editing.abcRequest.prompts).length),
          prompts: editing.abcRequest.mode === 'user' ? { ...editing.abcRequest.prompts } : {},
        }
        : { open: false, mode: 'model', count: 4, prompts: {} },
      micOn: false,
      voiceNote: editing ? (editing.voiceNote || null) : null,
      editId: editing ? editing.id : null,
    };

    const node = el('div', 'wk-card');
    const head = el('div', 'wk-row wk-head');
    const num = el('span', 'wk-num', String(editing ? editing.number : nextNumber()));
    const title = el('span', 'wk-card-title', editing ? 'edit point' : 'feedback');
    const micBtn = el('button', 'wk-mic');
    micBtn.type = 'button';
    micBtn.title = MIC_TITLE;
    micBtn.innerHTML = micGlyph;
    const langSelect = speechLanguageSelect();
    const voiceStatus = el('span', 'wk-voice-status');
    voiceStatus.hidden = DICTATION_MODE !== 'voice-note';
    head.append(num, title, voiceStatus, langSelect, micBtn);

    const taWrap = el('div', 'wk-ta-wrap');
    const ta = el('textarea', 'wk-ta');
    ta.placeholder = DICTATION_MODE === 'voice-note'
      ? 'Type a note, record one, or use both…'
      : 'What should change here?';
    ta.value = draft.text;
    const ghost = el('div', 'wk-ghost');
    ghost.setAttribute('aria-hidden', 'true');
    const gPre = el('span', 'g-pre');
    const gInt = el('span', 'g-int');
    ghost.append(gPre, gInt);
    taWrap.append(ghost, ta);

    // --- abc sub-panel ---
    const abcWrap = el('div', 'wk-abc');
    const abcHead = el('button', 'wk-abc-head');
    abcHead.type = 'button';
    abcHead.innerHTML = '<span class="wk-flask">⚗</span><span>Request A/B/C variants</span><span class="wk-caret">▸</span>';
    const abcBody = el('div', 'wk-abc-body');
    const seg = el('div', 'wk-seg');
    const segModel = el('button', 'wk-seg-btn', 'Model generates');
    const segUser = el('button', 'wk-seg-btn', "I'll describe each");
    segModel.type = segUser.type = 'button';
    seg.append(segModel, segUser);
    const stepRow = el('div', 'wk-row wk-step-row');
    const stepLabel = el('span', 'wk-step-label', 'variants');
    const minus = el('button', 'wk-step', '−');
    const countEl = el('span', 'wk-count-n', '4');
    const plus = el('button', 'wk-step', '+');
    minus.type = plus.type = 'button';
    stepRow.append(stepLabel, minus, countEl, plus);
    const promptsBox = el('div', 'wk-abc-prompts');
    abcBody.append(seg, stepRow, promptsBox);
    abcWrap.append(abcHead, abcBody);

    const actions = el('div', 'wk-row wk-actions');
    const delBtn = el('button', 'wk-btn danger', 'Delete');
    const cancelBtn = el('button', 'wk-btn ghost', 'Cancel');
    const doneBtn = el('button', 'wk-btn primary', 'Done');
    delBtn.type = cancelBtn.type = doneBtn.type = 'button';
    delBtn.hidden = !editing;
    actions.append(delBtn, el('span', 'wk-spacer'), cancelBtn, doneBtn);

    node.append(head, taWrap, abcWrap, actions);
    wrap.appendChild(node);
    buildFrozen();

    // --- draft persistence: every input debounced 150ms into wk:card ---------
    const saveDraft = debounce(() => {
      draft.text = ta.value;
      draft.caret = { start: ta.selectionStart, end: ta.selectionEnd };
      draft.micOn = mic.on;
      LS.setJSON('wk:card', draft);
    }, 150);

    function syncGhost(interim) {
      if (interim === undefined) interim = gInt.textContent;
      gPre.textContent = ta.value.slice(0, ta.selectionStart ?? ta.value.length);
      gInt.textContent = interim || '';
      ghost.scrollTop = ta.scrollTop;
    }
    function autoGrow() {
      ta.style.height = 'auto';
      ta.style.height = Math.min(ta.scrollHeight, innerHeight * 0.4) + 'px';
      ghost.style.height = ta.style.height;
      positionCard();
    }
    ta.addEventListener('input', () => { autoGrow(); syncGhost(); saveDraft(); });
    ta.addEventListener('scroll', () => { ghost.scrollTop = ta.scrollTop; });
    for (const evt of ['keyup', 'click', 'select']) {
      ta.addEventListener(evt, () => { syncGhost(); saveDraft(); });
    }

    function paintVoiceStatus(state) {
      if (DICTATION_MODE !== 'voice-note') return;
      if (state?.recording) voiceStatus.textContent = 'recording…';
      else if (state?.uploading) voiceStatus.textContent = 'saving…';
      else if (draft.voiceNote) voiceStatus.textContent = 'voice attached';
      else voiceStatus.textContent = 'voice note';
      voiceStatus.classList.toggle('ready', !!draft.voiceNote);
    }
    const mic = DICTATION_MODE === 'voice-note'
      ? makeVoiceRecorder(micBtn, langSelect, (note) => {
        if (draft.voiceNote && draft.voiceNote.path !== originalVoiceNote?.path) deleteVoiceNote(draft.voiceNote);
        draft.voiceNote = note;
        saveDraft();
        saveDraft.flush();
        paintVoiceStatus();
      }, paintVoiceStatus)
      : makeMic(ta, micBtn, langSelect, (interim) => { syncGhost(interim); saveDraft(); });
    paintVoiceStatus();
    if (draft.micOn) mic.arm();

    // --- abc panel wiring -----------------------------------------------------
    function paintAbc() {
      abcWrap.classList.toggle('open', draft.abc.open);
      abcHead.querySelector('.wk-caret').textContent = draft.abc.open ? '▾' : '▸';
      segModel.classList.toggle('active', draft.abc.mode === 'model');
      segUser.classList.toggle('active', draft.abc.mode === 'user');
      countEl.textContent = String(draft.abc.count);
      promptsBox.hidden = draft.abc.mode !== 'user';
      if (draft.abc.mode === 'user') {
        promptsBox.textContent = '';
        for (let i = 0; i < draft.abc.count; i++) {
          const L = LETTERS[i];
          const row = el('label', 'wk-prompt-row');
          row.appendChild(el('span', 'wk-prompt-letter', L));
          const inp = el('input', 'wk-prompt-in');
          inp.type = 'text';
          inp.placeholder = 'variant ' + L + '…';
          inp.value = draft.abc.prompts[L] || '';
          inp.addEventListener('input', () => { draft.abc.prompts[L] = inp.value; saveDraft(); });
          row.appendChild(inp);
          promptsBox.appendChild(row);
        }
      }
      positionCard();
    }
    abcHead.addEventListener('click', () => { draft.abc.open = !draft.abc.open; paintAbc(); saveDraft(); });
    segModel.addEventListener('click', () => { draft.abc.mode = 'model'; paintAbc(); saveDraft(); });
    segUser.addEventListener('click', () => { draft.abc.mode = 'user'; paintAbc(); saveDraft(); });
    minus.addEventListener('click', () => { draft.abc.count = clamp(draft.abc.count - 1, 2, 10); paintAbc(); saveDraft(); });
    plus.addEventListener('click', () => { draft.abc.count = clamp(draft.abc.count + 1, 2, 10); paintAbc(); saveDraft(); });

    // --- lifecycle ------------------------------------------------------------
    function teardown() {
      mic.stop();
      // cancel, don't flush: every teardown path has already decided the fate
      // of wk:card (removed it, or done saved the point) — a late debounced
      // write here would resurrect a discarded draft 150ms after the fact
      saveDraft.cancel();
      node.remove();
      if (frozenEl) { frozenEl.remove(); frozenEl = null; }
      S.card = null;
      renderPins();
      updateSendBtn();
    }
    function cancelDraft() {
      if (draft.voiceNote && draft.voiceNote.path !== originalVoiceNote?.path) deleteVoiceNote(draft.voiceNote);
      LS.remove('wk:card'); teardown();
    }
    cancelBtn.addEventListener('click', cancelDraft);
    delBtn.addEventListener('click', () => {
      // record the id as deleted BEFORE saving: the merge in savePoints keeps
      // foreign points, so without this the just-removed point (still in LS from
      // its own earlier write, or from another tab) would be resurrected.
      if (editing) { S.deletedIds.add(editing.id); S.points = S.points.filter((p) => p.id !== editing.id); }
      deleteVoiceNote(draft.voiceNote || originalVoiceNote);
      savePoints();
      savePoints.flush();
      LS.remove('wk:card');
      teardown();
    });
    // commit the draft into S.points. Returns false (and shakes) on empty text.
    // Exposed on S.card so Send can flush an open card before shipping the batch.
    function commit() {
      draft.text = ta.value;
      if (mic.on || mic.uploading) {
        toast(mic.on ? 'Stop the recording before saving this point.' : 'Wait for the voice note to finish saving.', { kind: 'warn' });
        return false;
      }
      if (!draft.text.trim() && !draft.voiceNote) { ta.focus(); node.classList.remove('attn'); void node.offsetWidth; node.classList.add('attn'); return false; }
      const pt = pointFromDraft(draft);
      // update-or-push: if the edited point was sent/deleted meanwhile (findIndex
      // misses), pointFromDraft already minted a fresh id/number, so PUSH it —
      // the old code's map()-only branch silently dropped the user's edit.
      const idx = S.points.findIndex((p) => p.id === pt.id);
      if (idx >= 0) S.points[idx] = pt;
      else S.points.push(pt);
      if (originalVoiceNote && originalVoiceNote.path !== draft.voiceNote?.path) deleteVoiceNote(originalVoiceNote);
      savePoints();
      savePoints.flush();
      LS.remove('wk:card');
      teardown();
      return true;
    }
    doneBtn.addEventListener('click', () => {
      if (!commit()) return;
      if (S.phase !== 'collecting' && S.phase !== null) {
        toast('Point queued — the agent is mid-round; it auto-sends when the round ends.');
      }
    });

    // micBtn is exposed so the dictate/record hotkey drives the same handler.
    S.card = { node, ta, micBtn, draft, mic, saveDraft, commit, cancel: cancelDraft };
    paintAbc();
    autoGrow();
    positionFrozen();
    positionCard();
    updateHint();
    renderPins();
    ta.focus();
    try { ta.setSelectionRange(draft.caret.start, draft.caret.end); } catch (e) { /* ok */ }
    syncGhost();
  }

  // card sits under the rect; flips above near the bottom edge, clamps sideways
  function positionCard() {
    if (!S.card) return;
    const node = S.card.node, r = S.card.draft.rect;
    const vx = r.x - scrollX, vy = r.y - scrollY;
    const cw = node.offsetWidth || 320, ch = node.offsetHeight || 160;
    let left = clamp(vx, 8, Math.max(8, innerWidth - cw - 8));
    let top = vy + r.h + 10;
    if (top + ch > innerHeight - 8) top = vy - ch - 10;   // flip above
    top = clamp(top, 8, Math.max(8, innerHeight - ch - 8));
    node.style.left = left + 'px';
    node.style.top = top + 'px';
  }

  // reload / toggle-in restore of an in-progress card
  function restoreCardDraft() {
    if (S.card || IS_BEFORE) return;
    const draft = LS.getJSON('wk:card', null);
    if (!draft || draft.page !== logicalPath() || !draft.rect) return;
    openCard({ draft, editId: draft.editId });
  }

  // ===== send ================================================================
  function updateSendBtn() {
    const n = S.points.length;
    sendBtn.hidden = n === 0;
    if (n === 0) return;
    const blocked = S.phase !== null && S.phase !== 'collecting';
    sendBtn.classList.toggle('queued', blocked);
    sendBtn.innerHTML = '';
    sendBtn.append(
      el('span', 'wk-send-label', blocked ? 'queued' : 'Send'),
      el('span', 'wk-badge', String(n)),
    );
    sendBtn.title = blocked
      ? n + ' point(s) queued — auto-sends when the agent finishes the current round'
      : 'Send ' + n + ' point(s) to the ' + COLOR + ' agent';
  }

  let sending = false;
  async function sendPoints(auto) {
    if (sending) return;
    // reconcile with storage first: another tab may already have sent (points
    // gone from LS → don't re-send stale memory) or added points (fold them in),
    // never resurrecting anything we deleted/sent this session.
    const stored = LS.getJSON('wk:points', []);
    const storedIds = new Set(stored.map((p) => p.id));
    S.points = stored.concat(S.points.filter((p) => !storedIds.has(p.id) && !S.deletedIds.has(p.id)));
    if (!S.points.length) { renderPins(); updateSendBtn(); return; }
    // phase null = server never answered (dead/booting). Let the POST attempt
    // surface the truth via the catch's "Send failed" toast instead of silently
    // returning; only a known non-collecting phase blocks (points auto-send later).
    if (S.phase !== 'collecting' && S.phase !== null) { updateSendBtn(); return; }
    sending = true;
    const now = nowISO();
    // strip nothing: points are built exactly to schema
    const batch = {
      version: 1,
      kind: 'feedback',
      batchId: newBatchId(),
      round: 1,
      color: COLOR,
      sessionId: SESSION_ID,
      createdAt: now,
      updatedAt: now,
      pages: [...new Set(S.points.map((p) => p.page))],
      points: S.points,
    };
    try {
      await api('/__wk/feedback', batch);
      // keep numbers climbing across sends (high-water mark) so the next batch's
      // pins never collide with this batch's review pins
      const maxSent = batch.points.reduce((m, p) => Math.max(m, p.number || 0), 0);
      LS.set('wk:lastNum', String(Math.max(Number(LS.get('wk:lastNum')) || 0, maxSent)));
      // drop only the ids we sent — keep any a concurrent tab added meanwhile
      const sentIds = new Set(batch.points.map((p) => p.id));
      for (const id of sentIds) S.deletedIds.add(id);
      S.points = LS.getJSON('wk:points', []).filter((p) => !sentIds.has(p.id));
      savePoints();
      savePoints.flush();
      renderPins();
      updateSendBtn();
      toast((auto ? 'Queued points auto-sent' : 'Sent ' + batch.points.length + ' point(s)') +
        ' — the ' + EMOJI + ' agent is on it.');
      pollNow();
    } catch (e) {
      toast('Send failed: ' + e.message, { kind: 'error' });
    } finally {
      sending = false;
    }
  }
  sendBtn.addEventListener('click', () => {
    // an open editor card holds an uncommitted note — flush it into the batch
    // ("type the note, hit Send" must not ship without it); shake+refuse if empty
    if (S.card) {
      const n = S.card.node;
      if (!S.card.ta.value.trim() && !S.card.draft.voiceNote) {
        n.classList.remove('attn'); void n.offsetWidth; n.classList.add('attn'); return;
      }
      if (!S.card.commit()) return;
    }
    if (S.phase !== 'collecting' && S.phase !== null) {
      toast('The agent is mid-round — your points auto-send the moment it finishes.');
      return;
    }
    sendPoints(false);
  });

  // Cross-tab sync: the 'storage' event fires only in OTHER tabs, so it is exactly
  // the channel to reconcile a second tab on the same origin — adopt its points /
  // verdicts instead of diverging and later re-sending a stale duplicate batch.
  window.addEventListener('storage', (e) => {
    if (e.key === 'wk:points') {
      S.points = LS.getJSON('wk:points', []);
      if (!S.card) renderPins();   // don't yank the layer out from under an open editor
      updateSendBtn();
      updateHint();
    } else if (e.key && S.verdictsKey && e.key === S.verdictsKey) {
      S.verdicts = LS.getJSON(e.key, {});
      renderPins();
      if (S.reviewing) updateBar();
    }
  });

  // ===== polling + phase machine =============================================
  // 2s while the overlay is in feedback mode (the user is actively waiting for
  // the agent), a lazy 15s in evaluate mode, fully paused while hidden.
  let pollT = 0;
  function schedulePoll(reset) {
    clearTimeout(pollT);
    if (document.hidden) return;
    pollT = setTimeout(pollNow, S.mode === 'feedback' ? 2000 : 15000);
  }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) clearTimeout(pollT);
    else pollNow();
  });

  let pollWarned = false;
  async function pollNow() {
    clearTimeout(pollT);
    try {
      const st = await api('/__wk/state?known=' + encodeURIComponent(S.rev));
      pollWarned = false;
      if (st && (st.changed || S.phase === null)) handleState(st);
      else if (st && st.rev) S.rev = st.rev;
    } catch (e) {
      if (!pollWarned) {
        pollWarned = true;
        console.warn('[wk] state poll failed (server down?):', e.message);
      }
    }
    schedulePoll();
  }

  function handleState(st) {
    const prevPhase = S.phase;
    S.rev = st.rev || '';
    S.phase = st.phase || 'collecting';
    S.batch = st.batch || null;
    S.review = st.review || null;

    // leaving verdicts_sent = the agent consumed our verdicts → the local
    // verdict cache for that batch is now history
    if (prevPhase === 'verdicts_sent' && S.phase !== 'verdicts_sent' && S.verdictsKey) {
      LS.remove(S.verdictsKey);   // round-qualified; enterReview's sweep is the real GC
      S.verdicts = {};
      S.sentVerdicts = false;
    }

    if (S.phase === 'collecting') {
      if (S.reviewing) {
        if (IS_BEFORE) {
          // the round is over — this git-snapshot document is now orphaned (its
          // URL 409s and it has no draw layer). Hand off to the live AFTER
          // document at the same spot; replace() so the dead URL leaves no history.
          SS.setJSON('wk:scroll', { path: logicalPath(), x: Math.round(scrollX), y: Math.round(scrollY) });
          location.replace(physicalPath(logicalPath(), 'after'));
          return;
        }
        exitReview();
        toast('Round complete — batch archived. Draw away!');
      }
      // anything still in wk:points while phase was non-collecting is queued
      // by construction (a send in collecting clears them) → flush it now
      if (prevPhase !== null && prevPhase !== 'collecting' && S.points.length) {
        sendPoints(true);
      } else if (!S.points.length) {
        // The batch is finished and nothing is queued behind it, so the numbering
        // starts over at #1. The high-water mark only exists to stop a NEW point
        // colliding with the pins of a batch still on screen; once the inbox is
        // empty there is nothing to collide with, and carrying on at "#6" just
        // reads as though the old round never closed.
        LS.remove('wk:lastNum');
      }
    } else if (S.phase === 'reviewing' && S.review) {
      const key = S.review.batchId + ':' + S.review.round;
      if (S.reviewing && S.reviewBatchId === S.review.batchId && S.reviewRound !== S.review.round) {
        // next round landed while we watch — re-enter at point 1
        enterReview({ auto: true });
        toast('Round ' + S.review.round + ' ready — walking the redone points.');
      } else if (!S.reviewing && S.offeredReview !== key && S.bootReview !== S.review.batchId) {
        S.offeredReview = key;
        toast(EMOJI + ' review ready — ' + (S.batch?.points?.length || '') + ' point(s) to walk', {
          ttl: 0,
          action: { label: 'Start review', fn: () => enterReview({}) },
        });
      }
    }

    updateStatusChip();
    updateSendBtn();
    if (S.reviewing) updateBar();
  }

  function updateStatusChip() {
    if (S.mode !== 'feedback' || S.reviewing) { statusChip.hidden = true; return; }
    if (S.phase === 'awaiting_agent') {
      statusChip.textContent = EMOJI + ' agent is working on your batch…';
      statusChip.hidden = false;
    } else if (S.phase === 'verdicts_sent') {
      statusChip.textContent = EMOJI + ' verdicts sent — agent is processing…';
      statusChip.hidden = false;
    } else {
      statusChip.hidden = true;
    }
  }

  // ===== review ==============================================================
  function orderedReviewList() {
    const pts = (S.batch && S.batch.points) || [];
    const ids = new Set(((S.review && S.review.points) || []).map((p) => p.id));
    const list = ids.size ? pts.filter((p) => ids.has(p.id)) : pts.slice();
    return list.sort((a, b) => (a.number || 0) - (b.number || 0));
  }

  function enterReview(opts) {
    if (!S.review || !S.batch) return;
    S.reviewing = true;
    S.sentVerdicts = S.phase === 'verdicts_sent';
    S.reviewBatchId = S.review.batchId;
    S.reviewRound = S.review.round;
    S.offeredReview = S.review.batchId + ':' + S.review.round;   // no offer toast for a round we're in
    S.reviewList = orderedReviewList();
    S.handledById = new Map(((S.review.points) || []).map((p) => [p.id, p]));
    // Key verdicts by batch AND round: without the round, round-1's {verdict:'redo'}
    // entries preloaded as round-2's verdicts (done===total instantly → one click
    // resubmits stale feedback, looping the agent). Sweep every other verdict key
    // here too, so orphans from a batch whose verdicts_sent transition we never
    // observed (tab closed while the agent worked) can't accumulate or preload.
    S.verdictsKey = 'wk:verdicts:' + S.reviewBatchId + ':' + S.reviewRound;
    for (const k of LS.keys()) {
      if (k.startsWith('wk:verdicts:') && k !== S.verdictsKey) LS.remove(k);
    }
    S.verdicts = LS.getJSON(S.verdictsKey, {});
    S.abcToggled = new Set();
    S.acceptArmed = null;
    setMode('feedback');

    // cursor: fresh round → point 1; explicit &wk-point=N → that number;
    // otherwise the sessionStorage cursor survives BEFORE|AFTER navigations
    let idx = 0;
    if (opts.pointNumber != null) {
      const i = S.reviewList.findIndex((p) => p.number === opts.pointNumber);
      if (i >= 0) idx = i;
    } else if (!opts.auto) {
      const saved = SS.getJSON('wk:reviewCursor', null);
      if (saved && saved.batchId === S.reviewBatchId && saved.round === S.reviewRound &&
        saved.idx >= 0 && saved.idx < S.reviewList.length) idx = saved.idx;
    }
    if (opts.auto) SS.remove('wk:reviewCursor');
    buildBar();
    bar.hidden = false;
    statusChip.hidden = true;
    jumpTo(idx, { noScroll: !!opts.noScroll });
  }

  function exitReview() {
    S.reviewing = false;
    S.reviewList = [];
    S.curPinEls = null;
    // leaving review = the page must go back to being itself: the live AFTER
    // DOM/stylesheets, and the page's own abc switcher visible again
    restoreSwap();
    if (!IS_BEFORE) S.side = 'after';
    restorePageAbc();
    bar.hidden = true;
    SS.remove('wk:reviewCursor');
    renderPins();
    updateStatusChip();
  }

  // Probe a cross-document target before navigating: the agent may have created,
  // renamed or deleted the page (or the round just ended), and a blind
  // location.href would strand the user on a bare 404/409 with no overlay. A GET
  // (not HEAD — the server has no do_HEAD, so HEAD bypasses /__wk/before) tells us.
  function navGuarded(url, failMsg) {
    fetch(url, { cache: 'no-store' }).then((r) => {
      if (r.ok) location.href = url;
      else toast(failMsg, { kind: 'error' });
    }).catch(() => toast('Preview server unreachable.', { kind: 'error' }));
  }

  function jumpTo(idx, opts) {
    opts = opts || {};
    if (!S.reviewList.length) return;
    idx = ((idx % S.reviewList.length) + S.reviewList.length) % S.reviewList.length;  // wrap
    const pt = S.reviewList[idx];
    S.cursor = idx;
    S.acceptArmed = null;
    SS.setJSON('wk:reviewCursor', { batchId: S.reviewBatchId, round: S.reviewRound, idx });
    if (pt.page !== logicalPath()) {
      // cross-page: full navigation; boot re-enters review at this point
      navGuarded(physicalPath(pt.page, S.side) +
        '?wk-review=' + encodeURIComponent(S.reviewBatchId) + '&wk-point=' + pt.number,
        "This point's page no longer exists on this side — the agent may have removed or renamed it.");
      return;
    }
    if (S.side === 'before' && !IS_BEFORE) resyncSwap(pt);   // per-point swap container
    renderPins();
    updateBar();
    const va = pinBox(pt);
    const box = va ? va.box : correctedRect(pt);
    if (!opts.noScroll && !va) {
      // instant, not smooth: the flash should land where the eye already is, and
      // smooth scrolls never finish in a backgrounded tab (the jump idiom the
      // abc widget's RELOAD mode uses is instant for the same reason). A
      // viewport-anchored (fixed/sticky) point is on screen at any scroll — skip.
      window.scrollTo({ top: Math.max(0, box.y + box.h / 2 - innerHeight / 2), behavior: 'instant' });
    }
    // flash twice — the CSS animation runs 2 iterations; restart it
    if (S.curPinEls) {
      for (const n of [S.curPinEls.rect, S.curPinEls.pin]) {
        n.classList.remove('wk-flash');
        void n.offsetWidth;
        n.classList.add('wk-flash');
      }
    }
  }

  // ===== BEFORE|AFTER: in-place swap =========================================
  // A full navigation to /__wk/before/<page> is correct but brutal on a long
  // scroll story: seconds of reload, the scroll story replays, the eye loses the
  // spot. So we swap like an A/B variant instead — fetch the before document
  // once, lift out the container the current point lives in, and put it in the
  // live DOM, keeping the live node in memory for the way back. Two things make
  // this useful: the reveal/doodle machinery is nudged so the swapped subtree
  // doesn't land inert. Point scope deliberately leaves the rest of the page
  // and its stylesheets alone; whole-site scope uses the exact git snapshot.
  // Anything that cannot be resolved falls back to the full snapshot page.
  const SWAP = {
    doc: null,      // parsed before-document (per logical page)
    docPath: '',
    sel: '',        // selector of the swapped container
    live: null,     // the AFTER node, detached, waiting to go back
    placed: null,   // the BEFORE node currently in the document
  };
  let swapBusy = false, swapQueued = null;

  async function beforeDocument() {
    const page = logicalPath();
    if (SWAP.doc && SWAP.docPath === page) return SWAP.doc;
    const r = await fetch(BEFORE_PREFIX + page, { cache: 'no-store' });
    if (!r.ok) throw new Error(r.status === 409 ? 'round just ended' : 'HTTP ' + r.status);
    const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
    SWAP.doc = doc;
    SWAP.docPath = page;
    return doc;
  }

  // The swap container must exist AND be unique in BOTH documents. Try the
  // exact element under the feedback rectangle first; only widen to its nearest
  // stable section/article/[id] if that exact element cannot be matched.
  function resolveSwapTarget(pt, doc) {
    const cands = [];
    for (const c of ((pt && pt.context) || []).slice(0, 4)) {
      if (!c.selector) continue;
      let live = null;
      try { live = document.querySelector(c.selector); } catch (e) { continue; }
      if (!live || live === document.body || live === document.documentElement) continue;
      cands.push(live);
      const anc = live.closest('section, article, [id]');
      if (anc && anc !== live && anc !== document.body) cands.push(anc);
    }
    const tried = new Set();
    for (const node of cands) {
      const sel = buildSelector(node);
      if (!sel || tried.has(sel)) continue;
      tried.add(sel);
      if (!matchesUnique(sel, node)) continue;
      let inc = null;
      try { inc = doc.querySelectorAll(sel); } catch (e) { continue; }
      if (inc.length === 1) return { sel, live: node, incoming: inc[0] };
    }
    return null;
  }

  // Reveal machinery is class-driven here (.is-in / .is-active land once, from an
  // IntersectionObserver that already fired): a freshly parsed before-node would
  // arrive without them and render as an invisible/unstarted scene. Copy state
  // classes across, stopping at the first structural divergence — below that the
  // trees aren't comparable and positional matching would paint the wrong nodes.
  const SWAP_STATE_CLASS = /^(?:is-|has-|js-)|^(?:in|active|visible|shown|open|current|played|done)$/;
  function carryState(from, to) {
    if (!from || !to) return;
    for (const c of from.classList) if (SWAP_STATE_CLASS.test(c)) to.classList.add(c);
    const a = from.children, b = to.children;
    if (a.length !== b.length) return;
    for (let i = 0; i < a.length; i++) carryState(a[i], b[i]);
  }

  // The doodles are drawn by a load-time pass over `.rough` groups; if the host
  // page exposes it as a callable, re-run it on the swapped subtree so line art
  // isn't left as raw un-warped geometry (or nothing at all).
  function reinitDoodles(node) {
    for (const name of ['roughen', '__roughen', 'wkRoughen', '__wkRoughen']) {
      const hook = window[name];
      const fn = typeof hook === 'function' ? hook : (hook && typeof hook.run === 'function' ? hook.run : null);
      if (!fn) continue;
      try { fn.call(hook === fn ? window : hook, node); } catch (e) { /* best effort */ }
      return;
    }
  }

  function afterSwapPaint(node) {
    reinitDoodles(node);
    // observers/parallax/beat-snap all recompute off these; the swap changed
    // heights, so pins must re-anchor through correctedRect too
    window.dispatchEvent(new Event('scroll'));
    window.dispatchEvent(new Event('resize'));
    renderPins();
    schedulePos();
    if (node && node.isConnected) window.__abc?.relayout?.();
  }

  // A parsed git-snapshot node would otherwise inherit the CURRENT page's CSS,
  // making CSS-only point changes invisible. Render the snapshot off-screen,
  // copy its computed styles onto the cloned target, then discard the frame.
  // The styles become inline on this one subtree, so nothing else on the live
  // page changes while point scope is active.
  function copyComputedTree(source, target) {
    if (!source || !target || source.nodeType !== 1 || target.nodeType !== 1) return;
    const computed = source.ownerDocument.defaultView.getComputedStyle(source);
    for (let i = 0; i < computed.length; i++) {
      const name = computed[i];
      target.style.setProperty(name, computed.getPropertyValue(name), computed.getPropertyPriority(name));
    }
    const sourceKids = source.children, targetKids = target.children;
    if (sourceKids.length !== targetKids.length) return;
    for (let i = 0; i < sourceKids.length; i++) copyComputedTree(sourceKids[i], targetKids[i]);
  }

  async function styledBeforeNode(sel, fallback) {
    const frame = document.createElement('iframe');
    frame.setAttribute('aria-hidden', 'true');
    frame.style.cssText = 'position:fixed;left:-100000px;top:0;width:' + innerWidth +
      'px;height:' + innerHeight + 'px;visibility:hidden;pointer-events:none;border:0;';
    frame.src = BEFORE_PREFIX + logicalPath() + '?wk-style-probe=' + Date.now();
    document.documentElement.appendChild(frame);
    try {
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('before style probe timed out')), 5000);
        frame.addEventListener('load', () => { clearTimeout(timer); resolve(); }, { once: true });
        frame.addEventListener('error', () => { clearTimeout(timer); reject(new Error('before style probe failed')); }, { once: true });
      });
      let source = null;
      try { source = frame.contentDocument.querySelector(sel); } catch (e) { /* fallback below */ }
      const node = document.importNode(source || fallback, true);
      if (source) copyComputedTree(source, node);
      return node;
    } finally {
      frame.remove();
    }
  }

  // Keep the eye on the thing being compared. A swap changes the height of the
  // container (and on a scroll-driven page the synthetic scroll/resize above can
  // make the host's own story JS re-snap), so without this the page can end up
  // thousands of px away from the point — toggling BEFORE|AFTER would show you
  // somewhere else entirely. Capture where the anchor sits in the viewport, then
  // put it back there afterwards. Returns a restore fn; call it AFTER the paint.
  function anchorViewport(pt) {
    const sels = [];
    for (const c of ((pt && pt.context) || []).slice(0, 4)) if (c.selector) sels.push(c.selector);
    if (SWAP.sel) sels.push(SWAP.sel);
    let sel = null, top = null;
    for (const s of sels) {
      let el = null;
      try { el = document.querySelector(s); } catch (e) { continue; }
      if (el) { sel = s; top = el.getBoundingClientRect().top; break; }
    }
    return () => {
      if (sel == null || top == null) return;
      const settle = () => {
        let el = null;
        try { el = document.querySelector(sel); } catch (e) { return; }
        if (!el) return;
        const delta = el.getBoundingClientRect().top - top;
        if (Math.abs(delta) > 1) window.scrollBy({ top: delta, behavior: 'instant' });
      };
      settle();
      // the host's scroll handlers may move things again on the next tick
      setTimeout(settle, 60);
    };
  }

  async function applySwap(pt) {
    const doc = await beforeDocument();
    const t = resolveSwapTarget(pt, doc);
    if (!t) throw new Error('no container shared by both versions');
    const reanchor = anchorViewport(pt);
    const node = await styledBeforeNode(t.sel, t.incoming);
    carryState(t.live, node);
    t.live.replaceWith(node);
    SWAP.sel = t.sel;
    SWAP.live = t.live;
    SWAP.placed = node;
    afterSwapPaint(node);
    reanchor();
  }

  function restoreSwap() {
    if (SWAP.placed && SWAP.live) {
      const reanchor = anchorViewport(currentPoint());
      if (SWAP.placed.isConnected) SWAP.placed.replaceWith(SWAP.live);
      SWAP.placed = null;
      SWAP.live = null;
      SWAP.sel = '';
      afterSwapPaint(document.body);
      reanchor();
      return;
    }
    SWAP.placed = SWAP.live = null;
    SWAP.sel = '';
  }

  function markSide(side) {
    S.side = side;
    LS.set('wk:side', side);   // sticky preference; the document is authoritative on load
    updateBar();
  }

  // the pre-existing behaviour, kept verbatim as the fallback path
  function navSide(side, pointBeforeOnArrival) {
    LS.set('wk:side', side);
    const pt = S.reviewList[S.cursor];
    SS.setJSON('wk:scroll', { path: logicalPath(), x: Math.round(scrollX), y: Math.round(scrollY) });
    const page = pt ? pt.page : logicalPath();
    navGuarded(physicalPath(page, side) +
      '?wk-review=' + encodeURIComponent(S.reviewBatchId) +
      (pt ? '&wk-point=' + pt.number : '') +
      (pointBeforeOnArrival ? '&wk-point-before=1' : ''),
      "This page isn't in the before snapshot (or the round just ended).");
  }

  function setSide(side) {
    if (S.compareScope === 'site') {
      if (side !== S.side) navSide(side);
      return;
    }
    // A document actually SERVED from /__wk/before is a git snapshot with no
    // live tree to restore — only a navigation can leave it.
    if (IS_BEFORE) { if (side !== S.side) navSide(side); return; }
    // The first BEFORE costs a fetch; a click landing during it must not be
    // swallowed (the button would just look dead) — remember it and settle there.
    if (swapBusy) { swapQueued = side; return; }
    if (side === S.side) return;
    if (side === 'after') { restoreSwap(); markSide('after'); return; }
    const pt = currentPoint();
    if (!pt) return navSide('before');
    swapBusy = true;
    applySwap(pt).then(() => { markSide('before'); }).catch((e) => {
      restoreSwap();
      toast('In-place BEFORE not possible here (' + e.message + ') — loading the snapshot page.',
        { kind: 'warn' });
      navSide('before');
    }).finally(() => { swapBusy = false; drainSwapQueue(); });
  }
  function drainSwapQueue() {
    const q = swapQueued;
    swapQueued = null;
    if (q && q !== S.side) setSide(q);
  }

  // Moving to another point while BEFORE is showing: the swapped container is
  // per-point, so re-resolve it. If the new point has no shared container we are
  // honestly on AFTER for it — say so rather than mislabel the bar.
  function resyncSwap(pt) {
    if (swapBusy || !SWAP.placed) return;
    const t = resolveSwapTarget(pt, SWAP.doc);
    if (t && t.sel === SWAP.sel) return;
    swapBusy = true;
    restoreSwap();
    applySwap(pt).catch(() => {
      restoreSwap();
      markSide('after');
      toast('No in-place BEFORE for this point — showing AFTER.', { kind: 'warn' });
    }).finally(() => { swapBusy = false; drainSwapQueue(); });
  }

  // --- review bar -------------------------------------------------------------
  const B = {};  // review-bar element refs, rebuilt per enterReview
  function buildBar() {
    bar.textContent = '';
    B.prev = el('button', 'wk-nav', '◀');
    B.next = el('button', 'wk-nav', '▶');
    B.prev.type = B.next.type = 'button';
    B.prev.title = 'previous point (←)';
    B.next.title = 'next point (→)';
    B.counter = el('span', 'wk-bar-count');
    B.dots = el('span', 'wk-dots');

    B.seg = el('div', 'wk-seg wk-side');
    B.before = el('button', 'wk-seg-btn', 'BEFORE');
    B.after = el('button', 'wk-seg-btn', 'AFTER');
    B.before.type = B.after.type = 'button';
    B.seg.append(B.before, B.after);
    B.scopeWrap = el('div', 'wk-scope-wrap');
    B.scope = el('button', 'wk-scope-btn', '▾');
    B.scope.type = 'button';
    B.scopeWrap.append(B.scope);

    B.abcChip = el('button', 'wk-abc-chip');
    B.abcChip.type = 'button';
    B.abcChip.hidden = true;

    B.note = el('span', 'wk-bar-note');
    B.note.hidden = true;

    B.accept = el('button', 'wk-v accept', 'Accept');
    B.redo = el('button', 'wk-v redo', 'Redo');
    B.del = el('button', 'wk-v delete', 'Delete');
    B.dismiss = el('button', 'wk-v delete', 'Dismiss');
    for (const b of [B.accept, B.redo, B.del, B.dismiss]) b.type = 'button';

    B.sendv = el('button', 'wk-btn primary wk-sendv');
    B.sendv.type = 'button';
    B.sendv.hidden = true;
    B.wait = el('span', 'wk-wait', EMOJI + ' waiting for the agent…');
    B.wait.hidden = true;

    bar.append(B.prev, B.counter, B.dots, B.next, el('span', 'wk-bar-sep'),
      B.seg, B.scopeWrap, B.abcChip, B.note, el('span', 'wk-bar-sep'),
      B.accept, B.redo, B.del, B.dismiss, B.sendv, B.wait);

    B.prev.addEventListener('click', () => jumpTo(S.cursor - 1));
    B.next.addEventListener('click', () => jumpTo(S.cursor + 1));
    B.before.addEventListener('click', () => setSide('before'));
    B.after.addEventListener('click', () => setSide('after'));
    B.scope.addEventListener('click', () => {
      setCompareScope(S.compareScope === 'point' ? 'site' : 'point');
    });
    B.accept.addEventListener('click', onAccept);
    B.del.addEventListener('click', () => recordVerdict({ verdict: 'delete' }));
    B.dismiss.addEventListener('click', () => recordVerdict({ verdict: 'delete' }));
    B.redo.addEventListener('click', openMini);
    B.sendv.addEventListener('click', sendVerdicts);
    // the chip's title promises "click to cycle" — honour it by driving the abc
    // widget's own switch button (reuses its handler: RELOAD-mode reloads AND the
    // abc:change dispatch that registers the toggle so Accept isn't gated).
    B.abcChip.addEventListener('click', () => {
      if (IS_BEFORE) return;
      const inst = currentAbcInst();
      if (inst && inst.btn) inst.btn.click();
    });
  }

  function setCompareScope(scope) {
    if (scope !== 'point' && scope !== 'site') return;
    if (scope === S.compareScope) { updateBar(); return; }
    S.compareScope = scope;
    LS.set('wk:compareScope', scope);
    updateBar();
    if (S.side !== 'before') return;
    if (scope === 'site' && !IS_BEFORE) {
      restoreSwap();
      navSide('before');
    } else if (scope === 'point' && IS_BEFORE) {
      navSide('after', true);
    }
  }

  function currentPoint() { return S.reviewList[S.cursor] || null; }
  function currentHandled() {
    const pt = currentPoint();
    return pt ? S.handledById.get(pt.id) : null;
  }
  function currentAbcInst() {
    const h = currentHandled();
    const scopeId = h && h.abc && h.abc.scopeId;
    return scopeId ? (window.__abc?.get?.(scopeId) || null) : null;
  }

  // The chip is the ONLY variant switcher on screen during review, so it has to
  // read as a real button rather than the flat status pill it used to be:
  // flask · the current letter, large · the whole letter run with the live one
  // lit · a cycle glyph that says "clicking me advances this".
  function paintAbcChip(letter, letters) {
    B.abcChip.textContent = '';
    B.abcChip.append(el('span', 'wk-chip-flask', '⚗'), el('span', 'wk-chip-letter', String(letter)));
    // the run only earns its width while it's short — /ABC allows up to 10
    // variants, and ten pips would push the review bar into a second row
    if (letters && letters.length > 1 && letters.length <= 5) {
      const run = el('span', 'wk-chip-run');
      for (const L of letters) run.appendChild(el('span', 'wk-chip-l' + (L === letter ? ' on' : ''), L));
      B.abcChip.appendChild(run);
    } else if (letters && letters.length > 5) {
      B.abcChip.appendChild(el('span', 'wk-chip-of', 'of ' + letters.length));
    }
    B.abcChip.appendChild(el('span', 'wk-chip-cycle', '⟳'));
  }

  function updateBar() {
    if (!S.reviewing || bar.hidden) return;
    const pt = currentPoint();
    if (!pt) return;
    const h = currentHandled();
    const v = S.verdicts[pt.id];
    const total = S.reviewList.length;
    const done = Object.keys(S.verdicts).filter((id) => S.reviewList.some((p) => p.id === id)).length;

    B.counter.textContent = (S.cursor + 1) + '/' + total + ' · p' + pt.number;
    B.dots.textContent = '';
    S.reviewList.forEach((p, i) => {
      const dv = S.verdicts[p.id];
      const d = el('button', 'wk-dot-i' + (dv ? ' v-' + dv.verdict : '') + (i === S.cursor ? ' cur' : ''));
      d.type = 'button';
      d.title = 'point ' + p.number + (dv ? ' — ' + dv.verdict : '');
      d.addEventListener('click', () => jumpTo(i));
      B.dots.appendChild(d);
    });

    B.before.classList.toggle('active', S.side === 'before');
    B.after.classList.toggle('active', S.side === 'after');
    B.scope.title = S.compareScope === 'point'
      ? 'Comparison scope: current feedback point'
      : 'Comparison scope: whole website';
    B.scope.setAttribute('aria-label', B.scope.title);
    B.scope.classList.toggle('site', S.compareScope === 'site');

    // abc chip: live current letter, bound to abc:change. This is the ONLY
    // switcher the user should see for the point under review — hidePageAbc
    // parks the page's own duplicate button for the same scope.
    const isAbc = !!(h && h.abc);
    B.abcChip.hidden = !isAbc;
    hidePageAbc(isAbc && !IS_BEFORE ? h.abc.scopeId : null);
    let abcBlocked = false;
    if (isAbc) {
      const inst = currentAbcInst();
      if (IS_BEFORE) {
        paintAbcChip('—', null);
        B.abcChip.classList.add('disabled');
        B.abcChip.classList.remove('error');
        B.abcChip.title = 'Variants live in the AFTER view — toggle AFTER to compare A/B/C';
      } else if (!inst) {
        paintAbcChip('?', null);
        B.abcChip.classList.add('error');
        B.abcChip.classList.remove('disabled');
        B.abcChip.title = 'abc scope "' + h.abc.scopeId + '" not found on this page — accept blocked';
        abcBlocked = true;
      } else {
        paintAbcChip(inst.current, inst.letters);
        B.abcChip.classList.remove('disabled', 'error');
        B.abcChip.title = 'variant ' + inst.current + ' of ' + inst.letters + ' — click to cycle';
      }
    }

    // skipped points get Dismiss/Redo instead of Accept/Redo/Delete
    const skipped = h && h.handled === 'skipped';
    B.note.hidden = !(h && h.note);
    if (h && h.note) B.note.textContent = (skipped ? 'skipped: ' : '') + h.note;
    B.accept.hidden = skipped || S.sentVerdicts;
    B.del.hidden = skipped || S.sentVerdicts;
    B.dismiss.hidden = !skipped || S.sentVerdicts;
    B.redo.hidden = S.sentVerdicts;
    B.accept.disabled = abcBlocked;
    B.accept.title = abcBlocked ? 'The abc switcher for this point is missing — cannot record a chosen letter'
      : (isAbc ? 'Keep the variant currently shown' : 'Keep this change');
    B.accept.classList.toggle('armed', S.acceptArmed === pt.id);
    B.accept.textContent = S.acceptArmed === pt.id ? 'Accept ✓?' : 'Accept';

    for (const [btn, name] of [[B.accept, 'accept'], [B.redo, 'redo'], [B.del, 'delete'], [B.dismiss, 'delete']]) {
      btn.classList.toggle('active', !!v && v.verdict === name);
    }

    const all = total > 0 && done === total;
    B.sendv.hidden = !all || S.sentVerdicts || S.phase !== 'reviewing';
    B.sendv.textContent = 'Send verdicts (' + done + ')';
    B.wait.hidden = !S.sentVerdicts;
  }

  function onAccept() {
    const pt = currentPoint();
    if (!pt) return;
    const h = currentHandled();
    const entry = { verdict: 'accept' };
    if (h && h.abc) {
      if (IS_BEFORE) { toast('Toggle to AFTER to pick a variant.', { kind: 'error' }); return; }
      const inst = currentAbcInst();
      if (!inst) return;   // button disabled anyway
      if (!S.abcToggled.has(h.abc.scopeId) && S.acceptArmed !== pt.id) {
        // the user never flipped through the variants — one warning, then allow
        S.acceptArmed = pt.id;
        toast('You haven’t tried the other variants (' + inst.letters +
          ') — click Accept again to keep "' + inst.current + '".', { kind: 'warn' });
        updateBar();
        return;
      }
      entry.chosenLetter = inst.current;
    }
    recordVerdict(entry);
  }

  function recordVerdict(entry) {
    const pt = currentPoint();
    if (!pt || S.sentVerdicts) return;
    const existing = S.verdicts[pt.id];
    if (existing && existing.verdict === entry.verdict && entry.verdict !== 'redo' &&
      existing.chosenLetter === entry.chosenLetter) {
      delete S.verdicts[pt.id];   // click the active verdict again = clear it
    } else {
      S.verdicts[pt.id] = entry;
    }
    S.acceptArmed = null;
    LS.setJSON(S.verdictsKey, S.verdicts);
    renderPins();
    updateBar();
    // glide to the next unverdicted point, if any
    if (S.verdicts[pt.id]) {
      for (let k = 1; k <= S.reviewList.length; k++) {
        const i = (S.cursor + k) % S.reviewList.length;
        if (!S.verdicts[S.reviewList[i].id]) { jumpTo(i); return; }
      }
    }
  }

  // --- redo mini-input --------------------------------------------------------
  function openMini() {
    if (S.mini || S.sentVerdicts) return;
    const pt = currentPoint();
    if (!pt) return;
    const existing = S.verdicts[pt.id];
    const node = el('div', 'wk-mini');
    const label = el('div', 'wk-mini-label', 'Redo point ' + pt.number + ' — what’s still wrong?');
    const row = el('div', 'wk-row');
    const ta = el('textarea', 'wk-ta wk-mini-ta');
    ta.placeholder = 'e.g. closer, but make it half the size…';
    ta.value = (existing && existing.redoText) || '';
    const originalRedoVoiceNote = (existing && existing.redoVoiceNote) || null;
    let redoVoiceNote = originalRedoVoiceNote;
    const micBtn = el('button', 'wk-mic');
    micBtn.type = 'button';
    micBtn.title = MIC_TITLE;
    micBtn.innerHTML = micGlyph;
    const langSelect = speechLanguageSelect();
    const cancel = el('button', 'wk-btn ghost', 'Cancel');
    const save = el('button', 'wk-btn primary', 'Redo it');
    micBtn.type = cancel.type = save.type = 'button';
    row.append(ta, langSelect, micBtn);
    const actions = el('div', 'wk-row wk-actions');
    actions.append(el('span', 'wk-spacer'), cancel, save);
    node.append(label, row, actions);
    wrap.appendChild(node);
    const mic = DICTATION_MODE === 'voice-note'
      ? makeVoiceRecorder(micBtn, langSelect, (note) => {
        if (redoVoiceNote && redoVoiceNote.path !== originalRedoVoiceNote?.path) deleteVoiceNote(redoVoiceNote);
        redoVoiceNote = note;
      }, null)
      : makeMic(ta, micBtn, langSelect, null);
    function close() { mic.stop(); node.remove(); S.mini = null; }
    function cancelMini() {
      if (redoVoiceNote && redoVoiceNote.path !== originalRedoVoiceNote?.path) deleteVoiceNote(redoVoiceNote);
      close();
    }
    cancel.addEventListener('click', cancelMini);
    save.addEventListener('click', () => {
      const txt = ta.value.trim();
      if (mic.on || mic.uploading) {
        toast(mic.on ? 'Stop the recording before saving.' : 'Wait for the voice note to finish saving.', { kind: 'warn' });
        return;
      }
      if (!txt && !redoVoiceNote) { ta.focus(); return; }
      if (originalRedoVoiceNote && originalRedoVoiceNote.path !== redoVoiceNote?.path) deleteVoiceNote(originalRedoVoiceNote);
      close();
      recordVerdict({ verdict: 'redo', redoText: txt, redoVoiceNote });
    });
    S.mini = { node, ta, micBtn, close: cancelMini };
    ta.focus();
  }

  // --- send verdicts ----------------------------------------------------------
  async function sendVerdicts() {
    const verdicts = S.reviewList
      .filter((p) => S.verdicts[p.id])
      .map((p) => ({ pointId: p.id, ...S.verdicts[p.id] }));
    if (verdicts.length !== S.reviewList.length) return;
    const body = {
      version: 1,
      kind: 'verdicts',
      batchId: S.reviewBatchId,
      round: S.reviewRound,
      sentAt: nowISO(),
      verdicts,
    };
    B.sendv.disabled = true;
    try {
      await api('/__wk/verdicts', body);
      S.sentVerdicts = true;
      toast('Verdicts sent — the ' + EMOJI + ' agent takes it from here.');
      updateBar();
      pollNow();
    } catch (e) {
      if (e.status === 409) {
        toast('The round changed underneath you — refreshing state.', { kind: 'error' });
        pollNow();
      } else {
        toast('Sending verdicts failed: ' + e.message, { kind: 'error' });
      }
    } finally {
      B.sendv.disabled = false;
    }
  }

  // ===== keyboard ============================================================
  // Capture phase so the shortcuts win even inside the host page's own key
  // handling; composedPath()[0] sees through shadow retargeting.
  window.addEventListener('keydown', (e) => {
    const t = e.composedPath ? e.composedPath()[0] : e.target;
    // t can be window/document for programmatic dispatch — contains() would throw
    const isNode = t instanceof Node;
    const editable = isNode && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);

    // THE toggle — deliberately the only one. Matched by CODE so a Hebrew (or
    // any) layout can't move it. Editable fields always win, including Webkit's
    // own feedback textarea: C must remain a normal typed character there.
    if (e.code === HOTKEY_TOGGLE && !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey) {
      if (editable) return;
      e.preventDefault();
      e.stopPropagation();
      // With a review waiting, the key walks into it rather than just unhiding —
      // this was the corner button's job before it was removed, and the offer
      // toast is dismissable, so without this a dismissed toast would strand you.
      if (S.phase === 'reviewing' && !S.reviewing && S.review && S.batch) enterReview({});
      else toggleMode();
      return;
    }

    // Dictation toggle. The modifier guard is load-bearing: Cmd+V must stay
    // paste, Ctrl+V too. Any editable field also wins unconditionally, so V is
    // always typable in notes (even an empty note or while the mic is running).
    // The nearby mic button remains the explicit start/stop control while typing.
    if (e.code === HOTKEY_DICTATE && !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey) {
      if (editable) return;
      if (liveMic && liveMic.on) {
        e.preventDefault();
        e.stopPropagation();
        liveMic.stop();
        return;
      }
      if (S.mode !== 'feedback') return;     // overlay hidden: don't dictate into an invisible card
      const holder = S.mini || S.card;      // the redo mini-input wins while open
      if (!holder || !holder.micBtn || holder.micBtn.hidden) return;
      e.preventDefault();
      e.stopPropagation();
      holder.micBtn.click();                // reuse the button's own start/stop path
      return;
    }

    // Escape only ever CANCELS. It used to double-tap into a mode toggle, but the
    // toggle key is deliberately the single way to switch modes now.
    if (e.key === 'Escape') {
      if (S.mode === 'feedback') {
        if (S.drag) { S.drag.cancel && S.drag.cancel(); S.drag = null; e.stopPropagation(); return; }
        if (S.mini) { S.mini.close(); e.stopPropagation(); return; }
        if (S.card) { S.card.cancel(); e.stopPropagation(); return; }
      }
      return;
    }

    // ←/→ walk the review points while the review bar has focus
    if (S.reviewing && !bar.hidden && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
      const active = root.activeElement;
      if (active && bar.contains(active) && !S.mini) {
        e.preventDefault();
        jumpTo(S.cursor + (e.key === 'ArrowRight' ? 1 : -1));
      }
    }
  }, true);

  // ===== boot ================================================================
  // flush pending debounced writes when the tab goes away mid-edit
  window.addEventListener('pagehide', () => {
    savePoints.flush();
    if (S.card) { S.card.draft.text = S.card.ta.value; LS.setJSON('wk:card', S.card.draft); }
  });

  async function boot() {
    await installCss();

    // BEFORE|AFTER navigation left us a scroll position — restore it exactly
    const sc = SS.getJSON('wk:scroll', null);
    if (sc && sc.path === logicalPath()) {
      SS.remove('wk:scroll');
      window.scrollTo(sc.x, sc.y);
      // some pages relayout late (fonts, images) — re-assert once
      setTimeout(() => window.scrollTo(sc.x, sc.y), 120);
    }

    const params = new URLSearchParams(location.search);
    const reviewParam = params.get('wk-review');
    const pointParam = parseInt(params.get('wk-point') || '', 10);
    const pointBeforeOnArrival = params.get('wk-point-before') === '1';
    S.bootReview = reviewParam;   // don't toast an offer for the round we're booting into

    setMode(reviewParam ? 'feedback' : (LS.get('wk:mode') === 'feedback' ? 'feedback' : 'evaluate'));
    renderPins();
    updateSendBtn();

    // first state fetch before deciding about review boot
    try {
      const st = await api('/__wk/state?known=');
      handleState(st);
    } catch (e) {
      console.warn('[wk] initial state fetch failed:', e.message);
    }

    if (reviewParam) {
      if (S.review && S.review.batchId === reviewParam &&
        (S.phase === 'reviewing' || S.phase === 'verdicts_sent')) {
        enterReview({
          pointNumber: Number.isFinite(pointParam) ? pointParam : null,
          noScroll: !!sc,   // a restored scroll position wins over re-centering
        });
        if (pointBeforeOnArrival && !IS_BEFORE && S.compareScope === 'point') {
          setTimeout(() => setSide('before'), 0);
        }
      } else if (IS_BEFORE) {
        // serve-then-archive race: the BEFORE snapshot loaded but the round is
        // already gone — hand off to the live AFTER document at the same spot
        // rather than leaving the user on an orphaned git-snapshot page.
        SS.setJSON('wk:scroll', { path: logicalPath(), x: Math.round(scrollX), y: Math.round(scrollY) });
        location.replace(physicalPath(logicalPath(), 'after'));
        return;
      } else {
        toast('That review round is finished or superseded.');
      }
    }
    S.bootReview = null;

    schedulePoll();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
