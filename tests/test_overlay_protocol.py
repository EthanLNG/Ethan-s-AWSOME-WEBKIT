import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "webkit" / "overlay" / "overlay.js"
OVERLAY_CSS = ROOT / "webkit" / "overlay" / "overlay.css"
QA_FIXTURE = ROOT / "tests" / "fixtures" / "overlay-qa.html"


class OverlayProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = OVERLAY.read_text(encoding="utf-8")
        cls.css = OVERLAY_CSS.read_text(encoding="utf-8")
        cls.qa_fixture = QA_FIXTURE.read_text(encoding="utf-8")
        start = cls.source.index("  function isOverlayScript")
        end = cls.source.index("  // ===== end pure protocol helpers")
        cls.pure_helpers = cls.source[start:end]

    def run_node(self, body):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable")
        exports = (
            self.pure_helpers
            + "\nreturn { isOverlayScript, findOverlayScript, makeStore, "
            + "safeWindowStorage, apiRequestOptions, singleFlight, modifierKeyLabel, "
            + "overlayScriptHandshake, findOverlayHandshake, findOverlayHandshakes, "
            + "claimOverlayInstance, authenticateOverlayToken, "
            + "authenticateOverlayHandshake, "
            + "queuedPointKey, queuedTombstoneKey, validQueuedPoint, "
            + "normalizeQueuedPointNumbers, readQueuedPointState, "
            + "hasQueuedTombstoneCapacity, pointRevision, pendingReviewPoints, "
            + "anchorFitScore, reanchorRect, tabActivityMode, tabPollDelay, "
            + "MAX_QUEUED_TOMBSTONES };"
        )
        program = "const H = new Function(%s)();\n%s" % (json.dumps(exports), body)
        result = subprocess.run(
            [node, "-"],
            input=program,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_script_discovery_uses_current_or_last_real_overlay_script(self):
        self.run_node(
            r"""
const assert = require('assert');
const page = 'http://127.0.0.1:5311/page';
function script(src, color) {
  return {
    tagName: 'script',
    dataset: { wkColor: color },
    getAttribute(name) { return name === 'src' ? src : null; },
  };
}
const decoy = script('/assets/app.js', 'decoy');
const first = script('/__wk/overlay.js?first=1', 'blue');
const foreign = script('http://example.test/__wk/overlay.js', 'foreign');
const last = script('http://127.0.0.1:5311/__wk/overlay.js', 'red');
assert.strictEqual(H.findOverlayScript(
  { currentScript: decoy, scripts: [first, foreign, last, decoy] }, page
), last);
assert.strictEqual(H.findOverlayScript(
  { currentScript: first, scripts: [first, last] }, page
), first);
assert.strictEqual(H.findOverlayScript(
  { currentScript: decoy, scripts: [decoy, foreign] }, page
), null);
"""
        )

    def test_invalid_early_overlay_script_cannot_claim_loaded_state(self):
        self.run_node(
            r"""
const assert = require('assert');
const page = 'http://127.0.0.1:5311/page';
function script(dataset, options = {}) {
  const attrs = new Map([
    ['src', options.src || '/__wk/overlay.js'],
    ['nonce', options.nonce || (dataset && dataset.wkNonce) || ''],
  ]);
  if (options.defer !== false) attrs.set('defer', '');
  if (options.nomodule) attrs.set('nomodule', '');
  return {
    tagName: 'script', dataset: dataset || {}, nonce: attrs.get('nonce'),
    getAttribute(name) { return attrs.has(name) ? attrs.get(name) : null; },
    hasAttribute(name) { return attrs.has(name); },
  };
}
const validDataset = {
  wkColor: 'blue',
  wkProject: 'project-0123456789abcdef',
  wkToken: 't'.repeat(32),
  wkNonce: 'n'.repeat(32),
  wkTrustedTypesPolicy: 'wk-overlay-test',
  wkEmoji: 'blue-dot',
  wkMode: 'after',
  wkDictationMode: 'speech',
  wkInteractionMode: 'browse-default',
  wkBeforePrefix: '',
};

const bare = script({}, { defer: false, nonce: '' });
const firstWindow = {};
const firstDocument = { currentScript: bare, scripts: [bare] };
assert.strictEqual(
  H.claimOverlayInstance(firstWindow, H.findOverlayHandshake(firstDocument, page)),
  null
);
assert.strictEqual(firstWindow.__wkOverlayLoaded, undefined);

const real = script(validDataset);
firstDocument.currentScript = real;
firstDocument.scripts.push(real);
const claimed = H.claimOverlayInstance(
  firstWindow, H.findOverlayHandshake(firstDocument, page)
);
assert.strictEqual(claimed.script, real);
assert.strictEqual(claimed.dataset.wkToken, validDataset.wkToken);
assert.strictEqual(firstWindow.__wkOverlayLoaded, true);
assert.strictEqual(
  H.claimOverlayInstance(firstWindow, H.findOverlayHandshake(firstDocument, page)),
  null
);

const fakeDataset = { ...validDataset, wkToken: 'f'.repeat(32) };
const fake = script(fakeDataset);
const secondWindow = {};
const secondDocument = { currentScript: fake, scripts: [fake] };
(async () => {
  const authenticate = async (path, options) => ({
    status: options.headers['X-WK-Token'] === validDataset.wkToken ? 204 : 403,
  });
  const fakeHandshake = H.findOverlayHandshake(secondDocument, page);
  assert.strictEqual(
    await H.authenticateOverlayToken(authenticate, fakeHandshake.dataset.wkToken),
    false
  );
  assert.strictEqual(secondWindow.__wkOverlayLoaded, undefined);
  secondDocument.currentScript = real;
  secondDocument.scripts.push(real);
  secondDocument.currentScript = fake;
  const realHandshake = await H.authenticateOverlayHandshake(
    authenticate, H.findOverlayHandshakes(secondDocument, page)
  );
  assert.strictEqual(realHandshake.script, real);
  assert.strictEqual(
    H.claimOverlayInstance(secondWindow, realHandshake).dataset.wkToken,
    validDataset.wkToken
  );
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        )

    def test_modifier_hint_is_platform_aware(self):
        self.run_node(
            r"""
const assert = require('assert');
for (const platform of ['MacIntel', 'macOS', 'iPhone', 'iPad']) {
  assert.strictEqual(H.modifierKeyLabel(platform), '⌥');
}
for (const platform of ['Win32', 'Linux x86_64', 'Android', '', null]) {
  assert.strictEqual(H.modifierKeyLabel(platform), 'Alt');
}
"""
        )

    def test_active_batch_points_and_overlay_input_survive_refresh_boundaries(self):
        self.assertIn("S.batch.points", self.source)
        self.assertIn("'added point ' + p.number + ' is saved and waiting - click to edit'", self.source)
        self.assertIn("submittedPoints: []", self.source)
        self.assertIn("B.pending = el('span', 'wk-pending-count')", self.source)
        self.assertNotIn("if (!S.reviewing && S.batch", self.source)
        self.assertIn("if (!S.card) renderPins();", self.source)
        self.assertIn("root.addEventListener(type, (event) => event.stopPropagation())", self.source)
        for event_name in ("keydown", "keyup", "keypress", "beforeinput", "input"):
            self.assertIn("'{}'".format(event_name), self.source)
        self.assertIn(".wk-pin-rect.submitted", self.css)
        self.assertIn(".wk-pin.submitted", self.css)
        self.assertIn("'hold ' + MODIFIER_LABEL + ' + drag to mark a spot'", self.source)

    def test_points_added_during_review_stay_pending_and_deduplicated(self):
        self.run_node(
            r"""
const assert = require('assert');
const p1 = { id: 'p1', number: 1 };
const p2 = { id: 'p2', number: 2, source: 'server' };
const p3 = { id: 'p3', number: 3, source: 'optimistic' };
const p4 = { id: 'p4', number: 4, source: 'queued' };
const pending = H.pendingReviewPoints(
  [p1, p2],
  [p1],
  [{ ...p2, source: 'stale-optimistic' }, p3],
  [{ ...p3, source: 'stale-queued' }, p4]
);
assert.deepStrictEqual(pending, [p2, p3, p4]);

const revised = H.pendingReviewPoints(
  [{ ...p1, revision: 2 }, p2],
  [{ id: 'p1', feedbackRevision: 1 }],
  [{ ...p1, revision: 3, source: 'latest' }],
  [],
  ['p1']
);
assert.deepStrictEqual(revised, [{ ...p1, revision: 3, source: 'latest' }]);

const authoritative = H.pendingReviewPoints(
  [p1, p2], [], [], [], ['p2']
);
assert.deepStrictEqual(authoritative, [p2]);
"""
        )
        self.assertIn("S.submittedPoints = pendingReviewPoints([], [], S.submittedPoints, acceptedPoints)", self.source)
        self.assertIn("openCard({ pendingPoint: p })", self.source)
        self.assertIn("api('/__wk/feedback/edit'", self.source)
        self.assertIn("pendingReviewPoints(\n      S.batch", self.source)
        self.assertIn(".wk-pending-count", self.css)

    def test_rectangle_anchoring_prefers_close_context_and_scales_stably(self):
        self.run_node(
            r"""
const assert = require('assert');
const mark = { x: 100, y: 100, w: 200, h: 80 };
const target = { x: 90, y: 90, w: 230, h: 100 };
const background = { x: 0, y: 0, w: 1920, h: 1080 };
assert(H.anchorFitScore(mark, target) > H.anchorFitScore(mark, background));
assert(H.anchorFitScore(mark, background) < 0.08);
assert.deepStrictEqual(
  H.reanchorRect(mark, target, { x: 180, y: 150, w: 460, h: 200 }),
  { x: 200, y: 170, w: 400, h: 160 }
);
assert.deepStrictEqual(
  H.reanchorRect(mark, target, { x: 180, y: 150, w: 2000, h: 100 }),
  { x: 190, y: 160, w: 200, h: 80 }
);
"""
        )
        self.assertIn("right[1].score - left[1].score", self.source)
        self.assertIn("const MIN_ANCHOR_FIT = 0.08", self.source)
        self.assertIn("rectContexts: rects.map((rect) => captureContext(rect))", self.source)
        self.assertIn("if (contexts.length) contexts[0].role = 'primary'", self.source)
        self.assertIn("const pendingEditingRects = pendingGeometry", self.source)
        self.assertIn("x: rect.x + scrollX", self.source)

    def test_tab_title_tracks_work_and_background_review_ready_state(self):
        self.run_node(
            r"""
const assert = require('assert');
for (const phase of ['awaiting_agent', 'verdicts_sent', 'transitioning']) {
  assert.strictEqual(H.tabActivityMode(phase), 'working');
}
assert.strictEqual(H.tabActivityMode('reviewing'), 'review-ready');
assert.strictEqual(H.tabActivityMode('collecting'), 'normal');
assert.strictEqual(H.tabPollDelay('working', 'feedback', true), 5000);
assert.strictEqual(H.tabPollDelay('review-ready', 'feedback', true), 0);
assert.strictEqual(H.tabPollDelay('normal', 'feedback', false), 2000);
assert.strictEqual(H.tabPollDelay('normal', 'evaluate', false), 15000);
"""
        )
        self.assertIn("EMOJI + ' Working'", self.source)
        self.assertIn("' Review ready'", self.source)
        self.assertIn("mode === 'review-ready' && !document.hidden", self.source)
        self.assertIn("if (document.hidden) schedulePoll(true)", self.source)
        self.assertIn("S.agentWakePending = true", self.source)
        self.assertIn("if (S.reviewing) S.sentVerdicts = S.phase !== 'reviewing'", self.source)

    def test_edit_card_actions_stay_inside_the_bounded_card(self):
        self.assertIn("'wk-row wk-actions wk-edit-actions'", self.source)
        self.assertIn("grid-template-columns: auto minmax(0, 1fr) auto auto auto", self.css)
        self.assertIn(".wk-edit-actions .wk-btn", self.css)
        self.assertIn(".wk-edit-actions > :nth-child(5) { grid-column: 5; }", self.css)
        self.assertIn("padding-inline: 8px", self.css)
        self.assertIn("white-space: nowrap", self.css)

    def test_browser_fixture_detects_space_leaking_into_the_page(self):
        self.assertIn("pageSpaceKeydowns: 0", self.qa_fixture)
        self.assertIn("window.addEventListener('keydown'", self.qa_fixture)
        self.assertIn("event.code !== 'Space'", self.qa_fixture)

    def test_namespaced_store_separates_projects_and_colors_and_persists(self):
        self.run_node(
            r"""
const assert = require('assert');
class Backing {
  constructor() { this.values = new Map(); }
  get length() { return this.values.size; }
  key(index) { return [...this.values.keys()][index] ?? null; }
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}
const backing = new Backing();
const aBlue = H.makeStore(backing, 'wk:project-a:blue:');
const aBlueReloaded = H.makeStore(backing, 'wk:project-a:blue:');
const aRed = H.makeStore(backing, 'wk:project-a:red:');
const bBlue = H.makeStore(backing, 'wk:project-b:blue:');
aBlue.setJSON('wk:points', [{ id: 'a' }]);
aRed.setJSON('wk:points', [{ id: 'red' }]);
bBlue.setJSON('wk:points', [{ id: 'b' }]);
assert.deepStrictEqual(aBlueReloaded.getJSON('wk:points', []), [{ id: 'a' }]);
assert.deepStrictEqual(aRed.getJSON('wk:points', []), [{ id: 'red' }]);
assert.deepStrictEqual(bBlue.getJSON('wk:points', []), [{ id: 'b' }]);
assert.deepStrictEqual(aBlueReloaded.keys(), ['wk:points']);
assert.strictEqual(aBlue.logicalKey('wk:project-a:blue:points'), 'wk:points');
assert.strictEqual(aBlue.logicalKey('wk:project-a:red:points'), null);
assert.strictEqual(backing.getItem('wk:project-a:blue:points'), '[{"id":"a"}]');
"""
        )

    def test_storage_property_failure_uses_inert_store(self):
        self.run_node(
            r"""
const assert = require('assert');
global.window = {};
Object.defineProperty(window, 'localStorage', {
  get() { throw new Error('blocked'); },
});
const backing = H.safeWindowStorage('localStorage');
const store = H.makeStore(backing, 'wk:p:blue:');
assert.strictEqual(store.set('wk:mode', 'feedback'), false);
assert.strictEqual(store.get('wk:mode'), 'feedback');
"""
        )

    def test_failed_store_writes_and_removals_override_stale_durable_values(self):
        self.run_node(
            r"""
const assert = require('assert');
class FlakyBacking {
  constructor() {
    this.values = new Map([['wk:p:blue:mode', 'old']]);
    this.failWrites = false;
  }
  get length() { return this.values.size; }
  key(index) { return [...this.values.keys()][index] ?? null; }
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  setItem(key, value) {
    if (this.failWrites) throw new Error('quota');
    this.values.set(key, String(value));
  }
  removeItem(key) {
    if (this.failWrites) throw new Error('quota');
    this.values.delete(key);
  }
}
const backing = new FlakyBacking();
const store = H.makeStore(backing, 'wk:p:blue:');
assert.strictEqual(store.get('wk:mode'), 'old');

backing.failWrites = true;
assert.strictEqual(store.set('wk:mode', 'new'), false);
assert.strictEqual(backing.getItem('wk:p:blue:mode'), 'old');
assert.strictEqual(store.get('wk:mode'), 'new');
assert.deepStrictEqual(store.keys(), ['wk:mode']);

backing.failWrites = false;
assert.strictEqual(store.set('wk:mode', 'new'), true);
assert.strictEqual(store.get('wk:mode'), 'new');

backing.failWrites = true;
assert.strictEqual(store.remove('wk:mode'), false);
assert.strictEqual(backing.getItem('wk:p:blue:mode'), 'new');
assert.strictEqual(store.get('wk:mode'), null);
assert.deepStrictEqual(store.keys(), []);

backing.failWrites = false;
assert.strictEqual(store.remove('wk:mode'), true);
assert.strictEqual(store.get('wk:mode'), null);
"""
        )

    def test_per_point_queue_converges_across_tabs_without_lost_additions(self):
        self.run_node(
            r"""
const assert = require('assert');
class Backing {
  constructor() { this.values = new Map(); }
  get length() { return this.values.size; }
  key(index) { return [...this.values.keys()][index] ?? null; }
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}
function point(id, number, createdAt) {
  return {
    id, number, createdAt, page: '/index.html', text: id,
    rect: { x: 1, y: 2, w: 30, h: 40 },
  };
}
const backing = new Backing();
const tabA = H.makeStore(backing, 'wk:project:blue:');
const tabB = H.makeStore(backing, 'wk:project:blue:');
const a = point('point-a', 1, '2026-08-19T10:00:00Z');
const b = point('point-b', 1, '2026-08-19T10:00:00Z');

// Both tabs read an empty queue and then commit independent keys with the same
// proposed display number. Neither write can overwrite the other.
assert.deepStrictEqual(H.readQueuedPointState(tabA, [], []), []);
assert.strictEqual(tabA.setJSON(H.queuedPointKey(a.id), a), true);
assert.strictEqual(tabB.setJSON(H.queuedPointKey(b.id), b), true);
for (const tab of [tabA, tabB]) {
  const state = H.readQueuedPointState(tab, [], []);
  assert.deepStrictEqual(state.map((value) => value.id), ['point-a', 'point-b']);
  assert.deepStrictEqual(state.map((value) => value.number), [1, 2]);
  assert.deepStrictEqual(
    H.readQueuedPointState(tab, [], [1, 2]).map((value) => value.number),
    [3, 4]
  );
}

// Delete linearizes before the next send snapshot. A stale rewrite of the old
// record cannot beat its tombstone.
assert.strictEqual(tabB.set(H.queuedTombstoneKey(a.id), 'deleted'), true);
assert.strictEqual(tabA.setJSON(H.queuedPointKey(a.id), a), true);
let snapshot = H.readQueuedPointState(tabA, [], []);
assert.deepStrictEqual(snapshot.map((value) => value.id), ['point-b']);

// A point committed after the snapshot remains queued when only snapshot IDs
// are retired after a successful send.
const c = point('point-c', 3, '2026-08-19T10:00:01Z');
assert.strictEqual(tabA.setJSON(H.queuedPointKey(c.id), c), true);
for (const sent of snapshot) {
  assert.strictEqual(tabB.set(H.queuedTombstoneKey(sent.id), 'sent'), true);
}
assert.deepStrictEqual(
  H.readQueuedPointState(tabA, [], []).map((value) => value.id),
  ['point-c']
);
"""
        )

    def test_tombstone_capacity_is_bounded_and_storage_failures_keep_memory(self):
        self.run_node(
            r"""
const assert = require('assert');
class Backing {
  constructor() { this.values = new Map(); }
  get length() { return this.values.size; }
  key(index) { return [...this.values.keys()][index] ?? null; }
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}
const store = H.makeStore(new Backing(), 'wk:project:blue:');
for (let i = 0; i < H.MAX_QUEUED_TOMBSTONES; i++) {
  assert.strictEqual(store.set(H.queuedTombstoneKey('retired-' + i), 'sent'), true);
}
assert.strictEqual(
  H.hasQueuedTombstoneCapacity(store, ['retired-0'], H.MAX_QUEUED_TOMBSTONES),
  true
);
assert.strictEqual(
  H.hasQueuedTombstoneCapacity(store, ['one-more'], H.MAX_QUEUED_TOMBSTONES),
  false
);

const unavailable = {
  length: 0,
  key() { return null; },
  getItem() { return null; },
  setItem() { throw new Error('quota'); },
  removeItem() { throw new Error('quota'); },
};
const memoryOnly = H.makeStore(unavailable, 'wk:project:blue:');
assert.strictEqual(memoryOnly.set(H.queuedTombstoneKey('kept-in-memory'), 'sent'), false);
assert.strictEqual(memoryOnly.get(H.queuedTombstoneKey('kept-in-memory')), 'sent');
"""
        )
        self.assertIn("const POINT_SAVE_RETRY_MS = [100, 500, 2000]", self.source)
        self.assertIn("Feedback storage is unavailable", self.source)
        self.assertIn("Feedback history is full. The point was kept.", self.source)
        self.assertIn("Close other preview tabs, then clear this preview site storage and reload.", self.source)
        self.assertIn("navigator.locks", self.source)
        self.assertIn("window.crypto.randomUUID", self.source)

    def test_single_flight_serializes_and_coalesces_poll_requests(self):
        self.run_node(
            r"""
const assert = require('assert');
(async () => {
  let calls = 0;
  let active = 0;
  let maxActive = 0;
  let idle = 0;
  const releases = [];
  const applied = [];
  const run = H.singleFlight(() => new Promise((resolve) => {
    const call = ++calls;
    active += 1;
    maxActive = Math.max(maxActive, active);
    releases.push(() => {
      applied.push(call);
      active -= 1;
      resolve();
    });
  }), () => { idle += 1; });

  const first = run();
  const second = run();
  assert.strictEqual(first, second);
  assert.strictEqual(calls, 1);
  releases.shift()();
  await new Promise(setImmediate);
  assert.strictEqual(calls, 2);
  const third = run();
  assert.strictEqual(third, first);
  releases.shift()();
  await new Promise(setImmediate);
  assert.strictEqual(calls, 3);
  releases.shift()();
  await first;
  assert.strictEqual(maxActive, 1);
  assert.deepStrictEqual(applied, [1, 2, 3]);
  assert.strictEqual(idle, 1);
})().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
"""
        )

    def test_authenticated_gets_receive_the_overlay_token(self):
        self.run_node(
            r"""
const assert = require('assert');
const token = 'private-token';
assert.deepStrictEqual(
  H.apiRequestOptions('/__wk/state?known=rev-1', undefined, token),
  { cache: 'no-store', headers: { 'X-WK-Token': token } }
);
assert.deepStrictEqual(
  H.apiRequestOptions('/__wk/state', undefined, token),
  { cache: 'no-store', headers: { 'X-WK-Token': token } }
);
assert.deepStrictEqual(
  H.apiRequestOptions('/__wk/handshake', undefined, token),
  { cache: 'no-store', headers: { 'X-WK-Token': token } }
);
for (const path of [
  '/__wk/overlay.css',
  '/__wk/overlay.js',
  '/__wk/before/index.html',
  '/__wk/stateful',
]) {
  assert.deepStrictEqual(H.apiRequestOptions(path, undefined, token), { cache: 'no-store' });
}
assert.deepStrictEqual(
  H.apiRequestOptions('/__wk/feedback', { version: 1 }, token),
  {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-WK-Token': token },
    body: '{"version":1}',
  }
);
"""
        )

        self.assertIn(
            "const options = apiRequestOptions(path, body, MUTATION_TOKEN)",
            self.source,
        )
        self.assertIn("fetch('/__wk/overlay.css', { cache: 'no-store' })", self.source)
        self.assertIn("fetch(BEFORE_PREFIX + context.page, { cache: 'no-store' })", self.source)
        self.assertIn("frame.src = BEFORE_PREFIX + logicalPath()", self.source)

    def test_poll_and_storage_events_use_the_protocol_helpers(self):
        self.assertIn("const pollNow = singleFlight(async () =>", self.source)
        self.assertIn("const key = LS.logicalKey(e.key);", self.source)
        self.assertNotIn("document.querySelector('script[data-wk-color]')", self.source)

    def test_voice_limits_and_synchronous_speech_failure_are_visible(self):
        size = re.search(r"const VOICE_MAX_BYTES = ([^;]+);", self.source)
        self.assertIsNotNone(size)
        self.assertEqual(eval(size.group(1), {"__builtins__": {}}, {}), 24 * 1024 * 1024)
        self.assertLess(eval(size.group(1), {"__builtins__": {}}, {}), 25 * 1024 * 1024)
        self.assertIn("VOICE_MAX_DURATION_MS", self.source)
        self.assertIn("Voice note reached the 5 minute recording limit.", self.source)
        self.assertIn("Voice note reached the 24 MB recording limit.", self.source)
        self.assertIn("failSynchronousStart(error);", self.source)
        self.assertIn("Dictation could not start. Click to retry.", self.source)

    def test_accessibility_and_sandbox_contract(self):
        required = [
            "statusChip.setAttribute('aria-live', 'polite')",
            "toasts.setAttribute('aria-live', 'polite')",
            "bar.setAttribute('aria-label', 'Feedback review controls')",
            "node.setAttribute('aria-labelledby', title.id)",
            "node.setAttribute('aria-labelledby', label.id)",
            "B.prev.setAttribute('aria-label', 'Previous review point')",
            "d.setAttribute('aria-label', 'Review point '",
            "micBtn.setAttribute('aria-label', MIC_ARIA_LABEL)",
            "frame.setAttribute('sandbox', 'allow-same-origin')",
            "abcHead.setAttribute('aria-controls', abcBody.id)",
            "abcHead.setAttribute('aria-expanded', String(draft.abc.open))",
            "segModel.setAttribute('aria-pressed'",
            "segUser.setAttribute('aria-pressed'",
            "B.before.setAttribute('aria-pressed'",
            "B.after.setAttribute('aria-pressed'",
            "btn.setAttribute('aria-pressed', String(selected))",
        ]
        for fragment in required:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.source)
        self.assertNotIn("allow-same-origin allow-scripts", self.source)
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.css)
        self.assertIn("animation-iteration-count: 1 !important", self.css)
        self.assertIn("transition-duration: 0.01ms !important", self.css)

    def test_trusted_types_and_csp_nonce_use_only_safe_dom_construction(self):
        self.assertNotIn(".innerHTML", self.source)
        self.assertNotIn("insertAdjacentHTML", self.source)
        self.assertNotIn("createContextualFragment", self.source)
        self.assertIn("window.trustedTypes.createPolicy(TRUSTED_TYPES_POLICY_NAME", self.source)
        self.assertIn("parseFromString(asTrustedHTML(html), 'text/html')", self.source)
        self.assertIn("cssStyleNode.setAttribute('nonce', CSP_NONCE)", self.source)
        self.assertIn("document.createElementNS(SVG_NS, tag)", self.source)
        self.assertIn("sendBtn.replaceChildren()", self.source)

    def test_touch_targets_focus_and_narrow_layout_are_explicit(self):
        pin = re.search(r"\.wk-pin \{(?P<body>.*?)\n\}", self.css, re.S)
        self.assertIsNotNone(pin)
        self.assertIn("width: 24px", pin.group("body"))
        self.assertIn("height: 24px", pin.group("body"))
        frozen_delete = re.search(
            r"\.wk-frozen-delete \{(?P<body>.*?)\n\}", self.css, re.S
        )
        self.assertIsNotNone(frozen_delete)
        self.assertIn("width: 24px", frozen_delete.group("body"))
        self.assertIn("height: 24px", frozen_delete.group("body"))
        dot = re.search(r"\.wk-dot-i \{(?P<body>.*?)\n\}", self.css, re.S)
        self.assertIsNotNone(dot)
        self.assertIn("width: 24px", dot.group("body"))
        self.assertIn("height: 24px", dot.group("body"))
        self.assertIn("button:focus-visible", self.css)
        self.assertIn("max-height: calc(100vh - 16px)", self.css)
        self.assertIn("B.abcChip.setAttribute('aria-disabled', 'true')", self.source)
        self.assertIn(".wk-mini {\n    left: 8px;\n    right: 8px;", self.css)
        self.assertIn(".wk-hint {\n    max-width: calc(100vw - 32px);", self.css)
        self.assertIn("max-width: min(280px, 35vw);\n  overflow-x: auto;", self.css)
        self.assertIn("x: box.x - 12, y: box.y - 12, w: 24, h: 24", self.source)
        self.assertIn("pin.setAttribute('aria-label'", self.source)

    def test_swap_cache_and_async_work_are_bound_to_active_review(self):
        required = [
            "docKey: '',",
            "const key = context.beforeRef + '\\n' + context.page;",
            "generation: ++swapGeneration",
            "batchId: S.reviewBatchId",
            "round: S.reviewRound",
            "pointId: pt && pt.id",
            "beforeRef: S.review?.beforeRef || ''",
            "requireCurrentSwap(context);",
            "if (e.wkStaleSwap) return;",
        ]
        for fragment in required:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.source)
        self.assertGreaterEqual(self.source.count("requireCurrentSwap(context);"), 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
