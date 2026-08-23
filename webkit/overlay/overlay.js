/* =============================================================================
 * Ethan's AWESOME WEBKIT - feedback overlay (webkit/overlay/overlay.js)
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
 *    were rejected - host resets still bleed; an iframe was rejected - the
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
 *   GET  /__wk/handshake            204 when the injected browser token is current
 *   GET  /__wk/state?known=<rev>  → {changed, rev, color, emoji, phase,
 *                                    batch, review, verdicts, pendingPointIds}
 *        phase: collecting | awaiting_agent | reviewing | verdicts_sent
 *   POST /__wk/feedback           ← the batch object (schema below)
 *   POST /__wk/feedback/edit      ← one revision-checked pending-point edit
 *   POST /__wk/voice-note?id=…    ← raw audio stored beside the feedback inbox
 *   POST /__wk/voice-note/delete  ← remove an abandoned/replaced recording
 *   POST /__wk/verdicts           ← the verdicts object (409 on batch mismatch)
 *   GET  /__wk/before/<path>      → the page at review.beforeRef, re-injected
 *                                   with data-wk-mode="before"
 * ========================================================================== */
(async () => {
  'use strict';

  // ===== pure protocol helpers (also exercised by focused tests) =============
  function isOverlayScript(node, pageURL) {
    if (!node || String(node.tagName || '').toUpperCase() !== 'SCRIPT') return false;
    const raw = node.getAttribute && node.getAttribute('src');
    if (!raw) return false;
    try {
      const url = new URL(raw, pageURL);
      return url.origin === new URL(pageURL).origin && url.pathname === '/__wk/overlay.js';
    } catch (e) {
      return false;
    }
  }

  function findOverlayScript(doc, pageURL) {
    if (isOverlayScript(doc.currentScript, pageURL)) return doc.currentScript;
    const matches = Array.from(doc.scripts || []).filter((node) => isOverlayScript(node, pageURL));
    return matches.length ? matches[matches.length - 1] : null;
  }

  function overlayScriptHandshake(node, pageURL) {
    if (!isOverlayScript(node, pageURL) || !node.hasAttribute ||
      !node.hasAttribute('defer') || node.hasAttribute('nomodule')) return null;
    const type = String(node.getAttribute('type') || '').trim().toLowerCase();
    if (type && ![
      'module', 'text/javascript', 'application/javascript',
      'text/ecmascript', 'application/ecmascript',
    ].includes(type)) return null;
    const dataset = Object.assign({}, node.dataset || {});
    const nonce = String(node.nonce || node.getAttribute('nonce') || '');
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(dataset.wkColor || '') ||
      !/^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$/.test(dataset.wkProject || '') ||
      !/^[A-Za-z0-9_-]{16,128}$/.test(dataset.wkToken || '') ||
      !/^[A-Za-z0-9+/_-]{16,128}={0,2}$/.test(dataset.wkNonce || '') ||
      dataset.wkNonce !== nonce ||
      !/^[A-Za-z0-9_-]{1,128}$/.test(dataset.wkTrustedTypesPolicy || '') ||
      !['before', 'after'].includes(dataset.wkMode) ||
      typeof dataset.wkEmoji !== 'string' || !dataset.wkEmoji ||
      !['speech', 'voice-note', 'cloud-voice-note'].includes(dataset.wkDictationMode) ||
      !['browse-default', 'draw-default'].includes(dataset.wkInteractionMode) ||
      (dataset.wkMode === 'after' && (dataset.wkBeforePrefix || '') !== '') ||
      (dataset.wkMode === 'before' &&
        !/^\/__wk\/before\/[0-9a-f]{64}$/.test(dataset.wkBeforePrefix || ''))) return null;
    return { script: node, dataset };
  }

  function findOverlayHandshakes(doc, pageURL) {
    const matches = [];
    const seen = new Set();
    const add = (node) => {
      if (!node || seen.has(node)) return;
      seen.add(node);
      const handshake = overlayScriptHandshake(node, pageURL);
      if (handshake) matches.push(handshake);
    };
    add(doc.currentScript);
    const scripts = Array.from(doc.scripts || []);
    for (let index = scripts.length - 1; index >= 0; index -= 1) add(scripts[index]);
    return matches;
  }

  function findOverlayHandshake(doc, pageURL) {
    return findOverlayHandshakes(doc, pageURL)[0] || null;
  }

  function claimOverlayInstance(targetWindow, handshake) {
    if (!handshake || targetWindow.__wkOverlayLoaded) return null;
    targetWindow.__wkOverlayLoaded = true;
    return handshake;
  }

  async function authenticateOverlayToken(request, token) {
    try {
      const response = await request(
        '/__wk/handshake', apiRequestOptions('/__wk/handshake', undefined, token)
      );
      return !!response && response.status === 204;
    } catch (error) {
      return false;
    }
  }

  async function authenticateOverlayHandshake(request, handshakes) {
    for (const handshake of (handshakes || []).slice(0, 32)) {
      if (await authenticateOverlayToken(request, handshake.dataset.wkToken)) {
        return handshake;
      }
    }
    return null;
  }

  function modifierKeyLabel(platform) {
    return /(?:mac|iphone|ipad|ipod)/i.test(String(platform || '')) ? '⌥' : 'Alt';
  }

  function makeStore(backing, namespace) {
    const cache = new Map();
    const dirtyValues = new Map();
    const dirtyRemovals = new Set();
    const physicalKey = (logical) => namespace + String(logical).replace(/^wk:/, '');
    const logicalKey = (physical) => typeof physical === 'string' && physical.startsWith(namespace)
      ? 'wk:' + physical.slice(namespace.length)
      : null;
    return {
      get(k) {
        if (dirtyRemovals.has(k)) return null;
        if (dirtyValues.has(k)) return dirtyValues.get(k);
        const key = physicalKey(k);
        try {
          const value = backing.getItem(key);
          if (value === null) cache.delete(k);
          else cache.set(k, value);
          return value;
        } catch (e) {
          return cache.has(k) ? cache.get(k) : null;
        }
      },
      set(k, v) {
        cache.set(k, v);
        dirtyRemovals.delete(k);
        try {
          backing.setItem(physicalKey(k), v);
          dirtyValues.delete(k);
          return true;
        } catch (e) {
          dirtyValues.set(k, v);
          return false;
        }
      },
      remove(k) {
        cache.delete(k);
        dirtyValues.delete(k);
        try {
          backing.removeItem(physicalKey(k));
          dirtyRemovals.delete(k);
          return true;
        } catch (e) {
          dirtyRemovals.add(k);
          return false;
        }
      },
      // Return logical keys only. Callers never need to know the physical prefix.
      keys() {
        const out = new Set();
        let backingReadable = true;
        try {
          for (let i = 0; i < backing.length; i++) {
            const logical = logicalKey(backing.key(i));
            if (logical !== null) out.add(logical);
          }
        } catch (e) {
          backingReadable = false;
        }
        if (!backingReadable) {
          for (const key of cache.keys()) out.add(key);
        }
        for (const key of dirtyValues.keys()) out.add(key);
        for (const key of dirtyRemovals) out.delete(key);
        return [...out];
      },
      logicalKey,
      getJSON(k, fallback) {
        const raw = this.get(k);
        if (raw === null) return fallback;
        try { return JSON.parse(raw); } catch (e) { return fallback; }
      },
      setJSON(k, v) { return this.set(k, JSON.stringify(v)); },
    };
  }

  // Each unsent point owns a separate localStorage key. This is deliberately
  // not one shared JSON array: two renderer processes can both read the same
  // array and then overwrite one another even though each individual
  // localStorage operation is atomic. Distinct point keys make independent
  // additions commute. A separate, persistent tombstone wins over a stale tab
  // rewriting an already sent or deleted point.
  const QUEUED_POINT_PREFIX = 'wk:queued-point:';
  const QUEUED_TOMBSTONE_PREFIX = 'wk:queued-tombstone:';
  const QUEUED_POINT_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
  const MAX_QUEUED_TOMBSTONES = 2048;

  function queuedPointKey(id) { return QUEUED_POINT_PREFIX + id; }
  function queuedTombstoneKey(id) { return QUEUED_TOMBSTONE_PREFIX + id; }

  function validQueuedRect(value) {
    return !!value && typeof value === 'object' &&
      ['x', 'y', 'w', 'h'].every((name) => Number.isFinite(value[name])) &&
      value.w > 0 && value.h > 0;
  }

  function validQueuedPoint(point) {
    return !!point && typeof point === 'object' &&
      typeof point.id === 'string' && QUEUED_POINT_ID.test(point.id) &&
      typeof point.page === 'string' && point.page.startsWith('/') &&
      !point.page.startsWith('//') &&
      Number.isInteger(point.number) && point.number > 0 &&
      typeof point.text === 'string' && validQueuedRect(point.rect) &&
      (!Array.isArray(point.rects) || point.rects.every(validQueuedRect));
  }

  function normalizeQueuedPointNumbers(points, reservedNumbers) {
    const reserved = new Set((reservedNumbers || []).filter(
      (number) => Number.isInteger(number) && number > 0
    ));
    const used = new Set(reserved);
    const ordered = points.slice().sort((left, right) => {
      const byNumber = left.number - right.number;
      if (byNumber) return byNumber;
      const byCreated = String(left.createdAt || '').localeCompare(String(right.createdAt || ''));
      return byCreated || left.id.localeCompare(right.id);
    });
    return ordered.map((point) => {
      let number = point.number;
      while (used.has(number)) number += 1;
      used.add(number);
      return number === point.number ? point : { ...point, number };
    });
  }

  function readQueuedPointState(store, legacyPoints, reservedNumbers) {
    const tombstones = new Set();
    const records = new Map();
    for (const key of store.keys()) {
      if (key.startsWith(QUEUED_TOMBSTONE_PREFIX)) {
        const id = key.slice(QUEUED_TOMBSTONE_PREFIX.length);
        if (QUEUED_POINT_ID.test(id)) tombstones.add(id);
      }
    }
    for (const point of Array.isArray(legacyPoints) ? legacyPoints : []) {
      if (validQueuedPoint(point) && !tombstones.has(point.id)) records.set(point.id, point);
    }
    for (const key of store.keys()) {
      if (!key.startsWith(QUEUED_POINT_PREFIX)) continue;
      const id = key.slice(QUEUED_POINT_PREFIX.length);
      const point = store.getJSON(key, null);
      if (QUEUED_POINT_ID.test(id) && validQueuedPoint(point) && point.id === id &&
        !tombstones.has(id)) records.set(id, point);
    }
    return normalizeQueuedPointNumbers([...records.values()], reservedNumbers);
  }

  function hasQueuedTombstoneCapacity(store, ids, limit) {
    const existing = new Set();
    let count = 0;
    for (const key of store.keys()) {
      if (!key.startsWith(QUEUED_TOMBSTONE_PREFIX)) continue;
      count += 1;
      const id = key.slice(QUEUED_TOMBSTONE_PREFIX.length);
      if (QUEUED_POINT_ID.test(id)) existing.add(id);
    }
    for (const id of new Set(ids || [])) {
      if (!QUEUED_POINT_ID.test(id)) return false;
      if (!existing.has(id)) count += 1;
    }
    return count <= limit;
  }

  function safeWindowStorage(name) {
    try {
      const value = window[name];
      if (value) return value;
    } catch (e) { /* use the inert fallback */ }
    return {
      length: 0,
      key() { return null; },
      getItem() { return null; },
      setItem() { throw new Error(name + ' is unavailable'); },
      removeItem() { throw new Error(name + ' is unavailable'); },
    };
  }

  function apiRequestOptions(path, body, token) {
    if (body !== undefined) {
      return {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-WK-Token': token },
        body: JSON.stringify(body),
      };
    }
    const options = { cache: 'no-store' };
    if (/^\/__wk\/(?:handshake|state)(?:\?|$)/.test(path)) {
      options.headers = { 'X-WK-Token': token };
    }
    return options;
  }

  function singleFlight(task, onIdle) {
    let active = null;
    let rerun = false;
    return function run() {
      if (active) {
        rerun = true;
        return active;
      }
      active = (async () => {
        do {
          rerun = false;
          await task();
        } while (rerun);
      })().finally(() => {
        active = null;
        if (onIdle) onIdle();
      });
      return active;
    };
  }

  function pointRevision(point) {
    return Number.isInteger(point && point.revision) && point.revision > 0
      ? point.revision
      : 1;
  }

  function pendingReviewPoints(
    batchPoints, reviewPoints, optimisticPoints, queuedPoints, serverPendingIds
  ) {
    const reviewed = new Set((reviewPoints || []).map((point) => {
      const revision = Number.isInteger(point && point.feedbackRevision) && point.feedbackRevision > 0
        ? point.feedbackRevision
        : 1;
      return point && point.id ? point.id + ':' + revision : '';
    }).filter(Boolean));
    const authoritativePending = Array.isArray(serverPendingIds)
      ? new Set(serverPendingIds)
      : null;
    const byId = new Map();
    for (const points of [batchPoints, optimisticPoints, queuedPoints]) {
      for (const point of (points || [])) {
        if (!point || typeof point.id !== 'string' || !point.id) continue;
        if (points === batchPoints && authoritativePending && !authoritativePending.has(point.id)) continue;
        const current = byId.get(point.id);
        if (!current || pointRevision(point) > pointRevision(current)) byId.set(point.id, point);
      }
    }
    return [...byId.values()].filter(
      (point) => !reviewed.has(point.id + ':' + pointRevision(point))
    );
  }

  function anchorFitScore(rect, box) {
    if (!rect || !box || rect.w <= 0 || rect.h <= 0 || box.w <= 0 || box.h <= 0) return 0;
    const overlapWidth = Math.min(rect.x + rect.w, box.x + box.w) - Math.max(rect.x, box.x);
    const overlapHeight = Math.min(rect.y + rect.h, box.y + box.h) - Math.max(rect.y, box.y);
    if (overlapWidth <= 0 || overlapHeight <= 0) return 0;
    const intersection = overlapWidth * overlapHeight;
    const union = rect.w * rect.h + box.w * box.h - intersection;
    return union > 0 ? intersection / union : 0;
  }

  function anchorCoverageScore(rect, box) {
    if (!rect || !box || rect.w <= 0 || rect.h <= 0 || box.w <= 0 || box.h <= 0) return 0;
    const overlapWidth = Math.min(rect.x + rect.w, box.x + box.w) - Math.max(rect.x, box.x);
    const overlapHeight = Math.min(rect.y + rect.h, box.y + box.h) - Math.max(rect.y, box.y);
    if (overlapWidth <= 0 || overlapHeight <= 0) return 0;
    return (overlapWidth * overlapHeight) / (rect.w * rect.h);
  }

  function anchorContextScore(rect, box, viewport) {
    const fit = anchorFitScore(rect, box);
    if (fit >= 0.08) return fit;
    const coverage = anchorCoverageScore(rect, box);
    if (coverage < 0.75) return 0;
    const viewportWidth = Number(viewport && viewport.w);
    const viewportHeight = Number(viewport && viewport.h);
    if (!(viewportWidth > 0 && viewportHeight > 0)) return 0;
    const maxSceneArea = viewportWidth * viewportHeight * 2;
    return box.w * box.h <= maxSceneArea ? coverage : 0;
  }

  function reanchorRect(rect, capturedBox, liveBox) {
    if (!rect || !capturedBox || !liveBox || capturedBox.w <= 0 || capturedBox.h <= 0) return rect;
    const scaleX = liveBox.w / capturedBox.w;
    const scaleY = liveBox.h / capturedBox.h;
    if (![scaleX, scaleY].every(Number.isFinite) || scaleX < 0.25 || scaleX > 4 || scaleY < 0.25 || scaleY > 4) {
      return {
        x: rect.x + liveBox.x - capturedBox.x,
        y: rect.y + liveBox.y - capturedBox.y,
        w: rect.w,
        h: rect.h,
      };
    }
    return {
      x: liveBox.x + (rect.x - capturedBox.x) * scaleX,
      y: liveBox.y + (rect.y - capturedBox.y) * scaleY,
      w: rect.w * scaleX,
      h: rect.h * scaleY,
    };
  }

  function reviewScrollTarget(
    box, viewportCoordinates, anchorMode, currentScrollY, viewportHeight, capturedStickyScrollY
  ) {
    if (!box || ![box.x, box.y, box.w, box.h, currentScrollY, viewportHeight].every(Number.isFinite) ||
      box.w <= 0 || box.h <= 0 || viewportHeight <= 0) return null;
    // A sticky scene can keep the exact same viewport box while replacing its
    // contents as scroll progress advances. Selecting the point must restore
    // the capture moment even when the stale box is technically still visible.
    if (viewportCoordinates && anchorMode === 'sticky' && Number.isFinite(capturedStickyScrollY)) {
      return Math.max(0, capturedStickyScrollY);
    }
    if (!viewportCoordinates) {
      return Math.max(0, box.y + box.h / 2 - viewportHeight / 2);
    }
    const margin = Math.min(80, viewportHeight * 0.1);
    if (box.y >= margin && box.y + box.h <= viewportHeight - margin) return null;
    // A genuinely fixed target cannot be revealed by scrolling. Sticky targets
    // can leave the viewport once their containing scene reaches its boundary,
    // so translate their current viewport position back into a document target.
    if (anchorMode === 'fixed') return null;
    return Math.max(0, currentScrollY + box.y + box.h / 2 - viewportHeight / 2);
  }

  function viewportStateAttributeName(name) {
    if (typeof name !== 'string') return false;
    const lower = name.toLowerCase();
    if (/^aria-(?:current|selected|expanded|pressed|hidden)$/.test(lower)) return true;
    if (!lower.startsWith('data-')) return false;
    const stateWords = new Set([
      'state', 'status', 'step', 'stage', 'slide', 'index', 'current',
      'active', 'view', 'screen', 'mode',
    ]);
    return lower.slice(5).split('-').some((part) => stateWords.has(part));
  }

  function surfaceStateValuesMatch(signal, readAttribute, hasClass) {
    if (!signal || typeof readAttribute !== 'function' || typeof hasClass !== 'function') return false;
    try {
      for (const [name, value] of Object.entries(signal.attrs || {})) {
        if (readAttribute(name) !== value) return false;
      }
      return (signal.classes || []).every((token) =>
        typeof token === 'string' && token.length > 0 && !/[\t\n\f\r ]/.test(token) && hasClass(token)
      );
    } catch (error) {
      return false;
    }
  }

  function effectiveStyleChainVisible(styles) {
    let cumulativeOpacity = 1;
    for (const style of styles || []) {
      if (!style || style.display === 'none' || style.visibility === 'hidden' ||
        style.visibility === 'collapse' || style.contentVisibility === 'hidden') return false;
      const opacity = Number.parseFloat(style.opacity);
      if (Number.isFinite(opacity)) cumulativeOpacity *= opacity;
      if (cumulativeOpacity <= 0.02) return false;
    }
    return true;
  }

  function sameRectGeometry(left, right) {
    return !!left && !!right && ['x', 'y', 'w', 'h'].every((key) =>
      Number.isFinite(left[key]) && Number.isFinite(right[key]) &&
      Math.abs(left[key] - right[key]) < 0.01
    );
  }

  function surfaceMutationRelevant(attributeName, tracked) {
    if (!tracked || typeof attributeName !== 'string') return false;
    return ['class', 'style', 'hidden', 'open'].includes(attributeName) ||
      viewportStateAttributeName(attributeName);
  }

  function surfaceChildListRelevant(target, removedNodes, trackedNodes, remountParents) {
    if ((!trackedNodes || !trackedNodes.size) && (!remountParents || !remountParents.size)) {
      return false;
    }
    if (trackedNodes?.has(target) || remountParents?.has(target)) return true;
    for (const removed of (removedNodes || [])) {
      if (trackedNodes?.has(removed)) return true;
    }
    return false;
  }

  function preferredSurfaceCandidate(candidates) {
    const list = Array.isArray(candidates) ? candidates : [];
    const anchored = list.find((candidate) =>
      ['fixed', 'sticky'].includes(candidate?.anchor?.mode)
    );
    const stateful = list.find((candidate) =>
      Array.isArray(candidate?.stateChain) && candidate.stateChain.length > 0
    );
    return anchored || stateful || list[0] || null;
  }

  function pointAnchorMode(liveModes, capturedModes, legacyViewportAnchor) {
    const modes = [...(liveModes || []), ...(capturedModes || [])];
    if (modes.includes('fixed')) return 'fixed';
    if (modes.includes('sticky')) return 'sticky';
    return legacyViewportAnchor ? 'unknown' : 'doc';
  }

  function rectUsesLegacyViewportAnchor(pointAnchor, surface) {
    return pointAnchor === 'viewport' && !surface;
  }

  function persistedRectForDraft(displayRect, metadataRect, persistedRect) {
    return persistedRect && sameRectGeometry(displayRect, metadataRect)
      ? persistedRect
      : displayRect;
  }

  function primaryRectIndex(rects, primaryRect) {
    const list = rects || [];
    for (let index = list.length - 1; index >= 0; index -= 1) {
      if (sameRectGeometry(list[index], primaryRect)) return index;
    }
    return Math.max(0, list.length - 1);
  }

  function visualSurfaceActive(
    visible, hasStateSignals, stateMatches, anchorMode, reviewing,
    currentScrollY, capturedScrollY, viewportHeight
  ) {
    if (!visible) return false;
    const nearCapture = [currentScrollY, capturedScrollY, viewportHeight].every(Number.isFinite) &&
      Math.abs(currentScrollY - capturedScrollY) <= Math.max(24, viewportHeight * 0.05);
    if (hasStateSignals) return stateMatches || (!!reviewing && nearCapture);
    if (anchorMode === 'sticky') return nearCapture;
    return true;
  }

  function tabActivityMode(phase) {
    if (phase === 'awaiting_agent' || phase === 'verdicts_sent' || phase === 'transitioning') {
      return 'working';
    }
    return phase === 'reviewing' ? 'review-ready' : 'normal';
  }

  function tabPollDelay(activity, overlayMode, hidden) {
    if (hidden && activity !== 'working') return 0;
    if (hidden) return 5000;
    return overlayMode === 'feedback' ? 2000 : 15000;
  }

  const KEYBOARD_ACTIVATION_ROLES = new Set([
    'button', 'link', 'checkbox', 'radio', 'switch', 'menuitem',
    'menuitemcheckbox', 'menuitemradio', 'option', 'tab', 'treeitem',
    'slider', 'spinbutton', 'combobox', 'textbox',
  ]);
  function keyboardActivationTarget(node) {
    for (let current = node; current && current.nodeType === 1; current = current.parentElement) {
      const tag = String(current.tagName || '').toUpperCase();
      const hasAttribute = typeof current.hasAttribute === 'function'
        ? (name) => current.hasAttribute(name)
        : () => false;
      if (tag === 'BUTTON' || tag === 'SUMMARY' ||
        ((tag === 'A' || tag === 'AREA') && hasAttribute('href')) ||
        ((tag === 'AUDIO' || tag === 'VIDEO') && hasAttribute('controls')) ||
        hasAttribute('tabindex')) return true;
      const role = typeof current.getAttribute === 'function'
        ? String(current.getAttribute('role') || '').trim().toLowerCase().split(/\s+/)[0]
        : '';
      if (KEYBOARD_ACTIVATION_ROLES.has(role)) return true;
    }
    return false;
  }

  function dictationTargetAllowsActivation(target, holderTextarea, editable) {
    if (target === holderTextarea) return true;
    return !editable && !keyboardActivationTarget(target);
  }

  function dictationKeyAction(event, hotkeyCode, hold, canActivate) {
    if (!event || event.code !== hotkeyCode || !hold) return 'pass';
    if (event.type === 'keyup') {
      if (hold.code !== event.code) return 'pass';
      hold.code = '';
      return 'suppress';
    }
    if (event.type !== 'keydown') return 'pass';
    if (event.repeat) return hold.code === event.code ? 'suppress' : 'pass';
    if (!canActivate) return 'pass';
    hold.code = event.code;
    return 'activate';
  }
  // ===== end pure protocol helpers ==========================================

  // ===== script dataset ======================================================
  // Claim idempotence only after validating the server handshake. A source
  // page may already contain a bare or stale /__wk/overlay.js script, and that
  // script must not prevent the correctly injected instance from starting.
  const handshakeCandidates = findOverlayHandshakes(document, location.href);
  const handshakeCandidate = await authenticateOverlayHandshake(
    window.fetch.bind(window), handshakeCandidates
  );
  if (!handshakeCandidate) {
    if (handshakeCandidates.length) console.warn('[wk] overlay handshake was rejected');
    return;
  }
  const overlayHandshake = claimOverlayInstance(window, handshakeCandidate);
  if (!overlayHandshake) return;
  const scriptEl = overlayHandshake.script;
  const DS = overlayHandshake.dataset;
  const COLOR = DS.wkColor || 'unknown';
  const PROJECT = DS.wkProject || 'unknown-project';
  const MUTATION_TOKEN = DS.wkToken || '';
  const CSP_NONCE = /^[A-Za-z0-9+/_-]{16,128}={0,2}$/.test(DS.wkNonce || '')
    ? DS.wkNonce
    : '';
  const TRUSTED_TYPES_POLICY_NAME = /^[A-Za-z0-9_-]{1,128}$/.test(
    DS.wkTrustedTypesPolicy || ''
  ) ? DS.wkTrustedTypesPolicy : '';
  let trustedHTMLPolicy = null;
  if (window.trustedTypes && TRUSTED_TYPES_POLICY_NAME) {
    try {
      trustedHTMLPolicy = window.trustedTypes.createPolicy(TRUSTED_TYPES_POLICY_NAME, {
        createHTML(value) { return value; },
      });
    } catch (error) {
      console.warn('[wk] Trusted Types policy could not be created:', error.message);
    }
  }
  const asTrustedHTML = (value) => trustedHTMLPolicy
    ? trustedHTMLPolicy.createHTML(value)
    : value;
  const EMOJI = DS.wkEmoji || '⬜';
  const NORMAL_TAB_TITLE = document.title;
  const TAB_BASE_TITLE = NORMAL_TAB_TITLE.startsWith(EMOJI)
    ? NORMAL_TAB_TITLE.slice(EMOJI.length).trim()
    : NORMAL_TAB_TITLE;
  const IS_BEFORE = DS.wkMode === 'before';   // reduced state: no drawing, ⚗ disabled
  const DICTATION_MODE = ['voice-note', 'cloud-voice-note'].includes(DS.wkDictationMode)
    ? DS.wkDictationMode
    : 'speech';
  const USES_VOICE_NOTES = DICTATION_MODE !== 'speech';
  const USES_CLOUD_TRANSCRIPTION = DICTATION_MODE === 'cloud-voice-note';
  const INTERACTION_MODE = DS.wkInteractionMode === 'draw-default'
    ? 'draw-default'
    : 'browse-default';
  const PLATFORM = (navigator.userAgentData && navigator.userAgentData.platform)
    || navigator.platform || '';
  const MODIFIER_LABEL = modifierKeyLabel(PLATFORM);

  // Hotkeys are KeyboardEvent.code values (LAYOUT-INDEPENDENT: this site is
  // Hebrew, so `e.key` would be a different character on every layout).
  // Ctrl/Cmd+. - the original primary - never reaches the page on macOS Chrome
  // (the browser eats the combo), hence a single bare key. The server injects
  // overrides from the config's `hotkeys` block; these are the defaults.
  const HOTKEY_TOGGLE = DS.wkHotkeyToggle || 'KeyC';
  const HOTKEY_DICTATE = DS.wkHotkeyDictate || 'Space';
  const KEY_LABELS = {
    Backquote: '`', Minus: '-', Equal: '=', BracketLeft: '[', BracketRight: ']',
    Backslash: '\\', Semicolon: ';', Quote: "'", Comma: ',', Period: '.', Slash: '/',
    Space: 'Space', Enter: '↵', ArrowUp: '↑', ArrowDown: '↓', ArrowLeft: '←', ArrowRight: '→',
  };
  const keyLabel = (code) => KEY_LABELS[code]
    || (/^Key[A-Z]$/.test(code) ? code.slice(3)
      : /^Digit[0-9]$/.test(code) ? code.slice(5)
        : code);
  const TOGGLE_LABEL = keyLabel(HOTKEY_TOGGLE);
  const DICTATE_LABEL = keyLabel(HOTKEY_DICTATE);
  const MIC_TITLE = USES_VOICE_NOTES
    ? 'Record a voice note for the agent - or press ' + DICTATE_LABEL + ' in an empty note'
    : 'Dictate (Chrome speech-to-text) - or press ' + DICTATE_LABEL + ' in an empty note';
  const MIC_ARIA_LABEL = USES_VOICE_NOTES
    ? 'Start or stop recording a voice note'
    : 'Start or stop speech dictation';

  const BEFORE_PREFIX_PATTERN = /^\/__wk\/before\/[a-f0-9]{64}$/;
  let BEFORE_PREFIX = BEFORE_PREFIX_PATTERN.test(DS.wkBeforePrefix || '')
    ? DS.wkBeforePrefix
    : '/__wk/before/invalid';
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

  let pointIdSequence = 0;
  function newPointId() {
    pointIdSequence += 1;
    try {
      if (window.crypto && typeof window.crypto.randomUUID === 'function') {
        return 'p-' + window.crypto.randomUUID();
      }
      if (window.crypto && typeof window.crypto.getRandomValues === 'function') {
        const bytes = new Uint8Array(16);
        window.crypto.getRandomValues(bytes);
        return 'p-' + Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
      }
    } catch (e) { /* use the session-local fallback */ }
    return 'p-' + Date.now().toString(36) + '-' + pointIdSequence.toString(36) + '-' + rand4() + rand4();
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  const SVG_NS = 'http://www.w3.org/2000/svg';
  function svgEl(tag, attributes) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [name, value] of Object.entries(attributes || {})) {
      node.setAttribute(name, value);
    }
    return node;
  }

  function microphoneIcon() {
    const svg = svgEl('svg', {
      viewBox: '0 0 16 16', width: '14', height: '14', fill: 'none',
      stroke: 'currentColor', 'stroke-width': '1.5', 'stroke-linecap': 'round',
      'aria-hidden': 'true',
    });
    svg.append(
      svgEl('rect', { x: '5.5', y: '1.75', width: '5', height: '8', rx: '2.5' }),
      svgEl('path', { d: 'M3 7.5a5 5 0 0 0 10 0M8 12.5v2' }),
    );
    return svg;
  }

  function closeIcon() {
    const svg = svgEl('svg', {
      viewBox: '0 0 10 10', 'aria-hidden': 'true',
    });
    svg.appendChild(svgEl('path', {
      d: 'M1 1l8 8M9 1L1 9', fill: 'none', stroke: 'currentColor',
      'stroke-width': '1.6', 'stroke-linecap': 'round',
    }));
    return svg;
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
  // Accessing the storage property itself can throw. Reads and writes can also
  // throw, so each store mirrors writes in memory for the current page lifetime.
  const STORAGE_NAMESPACE = 'wk:' + encodeURIComponent(PROJECT) + ':' + encodeURIComponent(COLOR) + ':';
  const LS = makeStore(safeWindowStorage('localStorage'), STORAGE_NAMESPACE);
  const SS = makeStore(safeWindowStorage('sessionStorage'), STORAGE_NAMESPACE);
  const POINT_QUEUE_LOCK = STORAGE_NAMESPACE + 'point-queue';

  async function withPointQueueLock(task) {
    const manager = navigator.locks;
    if (!manager || typeof manager.request !== 'function' ||
      typeof AbortController !== 'function') return task();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 5000);
    try {
      return await manager.request(
        POINT_QUEUE_LOCK, { mode: 'exclusive', signal: controller.signal }, task
      );
    } catch (error) {
      if (error && error.name === 'AbortError') {
        throw new Error('Another tab is still updating the feedback queue. Try again.');
      }
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

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
    select.setAttribute('aria-label', USES_VOICE_NOTES ? 'Voice-note language' : 'Dictation language');
    select.title = USES_VOICE_NOTES
      ? (USES_CLOUD_TRANSCRIPTION ? 'Language hint for OpenAI transcription' : 'Language hint for local transcription')
      : 'Speech recognition language';
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
    points: readQueuedPointState(LS, LS.getJSON('wk:points', []), []),
    drag: null,                 // active rubber/resize/move drag
    card: null,                 // open editor card {draft, els...}
    mini: null,                 // open redo mini-input
    reviewing: false,
    reviewBatchId: null,
    reviewRound: 0,
    verdictsKey: null,          // 'wk:verdicts:<batchId>:<round>' for the active review
    reviewList: [],             // batch points under review, ordered by number
    submittedPoints: [],        // accepted points awaiting the next server-state poll
    pendingPointIds: null,      // server-authoritative unreviewed point ids
    agentWakePending: false,    // successful local send awaiting authoritative state
    handledById: new Map(),     // pointId → review.points entry
    verdicts: {},               // pointId → {verdict, chosenLetter?, redoText?}
    deletedIds: new Set(),      // points deleted/sent this session - never resurrect on cross-tab merge
    cursor: 0,
    side: IS_BEFORE ? 'before' : 'after',  // the served document is authoritative
    compareScope: LS.get('wk:compareScope') === 'site' ? 'site' : 'point',
    sentVerdicts: false,
    abcToggled: new Set(),      // scopeIds the user toggled since review entry
    acceptArmed: null,          // pointId armed for "accept without toggling" confirm
    offeredReview: '',          // batchId:round already auto-entered or queued
    reviewAutoPending: false,   // ready review waiting for an open editor to close
    bootReview: null,           // ?wk-review target during boot - owns automatic entry
    pinEls: [],                 // [{node, point, rectIndex, kind}] for live positioning
    altHeld: false,
  };
  function reservedPointNumbers() {
    const points = S.batch && Array.isArray(S.batch.points) ? S.batch.points : [];
    return points.map((point) => point && point.number).filter(
      (number) => Number.isInteger(number) && number > 0
    );
  }

  function loadQueuedPoints() {
    return readQueuedPointState(
      LS, LS.getJSON('wk:points', []), reservedPointNumbers()
    );
  }

  function retireQueuedPointIds(ids) {
    const unique = [...new Set(ids || [])];
    if (!hasQueuedTombstoneCapacity(LS, unique, MAX_QUEUED_TOMBSTONES)) return false;
    for (const id of unique) {
      S.deletedIds.add(id);
      LS.set(queuedTombstoneKey(id), nowISO());
      LS.remove(queuedPointKey(id));
    }
    S.points = S.points.filter((point) => !S.deletedIds.has(point.id));
    savePoints();
    savePoints.flush();
    return true;
  }

  const POINT_SAVE_RETRY_MS = [100, 500, 2000];
  let pointSaveRetry = 0;
  let pointSaveRetryTimer = 0;
  let pointStorageWarned = false;
  let pointStorageWarningClose = null;

  function warnPointStorage() {
    if (pointStorageWarned) return;
    pointStorageWarned = true;
    console.warn('[wk] queued feedback is only stored in this tab because localStorage writes failed');
    pointStorageWarningClose = toast('Feedback storage is unavailable. Keep this tab open and send before reloading.', {
      kind: 'error', ttl: 0,
    });
  }

  function persistQueuedPoints() {
    let durable = true;
    for (const id of S.deletedIds) {
      // The tombstone is written before deleting the record. If a suspended tab
      // later rewrites stale point data, every reader still gives the tombstone
      // precedence and the deleted or sent point cannot reappear.
      durable = LS.set(queuedTombstoneKey(id), nowISO()) && durable;
      LS.remove(queuedPointKey(id));
    }

    const live = [];
    for (const point of S.points) {
      if (!validQueuedPoint(point) || S.deletedIds.has(point.id) ||
        LS.get(queuedTombstoneKey(point.id)) !== null) {
        if (point && typeof point.id === 'string') LS.remove(queuedPointKey(point.id));
        continue;
      }
      live.push(point);
      durable = LS.setJSON(queuedPointKey(point.id), point) && durable;
    }
    S.points = live;

    if (durable) {
      clearTimeout(pointSaveRetryTimer);
      pointSaveRetryTimer = 0;
      pointSaveRetry = 0;
      pointStorageWarned = false;
      if (pointStorageWarningClose) pointStorageWarningClose();
      pointStorageWarningClose = null;
      // This aggregate key is read only for one-time migration from older
      // overlays. Per-point keys are the authoritative cross-tab protocol.
      LS.remove('wk:points');
      LS.remove('wk:lastNum');
    } else if (!pointSaveRetryTimer && pointSaveRetry < POINT_SAVE_RETRY_MS.length) {
      const delay = POINT_SAVE_RETRY_MS[pointSaveRetry++];
      pointSaveRetryTimer = setTimeout(() => {
        pointSaveRetryTimer = 0;
        persistQueuedPoints();
      }, delay);
    } else if (!pointSaveRetryTimer) {
      warnPointStorage();
    }

    // Merge records added by other tabs after our write. Deterministic number
    // normalization means simultaneous claims for the same number converge.
    S.points = loadQueuedPoints();
    return durable;
  }

  const savePoints = debounce(persistQueuedPoints, 150);

  // ===== shadow shell ========================================================
  const host = document.createElement('div');
  host.setAttribute('data-wk-host', '');
  host.setAttribute('popover', 'manual');
  // Inline (not stylesheet) so isolation holds even if the CSS fetch fails.
  host.style.cssText =
    'all:initial;position:fixed;inset:0;z-index:2147483400;pointer-events:none;display:block;';
  const root = host.attachShadow({ mode: 'open' });
  document.documentElement.appendChild(host);

  // Overlay controls must never leak interaction events into the website.
  // Target handlers inside the shadow tree still run first, and stopping at
  // the shadow root keeps ordinary typing and button use away from host-page
  // keyboard, pointer, and delegated input listeners.
  for (const type of [
    'keydown', 'keyup', 'keypress', 'beforeinput', 'input',
    'compositionstart', 'compositionupdate', 'compositionend',
    'pointerdown', 'pointerup', 'pointermove', 'pointercancel',
    'mousedown', 'mouseup', 'mousemove', 'click', 'dblclick', 'contextmenu',
    'touchstart', 'touchmove', 'touchend', 'touchcancel',
  ]) {
    root.addEventListener(type, (event) => event.stopPropagation());
  }

  // Native dialogs and popovers live in the browser's "top layer", above
  // every z-index in the document. Keep WebKit in that same layer and raise it
  // again whenever the page opens another top-layer surface. This lets users
  // draw on settings dialogs, menus, and other popups instead of the feedback
  // UI disappearing behind them. Pointer-events still pass through whenever
  // the drawing layer is in browse mode.
  let topLayerRaiseQueued = false;
  const modalOpenOrder = new WeakMap();
  let modalOpenSequence = 0;
  function rememberModalState(dialog, promote) {
    if (!dialog.open) {
      modalOpenOrder.delete(dialog);
      return false;
    }
    try {
      if (!dialog.matches(':modal')) {
        modalOpenOrder.delete(dialog);
        return false;
      }
    } catch (e) {
      return false;
    }
    if (promote || !modalOpenOrder.has(dialog)) {
      modalOpenOrder.set(dialog, ++modalOpenSequence);
    }
    return true;
  }
  function activeModalDialog() {
    if (typeof HTMLDialogElement === 'undefined') return null;
    const openDialogs = [...document.querySelectorAll('dialog[open]')];
    let active = null;
    let activeOrder = -1;
    for (const dialog of openDialogs) {
      if (!rememberModalState(dialog, false)) continue;
      const order = modalOpenOrder.get(dialog);
      if (order > activeOrder) {
        active = dialog;
        activeOrder = order;
      }
    }
    return active;
  }

  function raiseAboveTopLayer() {
    topLayerRaiseQueued = false;
    // A showModal() dialog makes every node outside itself inert. Merely
    // putting WebKit later in the top-layer stack keeps the pill visible, but
    // its drawing surface still cannot receive pointer events. Temporarily
    // parent the host inside the active modal so modifier-drag works there too;
    // move it back to <html> as soon as that modal closes.
    const container = activeModalDialog() || document.documentElement;
    const moving = host.parentNode !== container;
    if (typeof host.showPopover !== 'function') return;
    try {
      if (moving && host.matches(':popover-open')) host.hidePopover();
      if (moving) container.appendChild(host);
      else if (host.matches(':popover-open')) host.hidePopover();
      host.showPopover();
    } catch (e) { /* older browser or a transient detached host */ }
  }
  function queueTopLayerRaise() {
    if (topLayerRaiseQueued) return;
    topLayerRaiseQueued = true;
    setTimeout(raiseAboveTopLayer, 0);
  }
  queueTopLayerRaise();
  document.addEventListener('toggle', (event) => {
    if (event.target !== host && event.newState === 'open') queueTopLayerRaise();
  }, true);

  // A tidy-minded host page (or a framework re-render) may remove foreign
  // nodes from the tree - quietly re-append ourselves.
  new MutationObserver((records) => {
    if (!host.isConnected) {
      document.documentElement.appendChild(host);
      queueTopLayerRaise();
    }
    let modalLayerChanged = false;
    if (typeof HTMLDialogElement !== 'undefined') {
      for (const record of records) {
        if (record.type !== 'attributes' || record.attributeName !== 'open' ||
          !(record.target instanceof HTMLDialogElement)) continue;
        rememberModalState(record.target, record.target.open);
        modalLayerChanged = true;
      }
    }
    if (modalLayerChanged) {
      queueTopLayerRaise();
    }
    if (records.some((record) => record.type === 'attributes' &&
      (record.attributeName === 'hidden' || record.attributeName === 'open'))) {
      requestAnimationFrame(() => { if (!S.card) renderPins(); });
    }
    const surfaceChildListRecords = records.filter(childListAffectsTrackedSurface);
    if (surfaceChildListRecords.length) {
      for (const record of surfaceChildListRecords) rememberSurfaceRemountParent(record.target);
      queueSurfaceRebind();
    }
  }).observe(document.documentElement, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ['open', 'hidden'],
  });

  // If the fetch fails there is no "unstyled but working" fallback: pointer-events
  // is inherited from the host div's inline `pointer-events:none`, so ONLY the
  // stylesheet re-enables clicks - without it every control is dead AND .wk-off
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
      cssSheet.replaceSync(css);   // replace, don't append - retries must not stack sheets
    } catch (e) {
      if (!cssStyleNode) {
        cssStyleNode = document.createElement('style');
        if (CSP_NONCE) cssStyleNode.setAttribute('nonce', CSP_NONCE);
        root.appendChild(cssStyleNode);
      }
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
    ? 'hold ' + MODIFIER_LABEL + ' + drag to mark a spot'
    : 'drag to mark a spot · hold ' + MODIFIER_LABEL + ' to use the page';
  const hintChip = el('div', 'wk-hint', interactionHint + ' · ' + TOGGLE_LABEL +
    ' to hide · ' + DICTATE_LABEL + ' to dictate in an empty note');
  const sendBtn = el('button', 'wk-send');
  sendBtn.type = 'button';
  sendBtn.hidden = true;
  const statusChip = el('div', 'wk-chip');
  statusChip.hidden = true;
  statusChip.setAttribute('role', 'status');
  statusChip.setAttribute('aria-live', 'polite');
  statusChip.setAttribute('aria-atomic', 'true');
  const bar = el('div', 'wk-bar');                   // review bar, built on demand
  bar.hidden = true;
  bar.tabIndex = 0;
  bar.setAttribute('role', 'region');
  bar.setAttribute('aria-label', 'Feedback review controls');
  const toasts = el('div', 'wk-toasts');
  toasts.setAttribute('aria-live', 'polite');
  toasts.setAttribute('aria-relevant', 'additions text');
  toasts.setAttribute('aria-label', 'Webkit notifications');

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
    t.setAttribute('role', opts.kind === 'error' ? 'alert' : 'status');
    t.setAttribute('aria-atomic', 'true');
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
    x.setAttribute('aria-label', 'Dismiss notification');
    x.addEventListener('click', close);
    t.appendChild(x);
    toasts.appendChild(t);
    if (opts.ttl !== 0) setTimeout(close, opts.ttl || 4200);
    return close;
  }

  // Migrate the pre-v0.9 aggregate queue after notifications are available.
  // A failed migration keeps the parsed points in memory and follows the
  // bounded retry path, so the failure is visible before a reload can lose it.
  persistQueuedPoints();

  // ===== server API ==========================================================
  async function api(path, body, timeoutMs) {
    const options = apiRequestOptions(path, body, MUTATION_TOKEN);
    const controller = timeoutMs && typeof AbortController === 'function'
      ? new AbortController()
      : null;
    const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : 0;
    if (controller) options.signal = controller.signal;
    try {
      const res = await fetch(path, options);
      let json = null;
      try { json = await res.json(); } catch (e) { /* non-JSON error body */ }
      if (!res.ok) {
        const err = new Error((json && json.error) || ('HTTP ' + res.status));
        err.status = res.status;
        throw err;
      }
      return json;
    } catch (error) {
      if (error && error.name === 'AbortError') {
        throw new Error('The local preview server did not respond in time.');
      }
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  function deleteVoiceNote(note) {
    if (!note || !note.path) return;
    api('/__wk/voice-note/delete', { path: note.path }).catch(() => { /* best-effort orphan cleanup */ });
  }

  function deleteDistinctVoiceNotes(...notes) {
    const paths = new Set();
    for (const note of notes) {
      if (!note || !note.path || paths.has(note.path)) continue;
      paths.add(note.path);
      deleteVoiceNote(note);
    }
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
    hintChip.hidden = !(S.mode === 'feedback' && !S.card && !S.reviewing);
  }

  // ===== rAF reposition engine ===============================================
  // Pins, rects, the frozen rect and the editor card are all positioned in
  // doc-coords and translated to viewport-coords in one coalesced rAF pass.
  let posRaf = 0;
  function schedulePos() { if (!posRaf) posRaf = requestAnimationFrame(repositionAll); }
  // viewport-anchored boxes (pins on a position:fixed/sticky ancestor) are already
  // stored in viewport coords, so they must NOT be scroll-translated - that is what
  // keeps them pinned to the fixed element instead of drifting up the document.
  function place(node, box, fixed) {
    const x = fixed ? box.x : box.x - scrollX;
    const y = fixed ? box.y : box.y - scrollY;
    node.style.transform = 'translate(' + x + 'px,' + y + 'px)';
    return { x, y };
  }
  function repositionAll() {
    posRaf = 0;
    const geometryCache = new Map();
    const signature = [];
    for (const marker of S.pinEls) {
      let geometry = geometryCache.get(marker.point);
      if (!geometry) {
        geometry = correctedPointRects(marker.point);
        geometryCache.set(marker.point, geometry);
      }
      const source = geometry.boxes[marker.rectIndex];
      if (geometry.active[marker.rectIndex] === false || !source ||
        ![source.x, source.y, source.w, source.h].every(Number.isFinite) ||
        source.w <= 0 || source.h <= 0) {
        marker.node.style.visibility = 'hidden';
        continue;
      }
      const box = marker.kind === 'pin'
        ? { x: source.x - 12, y: source.y - 12, w: 24, h: 24 }
        : source;
      if (marker.kind === 'rect') {
        marker.node.style.width = box.w + 'px';
        marker.node.style.height = box.h + 'px';
      }
      const placed = place(marker.node, box, geometry.fixedByRect[marker.rectIndex]);
      marker.node.style.visibility = '';
      signature.push(
        Math.round(placed.x * 10) + ':' + Math.round(placed.y * 10) + ':' +
        Math.round(box.w * 10) + ':' + Math.round(box.h * 10)
      );
    }
    if (S.card) positionFrozen(), positionCard();
    return signature.join('|');
  }

  // Moving sites often keep animating their scene for several frames after the
  // browser's final scroll event. Hide markers during motion, continuously
  // recompute their live anchors, and reveal only after the geometry settles.
  // This trades a brief absence for never flashing a marker over unrelated UI.
  let pinMotionTimer = 0;
  let pinSettleRaf = 0;
  let pinMotionGeneration = 0;
  function settlePinMotion(generation) {
    let lastSignature = '';
    let stableFrames = 0;
    let frames = 0;
    const settle = () => {
      if (generation !== pinMotionGeneration) return;
      const nextSignature = repositionAll();
      stableFrames = nextSignature === lastSignature ? stableFrames + 1 : 0;
      lastSignature = nextSignature;
      frames += 1;
      if (stableFrames >= 2 || frames >= 60) {
        pinSettleRaf = 0;
        pinLayer.classList.remove('wk-motion');
        return;
      }
      pinSettleRaf = requestAnimationFrame(settle);
    };
    if (posRaf) {
      cancelAnimationFrame(posRaf);
      posRaf = 0;
    }
    pinSettleRaf = requestAnimationFrame(settle);
  }
  function notePinMotion() {
    pinMotionGeneration += 1;
    const generation = pinMotionGeneration;
    pinLayer.classList.add('wk-motion');
    if (pinSettleRaf) {
      cancelAnimationFrame(pinSettleRaf);
      pinSettleRaf = 0;
    }
    if (pinMotionTimer) clearTimeout(pinMotionTimer);
    schedulePos();
    pinMotionTimer = setTimeout(() => {
      pinMotionTimer = 0;
      settlePinMotion(generation);
    }, 80);
  }

  let surfaceObserver = null;
  let surfaceRebindRaf = 0;
  const trackedSurfaceNodes = new Set();
  const surfaceRemountParents = new Set();
  function rememberSurfaceRemountParent(node) {
    if (!node || !node.isConnected || isOverlayNode(node) ||
      (surfaceRemountParents.size >= 64 && !surfaceRemountParents.has(node))) return;
    surfaceRemountParents.add(node);
  }
  function queueSurfaceRebind() {
    if (surfaceRebindRaf) return;
    surfaceRebindRaf = requestAnimationFrame(() => {
      surfaceRebindRaf = 0;
      refreshSurfaceObservation();
      notePinMotion();
    });
  }
  function nodeAffectsTrackedSurface(changedNode) {
    for (let current = changedNode; current && current !== document; current = current.parentElement) {
      if (trackedSurfaceNodes.has(current)) return true;
    }
    return false;
  }
  function childListAffectsTrackedSurface(record) {
    return !!record && record.type === 'childList' && surfaceChildListRelevant(
      record.target, record.removedNodes, trackedSurfaceNodes, surfaceRemountParents
    );
  }
  function refreshSurfaceObservation() {
    if (surfaceRebindRaf) {
      cancelAnimationFrame(surfaceRebindRaf);
      surfaceRebindRaf = 0;
    }
    const connectedTrackedNodes = [...trackedSurfaceNodes].filter((node) =>
      node.isConnected && !isOverlayNode(node)
    );
    trackedSurfaceNodes.clear();
    if (surfaceObserver) surfaceObserver.disconnect();
    if (!S.pinEls.length) {
      surfaceRemountParents.clear();
      return;
    }
    let unresolvedSurface = false;

    const addTrackedNode = (node) => {
      if (!node || node === document.body || node === document.documentElement ||
        isOverlayNode(node)) return false;
      trackedSurfaceNodes.add(node);
      return true;
    };
    const addTargetChain = (target) => {
      let depth = 0;
      for (let current = target; current && depth < 48; current = current.parentElement) {
        if (!addTrackedNode(current)) break;
        depth += 1;
      }
    };

    for (const marker of S.pinEls) {
      const point = marker.point;
      const surface = Array.isArray(point.rectSurfaces)
        ? point.rectSurfaces[marker.rectIndex]
        : null;
      if (surface) {
        const target = uniqueElement(surface.targetSelector);
        if (target) {
          addTargetChain(target);
        } else {
          unresolvedSurface = true;
          const fallbackContexts = Array.isArray(point.rectContexts) &&
            Array.isArray(point.rectContexts[marker.rectIndex])
            ? point.rectContexts[marker.rectIndex]
            : (point.context || []);
          for (const context of fallbackContexts) {
            const fallbackTarget = uniqueElement(context?.selector);
            if (!fallbackTarget) continue;
            addTargetChain(fallbackTarget);
            break;
          }
        }
        for (const signal of (surface.stateChain || [])) {
          addTrackedNode(uniqueElement(signal.selector));
        }
      } else {
        const contexts = Array.isArray(point.rectContexts) &&
          Array.isArray(point.rectContexts[marker.rectIndex])
          ? point.rectContexts[marker.rectIndex]
          : (point.context || []);
        let target = null;
        for (const context of contexts) {
          if (!context?.selector) continue;
          const contextTarget = uniqueElement(context.selector);
          if (!contextTarget) {
            unresolvedSurface = true;
            continue;
          }
          target = contextTarget;
          break;
        }
        if (target) addTargetChain(target);
      }
    }

    if (unresolvedSurface) {
      for (const node of connectedTrackedNodes) addTrackedNode(node);
      for (const node of [...surfaceRemountParents]) {
        if (!node.isConnected) surfaceRemountParents.delete(node);
      }
    } else {
      surfaceRemountParents.clear();
    }

    if (!surfaceObserver) return;
    for (const node of trackedSurfaceNodes) {
      surfaceObserver.observe(node, { attributes: true });
    }
  }
  document.addEventListener('wheel', notePinMotion, { capture: true, passive: true });
  document.addEventListener('touchmove', notePinMotion, { capture: true, passive: true });
  document.addEventListener('scroll', notePinMotion, { capture: true, passive: true });
  window.addEventListener('scroll', notePinMotion, { passive: true });
  window.addEventListener('resize', () => { renderPins(); notePinMotion(); });
  if (window.visualViewport) {
    window.visualViewport.addEventListener('scroll', notePinMotion, { passive: true });
    window.visualViewport.addEventListener('resize', notePinMotion, { passive: true });
  }
  document.addEventListener('transitionend', (event) => {
    if (nodeAffectsTrackedSurface(event.target)) notePinMotion();
  }, true);
  if (typeof MutationObserver === 'function') {
    surfaceObserver = new MutationObserver((records) => {
      if (records.some((record) => surfaceMutationRelevant(
        record.attributeName || '', trackedSurfaceNodes.has(record.target)
      ))) notePinMotion();
    });
  }

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
  const SURFACE_STATE_CLASS = /^(?:active|inactive|current|selected|open(?:ed)?|closed|visible|hidden|shown|expanded|collapsed|in-view|is-(?:active|inactive|current|selected|open|closed|visible|hidden|shown|off|on)|has-(?:active|current|selection|open))$/i;
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
    // (elements hung off <html> - fixed headers, portal roots - are not under
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
    return elm === host || elm && elm.getRootNode && elm.getRootNode() === root ||
      elm && elm.classList && elm.classList.contains('abc-switch');
  }
  function underlayElementsFromPoint(x, y) {
    const value = host.style.getPropertyValue('visibility');
    const priority = host.style.getPropertyPriority('visibility');
    host.style.setProperty('visibility', 'hidden', 'important');
    try {
      return [...document.elementsFromPoint(x, y)];
    } catch (e) {
      return [];
    } finally {
      if (value) host.style.setProperty('visibility', value, priority);
      else host.style.removeProperty('visibility');
    }
  }

  function uniqueElement(selector) {
    if (!selector) return null;
    try {
      const matches = document.querySelectorAll(selector);
      return matches.length === 1 ? matches[0] : null;
    } catch (e) { return null; }
  }

  function surfaceStateSignal(node) {
    if (!node || !node.attributes) return null;
    const attrs = {};
    for (const attr of node.attributes) {
      if (!viewportStateAttributeName(attr.name)) continue;
      attrs[attr.name] = String(attr.value).slice(0, 256);
      if (Object.keys(attrs).length >= 8) break;
    }
    const classes = node.classList
      ? [...node.classList].filter((token) =>
        token.length <= 128 && SURFACE_STATE_CLASS.test(token)
      ).sort().slice(0, 8)
      : [];
    if (!Object.keys(attrs).length && !classes.length) return null;
    const selector = buildSelector(node);
    return selector ? { selector, attrs, classes } : null;
  }

  function captureRectSurface(contexts) {
    const candidates = [];
    const seenTargets = new Set();
    for (const context of (Array.isArray(contexts) ? contexts : []).slice(0, 12)) {
      const target = uniqueElement(context?.selector);
      if (!target || seenTargets.has(target)) continue;
      seenTargets.add(target);
      const targetSelector = buildSelector(target);
      if (!targetSelector) continue;
      const anchor = elementViewportAnchorInfo(target);
      const stateChain = [];
      for (let current = target; current; current = current.parentElement) {
        const signal = surfaceStateSignal(current);
        if (signal) stateChain.push(signal);
        if (!anchor || current === anchor.node || stateChain.length >= 8) break;
      }
      const anchorSelector = anchor ? buildSelector(anchor.node) : null;
      candidates.push({
        targetSelector,
        anchor: anchor && anchorSelector ? { selector: anchorSelector, mode: anchor.mode } : null,
        stateChain,
      });
    }
    const selected = preferredSurfaceCandidate(candidates);
    if (!selected) return null;
    return {
      targetSelector: selected.targetSelector,
      anchor: selected.anchor,
      stateChain: selected.stateChain,
      scroll: { x: Math.round(scrollX), y: Math.round(scrollY) },
    };
  }

  function captureDraftRectMetadata(draft, rectIndex, rect) {
    const contexts = captureContext(rect);
    draft.rectContexts = Array.isArray(draft.rectContexts) ? draft.rectContexts : [];
    draft.rectSurfaces = Array.isArray(draft.rectSurfaces) ? draft.rectSurfaces : [];
    draft.rectMetadataRects = Array.isArray(draft.rectMetadataRects)
      ? draft.rectMetadataRects
      : [];
    draft.rectContexts[rectIndex] = contexts;
    draft.rectSurfaces[rectIndex] = captureRectSurface(contexts);
    draft.rectMetadataRects[rectIndex] = { ...rect };
    if (Array.isArray(draft.rectPersistedRects)) draft.rectPersistedRects[rectIndex] = null;
  }

  function surfaceSignalMatches(signal) {
    const node = signal && uniqueElement(signal.selector);
    if (!node) return false;
    return surfaceStateValuesMatch(
      signal,
      (name) => node.getAttribute(name),
      (token) => node.classList.contains(token)
    );
  }

  function effectivelyVisible(node) {
    if (!node || !node.isConnected || node.getClientRects().length === 0) return false;
    const styles = [];
    try {
      for (let current = node; current && current !== document; current = current.parentElement) {
        styles.push(getComputedStyle(current));
      }
    } catch (e) { return false; }
    return effectiveStyleChainVisible(styles);
  }

  function unresolvedSurfaceActive(point, surface) {
    const capturedScrollY = Number(surface?.scroll?.y ?? point.scroll?.y);
    const persistedMode = surface?.anchor?.mode || point.anchor || 'doc';
    const fallbackMode = ['fixed', 'sticky', 'viewport'].includes(persistedMode)
      ? 'sticky'
      : 'doc';
    return visualSurfaceActive(
      true, false, false, fallbackMode, S.reviewing,
      scrollY, capturedScrollY, innerHeight
    );
  }

  function rectSurfaceActive(point, rectIndex) {
    const surface = Array.isArray(point.rectSurfaces) ? point.rectSurfaces[rectIndex] : null;
    if (surface) {
      const target = uniqueElement(surface.targetSelector);
      if (!target) return unresolvedSurfaceActive(point, surface);
      const visible = effectivelyVisible(target);
      const stateChain = Array.isArray(surface.stateChain) ? surface.stateChain : [];
      const hasStateSignals = stateChain.length > 0;
      const stateMatches = hasStateSignals && stateChain.every(surfaceSignalMatches);
      const anchorMode = surface.anchor?.mode || elementViewportAnchorMode(target) || 'doc';
      const capturedScrollY = Number(surface.scroll?.y ?? point.scroll?.y);
      // The agent may intentionally rename a state while implementing the
      // point. At the exact captured scroll moment, review trusts the live
      // AFTER/BEFORE surface instead of hiding a correct changed result.
      return visualSurfaceActive(
        visible, hasStateSignals, stateMatches, anchorMode, S.reviewing,
        scrollY, capturedScrollY, innerHeight
      );
    }

    // Backward compatibility for points created before rectSurfaces: the
    // primary captured node is enough to catch the common case where a sticky
    // screen remains laid out but an ancestor has faded its branch to zero.
    const contexts = Array.isArray(point.rectContexts) && Array.isArray(point.rectContexts[rectIndex])
      ? point.rectContexts[rectIndex]
      : (point.context || []);
    const selector = contexts[0]?.selector;
    if (!selector) return unresolvedSurfaceActive(point, null);
    const target = uniqueElement(selector);
    if (!target) return unresolvedSurfaceActive(point, null);
    const anchorMode = elementViewportAnchorMode(target) ||
      (point.anchor === 'viewport' ? 'fixed' : 'doc');
    return visualSurfaceActive(
      effectivelyVisible(target), false, false, anchorMode, S.reviewing,
      scrollY, Number(point.scroll?.y), innerHeight
    );
  }
  const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'LINK', 'META', 'NOSCRIPT', 'TEMPLATE', 'HTML', 'BODY']);

  // rectDoc = {x,y,w,h} in document coords → context[] (≤12, ranked by overlap)
  function captureContext(rectDoc) {
    const vx = rectDoc.x - scrollX, vy = rectDoc.y - scrollY;
    const viewport = { w: innerWidth, h: innerHeight };
    const rankedElements = new Map();
    function consider(elm) {
      if (!elm || rankedElements.has(elm) || SKIP_TAGS.has(elm.tagName) || isOverlayNode(elm)) return;
      const b = elm.getBoundingClientRect();
      const box = { x: b.left + scrollX, y: b.top + scrollY, w: b.width, h: b.height };
      const score = anchorContextScore(rectDoc, box, viewport);
      if (score > 0) rankedElements.set(elm, { box, score, order: rankedElements.size });
    }
    // 1) elementsFromPoint over a 3x3 grid. The overlay is temporarily hidden
    //    because its armed draw surface otherwise masks the page under the mark.
    for (const fx of [0.12, 0.5, 0.88]) {
      for (const fy of [0.12, 0.5, 0.88]) {
        const px = vx + rectDoc.w * fx, py = vy + rectDoc.h * fy;
        if (px < 0 || py < 0 || px >= innerWidth || py >= innerHeight) continue;
        for (const elm of underlayElementsFromPoint(px, py)) consider(elm);
      }
    }
    // 2) bbox scan fallback - the rect may sit partly outside the viewport
    //    where elementsFromPoint can't see.
    if (rankedElements.size === 0 && document.body) {
      for (const elm of document.body.querySelectorAll('*')) consider(elm);
    }
    const ranked = [...rankedElements.entries()].sort((left, right) =>
      right[1].score - left[1].score || left[1].order - right[1].order
    ).slice(0, 12);
    const contexts = ranked.map(([elm, geometry]) => {
      return {
        selector: buildSelector(elm),
        tag: elm.tagName.toLowerCase(),
        text: (elm.innerText || elm.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 120),
        box: {
          x: Math.round(geometry.box.x), y: Math.round(geometry.box.y),
          w: Math.round(geometry.box.w), h: Math.round(geometry.box.h),
        },
        role: 'intersecting',
      };
    }).filter((context) => context.selector);
    if (contexts.length) contexts[0].role = 'primary';
    return contexts;
  }

  function captureUiState() {
    const surfaces = [];
    for (const dialog of document.querySelectorAll('dialog[open]')) {
      const selector = buildSelector(dialog);
      if (selector) surfaces.push({ kind: 'dialog', selector });
    }
    try {
      for (const popover of document.querySelectorAll(':popover-open')) {
        if (popover === host) continue;
        const selector = buildSelector(popover);
        if (selector) surfaces.push({ kind: 'popover', selector });
      }
    } catch (e) { /* :popover-open is unavailable in older browsers */ }
    return surfaces.length ? { surfaces } : null;
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
  // page's own bottom-left button for THAT scope is a confusing duplicate - park
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
    } catch (e) { /* exotic scope id - leave the page's button alone */ }
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
  // The number is a local proposal. Simultaneous tabs can propose the same
  // value, so readQueuedPointState deterministically resolves collisions before
  // display or send. Active server point numbers are always reserved.
  function nextNumber() {
    let n = 0;
    for (const number of reservedPointNumbers()) n = Math.max(n, number);
    for (const p of S.points) n = Math.max(n, p.number || 0);
    return n + 1;
  }

  function elementViewportAnchorInfo(node) {
    let sticky = null;
    try {
      for (let current = node;
        current && current !== document.body && current !== document.documentElement;
        current = current.parentElement) {
        if (isOverlayNode(current)) continue;
        const position = getComputedStyle(current).position;
        if (position === 'fixed') return { mode: 'fixed', node: current };
        if (position === 'sticky' && !sticky) sticky = current;
      }
    } catch (e) { /* detached or cross-realm node */ }
    return sticky ? { mode: 'sticky', node: sticky } : null;
  }
  function elementViewportAnchorMode(node) {
    return elementViewportAnchorInfo(node)?.mode || null;
  }
  function elementViewportAnchored(node) {
    return elementViewportAnchorMode(node) !== null;
  }

  // A rect drawn over a position:fixed/sticky ancestor is anchored to the viewport,
  // not the document. Probe through the armed draw layer so the page element, not
  // the overlay host, determines the anchor.
  function detectAnchor(vx, vy) {
    for (const node of underlayElementsFromPoint(vx, vy)) {
      if (!isOverlayNode(node) && elementViewportAnchored(node)) return 'viewport';
    }
    return 'doc';
  }
  const MIN_ANCHOR_FIT = 0.08;

  function correctedPointRects(p) {
    const rects = p.rects || [p.rect];
    const capturedScroll = p.scroll || { x: 0, y: 0 };
    const capturedViewport = {
      w: Number(p.viewport && p.viewport.w) || innerWidth,
      h: Number(p.viewport && p.viewport.h) || innerHeight,
    };
    const resolvedContexts = rects.map((rect, rectIndex) => {
      const contexts = Array.isArray(p.rectContexts) && Array.isArray(p.rectContexts[rectIndex])
        ? p.rectContexts[rectIndex]
        : (p.context || []);
      const resolved = [];
      for (const context of contexts) {
        if (!context || !context.selector || !context.box ||
          anchorContextScore(rect, context.box, capturedViewport) < MIN_ANCHOR_FIT) continue;
        try {
          const matches = document.querySelectorAll(context.selector);
          if (matches.length === 1) resolved.push({
            context,
            node: matches[0],
            viewportAnchorMode: elementViewportAnchorMode(matches[0]),
          });
        } catch (e) { /* try the next captured context */ }
      }
      return resolved;
    });
    // Older points could be saved as document anchored because the overlay
    // masked a sticky target during capture. Recover them from their trusted,
    // unique live context instead of preserving the bad classification.
    const anchorModes = rects.map((_rect, rectIndex) => {
      const liveModes = resolvedContexts[rectIndex]
        .map((entry) => entry.viewportAnchorMode)
        .filter(Boolean);
      const capturedSurface = Array.isArray(p.rectSurfaces) ? p.rectSurfaces[rectIndex] : null;
      const capturedMode = capturedSurface?.anchor?.mode || null;
      return pointAnchorMode(
        liveModes,
        capturedMode ? [capturedMode] : [],
        rectUsesLegacyViewportAnchor(p.anchor, capturedSurface)
      );
    });
    const fixedByRect = anchorModes.map((mode) => mode !== 'doc');
    return {
      fixedByRect,
      anchorModes,
      active: rects.map((_rect, rectIndex) => rectSurfaceActive(p, rectIndex)),
      boxes: rects.map((rect, rectIndex) => {
        const surface = Array.isArray(p.rectSurfaces) ? p.rectSurfaces[rectIndex] : null;
        const rectCapturedScroll = surface?.scroll || capturedScroll;
        const fixed = fixedByRect[rectIndex];
        const anchorMode = anchorModes[rectIndex];
        for (const entry of resolvedContexts[rectIndex]) {
          const context = entry.context;
          try {
            if (fixed && anchorMode !== 'unknown' && !entry.viewportAnchorMode) continue;
            const live = entry.node.getBoundingClientRect();
            const capturedBox = fixed ? {
              x: context.box.x - rectCapturedScroll.x,
              y: context.box.y - rectCapturedScroll.y,
              w: context.box.w,
              h: context.box.h,
            } : context.box;
            const sourceRect = fixed ? {
              x: rect.x - rectCapturedScroll.x,
              y: rect.y - rectCapturedScroll.y,
              w: rect.w,
              h: rect.h,
            } : rect;
            const liveBox = fixed ? {
              x: live.left, y: live.top, w: live.width, h: live.height,
            } : {
              x: live.left + scrollX, y: live.top + scrollY,
              w: live.width, h: live.height,
            };
            return reanchorRect(sourceRect, capturedBox, liveBox);
          } catch (e) { /* try the next resolved context */ }
        }
        return fixed ? {
          x: rect.x - rectCapturedScroll.x,
          y: rect.y - rectCapturedScroll.y,
          w: rect.w,
          h: rect.h,
        } : { ...rect };
      }),
    };
  }

  // → {box, fixed:true} in viewport coords for viewport-anchored points, else null
  function pinBox(p) {
    const geometry = correctedPointRects(p);
    if (!geometry.fixedByRect[0]) return null;
    return {
      box: geometry.boxes[0], fixed: true, anchorMode: geometry.anchorModes[0],
      active: geometry.active[0] !== false,
    };
  }
  function pointFromDraft(draft) {
    const existing = draft.editPoint ||
      (draft.editId ? S.points.find((p) => p.id === draft.editId) : null);
    const rects = draft.rects || [draft.rect];
    const storedRects = [];
    const rectContexts = [];
    const rectSurfaces = [];
    rects.forEach((rect, rectIndex) => {
      const canReuse = sameRectGeometry(rect, draft.rectMetadataRects?.[rectIndex]) &&
        Array.isArray(draft.rectContexts?.[rectIndex]) &&
        Array.isArray(draft.rectSurfaces) && rectIndex < draft.rectSurfaces.length;
      storedRects.push(canReuse
        ? persistedRectForDraft(
          rect, draft.rectMetadataRects[rectIndex], draft.rectPersistedRects?.[rectIndex]
        )
        : rect
      );
      if (canReuse) {
        rectContexts.push(draft.rectContexts[rectIndex]);
        rectSurfaces.push(draft.rectSurfaces[rectIndex] ?? null);
        return;
      }
      const contexts = captureContext(rect);
      rectContexts.push(contexts);
      rectSurfaces.push(captureRectSurface(contexts));
    });
    const activeRectIndex = primaryRectIndex(rects, draft.rect);
    const viewport = draft.viewport || existing?.viewport ||
      { w: innerWidth, h: innerHeight, dpr: window.devicePixelRatio || 1 };
    const capturedScroll = draft.scroll || existing?.scroll ||
      { x: Math.round(scrollX), y: Math.round(scrollY) };
    return {
      id: existing ? existing.id : newPointId(),
      number: existing ? existing.number : nextNumber(),
      page: logicalPath(),
      createdAt: existing ? existing.createdAt : nowISO(),
      rect: {
        x: Math.round(storedRects[activeRectIndex].x),
        y: Math.round(storedRects[activeRectIndex].y),
        w: Math.round(storedRects[activeRectIndex].w),
        h: Math.round(storedRects[activeRectIndex].h),
      },
      rects: storedRects.map((rect) => ({
        x: Math.round(rect.x), y: Math.round(rect.y),
        w: Math.round(rect.w), h: Math.round(rect.h),
      })),
      rectContexts,
      rectSurfaces,
      viewport: { ...viewport },
      scroll: { ...capturedScroll },
      anchor: existing ? (existing.anchor || 'doc') : (draft.anchor || 'doc'),
      context: rectContexts[activeRectIndex] || captureContext(draft.rect),
      uiState: draft.uiState !== undefined
        ? draft.uiState
        : (existing?.uiState !== undefined ? existing.uiState : captureUiState()),
      abcState: draft.abcState !== undefined
        ? draft.abcState
        : (existing?.abcState !== undefined ? existing.abcState : snapshotAbc()),
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
  // pointer-events:none layer and resolve their live target geometry on every
  // coalesced positioning frame.
  function renderPins() {
    pinLayer.textContent = '';
    S.pinEls = [];
    S.curPinEls = null;   // never flash a node that just got detached
    const page = logicalPath();
    const addPin = (point, rectIndex, cls, onClick, title) => {
      const rect = el('div', 'wk-pin-rect ' + cls);
      rect.style.visibility = 'hidden';
      const pin = el('button', 'wk-pin ' + cls, String(point.number));
      pin.style.visibility = 'hidden';
      pin.type = 'button';
      pin.title = title || '';
      pin.setAttribute('aria-label', title || ('Feedback point ' + point.number));
      pin.addEventListener('click', onClick);
      pinLayer.appendChild(rect);
      pinLayer.appendChild(pin);
      S.pinEls.push({ node: rect, point, rectIndex, kind: 'rect' });
      S.pinEls.push({ node: pin, point, rectIndex, kind: 'pin' });
      return { rect, pin };
    };
    if (S.reviewing) {
      S.reviewList.forEach((p, i) => {
        if (p.page !== page) return;
        const v = S.verdicts[p.id];
        (p.rects || [p.rect]).forEach((_box, rectIndex) => {
          const els = addPin(p, rectIndex,
            'review' + (v ? ' verdicted v-' + v.verdict : '') + (i === S.cursor ? ' current' : ''),
            () => jumpTo(i), 'point ' + p.number + (v ? ' - ' + v.verdict : ''));
          if (i === S.cursor && rectIndex === 0) { S.curPinEls = els; }
        });
      });
    }
    const batchPoints = S.batch && Array.isArray(S.batch.points) ? S.batch.points : [];
    const submitted = S.phase !== null && S.phase !== 'collecting'
      ? pendingReviewPoints(
        batchPoints, S.reviewList, S.submittedPoints, [], S.pendingPointIds
      )
      : [];
    const submittedIds = new Set(submitted.map((point) => point.id));
    for (const p of submitted) {
      if (p.page !== page) continue;
      (p.rects || [p.rect]).forEach((_box, rectIndex) => addPin(p, rectIndex, 'submitted', () => {
        if (IS_BEFORE || S.card) return;
        openCard({ pendingPoint: p });
      }, 'added point ' + p.number + ' is saved and waiting - click to edit'));
    }
    for (const p of S.points) {
      if (submittedIds.has(p.id) || p.page !== page) continue;
      (p.rects || [p.rect]).forEach((_box, rectIndex) => addPin(p, rectIndex, 'queued', () => {
        if (IS_BEFORE || S.card) return;
        openCard({ editId: p.id });
      }, S.reviewing ? 'queued point ' + p.number + ' is saved and waiting - click to edit' : 'click to edit'));
    }
    refreshSurfaceObservation();
    repositionAll();
    updateHint();
  }

  // Anchor correction follows only a close-fitting captured element. Giant
  // backgrounds and page shells are intentionally ignored, leaving the exact
  // document coordinates the user drew instead of introducing false drift.
  function correctedRect(p) {
    return correctedPointRects(p).boxes[0];
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
      const rect = { x: d.x + scrollX, y: d.y + scrollY, w: d.w, h: d.h };
      const rectContext = captureContext(rect);
      const rectSurface = captureRectSurface(rectContext);
      if (S.addRectDraft) {
        const draft = S.addRectDraft;
        S.addRectDraft = null;
        const previousRects = draft.rects || [draft.rect];
        const source = draft.editPoint ||
          (draft.editId ? S.points.find((point) => point.id === draft.editId) : null);
        if (!Array.isArray(draft.rectContexts)) {
          draft.rectContexts = previousRects.map((_box, index) =>
            source?.rectContexts?.[index] || (index === 0 ? source?.context || [] : [])
          );
        }
        if (!Array.isArray(draft.rectSurfaces)) {
          draft.rectSurfaces = previousRects.map((_box, index) =>
            source?.rectSurfaces?.[index] ?? null
          );
        }
        if (!Array.isArray(draft.rectMetadataRects)) {
          draft.rectMetadataRects = previousRects.map((box) => ({ ...box }));
        }
        if (!Array.isArray(draft.rectPersistedRects)) {
          const sourceRects = source ? (source.rects || [source.rect]) : [];
          draft.rectPersistedRects = previousRects.map((_box, index) =>
            sourceRects[index] ? { ...sourceRects[index] } : null
          );
        }
        draft.rects = previousRects.concat([rect]);
        draft.rect = rect;
        const rectIndex = draft.rects.length - 1;
        draft.rectContexts[rectIndex] = rectContext;
        draft.rectSurfaces[rectIndex] = rectSurface;
        draft.rectMetadataRects[rectIndex] = { ...rect };
        draft.rectPersistedRects[rectIndex] = null;
        openCard({ draft, editId: draft.editId });
      } else {
        openCard({
          rect,
          anchor,
          rectContext,
          rectSurface,
          viewport: { w: innerWidth, h: innerHeight, dpr: window.devicePixelRatio || 1 },
          scroll: { x: Math.round(scrollX), y: Math.round(scrollY) },
          uiState: captureUiState(),
          abcState: snapshotAbc(),
        });
      }
    });
    drawLayer.addEventListener('pointercancel', () => { if (S.drag?.kind === 'rubber') S.drag.cancel(); });
  }

  // ===== frozen rect (resize handles + move) =================================
  const HANDLES = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];
  let frozenEls = [];

  function clearFrozen() {
    for (const node of frozenEls) node.remove();
    frozenEls = [];
  }

  function buildFrozen(draftOverride) {
    clearFrozen();
    const activeDraft = draftOverride || S.card?.draft;
    if (!activeDraft) return;
    const rects = activeDraft.rects || [activeDraft.rect];
    rects.forEach((rect, index) => {
      const frozen = el('div', 'wk-frozen');
      const body = el('div', 'wk-frozen-body');
      body.dataset.h = 'move';
      frozen.appendChild(body);
      const remove = el('button', 'wk-frozen-delete');
      remove.type = 'button';
      remove.title = 'Delete this rectangle';
      remove.setAttribute('aria-label', 'Delete this rectangle');
      remove.appendChild(closeIcon());
      remove.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        if (!S.card) return;
        if (S.card.draft.rects.length === 1) { S.card.cancel(); return; }
        S.card.draft.rects.splice(index, 1);
        if (Array.isArray(S.card.draft.rectContexts)) S.card.draft.rectContexts.splice(index, 1);
        if (Array.isArray(S.card.draft.rectSurfaces)) S.card.draft.rectSurfaces.splice(index, 1);
        if (Array.isArray(S.card.draft.rectMetadataRects)) {
          S.card.draft.rectMetadataRects.splice(index, 1);
        }
        if (Array.isArray(S.card.draft.rectPersistedRects)) {
          S.card.draft.rectPersistedRects.splice(index, 1);
        }
        S.card.draft.rect = S.card.draft.rects[S.card.draft.rects.length - 1];
        buildFrozen(); positionFrozen(); positionCard(); S.card.saveDraft();
      });
      frozen.appendChild(remove);
      for (const h of HANDLES) {
        const hd = el('div', 'wk-handle h-' + h);
        hd.dataset.h = h;
        frozen.appendChild(hd);
      }
      frozen.addEventListener('pointerdown', (e) => {
        const h = e.target.dataset && e.target.dataset.h;
        if (!h || e.button !== 0 || !S.card || S.drag) return;
        e.preventDefault();
        e.target.setPointerCapture(e.pointerId);
        const r0 = { ...S.card.draft.rects[index] };
        S.card.draft.rect = S.card.draft.rects[index];
        S.drag = {
          kind: 'frozen', h, index, sx: e.clientX, sy: e.clientY, r0, target: e.target,
          cancel() { S.card.draft.rects[index] = r0; S.card.draft.rect = r0; S.drag = null; positionFrozen(); positionCard(); },
        };
      });
      frozen.addEventListener('pointermove', (e) => {
        const d = S.drag;
        if (!d || d.kind !== 'frozen' || d.index !== index || !S.card) return;
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
        S.card.draft.rects[index] = { x, y, w, h };
        S.card.draft.rect = S.card.draft.rects[index];
        positionFrozen(); positionCard(); S.card.saveDraft();
      });
      frozen.addEventListener('pointerup', () => {
        if (S.drag?.kind !== 'frozen' || S.drag.index !== index || !S.card) return;
        if (!sameRectGeometry(S.card.draft.rects[index], S.drag.r0)) {
          captureDraftRectMetadata(S.card.draft, index, S.card.draft.rects[index]);
        }
        S.drag = null;
        S.card.saveDraft();
      });
      frozen.addEventListener('pointercancel', () => { if (S.drag?.kind === 'frozen') S.drag.cancel(); });
      frozenEls.push(frozen);
      wrap.appendChild(frozen);
    });
  }
  function positionFrozen() {
    if (!S.card) return;
    frozenEls.forEach((node, index) => {
      const r = S.card.draft.rects[index];
      if (!r) return;
      place(node, r);
      node.style.width = r.w + 'px';
      node.style.height = r.h + 'px';
    });
  }

  // ===== speech (webkitSpeechRecognition) ====================================
  // One factory reused by the editor card and the redo mini-input. Chrome-only;
  // feature-gated so other browsers simply don't get a mic button.
  const SRClass = window.webkitSpeechRecognition || window.SpeechRecognition;
  // Chrome permits exactly one live recognition. Two (editor card + redo mini)
  // would abort each other, and each aborted onend restarts 250ms later - an
  // endless ping-pong where neither transcribes. This registry guarantees one.
  let liveMic = null;

  function makeMic(ta, btn, langSelect, onText) {
    if (!SRClass) {
      btn.hidden = true;
      langSelect.hidden = true;
      return {
        stop() {}, arm() {}, finishAndWait() { return Promise.resolve(true); },
        get on() { return false; }, get starting() { return false; },
        get stopping() { return false; }, get uploading() { return false; },
      };
    }
    let rec = null, userOn = false, netFails = 0, restartT = 0, interim = '';
    let languageRestart = false;
    let finishCycle = null;

    function paint() {
      btn.classList.toggle('on', userOn);
      btn.classList.remove('armed');
      btn.setAttribute('aria-pressed', String(userOn));
    }
    function settleFinish(activeRec, ready) {
      if (!finishCycle || finishCycle.rec !== activeRec) return;
      const cycle = finishCycle;
      finishCycle = null;
      clearTimeout(cycle.timer);
      if (rec === activeRec) rec = null;
      interim = '';
      onText && onText('');
      if (liveMic === self) liveMic = null;
      cycle.resolve(ready);
    }
    function failSynchronousStart(error, activeRec) {
      userOn = false;
      languageRestart = false;
      clearTimeout(restartT);
      if (!activeRec || rec === activeRec) rec = null;
      if (activeRec) settleFinish(activeRec, false);
      interim = '';
      onText && onText('');
      if (liveMic === self) liveMic = null;
      paint();
      btn.classList.add('error');
      btn.title = 'Dictation could not start. Click to retry.';
      toast('Could not start dictation: ' + (error?.message || 'browser rejected the request') + '. Click the mic to retry.',
        { kind: 'error' });
    }
    function start() {
      if (!userOn || finishCycle) return;
      let activeRec;
      try {
        activeRec = new SRClass();
        rec = activeRec;
      } catch (error) {
        failSynchronousStart(error);
        return;
      }
      activeRec.continuous = true;
      activeRec.interimResults = true;
      // The Web Speech API sends this BCP 47 tag to the recognition service
      // when the request starts. This is what makes the UI selector functional.
      activeRec.lang = langSelect.value;
      activeRec.onresult = (e) => {
        if (rec !== activeRec && (!finishCycle || finishCycle.rec !== activeRec)) return;
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
      activeRec.onerror = (e) => {
        const finishing = finishCycle && finishCycle.rec === activeRec;
        if (rec !== activeRec && !finishing) return;
        if (finishing && (e.error === 'aborted' || e.error === 'no-speech')) return;
        if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
          userOn = false;
          paint();                // drop the red 'on' pulse - dictation is dead
          btn.classList.add('error');
          btn.title = 'Microphone blocked - allow the mic for this site in Chrome, then click again';
        } else if (e.error === 'network') {
          netFails++;
          btn.classList.add('error');
          btn.title = 'Speech service network error (' + netFails + '/3)';
          if (netFails >= 3) {
            userOn = false;
            paint();
            btn.classList.add('error');
            btn.title = 'Speech service unreachable - dictation stopped after 3 network errors';
          }
        } else if (e.error !== 'no-speech' && !(languageRestart && e.error === 'aborted')) {
          // audio-capture / language-not-supported / 'aborted' are terminal: make
          // them stop userOn so onend's 250ms restart loop ends (and the two-mic
          // ping-pong breaks - a preempted recognition lands here and must not
          // resurrect itself). 'no-speech' stays routine; onend re-arms it.
          userOn = false;
          paint();
          btn.classList.add('error');
          btn.title = 'Dictation stopped (' + e.error + ') - click to retry';
        }
      };
      activeRec.onend = () => {
        const wasCurrent = rec === activeRec;
        if (wasCurrent) rec = null;
        interim = '';
        onText && onText('');
        if (finishCycle && finishCycle.rec === activeRec) {
          settleFinish(activeRec, true);
          return;
        }
        if (!wasCurrent) return;
        if (languageRestart) {
          languageRestart = false;
          if (userOn) restartT = setTimeout(start, 0);
          return;
        }
        // Chrome ends recognition on every silence - quietly re-arm unless the
        // user toggled off or errors made restarting pointless.
        if (userOn) restartT = setTimeout(start, 250);
        else if (liveMic === self) liveMic = null;
      };
      try {
        activeRec.start();
      } catch (error) {
        failSynchronousStart(error, activeRec);
      }
    }
    function stop() {
      userOn = false;
      languageRestart = false;
      clearTimeout(restartT);
      const activeRec = rec;
      rec = null;
      try { activeRec && activeRec.stop(); } catch (e) { /* ok */ }
      if (activeRec) settleFinish(activeRec, false);
      interim = '';
      onText && onText('');
      if (liveMic === self) liveMic = null;
      paint();
    }
    function finishAndWait() {
      if (finishCycle) return finishCycle.promise;
      userOn = false;
      languageRestart = false;
      clearTimeout(restartT);
      paint();
      const activeRec = rec;
      if (!activeRec) {
        if (liveMic === self) liveMic = null;
        return Promise.resolve(true);
      }
      let resolveCycle;
      const promise = new Promise((resolve) => { resolveCycle = resolve; });
      finishCycle = { rec: activeRec, promise, resolve: resolveCycle, timer: 0 };
      // Final SpeechRecognition results are delivered before onend. Keep the
      // exact instance alive until then so Done cannot snapshot stale text.
      finishCycle.timer = setTimeout(() => settleFinish(activeRec, true), 3000);
      try {
        activeRec.stop();
      } catch (error) {
        settleFinish(activeRec, true);
      }
      return promise;
    }
    btn.addEventListener('click', () => {
      if (userOn) { finishAndWait(); return; }
      if (finishCycle) return;
      if (liveMic && liveMic !== self) liveMic.stop();   // one live recognition at a time
      liveMic = self;
      userOn = true;
      netFails = 0;
      btn.classList.remove('error');
      btn.title = 'Dictating - click to stop';
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
      finishAndWait,
      // after a reload we can't auto-start (browser gesture rule) - show the
      // armed look so the user knows one click resumes dictation
      arm() {
        if (!userOn) {
          btn.classList.add('armed');
          btn.title = 'Click (or press ' + DICTATE_LABEL + ' in an empty note) to resume dictation';
        }
      },
      get on() { return userOn; },
      get starting() { return false; },
      get stopping() { return !!finishCycle; },
      get uploading() { return false; },
    };
    return self;
  }

  // Voice-note mode deliberately avoids browser speech recognition. It keeps
  // the original audio, uploads it into this color's private feedback inbox,
  // and lets the background agent run the bundled local Whisper helper.
  const VOICE_MAX_BYTES = 24 * 1024 * 1024;
  const VOICE_MAX_DURATION_MS = 5 * 60 * 1000;
  function makeVoiceRecorder(btn, langSelect, onSaved, onState) {
    if (!window.MediaRecorder || !navigator.mediaDevices?.getUserMedia) {
      btn.hidden = true;
      langSelect.hidden = true;
      return {
        stop() {}, arm() {}, get on() { return false; },
        get starting() { return false; }, get stopping() { return false; },
        get uploading() { return false; },
      };
    }
    let recorder = null, stream = null, chunks = [], startedAt = 0;
    let recording = false, starting = false, stopping = false, uploading = false;
    let saveOnStop = false, errorMessage = '';
    let discarded = false, generation = 0;
    let recordedBytes = 0, limitTimer = 0, limitReason = '', limitStop = null;
    const closedStreams = new WeakSet();
    let cycle = { promise: Promise.resolve(true), resolve() {}, settled: true };

    function newCycle() {
      let finishPromise;
      const state = {
        settled: false,
        promise: new Promise((done) => { finishPromise = done; }),
        resolve(value) {
          if (state.settled) return;
          state.settled = true;
          finishPromise(value);
        },
      };
      return state;
    }

    function paint() {
      btn.classList.toggle('on', recording);
      btn.classList.toggle('uploading', uploading);
      btn.classList.toggle('error', !!errorMessage);
      btn.setAttribute('aria-pressed', String(recording));
      btn.disabled = starting || stopping || uploading;
      btn.setAttribute('aria-busy', String(starting || stopping || uploading));
      if (recording) btn.title = 'Recording voice note - click to stop and attach';
      else if (starting) btn.title = 'Waiting for microphone permission…';
      else if (stopping) btn.title = 'Finishing voice note…';
      else if (uploading) btn.title = 'Saving voice note…';
      else if (errorMessage) btn.title = errorMessage;
      else btn.title = MIC_TITLE;
      onState && onState({ recording, starting, stopping, uploading });
    }
    function closeStream(target = stream) {
      if (target && !closedStreams.has(target)) {
        closedStreams.add(target);
        for (const track of target.getTracks?.() || []) track.stop();
      }
      if (target === stream) stream = null;
    }
    function clearLimitTimer() {
      clearTimeout(limitTimer);
      limitTimer = 0;
    }
    async function upload(blob, durationMs, attempt, currentCycle) {
      if (blob.size > VOICE_MAX_BYTES) {
        errorMessage = 'Voice note is too large. Click to record a shorter note.';
        toast('Voice note exceeded the 24 MB upload limit. Record a shorter note.', { kind: 'error' });
        paint();
        currentCycle.resolve(false);
        return;
      }
      uploading = true;
      paint();
      const id = 'voice-' + Date.now().toString(36) + '-' + rand4();
      let saved = false;
      try {
        const response = await fetch('/__wk/voice-note?id=' + encodeURIComponent(id), {
          method: 'POST',
          headers: { 'Content-Type': blob.type || 'audio/webm', 'X-WK-Token': MUTATION_TOKEN },
          body: blob,
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.error || ('HTTP ' + response.status));
        const note = result.voiceNote;
        note.durationMs = durationMs;
        note.language = langSelect.value === 'he-IL' ? 'he' : 'en';
        if (USES_CLOUD_TRANSCRIPTION) note.transcription = 'openai';
        if (discarded || attempt !== generation) {
          deleteVoiceNote(note);
        } else {
          onSaved(note);
          saved = true;
          toast(USES_CLOUD_TRANSCRIPTION
            ? 'Voice note attached. OpenAI will transcribe it for the agent.'
            : 'Voice note attached. The agent will transcribe it locally.');
        }
      } catch (error) {
        if (!discarded && attempt === generation) {
          toast('Voice note failed to save: ' + error.message, { kind: 'error' });
        }
      } finally {
        uploading = false;
        paint();
        currentCycle.resolve(saved);
      }
    }
    async function start() {
      if (recording || starting || stopping || uploading) return cycle.promise;
      if (liveMic && liveMic !== self) liveMic.stop();
      const attempt = ++generation;
      const currentCycle = newCycle();
      cycle = currentCycle;
      discarded = false;
      errorMessage = '';
      recordedBytes = 0;
      limitReason = '';
      clearLimitTimer();
      starting = true;
      liveMic = self;
      paint();
      let acquiredStream = null;
      try {
        acquiredStream = await navigator.mediaDevices.getUserMedia({ audio: true });
        if (attempt !== generation || discarded) {
          closeStream(acquiredStream);
          currentCycle.resolve(false);
          return currentCycle.promise;
        }
        stream = acquiredStream;
        const preferred = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4']
          .find((type) => !MediaRecorder.isTypeSupported || MediaRecorder.isTypeSupported(type));
        const currentRecorder = preferred
          ? new MediaRecorder(stream, { mimeType: preferred })
          : new MediaRecorder(stream);
        recorder = currentRecorder;
        const currentChunks = [];
        chunks = currentChunks;
        saveOnStop = false;
        const failRecording = (message, detail) => {
          clearLimitTimer();
          limitStop = null;
          const ownsRecorder = recorder === currentRecorder;
          if (ownsRecorder) {
            recording = false;
            stopping = false;
            recorder = null;
            if (liveMic === self) liveMic = null;
          }
          closeStream(acquiredStream);
          if (attempt === generation && !discarded && !currentCycle.settled) {
            errorMessage = message;
            toast(detail || message, { kind: 'error' });
          }
          currentCycle.resolve(false);
          paint();
        };
        const stopAtLimit = (message) => {
          if (limitReason || attempt !== generation || discarded || currentCycle.settled) return;
          limitReason = message;
          saveOnStop = false;
          errorMessage = 'Recording limit reached. Click to record a shorter note.';
          toast(message, { kind: 'error' });
          const alreadyStopping = stopping;
          recording = false;
          stopping = true;
          if (liveMic === self) liveMic = null;
          clearLimitTimer();
          paint();
          if (alreadyStopping) return;
          try {
            currentRecorder.stop();
          } catch (error) {
            failRecording(errorMessage, message);
          }
        };
        limitStop = stopAtLimit;
        currentRecorder.ondataavailable = (event) => {
          if (!event.data?.size || limitReason) return;
          if (Date.now() - startedAt >= VOICE_MAX_DURATION_MS) {
            stopAtLimit('Voice note reached the 5 minute recording limit. Record a shorter note.');
            return;
          }
          recordedBytes += event.data.size;
          if (recordedBytes > VOICE_MAX_BYTES) {
            stopAtLimit('Voice note reached the 24 MB recording limit. Record a shorter note.');
            return;
          }
          currentChunks.push(event.data);
        };
        currentRecorder.onerror = (event) => {
          const detail = event?.error?.message || 'unknown recorder error';
          failRecording(
            'Voice recording failed. Click to retry.',
            'Voice recording failed: ' + detail,
          );
        };
        currentRecorder.onstop = async () => {
          clearLimitTimer();
          limitStop = null;
          const shouldSave = saveOnStop && attempt === generation && !discarded;
          const unexpected = !saveOnStop && attempt === generation && !discarded && !currentCycle.settled;
          const stoppedAtLimit = !!limitReason;
          const durationMs = Math.max(0, Date.now() - startedAt);
          const type = currentRecorder.mimeType || currentChunks[0]?.type || 'audio/webm';
          const ownsRecorder = recorder === currentRecorder;
          if (ownsRecorder) {
            recording = false;
            stopping = false;
            recorder = null;
            if (liveMic === self) liveMic = null;
          }
          closeStream(acquiredStream);
          paint();
          if (currentCycle.settled) {
            // The error event already made the failure visible and completed the cycle.
          } else if (stoppedAtLimit) {
            currentCycle.resolve(false);
          } else if (shouldSave && currentChunks.length) {
            await upload(new Blob(currentChunks, { type }), durationMs, attempt, currentCycle);
          } else if (shouldSave) {
            errorMessage = 'No audio was captured. Click to retry.';
            toast('No audio was captured. Please record the voice note again.', { kind: 'error' });
            currentCycle.resolve(false);
            paint();
          } else if (unexpected) {
            errorMessage = 'Recording stopped unexpectedly. Click to retry.';
            toast('Voice recording stopped unexpectedly. Please try again.', { kind: 'error' });
            currentCycle.resolve(false);
            paint();
          } else {
            currentCycle.resolve(false);
          }
          if (chunks === currentChunks) chunks = [];
        };
        startedAt = Date.now();
        currentRecorder.start(250);
        recording = true;
        limitTimer = setTimeout(() => {
          stopAtLimit('Voice note reached the 5 minute recording limit. Record a shorter note.');
        }, VOICE_MAX_DURATION_MS);
      } catch (error) {
        clearLimitTimer();
        limitStop = null;
        closeStream(acquiredStream);
        recording = false;
        stopping = false;
        if (attempt === generation) recorder = null;
        currentCycle.resolve(false);
        if (attempt === generation && !discarded) {
          errorMessage = 'Microphone blocked or unavailable. Click to retry.';
          toast('Could not start voice recording: ' + error.message, { kind: 'error' });
        }
      } finally {
        if (attempt === generation) starting = false;
        if (!recording && liveMic === self) liveMic = null;
        paint();
      }
      return currentCycle.promise;
    }
    function finish() {
      if (stopping) return cycle.promise;
      if (!recording || !recorder) return cycle.promise;
      if (Date.now() - startedAt >= VOICE_MAX_DURATION_MS && limitStop) {
        limitStop('Voice note reached the 5 minute recording limit. Record a shorter note.');
        return cycle.promise;
      }
      const currentRecorder = recorder;
      clearLimitTimer();
      saveOnStop = true;
      recording = false;
      stopping = true;
      if (liveMic === self) liveMic = null;
      paint();
      try {
        currentRecorder.stop();
      } catch (error) {
        limitStop = null;
        stopping = false;
        if (recorder === currentRecorder) recorder = null;
        closeStream();
        errorMessage = 'Could not finish this voice note. Click to retry.';
        toast('Could not finish voice recording: ' + error.message, { kind: 'error' });
        cycle.resolve(false);
        paint();
      }
      return cycle.promise;
    }
    function stop() {
      clearLimitTimer();
      limitStop = null;
      discarded = true;
      generation += 1;
      saveOnStop = false;
      starting = false;
      if (stopping) {
        if (liveMic === self) liveMic = null;
        paint();
        return;
      }
      if (recording && recorder) {
        const currentRecorder = recorder;
        recording = false;
        stopping = true;
        if (liveMic === self) liveMic = null;
        paint();
        try {
          currentRecorder.stop();
        } catch (error) {
          stopping = false;
          if (recorder === currentRecorder) recorder = null;
          closeStream();
          cycle.resolve(false);
        }
      } else {
        closeStream();
        cycle.resolve(false);
      }
      recording = false;
      if (liveMic === self) liveMic = null;
      paint();
    }
    btn.addEventListener('click', () => {
      if (starting || stopping || uploading) return;
      if (recording) finish();
      else start();
    });
    langSelect.addEventListener('change', () => {
      if (!SPEECH_LANGS.has(langSelect.value)) langSelect.value = 'en-US';
      speechLang = langSelect.value;
      LS.set('wk:speechLang', speechLang);
    });
    const self = {
      stop,
      finishAndWait() { return recording ? finish() : cycle.promise; },
      arm() { btn.classList.add('armed'); },
      get on() { return recording; },
      get starting() { return starting; },
      get stopping() { return stopping; },
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
  function openCard(init) {
    if (S.card || IS_BEFORE) return;
    const restoredPending = init.draft && init.draft.editSource === 'pending'
      ? init.draft.editPoint
      : null;
    const pendingEditing = init.pendingPoint || restoredPending || null;
    const queuedEditing = !pendingEditing && init.editId
      ? S.points.find((p) => p.id === init.editId)
      : null;
    const editingSource = pendingEditing || queuedEditing;
    const editingSourceRects = editingSource ? (editingSource.rects || [editingSource.rect]) : null;
    const editingPrimaryIndex = editingSource
      ? primaryRectIndex(editingSourceRects, editingSource.rect)
      : 0;
    const editingGeometry = editingSource ? correctedPointRects(editingSource) : null;
    const editingDisplayRects = editingGeometry
      ? editingGeometry.boxes.map((rect, rectIndex) => editingGeometry.fixedByRect[rectIndex] ? {
        x: rect.x + scrollX,
        y: rect.y + scrollY,
        w: rect.w,
        h: rect.h,
      } : { ...rect })
      : null;
    const editing = editingSource ? {
      ...editingSource,
      rect: { ...editingDisplayRects[editingPrimaryIndex] },
      rects: editingDisplayRects,
    } : null;
    const originalVoiceNote = editing && editing.voiceNote ? editing.voiceNote : null;
    const editingRects = editing ? (editing.rects || [editing.rect]) : null;
    const editingRectContexts = editing && Array.isArray(editing.rectContexts) &&
      editing.rectContexts.length === editingRects.length
      ? editing.rectContexts
      : (editing ? editingRects.map(() => editing.context || []) : null);
    const editingRectSurfaces = editing && Array.isArray(editing.rectSurfaces) &&
      editing.rectSurfaces.length === editingRects.length
      ? editing.rectSurfaces
      : (editing ? editingRects.map(() => null) : null);
    const draft = init.draft || {
      page: logicalPath(),
      rect: editing ? { ...editing.rect } : init.rect,
      rects: editing ? editingRects.map((rect) => ({ ...rect })) : [init.rect],
      rectContexts: editing ? editingRectContexts.slice() : [init.rectContext || []],
      rectSurfaces: editing ? editingRectSurfaces.slice() : [init.rectSurface || null],
      rectMetadataRects: editing
        ? editingRects.map((rect) => ({ ...rect }))
        : [{ ...init.rect }],
      rectPersistedRects: editingSource
        ? editingSourceRects.map((rect) => ({ ...rect }))
        : [null],
      viewport: editing?.viewport || init.viewport ||
        { w: innerWidth, h: innerHeight, dpr: window.devicePixelRatio || 1 },
      scroll: editing?.scroll || init.scroll ||
        { x: Math.round(scrollX), y: Math.round(scrollY) },
      uiState: editing?.uiState !== undefined ? editing.uiState : init.uiState,
      abcState: editing?.abcState !== undefined ? editing.abcState : init.abcState,
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
      editSource: pendingEditing ? 'pending' : (queuedEditing ? 'queued' : null),
      editPoint: pendingEditing ? { ...pendingEditing } : null,
    };
    let cardOwner = null;
    let doneBusy = false;

    const node = el('div', 'wk-card');
    node.setAttribute('role', 'dialog');
    const head = el('div', 'wk-row wk-head');
    const num = el('span', 'wk-num', String(editing ? editing.number : nextNumber()));
    const title = el('span', 'wk-card-title', editing ? 'edit point' : 'feedback');
    title.id = 'wk-card-title-' + rand4();
    node.setAttribute('aria-labelledby', title.id);
    const micBtn = el('button', 'wk-mic');
    micBtn.type = 'button';
    micBtn.title = MIC_TITLE;
    micBtn.setAttribute('aria-label', MIC_ARIA_LABEL);
    micBtn.appendChild(microphoneIcon());
    const langSelect = speechLanguageSelect();
    const voiceStatus = el('span', 'wk-voice-status');
    voiceStatus.hidden = !USES_VOICE_NOTES;
    voiceStatus.setAttribute('role', 'status');
    voiceStatus.setAttribute('aria-live', 'polite');
    head.append(num, title, voiceStatus, langSelect, micBtn);

    const taWrap = el('div', 'wk-ta-wrap');
    const ta = el('textarea', 'wk-ta');
    ta.setAttribute('aria-label', editing ? 'Edit feedback details' : 'Feedback details');
    ta.placeholder = USES_VOICE_NOTES
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
    abcHead.append(
      el('span', 'wk-flask', '⚗'),
      el('span', '', 'Request A/B variants'),
      el('span', 'wk-caret', '▸'),
    );
    const abcBody = el('div', 'wk-abc-body');
    abcBody.id = 'wk-abc-body-' + rand4();
    abcHead.setAttribute('aria-controls', abcBody.id);
    const seg = el('div', 'wk-seg');
    seg.setAttribute('role', 'group');
    seg.setAttribute('aria-label', 'A/B variant authoring mode');
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
    minus.setAttribute('aria-label', 'Fewer variants');
    plus.setAttribute('aria-label', 'More variants');
    stepRow.append(stepLabel, minus, countEl, plus);
    const promptsBox = el('div', 'wk-abc-prompts');
    abcBody.append(seg, stepRow, promptsBox);
    abcWrap.append(abcHead, abcBody);

    const actions = el('div', 'wk-row wk-actions wk-edit-actions');
    const delBtn = el('button', 'wk-btn danger', 'Delete');
    const cancelBtn = el('button', 'wk-btn ghost', 'Cancel');
    const addRectBtn = el('button', 'wk-btn ghost', '+ Rectangle');
    const doneBtn = el('button', 'wk-btn primary', 'Done');
    delBtn.type = cancelBtn.type = addRectBtn.type = doneBtn.type = 'button';
    delBtn.hidden = !queuedEditing;
    actions.append(delBtn, el('span', 'wk-spacer'), cancelBtn, addRectBtn, doneBtn);

    node.append(head, taWrap, abcWrap, actions);
    wrap.appendChild(node);
    buildFrozen(draft);

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
      if (!USES_VOICE_NOTES) return;
      if (state?.recording) voiceStatus.textContent = 'recording…';
      else if (state?.starting) voiceStatus.textContent = 'connecting…';
      else if (state?.stopping) voiceStatus.textContent = 'finishing…';
      else if (state?.uploading) voiceStatus.textContent = 'saving…';
      else if (draft.voiceNote) voiceStatus.textContent = 'voice attached';
      else voiceStatus.textContent = 'voice note';
      voiceStatus.classList.toggle('ready', !!draft.voiceNote);
    }
    const mic = USES_VOICE_NOTES
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
      abcHead.setAttribute('aria-expanded', String(draft.abc.open));
      segModel.classList.toggle('active', draft.abc.mode === 'model');
      segUser.classList.toggle('active', draft.abc.mode === 'user');
      segModel.setAttribute('aria-pressed', String(draft.abc.mode === 'model'));
      segUser.setAttribute('aria-pressed', String(draft.abc.mode === 'user'));
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
      // of wk:card (removed it, or done saved the point) - a late debounced
      // write here would resurrect a discarded draft 150ms after the fact
      saveDraft.cancel();
      node.remove();
      clearFrozen();
      S.card = null;
      renderPins();
      updateSendBtn();
      if (S.reviewing) updateBar();
      maybeAutoEnterReview();
    }
    function cancelDraft() {
      if (draft.voiceNote && draft.voiceNote.path !== originalVoiceNote?.path) deleteVoiceNote(draft.voiceNote);
      LS.remove('wk:card'); teardown();
    }
    cancelBtn.addEventListener('click', cancelDraft);
    delBtn.addEventListener('click', async () => {
      delBtn.disabled = true;
      try {
        if (queuedEditing) {
          let retired = false;
          try {
            retired = await withPointQueueLock(() => retireQueuedPointIds([queuedEditing.id]));
          } catch (error) {
            toast('Could not delete this point: ' + error.message, { kind: 'error' });
            return;
          }
          if (!retired) {
            toast('Feedback history is full. The point was kept. Close other preview tabs, then clear this preview site storage and reload.', {
              kind: 'error', ttl: 0,
            });
            return;
          }
        }
        if (cardOwner && S.card !== cardOwner) return;
        deleteDistinctVoiceNotes(draft.voiceNote, originalVoiceNote);
        LS.remove('wk:card');
        teardown();
      } finally {
        if (node.isConnected) delBtn.disabled = false;
      }
    });
    // Commit a local point, or atomically revise a server-accepted point that
    // has not entered review yet. Returns false and keeps the card open when a
    // validation or concurrency check fails.
    async function commit() {
      draft.text = ta.value;
      if (mic.starting) {
        toast('Wait for microphone permission before saving this point.', { kind: 'warn' });
        return false;
      }
      if ((mic.on || mic.stopping || mic.uploading) && mic.finishAndWait) {
        // commit() is shared by Done and the control pill's Send action. Keep
        // the recorder shutdown here so every save path captures the final
        // MediaRecorder chunk, waits for its upload, and only then snapshots
        // the point. Callers must never need to stop a recording by hand.
        const voiceReady = await mic.finishAndWait();
        if (!voiceReady) return false;
      }
      if (mic.on || mic.starting || mic.stopping || mic.uploading) return false;
      if (!cardOwner || S.card !== cardOwner || !node.isConnected) return false;
      // SpeechRecognition can deliver its final result immediately before
      // onend. Snapshot again after finishAndWait so that last phrase is saved.
      draft.text = ta.value;
      if (!draft.text.trim() && !draft.voiceNote) { ta.focus(); node.classList.remove('attn'); void node.offsetWidth; node.classList.add('attn'); return false; }
      let pt = pointFromDraft(draft);
      if (pendingEditing) {
        try {
          const result = await api('/__wk/feedback/edit', {
            version: 1,
            kind: 'feedback_edit',
            batchId: S.batch && S.batch.batchId,
            round: S.batch && S.batch.round,
            pointId: pendingEditing.id,
            expectedRevision: pointRevision(draft.editPoint || pendingEditing),
            point: pt,
          }, 15000);
          if (!result || !result.point || result.point.id !== pendingEditing.id) {
            throw new Error('The preview server returned an invalid edited point.');
          }
          if (S.batch && Array.isArray(S.batch.points)) {
            S.batch = {
              ...S.batch,
              points: S.batch.points.map((point) =>
                point.id === result.point.id ? result.point : point
              ),
            };
          }
          S.submittedPoints = pendingReviewPoints(
            [], [], S.submittedPoints.filter((point) => point.id !== result.point.id),
            [result.point]
          );
          if (Array.isArray(S.pendingPointIds) && !S.pendingPointIds.includes(result.point.id)) {
            S.pendingPointIds.push(result.point.id);
          }
          S.agentWakePending = true;
          if (S.reviewing) S.sentVerdicts = true;
          syncTabTitle();
          if (originalVoiceNote && originalVoiceNote.path !== draft.voiceNote?.path) {
            deleteVoiceNote(originalVoiceNote);
          }
          LS.remove('wk:card');
          teardown();
          toast('Updated point ' + result.point.number + '. The agent will use the latest version.');
          pollNow();
          return true;
        } catch (error) {
          toast('Could not update this pending point: ' + error.message, { kind: 'error' });
          pollNow();
          return false;
        }
      }
      if (S.deletedIds.has(pt.id) || LS.get(queuedTombstoneKey(pt.id)) !== null) {
        // A send or delete in another tab won while this editor was open. Keep
        // the user's work as a new point instead of reviving the retired id.
        pt = { ...pt, id: newPointId(), number: nextNumber(), createdAt: nowISO() };
      }
      // update-or-push: if the edited point was sent/deleted meanwhile (findIndex
      // misses), pointFromDraft already minted a fresh id/number, so PUSH it -
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
    addRectBtn.addEventListener('click', () => {
      if (mic.on || mic.starting || mic.stopping || mic.uploading) {
        toast('Finish the voice note before adding another rectangle.', { kind: 'warn' });
        return;
      }
      draft.text = ta.value;
      draft.rects = draft.rects || [draft.rect];
      saveDraft.flush();
      S.addRectDraft = draft;
      mic.stop();
      saveDraft.cancel();
      node.remove();
      S.card = null;
      toast('Draw another rectangle for this same feedback point.');
    });
    doneBtn.addEventListener('click', async () => {
      if (doneBusy || !cardOwner || S.card !== cardOwner) return;
      if (mic.starting) {
        toast('Wait for microphone permission before saving this point.', { kind: 'warn' });
        return;
      }
      doneBusy = true;
      doneBtn.disabled = true;
      try {
        if (!(await commit())) return;
      } finally {
        doneBusy = false;
        if (S.card === cardOwner && node.isConnected) doneBtn.disabled = false;
      }
    });

    // The header doubles as a drag handle. Keep the chosen viewport position in
    // the live draft so resize/scroll passes do not snap the card back beside
    // its rectangle, and a reload restores the user's placement.
    head.addEventListener('pointerdown', (event) => {
      if (event.button !== 0 || S.drag || event.target.closest('button, select, input')) return;
      event.preventDefault();
      head.setPointerCapture(event.pointerId);
      const start = { left: node.offsetLeft, top: node.offsetTop };
      const previous = draft.cardPosition ? { ...draft.cardPosition } : null;
      S.drag = {
        kind: 'card',
        pointerId: event.pointerId,
        sx: event.clientX,
        sy: event.clientY,
        start,
        cancel() {
          draft.cardPosition = previous;
          S.drag = null;
          positionCard();
          saveDraft();
        },
      };
    });
    head.addEventListener('pointermove', (event) => {
      const drag = S.drag;
      if (!drag || drag.kind !== 'card' || drag.pointerId !== event.pointerId) return;
      const left = clamp(drag.start.left + event.clientX - drag.sx, 8, Math.max(8, innerWidth - node.offsetWidth - 8));
      const top = clamp(drag.start.top + event.clientY - drag.sy, 8, Math.max(8, innerHeight - node.offsetHeight - 8));
      draft.cardPosition = { left, top };
      node.style.left = left + 'px';
      node.style.top = top + 'px';
    });
    head.addEventListener('pointerup', (event) => {
      if (!S.drag || S.drag.kind !== 'card' || S.drag.pointerId !== event.pointerId) return;
      S.drag = null;
      saveDraft();
    });
    head.addEventListener('pointercancel', (event) => {
      if (S.drag?.kind === 'card' && S.drag.pointerId === event.pointerId) S.drag.cancel();
    });

    // micBtn is exposed so the dictate/record hotkey drives the same handler.
    cardOwner = {
      node, ta, micBtn, draft, mic, saveDraft, commit, cancel: cancelDraft,
      dictationHotkeyReady() { return ta.value === ''; },
    };
    S.card = cardOwner;
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
    const cw = node.offsetWidth || 320, ch = node.offsetHeight || 160;
    if (S.card.draft.cardPosition) {
      const saved = S.card.draft.cardPosition;
      const left = clamp(saved.left, 8, Math.max(8, innerWidth - cw - 8));
      const top = clamp(saved.top, 8, Math.max(8, innerHeight - ch - 8));
      S.card.draft.cardPosition = { left, top };
      node.style.left = left + 'px';
      node.style.top = top + 'px';
      return;
    }
    const vx = r.x - scrollX, vy = r.y - scrollY;
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
    if (n === 0) {
      sendBtn.classList.remove('queued');
      sendBtn.replaceChildren();
      sendBtn.title = '';
      return;
    }
    const activeRound = S.phase !== null && S.phase !== 'collecting';
    const queued = S.phase === 'verdicts_sent';
    sendBtn.classList.toggle('queued', activeRound);
    sendBtn.replaceChildren();
    sendBtn.append(
      el('span', 'wk-send-label', queued ? 'Saved' : (activeRound ? 'Add' : 'Send')),
      el('span', 'wk-badge', String(n)),
    );
    sendBtn.title = queued
      ? 'Saved locally. Send after the current verdicts finish processing'
      : activeRound
      ? 'Add ' + n + ' point(s) to the current feedback batch'
      : 'Send ' + n + ' point(s) to the ' + COLOR + ' agent';
  }

  let sending = false;
  async function sendPoints() {
    if (sending) return;
    if (S.phase === 'verdicts_sent') {
      updateSendBtn();
      toast('Points are saved locally. Send them after the current verdicts finish processing.');
      return;
    }
    sending = true;
    let sentCount = 0;
    let addedToActiveRound = false;
    let accepted = false;
    try {
      await withPointQueueLock(async () => {
        // This is the send linearization point. A completed tombstone already
        // present here wins and is excluded. A delete that acquires the lock
        // later cannot retract a request the server has already accepted.
        S.points = loadQueuedPoints();
        if (!S.points.length) return;
        const snapshot = S.points.slice();
        const sentIds = snapshot.map((point) => point.id);
        if (!hasQueuedTombstoneCapacity(LS, sentIds, MAX_QUEUED_TOMBSTONES)) {
          throw new Error(
            'Feedback history is full. Points were kept. Close other preview tabs, then clear this preview site storage and reload.'
          );
        }
        const now = nowISO();
        const batch = {
          version: 1,
          kind: 'feedback',
          batchId: S.batch?.batchId || newBatchId(),
          round: S.batch?.round || 1,
          color: COLOR,
          sessionId: SESSION_ID,
          createdAt: now,
          updatedAt: now,
          pages: [...new Set(snapshot.map((point) => point.page))],
          points: snapshot,
        };
        addedToActiveRound = S.phase !== 'collecting' && S.phase !== null;
        const result = await api('/__wk/feedback', batch, 15000);
        accepted = true;
        const acceptedPoints = result && Array.isArray(result.points) ? result.points : snapshot;
        S.agentWakePending = true;
        if (S.reviewing) S.sentVerdicts = true;
        syncTabTitle();
        S.submittedPoints = pendingReviewPoints([], [], S.submittedPoints, acceptedPoints);
        if (Array.isArray(S.pendingPointIds)) {
          for (const point of acceptedPoints) {
            if (point && !S.pendingPointIds.includes(point.id)) S.pendingPointIds.push(point.id);
          }
        }
        if (!retireQueuedPointIds(sentIds)) {
          // The lock makes this unreachable for cooperating tabs. Keep it as a
          // fail-visible guard for browsers without Web Locks or hostile writes.
          throw new Error('Feedback was accepted, but its local history could not be retired safely.');
        }
        sentCount = snapshot.length;
      });
      renderPins();
      updateSendBtn();
      if (S.reviewing) updateBar();
      if (sentCount) {
        toast((addedToActiveRound
          ? 'Added ' + sentCount + ' point(s) to the current batch'
          : 'Sent ' + sentCount + ' point(s)') + '. The ' + EMOJI + ' agent is on it.');
        pollNow();
      }
    } catch (e) {
      toast((accepted ? 'Feedback was sent, but local cleanup failed: ' : 'Send failed: ') +
        e.message, { kind: 'error' });
    } finally {
      sending = false;
    }
  }
  sendBtn.addEventListener('click', async () => {
    // an open editor card holds an uncommitted note - flush it into the batch
    // ("type the note, hit Send" must not ship without it); shake+refuse if empty
    if (S.card) {
      const n = S.card.node;
      if (!S.card.ta.value.trim() && !S.card.draft.voiceNote) {
        n.classList.remove('attn'); void n.offsetWidth; n.classList.add('attn'); return;
      }
      if (!(await S.card.commit())) return;
    }
    sendPoints();
  });

  // Cross-tab sync: distinct point keys commute, and tombstones always win over
  // stale records. Re-read the whole bounded queue on each related event so all
  // tabs converge on the same deterministic numbering.
  window.addEventListener('storage', (e) => {
    const key = LS.logicalKey(e.key);
    if (e.key === null || key === 'wk:points' ||
      (key && (key.startsWith(QUEUED_POINT_PREFIX) ||
        key.startsWith(QUEUED_TOMBSTONE_PREFIX)))) {
      S.points = loadQueuedPoints();
      if (!S.card) renderPins();   // don't yank the layer out from under an open editor
      updateSendBtn();
      updateHint();
    } else if (key && S.verdictsKey && key === S.verdictsKey) {
      S.verdicts = LS.getJSON(key, {});
      renderPins();
      if (S.reviewing) updateBar();
    }
  });

  // ===== polling + phase machine =============================================
  // 2s while the overlay is in feedback mode and a lazy 15s in evaluate mode.
  // A hidden working tab keeps a throttled 5s watch so it can announce review
  // readiness; other hidden phases pause completely.
  let pollT = 0;
  let tabTitleTimer = 0;
  let tabTitleTick = 0;

  function syncTabTitle() {
    clearInterval(tabTitleTimer);
    tabTitleTimer = 0;
    tabTitleTick = 0;
    const mode = S.agentWakePending
      ? 'working'
      : tabActivityMode(S.phase);
    if (mode === 'normal' || (mode === 'review-ready' && !document.hidden)) {
      document.title = NORMAL_TAB_TITLE;
      return;
    }
    const paint = () => {
      tabTitleTick += 1;
      if (mode === 'working') {
        document.title = EMOJI + ' Working' + '.'.repeat((tabTitleTick % 3) + 1) +
          (TAB_BASE_TITLE ? ' · ' + TAB_BASE_TITLE : '');
      } else {
        document.title = EMOJI + (tabTitleTick % 2 ? ' Review ready' : ' ● Review ready') +
          (TAB_BASE_TITLE ? ' · ' + TAB_BASE_TITLE : '');
      }
    };
    paint();
    tabTitleTimer = setInterval(paint, mode === 'working' ? 900 : 1100);
  }

  function schedulePoll(reset) {
    clearTimeout(pollT);
    const activity = S.agentWakePending
      ? 'working'
      : tabActivityMode(S.phase);
    const delay = tabPollDelay(activity, S.mode, document.hidden);
    if (!delay) return;
    pollT = setTimeout(pollNow, delay);
  }
  document.addEventListener('visibilitychange', () => {
    syncTabTitle();
    clearTimeout(pollT);
    if (document.hidden) schedulePoll(true);
    else pollNow();
  });

  let pollWarned = false;
  const pollNow = singleFlight(async () => {
    clearTimeout(pollT);
    try {
      const st = await api('/__wk/state?known=' + encodeURIComponent(S.rev));
      pollWarned = false;
      if (st && BEFORE_PREFIX_PATTERN.test(st.beforePrefix || '')) {
        BEFORE_PREFIX = st.beforePrefix;
      } else if (st && Object.prototype.hasOwnProperty.call(st, 'beforePrefix')) {
        BEFORE_PREFIX = '/__wk/before/invalid';
      }
      if (st && (st.changed || S.phase === null)) handleState(st);
      else if (st && st.rev) S.rev = st.rev;
    } catch (e) {
      if (!pollWarned) {
        pollWarned = true;
        console.warn('[wk] state poll failed (server down?):', e.message);
      }
    }
  }, schedulePoll);

  function handleState(st) {
    const prevPhase = S.phase;
    const previousReviewIdentity = S.review
      ? [S.review.batchId, S.review.round, S.review.beforeRef || ''].join(':')
      : '';
    S.rev = st.rev || '';
    S.phase = st.phase || 'collecting';
    S.agentWakePending = false;
    if (S.phase !== 'reviewing') S.reviewAutoPending = false;
    S.batch = st.batch || null;
    S.review = st.review || null;
    S.pendingPointIds = Array.isArray(st.pendingPointIds)
      ? st.pendingPointIds.filter((id) => typeof id === 'string')
      : null;
    if (S.phase === 'collecting') {
      S.submittedPoints = [];
    } else {
      const serverPointIds = new Set(
        (S.batch && Array.isArray(S.batch.points) ? S.batch.points : [])
          .map((point) => point && point.id).filter(Boolean)
      );
      S.submittedPoints = S.submittedPoints.filter((point) => !serverPointIds.has(point.id));
    }
    S.points = loadQueuedPoints();
    const nextReviewIdentity = S.review
      ? [S.review.batchId, S.review.round, S.review.beforeRef || ''].join(':')
      : '';
    if (previousReviewIdentity !== nextReviewIdentity) {
      invalidateSwapWork();
      swapQueued = null;
      restoreSwap();
      resetSwapDocumentCache();
      if (!IS_BEFORE) S.side = 'after';
    }
    if (S.mini && !reviewTargetIsCurrent(S.mini.target)) S.mini.cancel();

    // leaving verdicts_sent = the agent consumed our verdicts → the local
    // verdict cache for that batch is now history
    if (prevPhase === 'verdicts_sent' && S.phase !== 'verdicts_sent' && S.verdictsKey) {
      LS.remove(S.verdictsKey);   // round-qualified; enterReview's sweep is the real GC
      S.verdicts = {};
      S.sentVerdicts = false;
    }
    if (S.reviewing) S.sentVerdicts = S.phase !== 'reviewing';
    syncTabTitle();

    if (S.phase === 'collecting') {
      if (S.reviewing) {
        if (IS_BEFORE) {
          // the round is over - this git-snapshot document is now orphaned (its
          // URL 409s and it has no draw layer). Hand off to the live AFTER
          // document at the same spot; replace() so the dead URL leaves no history.
          SS.setJSON('wk:scroll', { path: logicalPath(), x: Math.round(scrollX), y: Math.round(scrollY) });
          location.replace(physicalPath(logicalPath(), 'after'));
          return;
        }
        exitReview();
        toast('Round complete - batch archived. Draw away!');
      }
    } else if (S.phase === 'reviewing' && S.review) {
      const key = S.review.batchId + ':' + S.review.round;
      const reloadKey = 'wk:review-assets:' + key;
      if (SS.get(reloadKey) !== 'ready') {
        SS.set(reloadKey, 'ready');
        // review.json is written only after the point commits. Reload once per
        // round before exposing review controls so the host document, CSS, and
        // JS all come from that committed AFTER state rather than a stale DOM.
        location.reload();
        return;
      }
      if (S.reviewing && S.reviewBatchId === S.review.batchId && S.reviewRound !== S.review.round) {
        // next round landed while we watch - re-enter at point 1
        enterReview({ auto: true });
        toast('Round ' + S.review.round + ' ready - walking the redone points.');
      } else if (!S.reviewing && S.offeredReview !== key && S.bootReview !== S.review.batchId) {
        S.offeredReview = key;
        S.reviewAutoPending = true;
        maybeAutoEnterReview();
      }
    }

    if (!S.card) renderPins();
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
      statusChip.textContent = EMOJI + ' verdicts sent - agent is processing…';
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

  function maybeAutoEnterReview() {
    if (
      !S.reviewAutoPending || S.reviewing || S.card ||
      S.phase !== 'reviewing' || !S.review || !S.batch
    ) return false;
    S.reviewAutoPending = false;
    enterReview({ auto: true });
    return true;
  }

  function enterReview(opts) {
    if (!S.review || !S.batch) return;
    if (S.mini) S.mini.cancel();
    S.reviewAutoPending = false;
    S.reviewing = true;
    S.sentVerdicts = S.phase === 'verdicts_sent';
    S.reviewBatchId = S.review.batchId;
    S.reviewRound = S.review.round;
    S.offeredReview = S.review.batchId + ':' + S.review.round;   // this round is already open
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
    if (S.mini) S.mini.cancel();
    S.reviewing = false;
    invalidateSwapWork();
    swapQueued = null;
    S.reviewList = [];
    S.curPinEls = null;
    // leaving review = the page must go back to being itself: the live AFTER
    // DOM/stylesheets, and the page's own abc switcher visible again
    restoreSwap();
    resetSwapDocumentCache();
    if (!IS_BEFORE) S.side = 'after';
    restorePageAbc();
    closeAutoOpenedReviewSurfaces();
    bar.hidden = true;
    SS.remove('wk:reviewCursor');
    renderPins();
    updateStatusChip();
  }

  // Probe a cross-document target before navigating: the agent may have created,
  // renamed or deleted the page (or the round just ended), and a blind
  // location.href would strand the user on a bare 404/409 with no overlay. A GET
  // (not HEAD - the server has no do_HEAD, so HEAD bypasses /__wk/before) tells us.
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
    if (S.mini && (S.mini.target.batchId !== S.reviewBatchId ||
      S.mini.target.round !== S.reviewRound || S.mini.target.pointId !== pt.id)) {
      S.mini.cancel();
    }
    S.cursor = idx;
    S.acceptArmed = null;
    SS.setJSON('wk:reviewCursor', { batchId: S.reviewBatchId, round: S.reviewRound, idx });
    if (pt.page !== logicalPath()) {
      // cross-page: full navigation; boot re-enters review at this point
      navGuarded(physicalPath(pt.page, S.side) +
        '?wk-review=' + encodeURIComponent(S.reviewBatchId) + '&wk-point=' + pt.number,
        "This point's page no longer exists on this side - the agent may have removed or renamed it.");
      return;
    }
    restoreReviewSurfaces(pt);
    if (S.side === 'before' && !IS_BEFORE) resyncSwap(pt);   // per-point swap container
    renderPins();
    updateBar();
    const va = pinBox(pt);
    const box = va ? va.box : correctedRect(pt);
    const capturedStickyScrollY = Number(pt.rectSurfaces?.[0]?.scroll?.y ?? pt.scroll?.y);
    if (!opts.noScroll) {
      // instant, not smooth: the flash should land where the eye already is, and
      // smooth scrolls never finish in a backgrounded tab (the jump idiom the
      // abc widget's RELOAD mode uses is instant for the same reason). Sticky
      // points are viewport-anchored only while their containing scene is live;
      // once that scene passes, bring them back instead of treating them as a
      // permanently visible fixed control.
      const target = reviewScrollTarget(
        box, !!va, va ? va.anchorMode : 'doc', scrollY, innerHeight,
        capturedStickyScrollY
      );
      if (target !== null) {
        window.scrollTo({ top: target, behavior: 'instant' });
        requestAnimationFrame(() => {
          const settled = pinBox(pt);
          const settledBox = settled ? settled.box : correctedRect(pt);
          const retry = reviewScrollTarget(
            settledBox, !!settled, settled ? settled.anchorMode : 'doc', scrollY, innerHeight,
            capturedStickyScrollY
          );
          if (retry !== null && Math.abs(retry - scrollY) > 1) {
            window.scrollTo({ top: retry, behavior: 'instant' });
          }
          schedulePos();
        });
      }
    }
    // flash twice - the CSS animation runs 2 iterations; restart it
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
  // spot. So we swap like an A/B variant instead - fetch the before document
  // once, lift out the container the current point lives in, and put it in the
  // live DOM, keeping the live node in memory for the way back. Two things make
  // this useful: the reveal/doodle machinery is nudged so the swapped subtree
  // doesn't land inert. Point scope deliberately leaves the rest of the page
  // and its stylesheets alone; whole-site scope uses the exact git snapshot.
  // Anything that cannot be resolved falls back to the full snapshot page.
  const SWAP = {
    doc: null,      // parsed before-document (per logical page)
    docPath: '',
    docKey: '',     // beforeRef plus logical page
    sel: '',        // selector of the swapped container
    live: null,     // the AFTER node, detached, waiting to go back
    placed: null,   // the BEFORE node currently in the document
  };
  let swapBusy = false, swapQueued = null;
  let swapGeneration = 0;

  function newSwapContext(pt) {
    return {
      generation: ++swapGeneration,
      batchId: S.reviewBatchId,
      round: S.reviewRound,
      pointId: pt && pt.id,
      beforeRef: S.review?.beforeRef || '',
      page: logicalPath(),
      scope: S.compareScope,
    };
  }

  function invalidateSwapWork() {
    swapGeneration += 1;
  }

  function swapContextIsCurrent(context) {
    const pt = currentPoint();
    return !!context && context.generation === swapGeneration && S.reviewing &&
      (S.phase === 'reviewing' || S.phase === 'verdicts_sent') &&
      S.reviewBatchId === context.batchId && S.reviewRound === context.round &&
      S.batch?.batchId === context.batchId && S.batch?.round === context.round &&
      S.review?.batchId === context.batchId &&
      S.review?.round === context.round && S.review?.beforeRef === context.beforeRef &&
      logicalPath() === context.page && S.compareScope === context.scope && pt?.id === context.pointId;
  }

  function requireCurrentSwap(context) {
    if (swapContextIsCurrent(context)) return;
    const error = new Error('comparison target changed');
    error.wkStaleSwap = true;
    throw error;
  }

  function resetSwapDocumentCache() {
    SWAP.doc = null;
    SWAP.docPath = '';
    SWAP.docKey = '';
  }

  async function beforeDocument(context) {
    requireCurrentSwap(context);
    const key = context.beforeRef + '\n' + context.page;
    if (SWAP.doc && SWAP.docKey === key) return SWAP.doc;
    const r = await fetch(BEFORE_PREFIX + context.page, { cache: 'no-store' });
    if (!r.ok) throw new Error(r.status === 409 ? 'round just ended' : 'HTTP ' + r.status);
    const html = await r.text();
    requireCurrentSwap(context);
    const doc = new DOMParser().parseFromString(asTrustedHTML(html), 'text/html');
    requireCurrentSwap(context);
    SWAP.doc = doc;
    SWAP.docPath = context.page;
    SWAP.docKey = key;
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
  // classes across, stopping at the first structural divergence - below that the
  // trees aren't comparable and positional matching would paint the wrong nodes.
  const SWAP_STATE_CLASS = /^(?:is-|has-|js-)|^(?:in|active|visible|shown|open|current|played|done)$/;
  function carryState(from, to) {
    if (!from || !to) return;
    for (const c of from.classList) if (SWAP_STATE_CLASS.test(c)) to.classList.add(c);
    const a = from.children, b = to.children;
    if (a.length !== b.length) return;
    for (let i = 0; i < a.length; i++) carryState(a[i], b[i]);
  }

  // Point-scope BEFORE is an optimization, not a different definition of
  // correctness. A detached DOM clone cannot reproduce runtime-owned pixels,
  // generated trees, historical URL resolution, shadow DOM, pseudo-elements,
  // or active animation. Refuse the optimization whenever either side is not a
  // plain, structurally identical DOM subtree. The caller then loads the full
  // historical page, where the original scripts and asset paths run normally.
  const UNSAFE_SWAP_TAGS = new Set([
    'CANVAS', 'VIDEO', 'AUDIO', 'IFRAME', 'OBJECT', 'EMBED', 'SCRIPT',
    'IMG', 'PICTURE', 'SOURCE',
  ]);
  const UNSAFE_SWAP_URL_ATTRS = ['src', 'srcset', 'poster', 'data', 'xlink:href'];
  function pointSwapUnsafeReason(live, incoming) {
    if (!live || !incoming) return 'comparison container is missing';
    const queue = [[live, incoming]];
    while (queue.length) {
      const [current, historical] = queue.pop();
      if (current.tagName !== historical.tagName) return 'the page generates different markup at runtime';
      if (UNSAFE_SWAP_TAGS.has(current.tagName)) return 'this element is rendered by the browser or page runtime';
      if (current.tagName.includes('-') || historical.tagName.includes('-') ||
        current.shadowRoot || historical.shadowRoot) return 'this element uses a runtime component';
      for (const attr of UNSAFE_SWAP_URL_ATTRS) {
        if (current.hasAttribute(attr) || historical.hasAttribute(attr)) {
          return 'this element owns a versioned asset';
        }
      }
      const currentHref = current.getAttribute('href');
      const historicalHref = historical.getAttribute('href');
      if ((currentHref && !currentHref.startsWith('#')) ||
        (historicalHref && !historicalHref.startsWith('#'))) {
        return 'this element owns a versioned asset';
      }
      if (/url\s*\(/i.test(current.getAttribute('style') || '') ||
        /url\s*\(/i.test(historical.getAttribute('style') || '')) {
        return 'this element owns a versioned asset';
      }
      try {
        const style = getComputedStyle(current);
        const before = getComputedStyle(current, '::before').content;
        const after = getComputedStyle(current, '::after').content;
        if ((style.animationName && style.animationName !== 'none') ||
          !['none', 'normal', ''].includes(before) || !['none', 'normal', ''].includes(after) ||
          [style.backgroundImage, style.maskImage, style.listStyleImage].some(
            (value) => value && value !== 'none' && /url\s*\(/i.test(value)
          )) return 'this element depends on runtime styling';
      } catch (e) {
        return 'this element runtime could not be verified';
      }
      const currentKids = current.children;
      const historicalKids = historical.children;
      if (currentKids.length !== historicalKids.length) {
        return 'the page generates different markup at runtime';
      }
      for (let i = 0; i < currentKids.length; i++) queue.push([currentKids[i], historicalKids[i]]);
    }
    return '';
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

  async function styledBeforeNode(sel, fallback, context, live) {
    const frame = document.createElement('iframe');
    frame.setAttribute('aria-hidden', 'true');
    frame.setAttribute('sandbox', 'allow-same-origin');
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
      requireCurrentSwap(context);
      let source = null;
      try { source = frame.contentDocument.querySelector(sel); } catch (e) { /* fallback below */ }
      if (source) carryState(live, source);
      const node = document.importNode(source || fallback, true);
      if (source) copyComputedTree(source, node);
      requireCurrentSwap(context);
      return node;
    } finally {
      frame.remove();
    }
  }

  // Keep the eye on the thing being compared. A swap changes the height of the
  // container (and on a scroll-driven page the synthetic scroll/resize above can
  // make the host's own story JS re-snap), so without this the page can end up
  // thousands of px away from the point - toggling BEFORE|AFTER would show you
  // somewhere else entirely. Capture where the anchor sits in the viewport, then
  // put it back there afterwards. Returns a restore fn; call it AFTER the paint.
  function anchorViewport(pt, context) {
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
        if (context && !swapContextIsCurrent(context)) return;
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

  async function applySwap(pt, context) {
    requireCurrentSwap(context);
    const doc = await beforeDocument(context);
    requireCurrentSwap(context);
    const t = resolveSwapTarget(pt, doc);
    if (!t) throw new Error('no container shared by both versions');
    const unsafeReason = pointSwapUnsafeReason(t.live, t.incoming);
    if (unsafeReason) {
      const error = new Error(unsafeReason);
      error.wkFullBefore = true;
      throw error;
    }
    const reanchor = anchorViewport(pt, context);
    const node = await styledBeforeNode(t.sel, t.incoming, context, t.live);
    requireCurrentSwap(context);
    if (!t.live.isConnected) throw new Error('live comparison container changed');
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
    restoreReviewSurfaces(currentPoint());
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
    if (side !== S.side && S.mini) S.mini.cancel();
    if (S.compareScope === 'site') {
      if (side !== S.side) navSide(side);
      return;
    }
    // A document actually SERVED from /__wk/before is a git snapshot with no
    // live tree to restore - only a navigation can leave it.
    if (IS_BEFORE) { if (side !== S.side) navSide(side); return; }
    // The first BEFORE costs a fetch; a click landing during it must not be
    // swallowed (the button would just look dead) - remember it and settle there.
    if (swapBusy) {
      invalidateSwapWork();
      swapQueued = { kind: 'side', side };
      return;
    }
    if (side === S.side) return;
    if (side === 'after') {
      invalidateSwapWork();
      restoreSwap();
      markSide('after');
      return;
    }
    const pt = currentPoint();
    if (!pt) return navSide('before');
    const context = newSwapContext(pt);
    swapBusy = true;
    applySwap(pt, context).then(() => {
      requireCurrentSwap(context);
      markSide('before');
    }).catch((e) => {
      if (e.wkStaleSwap) return;
      restoreSwap();
      if (!e.wkFullBefore) {
        toast('In-place BEFORE not possible here (' + e.message + ') - loading the snapshot page.',
          { kind: 'warn' });
      }
      navSide('before');
    }).finally(() => { swapBusy = false; drainSwapQueue(); });
  }
  function drainSwapQueue() {
    const q = swapQueued;
    swapQueued = null;
    if (!q) return;
    if (q.kind === 'resync') {
      if (S.side === 'before' && !IS_BEFORE) resyncSwap(currentPoint());
      return;
    }
    if (q.side !== S.side) setSide(q.side);
  }

  // Moving to another point while BEFORE is showing: the swapped container is
  // per-point, so re-resolve it. If the new point has no shared container we are
  // honestly on AFTER for it - say so rather than mislabel the bar.
  function resyncSwap(pt) {
    if (swapBusy) {
      invalidateSwapWork();
      swapQueued = { kind: 'resync' };
      return;
    }
    if (!SWAP.placed) return;
    const t = resolveSwapTarget(pt, SWAP.doc);
    if (t && t.sel === SWAP.sel) return;
    const context = newSwapContext(pt);
    swapBusy = true;
    restoreSwap();
    applySwap(pt, context).catch((error) => {
      if (error.wkStaleSwap) return;
      restoreSwap();
      markSide('after');
      toast('No in-place BEFORE for this point - showing AFTER.', { kind: 'warn' });
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
    B.prev.setAttribute('aria-label', 'Previous review point');
    B.next.setAttribute('aria-label', 'Next review point');
    B.counter = el('span', 'wk-bar-count');
    B.dots = el('span', 'wk-dots');
    B.pending = el('span', 'wk-pending-count');
    B.pending.hidden = true;

    B.seg = el('div', 'wk-seg wk-side');
    B.seg.setAttribute('role', 'group');
    B.seg.setAttribute('aria-label', 'Choose before or after view');
    B.before = el('button', 'wk-seg-btn', 'BEFORE');
    B.after = el('button', 'wk-seg-btn', 'AFTER');
    B.before.type = B.after.type = 'button';
    B.seg.append(B.before, B.after);
    B.scopeWrap = el('div', 'wk-scope-wrap');
    B.scope = el('button', 'wk-scope-btn', '▾');
    B.scope.type = 'button';
    B.scope.setAttribute('aria-label', 'Choose comparison scope');
    B.scope.setAttribute('aria-haspopup', 'menu');
    B.scopeMenu = el('div', 'wk-scope-menu');
    B.scopeMenu.hidden = true;
    B.scopeMenu.setAttribute('role', 'menu');
    B.scopeHeading = el('div', 'wk-scope-heading', 'BEFORE/AFTER');
    B.scopePoint = el('button', 'wk-scope-option', 'Current point');
    B.scopeSite = el('button', 'wk-scope-option', 'Whole website');
    B.scopePoint.type = B.scopeSite.type = 'button';
    B.scopePoint.setAttribute('role', 'menuitemradio');
    B.scopeSite.setAttribute('role', 'menuitemradio');
    B.scopeMenu.append(B.scopeHeading, B.scopePoint, B.scopeSite);
    B.scopeWrap.append(B.scope, B.scopeMenu);

    B.abcChip = el('button', 'wk-abc-chip');
    B.abcChip.type = 'button';
    B.abcChip.hidden = true;
    B.abcChip.setAttribute('aria-label', 'Cycle comparison variant');

    B.note = el('span', 'wk-bar-note');
    B.noteText = el('span', 'wk-bar-note-text');
    B.noteTip = el('span', 'wk-bar-note-tip');
    B.note.append(B.noteText, B.noteTip);
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

    bar.append(B.prev, B.counter, B.dots, B.pending, B.next, el('span', 'wk-bar-sep'),
      B.seg, B.scopeWrap, B.abcChip, B.note, el('span', 'wk-bar-sep'),
      B.accept, B.redo, B.del, B.dismiss, B.sendv, B.wait);

    B.prev.addEventListener('click', () => jumpTo(S.cursor - 1));
    B.next.addEventListener('click', () => jumpTo(S.cursor + 1));
    B.before.addEventListener('click', () => setSide('before'));
    B.after.addEventListener('click', () => setSide('after'));
    B.scope.addEventListener('click', () => {
      B.scopeMenu.hidden = !B.scopeMenu.hidden;
      B.scope.setAttribute('aria-expanded', String(!B.scopeMenu.hidden));
      B.scope.textContent = B.scopeMenu.hidden ? '▾' : '▴';
    });
    B.scopePoint.addEventListener('click', () => {
      B.scopeMenu.hidden = true;
      setCompareScope('point');
    });
    B.scopeSite.addEventListener('click', () => {
      B.scopeMenu.hidden = true;
      setCompareScope('site');
    });
    bar.onpointerdown = (event) => {
      if (!B.scopeWrap.contains(event.target)) {
        B.scopeMenu.hidden = true;
        B.scope.setAttribute('aria-expanded', 'false');
        B.scope.textContent = '▾';
      }
    };
    B.accept.addEventListener('click', onAccept);
    B.del.addEventListener('click', () => recordVerdict({ verdict: 'delete' }));
    B.dismiss.addEventListener('click', () => recordVerdict({ verdict: 'delete' }));
    B.redo.addEventListener('click', openMini);
    B.sendv.addEventListener('click', sendVerdicts);
    // the chip's title promises "click to cycle" - honour it by driving the abc
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
    invalidateSwapWork();
    swapQueued = null;
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

  function reviewTargetIsCurrent(target) {
    if (!target || !S.reviewing || S.phase !== 'reviewing' || S.sentVerdicts) return false;
    if (target.batchId !== S.reviewBatchId || target.round !== S.reviewRound) return false;
    if (!S.review || target.batchId !== S.review.batchId || target.round !== S.review.round) return false;
    const pt = currentPoint();
    if (!pt || pt.id !== target.pointId) return false;
    return ((S.batch && S.batch.points) || []).some((point) => point.id === target.pointId);
  }

  let autoOpenedReviewSurfaces = [];
  function closeAutoOpenedReviewSurfaces() {
    for (const entry of autoOpenedReviewSurfaces.reverse()) {
      try {
        if (entry.kind === 'dialog' && entry.element.open) entry.element.close();
        else if (entry.kind === 'popover' && entry.element.matches(':popover-open')) entry.element.hidePopover();
      } catch (e) { /* the page may have replaced the surface */ }
    }
    autoOpenedReviewSurfaces = [];
  }
  function restoreReviewSurfaces(pt) {
    closeAutoOpenedReviewSurfaces();
    const surfaces = pt && pt.uiState && Array.isArray(pt.uiState.surfaces)
      ? pt.uiState.surfaces
      : [];
    for (const surface of surfaces) {
      if (!surface || !surface.selector) continue;
      let element = null;
      try { element = document.querySelector(surface.selector); } catch (e) { continue; }
      if (!element) continue;
      try {
        if (surface.kind === 'dialog' &&
          typeof HTMLDialogElement !== 'undefined' &&
          element instanceof HTMLDialogElement && !element.open) {
          element.showModal();
          autoOpenedReviewSurfaces.push({ kind: 'dialog', element });
        } else if (surface.kind === 'popover' && !element.matches(':popover-open')) {
          element.showPopover();
          autoOpenedReviewSurfaces.push({ kind: 'popover', element });
        }
      } catch (e) { /* surface is no longer openable on this page version */ }
    }
  }
  function currentHandled() {
    const pt = currentPoint();
    return pt ? S.handledById.get(pt.id) : null;
  }
  function currentAbcInst() {
    const h = currentHandled();
    const scopeId = h && h.abc && h.abc.scopeId;
    return scopeId ? (window.__abc?.get?.(scopeId) || null) : null;
  }

  // The chip is the only variant switcher on screen during review. Keep it
  // compact: the active letter plus one dot for each available variant.
  function paintAbcChip(letter, letters) {
    B.abcChip.textContent = '';
    B.abcChip.append(el('span', 'wk-chip-letter', String(letter)));
    if (letters && letters.length > 1) {
      const run = el('span', 'wk-chip-run');
      run.setAttribute('aria-hidden', 'true');
      for (const L of letters) {
        run.appendChild(el('span', 'wk-chip-dot' + (L === letter ? ' on' : '')));
      }
      B.abcChip.appendChild(run);
    }
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
    const pending = pendingReviewPoints(
      S.batch && Array.isArray(S.batch.points) ? S.batch.points : [],
      S.reviewList,
      S.submittedPoints,
      S.points,
      S.pendingPointIds
    );
    B.pending.hidden = pending.length === 0;
    B.pending.textContent = '+' + pending.length + ' pending';
    B.pending.title = pending.length + ' added feedback point' + (pending.length === 1 ? '' : 's') +
      ' saved and waiting for the agent';
    B.pending.setAttribute('aria-label', B.pending.title);
    B.dots.textContent = '';
    S.reviewList.forEach((p, i) => {
      const dv = S.verdicts[p.id];
      const d = el('button', 'wk-dot-i' + (dv ? ' v-' + dv.verdict : '') + (i === S.cursor ? ' cur' : ''));
      d.type = 'button';
      d.title = 'point ' + p.number + (dv ? ' - ' + dv.verdict : '');
      d.setAttribute('aria-label', 'Review point ' + p.number +
        (dv ? ', marked ' + dv.verdict : ', not yet decided'));
      if (i === S.cursor) d.setAttribute('aria-current', 'true');
      d.addEventListener('click', () => jumpTo(i));
      B.dots.appendChild(d);
    });

    B.before.classList.toggle('active', S.side === 'before');
    B.after.classList.toggle('active', S.side === 'after');
    B.before.setAttribute('aria-pressed', String(S.side === 'before'));
    B.after.setAttribute('aria-pressed', String(S.side === 'after'));
    B.scope.title = 'BEFORE/AFTER Comparison scope';
    B.scope.setAttribute('aria-label', B.scope.title);
    B.scope.setAttribute('aria-expanded', String(!B.scopeMenu.hidden));
    B.scope.textContent = B.scopeMenu.hidden ? '▾' : '▴';
    B.scope.classList.toggle('site', S.compareScope === 'site');
    B.scopePoint.classList.toggle('active', S.compareScope === 'point');
    B.scopeSite.classList.toggle('active', S.compareScope === 'site');
    B.scopePoint.setAttribute('aria-checked', String(S.compareScope === 'point'));
    B.scopeSite.setAttribute('aria-checked', String(S.compareScope === 'site'));

    // abc chip: live current letter, bound to abc:change. This is the ONLY
    // switcher the user should see for the point under review - hidePageAbc
    // parks the page's own duplicate button for the same scope.
    const isAbc = !!(h && h.abc);
    B.abcChip.hidden = !isAbc;
    B.abcChip.disabled = false;
    B.abcChip.setAttribute('aria-disabled', 'false');
    hidePageAbc(isAbc && !IS_BEFORE ? h.abc.scopeId : null);
    let abcBlocked = false;
    if (isAbc) {
      const inst = currentAbcInst();
      if (IS_BEFORE) {
        paintAbcChip('-', null);
        B.abcChip.disabled = true;
        B.abcChip.setAttribute('aria-disabled', 'true');
        B.abcChip.classList.add('disabled');
        B.abcChip.classList.remove('error');
        B.abcChip.title = 'Variants live in the AFTER view - toggle AFTER to compare A/B/C';
      } else if (!inst) {
        paintAbcChip('?', null);
        B.abcChip.disabled = true;
        B.abcChip.setAttribute('aria-disabled', 'true');
        B.abcChip.classList.add('error');
        B.abcChip.classList.remove('disabled');
        B.abcChip.title = 'abc scope "' + h.abc.scopeId + '" not found on this page - accept blocked';
        abcBlocked = true;
      } else {
        paintAbcChip(inst.current, inst.letters);
        B.abcChip.classList.remove('disabled', 'error');
        B.abcChip.title = 'variant ' + inst.current + ' of ' + inst.letters + ' - click to cycle';
      }
    }

    // skipped points get Dismiss/Redo instead of Accept/Redo/Delete
    const skipped = h && h.handled === 'skipped';
    B.note.hidden = !(h && h.note);
    if (h && h.note) {
      const fullNote = (skipped ? 'skipped: ' : '') + h.note;
      B.noteText.textContent = fullNote;
      B.noteTip.textContent = fullNote;
    } else {
      B.noteText.textContent = '';
      B.noteTip.textContent = '';
    }
    B.accept.hidden = skipped || S.sentVerdicts;
    B.del.hidden = skipped || S.sentVerdicts;
    B.dismiss.hidden = !skipped || S.sentVerdicts;
    B.redo.hidden = S.sentVerdicts;
    B.accept.disabled = abcBlocked;
    B.accept.title = abcBlocked ? 'The abc switcher for this point is missing - cannot record a chosen letter'
      : (isAbc ? 'Keep the variant currently shown' : 'Keep this change');
    B.accept.classList.toggle('armed', S.acceptArmed === pt.id);
    B.accept.textContent = S.acceptArmed === pt.id ? 'Accept ✓?' : 'Accept';

    for (const [btn, name] of [[B.accept, 'accept'], [B.redo, 'redo'], [B.del, 'delete'], [B.dismiss, 'delete']]) {
      const selected = !!v && v.verdict === name;
      btn.classList.toggle('active', selected);
      btn.setAttribute('aria-pressed', String(selected));
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
        // the user never flipped through the variants - one warning, then allow
        S.acceptArmed = pt.id;
        toast('You haven’t tried the other variants (' + inst.letters +
          ') - click Accept again to keep "' + inst.current + '".', { kind: 'warn' });
        updateBar();
        return;
      }
      entry.chosenLetter = inst.current;
    }
    recordVerdict(entry);
  }

  function recordVerdict(entry, target) {
    if (target && !reviewTargetIsCurrent(target)) return false;
    const pt = currentPoint();
    if (!pt || !S.reviewing || S.phase !== 'reviewing' || S.sentVerdicts) return false;
    const existing = S.verdicts[pt.id];
    let next = entry;
    if (existing && existing.verdict === entry.verdict && entry.verdict !== 'redo' &&
      existing.chosenLetter === entry.chosenLetter) {
      next = null;
      delete S.verdicts[pt.id];   // click the active verdict again = clear it
    } else {
      S.verdicts[pt.id] = entry;
    }
    if (existing?.redoVoiceNote?.path !== next?.redoVoiceNote?.path) {
      deleteVoiceNote(existing?.redoVoiceNote);
    }
    S.acceptArmed = null;
    LS.setJSON(S.verdictsKey, S.verdicts);
    renderPins();
    updateBar();
    // glide to the next unverdicted point, if any
    if (S.verdicts[pt.id]) {
      for (let k = 1; k <= S.reviewList.length; k++) {
        const i = (S.cursor + k) % S.reviewList.length;
        if (!S.verdicts[S.reviewList[i].id]) { jumpTo(i); return true; }
      }
    }
    return true;
  }

  // --- redo mini-input --------------------------------------------------------
  function openMini() {
    if (S.mini || S.sentVerdicts) return;
    const pt = currentPoint();
    if (!pt) return;
    const target = { batchId: S.reviewBatchId, round: S.reviewRound, pointId: pt.id };
    if (!reviewTargetIsCurrent(target)) return;
    const existing = S.verdicts[pt.id];
    const node = el('div', 'wk-mini');
    const label = el('div', 'wk-mini-label', 'Redo point ' + pt.number + ' - what’s still wrong?');
    node.setAttribute('role', 'dialog');
    label.id = 'wk-redo-title-' + rand4();
    node.setAttribute('aria-labelledby', label.id);
    const row = el('div', 'wk-row');
    const ta = el('textarea', 'wk-ta wk-mini-ta');
    ta.setAttribute('aria-label', 'Redo instructions for point ' + pt.number);
    ta.placeholder = 'e.g. closer, but make it half the size…';
    ta.value = (existing && existing.redoText) || '';
    const originalRedoVoiceNote = (existing && existing.redoVoiceNote) || null;
    let redoVoiceNote = originalRedoVoiceNote;
    const existingRedoAbc = existing && existing.redoAbcRequest;
    let redoVariantCount = existingRedoAbc && existingRedoAbc.mode === 'model' &&
      Number.isInteger(existingRedoAbc.count) ? existingRedoAbc.count : 4;
    let redoVariants = !!existingRedoAbc;
    let miniOwner = null;
    let saveBusy = false;
    let closed = false;
    const micBtn = el('button', 'wk-mic');
    micBtn.type = 'button';
    micBtn.title = MIC_TITLE;
    micBtn.setAttribute('aria-label', MIC_ARIA_LABEL);
    micBtn.appendChild(microphoneIcon());
    const langSelect = speechLanguageSelect();
    const variantRow = el('div', 'wk-mini-variant-row');
    const variants = el('button', 'wk-btn wk-mini-variants', 'Generate variants');
    variants.type = 'button';
    const variantStep = el('div', 'wk-mini-variant-step');
    const variantMinus = el('button', 'wk-step', '-');
    const variantCount = el('span', 'wk-count-n', String(redoVariantCount));
    const variantPlus = el('button', 'wk-step', '+');
    variantMinus.type = variantPlus.type = 'button';
    variantMinus.setAttribute('aria-label', 'Generate fewer variants');
    variantPlus.setAttribute('aria-label', 'Generate more variants');
    function paintRedoVariants() {
      variants.setAttribute('aria-pressed', String(redoVariants));
      variants.classList.toggle('active', redoVariants);
      variantStep.hidden = !redoVariants;
      variantCount.textContent = String(redoVariantCount);
      variantMinus.disabled = redoVariantCount <= 2;
      variantPlus.disabled = redoVariantCount >= 10;
    }
    variants.addEventListener('click', () => {
      redoVariants = !redoVariants;
      paintRedoVariants();
    });
    variantMinus.addEventListener('click', () => {
      redoVariantCount = Math.max(2, redoVariantCount - 1);
      paintRedoVariants();
    });
    variantPlus.addEventListener('click', () => {
      redoVariantCount = Math.min(10, redoVariantCount + 1);
      paintRedoVariants();
    });
    variantStep.append(variantMinus, variantCount, variantPlus);
    variantRow.append(variants, variantStep);
    paintRedoVariants();
    const cancel = el('button', 'wk-btn ghost', 'Cancel');
    const save = el('button', 'wk-btn primary', 'Redo it');
    micBtn.type = cancel.type = save.type = 'button';
    row.append(ta, langSelect, micBtn);
    const actions = el('div', 'wk-row wk-actions');
    actions.append(el('span', 'wk-spacer'), cancel, save);
    node.append(label, row, variantRow, actions);
    wrap.appendChild(node);
    const mic = USES_VOICE_NOTES
      ? makeVoiceRecorder(micBtn, langSelect, (note) => {
        if (redoVoiceNote && redoVoiceNote.path !== originalRedoVoiceNote?.path) deleteVoiceNote(redoVoiceNote);
        redoVoiceNote = note;
      }, null)
      : makeMic(ta, micBtn, langSelect, null);
    function close(discardNew) {
      if (closed) return;
      closed = true;
      mic.stop();
      if (discardNew && redoVoiceNote?.path !== originalRedoVoiceNote?.path) {
        deleteVoiceNote(redoVoiceNote);
      }
      node.remove();
      if (S.mini === miniOwner) S.mini = null;
    }
    function cancelMini() {
      close(true);
    }
    cancel.addEventListener('click', cancelMini);
    save.addEventListener('click', async () => {
      if (saveBusy || !miniOwner || S.mini !== miniOwner) return;
      if (!reviewTargetIsCurrent(target)) {
        cancelMini();
        toast('That review target changed. Open Redo again on the current point.', { kind: 'warn' });
        return;
      }
      if (mic.starting) {
        toast('Wait for microphone permission before saving this redo.', { kind: 'warn' });
        return;
      }
      saveBusy = true;
      save.disabled = true;
      try {
        let voiceReady = true;
        if (mic.on && mic.finishAndWait) voiceReady = await mic.finishAndWait();
        else if ((mic.stopping || mic.uploading) && mic.finishAndWait) {
          voiceReady = await mic.finishAndWait();
        }
        if (voiceReady === false || S.mini !== miniOwner || !node.isConnected) return;
        if (!reviewTargetIsCurrent(target)) {
          cancelMini();
          toast('That review target changed. Open Redo again on the current point.', { kind: 'warn' });
          return;
        }
        const txt = ta.value.trim();
        if (!txt && !redoVoiceNote && !redoVariants) { ta.focus(); return; }
        close(false);
        const nextVerdict = { verdict: 'redo', redoText: txt, redoVoiceNote };
        if (redoVariants) {
          nextVerdict.redoAbcRequest = { mode: 'model', count: redoVariantCount };
        }
        const saved = recordVerdict(nextVerdict, target);
        if (!saved && redoVoiceNote?.path !== originalRedoVoiceNote?.path) deleteVoiceNote(redoVoiceNote);
      } finally {
        saveBusy = false;
        if (S.mini === miniOwner && node.isConnected) save.disabled = false;
      }
    });
    miniOwner = {
      node, ta, micBtn, mic, target, close: cancelMini, cancel: cancelMini,
      dictationHotkeyReady() { return ta.value === ''; },
    };
    S.mini = miniOwner;
    ta.focus();
  }

  // --- send verdicts ----------------------------------------------------------
  async function sendVerdicts() {
    if (S.mini) {
      toast('Finish or cancel the open Redo note before sending verdicts.', { kind: 'warn' });
      return;
    }
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
      S.agentWakePending = true;
      syncTabTitle();
      toast('Verdicts sent - the ' + EMOJI + ' agent takes it from here.');
      updateBar();
      pollNow();
    } catch (e) {
      if (e.status === 409) {
        toast('The round changed underneath you - refreshing state.', { kind: 'error' });
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
  const dictationKeyHold = { code: '' };
  window.addEventListener('keydown', (e) => {
    const t = e.composedPath ? e.composedPath()[0] : e.target;
    // t can be window/document for programmatic dispatch - contains() would throw
    const isNode = t instanceof Node;
    const editable = isNode && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' ||
      t.tagName === 'SELECT' || t.isContentEditable);
    const heldDictationAction = dictationKeyAction(
      e, HOTKEY_DICTATE, dictationKeyHold, false
    );
    if (heldDictationAction === 'suppress') {
      e.preventDefault();
      e.stopPropagation();
      return;
    }
    if (e.repeat) return;

    // THE toggle - deliberately the only one. Matched by CODE so a Hebrew (or
    // any) layout can't move it. Editable fields always win, including Webkit's
    // own feedback textarea: C must remain a normal typed character there.
    if (e.code === HOTKEY_TOGGLE && !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey) {
      if (editable) return;
      e.preventDefault();
      e.stopPropagation();
      // With a review waiting, the key walks into it rather than just unhiding -
      // this was the corner button's job before it was removed, and the offer
      // toast is dismissable, so without this a dismissed toast would strand you.
      if (S.phase === 'reviewing' && !S.reviewing && S.review && S.batch) enterReview({});
      else toggleMode();
      return;
    }

    // Dictation toggle. Space belongs to dictation only while the active Webkit
    // note is empty. As soon as content exists it is an ordinary typed space;
    // deleting the content makes the empty-note shortcut available again.
    // Other editable fields always win, and modifiers keep browser shortcuts.
    if (e.code === HOTKEY_DICTATE && !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey) {
      if (S.mode !== 'feedback') return;     // overlay hidden: don't dictate into an invisible card
      const holder = S.mini || S.card;      // the redo mini-input wins while open
      if (!holder || !holder.micBtn || holder.micBtn.hidden) return;
      if (!holder.dictationHotkeyReady || !holder.dictationHotkeyReady()) return;
      if (!dictationTargetAllowsActivation(t, holder.ta, editable)) return;
      if (dictationKeyAction(e, HOTKEY_DICTATE, dictationKeyHold, true) !== 'activate') return;
      e.preventDefault();
      e.stopPropagation();
      holder.micBtn.click();                // reuse the button's own start/stop path
      return;
    }

    // Escape only ever CANCELS. It used to double-tap into a mode toggle, but the
    // toggle key is deliberately the single way to switch modes now.
    if (e.key === 'Escape') {
      if (S.mode === 'feedback') {
        if (S.drag) {
          S.drag.cancel && S.drag.cancel();
          S.drag = null;
          e.preventDefault();
          e.stopPropagation();
          return;
        }
        if (S.mini) {
          S.mini.close();
          e.preventDefault();
          e.stopPropagation();
          return;
        }
        if (S.card) {
          S.card.cancel();
          e.preventDefault();
          e.stopPropagation();
          return;
        }
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
  window.addEventListener('keyup', (event) => {
    if (dictationKeyAction(event, HOTKEY_DICTATE, dictationKeyHold, false) !== 'suppress') return;
    event.preventDefault();
    event.stopPropagation();
  }, true);
  window.addEventListener('blur', () => { dictationKeyHold.code = ''; });

  // ===== boot ================================================================
  // flush pending debounced writes when the tab goes away mid-edit
  window.addEventListener('pagehide', () => {
    savePoints.flush();
    if (S.card) { S.card.draft.text = S.card.ta.value; LS.setJSON('wk:card', S.card.draft); }
  });

  async function boot() {
    await installCss();

    // BEFORE|AFTER navigation left us a scroll position - restore it exactly
    const sc = SS.getJSON('wk:scroll', null);
    if (sc && sc.path === logicalPath()) {
      SS.remove('wk:scroll');
      window.scrollTo(sc.x, sc.y);
      // some pages relayout late (fonts, images) - re-assert once
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
        // already gone - hand off to the live AFTER document at the same spot
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
})().catch((error) => {
  console.error('[wk] overlay failed to initialize:', error);
});
