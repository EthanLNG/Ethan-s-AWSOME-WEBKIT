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
            + "anchorFitScore, anchorCoverageScore, anchorContextScore, "
            + "reanchorRect, reviewScrollTarget, feedbackToolsBottom, "
            + "feedbackPointListSignature, ownedSurfaceSessionCanFinish, viewportStateAttributeName, "
            + "surfaceStateAttributeCapturable, surfaceStateClassCapturable, "
            + "surfaceStateMatchStatus, surfaceStateValuesMatch, "
            + "surfaceStateSignalHasActivation, surfaceStateSignalHasEvidence, "
            + "aggregateSurfaceStateStatus, "
            + "surfaceCandidateSpatialScore, effectiveStyleChainVisible, sameRectGeometry, "
            + "surfaceAnchorOwnsRoles, surfaceAnimationRunning, surfaceAnimationCapturable, "
            + "surfaceAnimationTarget, surfaceNodeAnimationRole, surfaceNodeAnimationRelation, "
            + "surfaceMutationRelevant, surfaceChildListRelevant, surfaceMarkerIdentity, "
            + "retainCurrentSurfaceMarkerEntries, surfaceTrackedNodeSupportsRoles, pointAnchorMode, "
            + "preferredSurfaceCandidate, rankedCaptureEntries, "
            + "rectUsesLegacyViewportAnchor, surfaceUsesSeparatedGeometry, "
            + "resolvedRectAnchorMode, "
            + "firstUnderlayAnchorMode, surfaceGeometryEntryAllowed, "
            + "surfaceGeometryEntryPriority, "
            + "capturedSurfaceGeometrySelector, "
            + "persistedRectForDraft, primaryRectIndex, visualSurfaceActive, "
            + "sectionLocalFixedScopeEligible, "
            + "surfaceScopeRectActive, "
            + "tabActivityMode, tabPollDelay, keyboardActivationTarget, "
            + "dictationTargetAllowsActivation, dictationKeyAction, "
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
assert.strictEqual(
  H.overlayScriptHandshake(script({ ...validDataset, wkTheme: 'white' }), page).dataset.wkTheme,
  'white'
);
assert.strictEqual(
  H.overlayScriptHandshake(script({ ...validDataset, wkTheme: 'sepia' }), page),
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
const tinyMark = { x: 1004, y: 5391, w: 23, h: 30 };
const stickyScene = { x: 0, y: 5058, w: 1920, h: 958 };
const pageShell = { x: 0, y: 0, w: 1920, h: 17026 };
const viewport = { w: 1920, h: 902 };
assert(H.anchorFitScore(tinyMark, stickyScene) < 0.08);
assert.strictEqual(H.anchorCoverageScore(tinyMark, stickyScene), 1);
assert(H.anchorContextScore(tinyMark, stickyScene, viewport) >= 0.75);
assert.strictEqual(H.anchorContextScore(tinyMark, pageShell, viewport), 0);
assert.deepStrictEqual(
  H.reanchorRect(mark, target, { x: 180, y: 150, w: 460, h: 200 }),
  { x: 200, y: 170, w: 400, h: 160 }
);
assert.deepStrictEqual(
  H.reanchorRect(mark, target, { x: 180, y: 150, w: 2000, h: 100 }),
  { x: 190, y: 160, w: 200, h: 80 }
);
for (const name of [
  'data-state', 'data-current-step', 'data-screen-mode', 'data-scene',
  'aria-hidden', 'aria-selected'
]) assert.strictEqual(H.viewportStateAttributeName(name), true, name);
for (const name of ['data-copy', 'data-testid', 'aria-label', 'style']) {
  assert.strictEqual(H.viewportStateAttributeName(name), false, name);
}
for (const [name, value] of [
  ['aria-current', 'page'], ['aria-selected', 'true'],
  ['aria-expanded', 'true'], ['aria-pressed', 'mixed'], ['aria-hidden', 'false'],
  ['data-scene', '0'], ['data-state', 'setup'],
]) assert.strictEqual(H.surfaceStateAttributeCapturable(name, value), true, `${name}=${value}`);
for (const [name, value] of [
  ['aria-current', 'false'], ['aria-selected', 'false'],
  ['aria-expanded', 'false'], ['aria-pressed', 'false'], ['aria-hidden', 'true'],
]) assert.strictEqual(H.surfaceStateAttributeCapturable(name, value), false, `${name}=${value}`);
for (const token of [
  'active', 'is-active', 'current', 'visible', 'has-open',
  'lit', 'is-lit',
]) {
  assert.strictEqual(H.surfaceStateClassCapturable(token), true, token);
}
for (const token of [
  'inactive', 'is-off', 'hidden', 'closed', 'collapsed', 'is-mobile', 'is-loaded',
  'ready', 'is-ready', 'revealed', 'is-entered', 'is-playing',
]) {
  assert.strictEqual(H.surfaceStateClassCapturable(token), false, token);
}
const capturedState = {
  attrs: { 'data-state': 'setup' }, classes: ['is-active'],
};
assert.strictEqual(H.surfaceStateValuesMatch(
  capturedState, (name) => ({ 'data-state': 'setup' })[name], (name) => name === 'is-active'
), true);
assert.strictEqual(H.surfaceStateValuesMatch(
  capturedState, () => 'out', () => true
), false);
assert.strictEqual(H.surfaceStateMatchStatus(
  capturedState, (name) => ({ 'data-state': 'setup' })[name],
  (name) => name === 'is-active'
), 'match');
assert.strictEqual(H.surfaceStateMatchStatus(
  capturedState, () => 'out', () => true
), 'mismatch');
assert.strictEqual(H.surfaceStateMatchStatus(
  capturedState, () => null, () => true
), 'unknown');
assert.strictEqual(H.surfaceStateMatchStatus(
  capturedState, () => null, () => false
), 'mismatch');
assert.strictEqual(H.surfaceStateMatchStatus(
  { attrs: {}, classes: ['is-lit'] }, () => null, () => false
), 'mismatch');
assert.strictEqual(H.surfaceStateMatchStatus(
  { attrs: { 'aria-hidden': 'true' }, classes: [] }, () => 'false', () => false
), 'unknown');
assert.strictEqual(H.surfaceStateMatchStatus(
  { attrs: {}, classes: ['is-off'] }, () => null, () => false
), 'unknown');
assert.strictEqual(H.surfaceStateMatchStatus(
  { attrs: { 'aria-hidden': 'true' }, classes: ['is-active'] },
  () => 'true', (name) => name === 'is-active'
), 'match');
assert.strictEqual(H.aggregateSurfaceStateStatus(['match', 'match']), 'match');
assert.strictEqual(H.aggregateSurfaceStateStatus(['match', 'unknown']), 'unknown');
assert.strictEqual(H.aggregateSurfaceStateStatus(['mismatch', 'unknown']), 'mismatch');
assert.strictEqual(H.aggregateSurfaceStateStatus([]), 'unknown');
assert.strictEqual(H.surfaceStateSignalHasActivation({
  attrs: { 'aria-selected': 'true', 'data-index': '2' }, classes: [],
}), true);
assert.strictEqual(H.surfaceStateSignalHasActivation({
  attrs: { 'data-index': '2', 'data-scene': 'intro' }, classes: [],
}), false);
assert.strictEqual(H.surfaceStateSignalHasEvidence({
  attrs: { 'aria-hidden': 'true' }, classes: [],
}), false);
assert.strictEqual(H.surfaceStateSignalHasEvidence({
  attrs: {}, classes: ['is-off'],
}), false);
assert.strictEqual(H.surfaceStateSignalHasEvidence({
  attrs: { 'data-index': '2' }, classes: [],
}), true);
assert.strictEqual(H.surfaceStateSignalHasEvidence({
  attrs: { 'aria-hidden': 'true' }, classes: ['is-active'],
}), true);

const legacyNegativeOnly = [
  { attrs: { 'aria-hidden': 'true' }, classes: [] },
  { attrs: {}, classes: ['is-off'] },
].filter(H.surfaceStateSignalHasEvidence);
assert.deepStrictEqual(legacyNegativeOnly, []);
assert.strictEqual(H.visualSurfaceActive(
  true, legacyNegativeOnly.length > 0, false, 'doc', false, 400, 100, 800
), true);

const mixedLegacySignals = [
  { attrs: { 'aria-hidden': 'true' }, classes: [] },
  { attrs: { 'aria-selected': 'true' }, classes: ['is-active'] },
].filter(H.surfaceStateSignalHasEvidence);
assert.strictEqual(mixedLegacySignals.length, 1);
const mixedLegacyStatus = H.aggregateSurfaceStateStatus(mixedLegacySignals.map((signal) =>
  H.surfaceStateMatchStatus(
    signal,
    (name) => ({ 'aria-selected': 'true' })[name],
    (name) => name === 'is-active'
  )
));
assert.strictEqual(mixedLegacyStatus, 'match');
assert.strictEqual(H.visualSurfaceActive(
  true, true, mixedLegacyStatus === 'match', 'sticky', false, 900, 100, 800
), true);
assert.strictEqual(H.surfaceStateSignalHasActivation({
  attrs: {}, classes: ['is-active'],
}), true);
assert.strictEqual(H.surfaceStateValuesMatch(
  { attrs: {}, classes: ['active state'] }, () => null, () => {
    throw new Error('DOMTokenList would reject this token');
  }
), false);
assert.strictEqual(H.effectiveStyleChainVisible([
  { display: 'block', visibility: 'visible', contentVisibility: 'visible', opacity: '1' },
  { display: 'block', visibility: 'visible', contentVisibility: 'visible', opacity: '0' },
]), false);
assert.strictEqual(H.effectiveStyleChainVisible([
  { display: 'block', visibility: 'visible', contentVisibility: 'visible', opacity: '0.5' },
  { display: 'block', visibility: 'visible', contentVisibility: 'visible', opacity: '0.5' },
]), true);
assert.strictEqual(H.visualSurfaceActive(
  true, true, false, 'sticky', false, 7200, 5107, 900
), false);
assert.strictEqual(H.visualSurfaceActive(
  true, true, false, 'sticky', true, 5107, 5107, 900
), true);
assert.strictEqual(H.visualSurfaceActive(
  false, true, true, 'sticky', true, 5107, 5107, 900
), false);
// A sticky scene with no explicit data/class state signal is still tied to its
// capture moment. Global fixed and document content remain active while visible.
assert.strictEqual(H.visualSurfaceActive(
  true, false, false, 'sticky', false, 7200, 5107, 900
), false);
assert.strictEqual(H.visualSurfaceActive(
  true, false, false, 'sticky', false, 5140, 5107, 900
), true);
assert.strictEqual(H.visualSurfaceActive(
  true, false, false, 'fixed', false, 7200, 5107, 900
), true);
assert.strictEqual(H.visualSurfaceActive(
  true, false, false, 'doc', false, 7200, 5107, 900
), true);
// Section-local fixed visuals must follow their ordinary flow scope. An
// unresolved scope preserves the legacy global-fixed behavior, while the
// current review point can still recover at its exact capture moment.
assert.strictEqual(H.visualSurfaceActive(
  true, false, false, 'fixed', false, 7200, 5107, 900, false
), false);
assert.strictEqual(H.visualSurfaceActive(
  true, false, false, 'fixed', false, 7200, 5107, 900, true
), false);
assert.strictEqual(H.visualSurfaceActive(
  true, false, false, 'fixed', false, 5140, 5107, 900, true
), true);
assert.strictEqual(H.visualSurfaceActive(
  true, false, false, 'fixed', true, 5107, 5107, 900, false
), true);
assert.strictEqual(H.surfaceScopeRectActive(
  { top: 920, bottom: 2100, height: 1180 }, 900
), false);
assert.strictEqual(H.surfaceScopeRectActive(
  { top: -100, bottom: 1070, height: 1170 }, 900
), true);
assert.strictEqual(H.surfaceScopeRectActive(null, 900), true);
assert.strictEqual(H.sectionLocalFixedScopeEligible(true, true, true), true);
assert.strictEqual(H.sectionLocalFixedScopeEligible(true, false, true), false);
assert.strictEqual(H.sectionLocalFixedScopeEligible(true, true, false), false);
assert.strictEqual(H.sameRectGeometry(
  { x: 10, y: 20, w: 30, h: 40 }, { x: 10, y: 20, w: 30, h: 40 }
), true);
assert.strictEqual(H.sameRectGeometry(
  { x: 10, y: 20, w: 30, h: 40 }, { x: 11, y: 20, w: 30, h: 40 }
), false);
assert.strictEqual(H.surfaceMutationRelevant('style', false), false);
assert.strictEqual(H.surfaceMutationRelevant('data-copy', true), false);
assert.strictEqual(H.surfaceMutationRelevant('data-state', true), true);
assert.strictEqual(H.pointAnchorMode([], ['sticky'], false), 'sticky');
assert.strictEqual(H.pointAnchorMode([], ['fixed'], false), 'fixed');
assert.strictEqual(H.pointAnchorMode([], [], true), 'unknown');
assert.strictEqual(H.pointAnchorMode([], [], false), 'doc');
assert.strictEqual(H.rectUsesLegacyViewportAnchor('viewport', null), true);
assert.strictEqual(H.rectUsesLegacyViewportAnchor('viewport', { anchor: null }), false);
assert.strictEqual(H.rectUsesLegacyViewportAnchor('doc', null), false);
assert.strictEqual(H.surfaceUsesSeparatedGeometry({ anchor: null }), false);
assert.strictEqual(H.surfaceUsesSeparatedGeometry({
  geometrySelector: '#target', anchor: null,
}), true);
assert.strictEqual(H.resolvedRectAnchorMode(
  ['sticky'], { anchor: null }, 'doc'
), 'sticky');
assert.strictEqual(H.resolvedRectAnchorMode(
  ['sticky'], { geometrySelector: '#target', anchor: null }, 'doc'
), 'doc');
assert.strictEqual(H.resolvedRectAnchorMode([], { anchor: { mode: 'sticky' } }, 'doc'), 'sticky');
assert.strictEqual(H.resolvedRectAnchorMode(['sticky'], null, 'doc'), 'sticky');
assert.strictEqual(H.resolvedRectAnchorMode([], null, 'viewport'), 'unknown');
const normalTarget = { id: 'normal' };
const stickyBackdrop = { id: 'sticky' };
assert.strictEqual(H.firstUnderlayAnchorMode(
  [normalTarget, stickyBackdrop], () => false, (node) => node === stickyBackdrop
), 'doc');
assert.strictEqual(H.firstUnderlayAnchorMode(
  [{ id: 'overlay' }, stickyBackdrop], (node) => node.id === 'overlay',
  (node) => node === stickyBackdrop
), 'viewport');
const surfaceRoles = {
  geometrySelector: '#normal-target',
  targetSelector: '#normal-state',
  anchor: null,
};
const normalEntry = { context: { selector: '#normal-target' }, viewportAnchorMode: null };
const stickyBackdropEntry = {
  context: { selector: '#sticky-backdrop' }, viewportAnchorMode: 'sticky',
};
assert.strictEqual(H.surfaceGeometryEntryAllowed(normalEntry, surfaceRoles, 'doc', false), true);
assert.strictEqual(
  H.surfaceGeometryEntryAllowed(stickyBackdropEntry, surfaceRoles, 'doc', false), false
);
assert.strictEqual(H.surfaceGeometryEntryPriority(normalEntry, surfaceRoles), 0);
assert.strictEqual(H.surfaceGeometryEntryPriority(stickyBackdropEntry, surfaceRoles), 3);
const stickyRoles = {
  geometrySelector: '#normal-target', targetSelector: '#scene',
  anchor: { selector: '#sticky-anchor', mode: 'sticky' },
};
assert.strictEqual(H.surfaceGeometryEntryAllowed(normalEntry, stickyRoles, 'sticky', false), false);
assert.strictEqual(H.surfaceGeometryEntryAllowed(normalEntry, stickyRoles, 'sticky', true), true);
const spatialMark = { x: 100, y: 100, w: 200, h: 80 };
assert(
  H.surfaceCandidateSpatialScore(spatialMark, { x: 90, y: 90, w: 230, h: 100 }) >
  H.surfaceCandidateSpatialScore(spatialMark, { x: 0, y: 0, w: 1920, h: 1080 })
);
assert.strictEqual(
  H.surfaceCandidateSpatialScore(spatialMark, { x: 500, y: 500, w: 200, h: 80 }),
  0
);
"""
        )
        self.assertIn("rankedCaptureEntries([...rankedElements.entries()], 12, 4)", self.source)
        self.assertIn("const MIN_ANCHOR_FIT = 0.08", self.source)
        self.assertIn("sameRectGeometry(rect, draft.rectMetadataRects?.[rectIndex])", self.source)
        self.assertIn("if (contexts.length) contexts[0].role = 'primary'", self.source)
        self.assertIn("const editingDisplayRects = editingGeometry", self.source)
        self.assertIn("x: rect.x + scrollX", self.source)
        self.assertIn("underlayElementsFromPoint", self.source)
        self.assertIn("elementViewportAnchored", self.source)
        self.assertIn("rectSurfaces,", self.source)
        self.assertIn("function effectivelyVisible(node)", self.source)
        self.assertIn("geometry.active[marker.rectIndex] === false", self.source)
        self.assertIn("surfaceObserver = new MutationObserver((records) =>", self.source)
        self.assertIn("animationNodeAffectsTrackedSurface(event.target)", self.source)

    def test_pins_track_live_geometry_without_scroll_ghosts(self):
        for fragment in (
            "const geometryCache = new Map()",
            "marker.node.style.visibility = 'hidden'",
            "S.pinEls.push({ node: rect, point, rectIndex, kind: 'rect' })",
            "document.addEventListener('scroll', notePinMotion, { capture: true, passive: true })",
            "window.visualViewport.addEventListener('scroll', notePinMotion",
            "pinLayer.classList.remove('wk-motion')",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.source)
        self.assertIn(".wk-pins.wk-motion", self.css)
        pin = re.search(r"\.wk-pin \{(?P<body>.*?)\n\}", self.css, re.S)
        self.assertIsNotNone(pin)
        self.assertNotIn("transition: transform", pin.group("body"))

    def test_surface_tracking_is_targeted_and_can_reactivate_hidden_markers(self):
        for fragment in (
            "refreshSurfaceObservation();\n    repositionAll();",
            "surfaceObserver.observe(node, { attributes: true, attributeFilter })",
            "trackedNodes?.has(target) || remountParents?.has(target)",
            "if (!separatedGeometry) return unresolvedSurfaceActive(point, surface, reviewFallback);",
            "return reviewFallback ? unresolvedSurfaceActive(point, surface, true) : false;",
            "records.filter(childListAffectsTrackedSurface)",
            "surfaceChildListRelevant(",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.source)
        self.assertNotIn(
            "surfaceObserver.observe(document.documentElement, { attributes: true, subtree: true })",
            self.source,
        )
        self.assertNotIn("pointSurfaceVisible(", self.source)

    def test_async_surface_remount_stays_observed_across_remove_and_later_insert(self):
        self.run_node(
            r"""
const assert = require('assert');
const parent = {};
const trackedLeaf = {};
const unrelated = {};
const initialTracked = new Set([parent, trackedLeaf]);
const remountParents = new Set();
assert.strictEqual(H.surfaceChildListRelevant(
  parent, [trackedLeaf], initialTracked, remountParents
), true);

// First rAF refresh sees the selector unresolved. Only the connected parent is
// retained, and the exact mutation parent remains a bounded remount watch.
const afterRemovalTracked = new Set([parent]);
remountParents.add(parent);
assert.strictEqual(H.surfaceChildListRelevant(
  parent, [], afterRemovalTracked, remountParents
), true);
assert.strictEqual(H.surfaceChildListRelevant(
  parent, [], new Set(), remountParents
), true);
assert.strictEqual(H.surfaceChildListRelevant(
  unrelated, [], afterRemovalTracked, remountParents
), false);
"""
        )
        for fragment in (
            "const connectedTrackedNodes = [...trackedSurfaceNodes]",
            "if (unresolvedSurface)",
            "for (const node of connectedTrackedNodes)",
            "const connectedTrackedMarkers = retainCurrentSurfaceMarkerEntries(",
            "rememberSurfaceRemountParent(record.target)",
            "if (surfaceRebindRaf) return",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.source)

    def test_surface_remount_retention_rebinds_to_current_marker_generation(self):
        self.run_node(
            r"""
const assert = require('assert');
const role = { isConnected: true, contains(node) { return node === this; } };
const ancestor = { contains(node) { return node === role; } };
const removedRole = { isConnected: true };
const marker = (generation, connected = true) => ({
  point: { id: 'p-stable' }, rectIndex: 0, kind: 'rect',
  node: { generation, isConnected: connected },
});
let current = marker(0);
let entries = new Map([
  [role, new Set([current])],
  [removedRole, new Set([{
    point: { id: 'p-removed' }, rectIndex: 0, kind: 'rect',
    node: { generation: 0, isConnected: false },
  }])],
]);
let trackedNodes = new Set([role, ancestor, removedRole]);
for (let generation = 1; generation <= 100; generation += 1) {
  current = marker(generation);
  entries = H.retainCurrentSurfaceMarkerEntries(entries, [current]);
  const roles = [...entries.keys()];
  trackedNodes = new Set([...trackedNodes].filter((node) =>
    H.surfaceTrackedNodeSupportsRoles(node, roles)
  ));
  assert.strictEqual(entries.size, 1);
  assert.strictEqual(entries.get(role).size, 1);
  assert.strictEqual([...entries.get(role)][0], current);
  assert.strictEqual([...entries.get(role)][0].node.isConnected, true);
  assert.deepStrictEqual([...trackedNodes], [role, ancestor]);
}
assert.strictEqual(H.surfaceMarkerIdentity(current), 'p-stable:0:rect');
assert.strictEqual(
  H.retainCurrentSurfaceMarkerEntries(entries, [marker(101, false)]).size,
  0
);
"""
        )
        for fragment in (
            "retainCurrentSurfaceMarkerEntries(\n      trackedSurfaceMarkers, S.pinEls",
            "surfaceTrackedNodeSupportsRoles(node, connectedRoleNodes)",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.source)

    def test_initially_unresolved_surface_uses_first_resolved_context_as_remount_watch(self):
        self.run_node(
            r"""
const assert = require('assert');
const fallbackContext = {};
const laterInsertionParent = fallbackContext;
assert.strictEqual(H.surfaceChildListRelevant(
  laterInsertionParent, [], new Set([fallbackContext]), new Set()
), true);
"""
        )
        for fragment in (
            "const fallbackContexts = Array.isArray(point.rectContexts)",
            "const fallbackTarget = uniqueElement(context?.selector)",
            "addTargetChain(fallbackTarget, marker)",
            "for (const context of contexts)",
            "const contextTarget = uniqueElement(context.selector)",
            "target = contextTarget",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.source)

    def test_surface_capture_prefers_later_sticky_state_target_over_generic_stage(self):
        self.run_node(
            r"""
const assert = require('assert');
const genericStage = {
  targetSelector: '#scroll-stage',
  anchor: null,
  stateChain: [],
  spatialScore: 0.6,
};
const stickyTarget = {
  targetSelector: '#sticky-target',
  anchor: { selector: '#sticky-target', mode: 'sticky' },
  stateChain: [{ selector: '#sticky-target', attrs: { 'data-state': 'one' }, classes: [] }],
  spatialScore: 0.12,
  relatedToPrimary: true,
  stronglyRelatedToPrimary: true,
};
const statefulDocumentTarget = {
  targetSelector: '#stateful-document-target',
  anchor: null,
  stateChain: [{
    selector: '#stateful-document-target',
    attrs: { 'data-state': 'one' },
    classes: [],
  }],
  spatialScore: 0.2,
  relatedToPrimary: true,
};
const strongStatefulDocumentTarget = {
  ...statefulDocumentTarget,
  stronglyRelatedToPrimary: true,
};
const inactiveStickyVisual = {
  ...stickyTarget,
  targetSelector: '#inactive-canvas',
  stateChain: [],
  spatialScore: 0.3,
};
const activeIndexedPane = {
  ...stickyTarget,
  targetSelector: '#active-indexed-pane',
  stateChain: [{ selector: '#active-indexed-pane', attrs: {
    'aria-selected': 'true', 'data-index': '1',
  }, classes: [] }],
  hasPositiveActivation: true,
};
const inactiveIndexedPane = {
  ...stickyTarget,
  targetSelector: '#inactive-indexed-pane',
  stateChain: [{ selector: '#inactive-indexed-pane', attrs: {
    'data-index': '2',
  }, classes: [] }],
  inactiveBoundary: true,
  spatialScore: 0.4,
};
assert.strictEqual(
  H.preferredSurfaceCandidate([genericStage, statefulDocumentTarget, stickyTarget]),
  stickyTarget
);
assert.strictEqual(
  H.preferredSurfaceCandidate([genericStage, strongStatefulDocumentTarget]),
  strongStatefulDocumentTarget
);
assert.strictEqual(
  H.preferredSurfaceCandidate([genericStage, strongStatefulDocumentTarget, stickyTarget]),
  stickyTarget
);
assert.strictEqual(
  H.preferredSurfaceCandidate([genericStage, inactiveStickyVisual, stickyTarget]),
  stickyTarget
);
assert.strictEqual(H.preferredSurfaceCandidate([
  genericStage,
  { ...inactiveStickyVisual, inactiveBoundary: true },
]), genericStage);
assert.strictEqual(H.preferredSurfaceCandidate([
  genericStage, inactiveIndexedPane, activeIndexedPane,
]), activeIndexedPane);
const statelessCanvas = {
  targetSelector: '#pointerless-canvas',
  anchor: null,
  stateChain: [],
  visualKind: 'canvas',
  spatialScore: 0.5,
  relatedToPrimary: true,
};
assert.strictEqual(
  H.preferredSurfaceCandidate([genericStage, statelessCanvas]), statelessCanvas
);
assert.strictEqual(
  H.capturedSurfaceGeometrySelector(genericStage, statelessCanvas, null),
  statelessCanvas.targetSelector
);
const statelessSvg = {
  ...statelessCanvas,
  targetSelector: '#pointerless-svg',
  visualKind: 'svg',
};
assert.strictEqual(
  H.preferredSurfaceCandidate([genericStage, statelessSvg]), statelessSvg
);
assert.strictEqual(
  H.capturedSurfaceGeometrySelector(genericStage, statelessSvg, null),
  statelessSvg.targetSelector
);
const animatedSection = {
  ...statelessCanvas,
  targetSelector: '#animated-section',
  visualKind: 'section',
  hasActiveAnimation: true,
  paintOrderEvidence: true,
};
assert.strictEqual(
  H.preferredSurfaceCandidate([genericStage, animatedSection]), animatedSection
);
assert.strictEqual(
  H.capturedSurfaceGeometrySelector(genericStage, animatedSection, null),
  animatedSection.targetSelector
);
assert.strictEqual(H.preferredSurfaceCandidate([
  genericStage,
  { ...animatedSection, targetSelector: '#background-animation',
    stronglyRelatedToPrimary: false, relatedToPrimary: true, paintOrderEvidence: false },
]), genericStage);
const tinyIconSvg = {
  ...statelessSvg,
  targetSelector: '#button-icon',
  spatialScore: 0,
  stronglyRelatedToPrimary: true,
};
assert.strictEqual(
  H.preferredSurfaceCandidate([genericStage, tinyIconSvg]), genericStage
);
const activeTinyIconSvg = {
  ...tinyIconSvg,
  stateChain: [{ selector: '#button-icon', attrs: { 'data-state': 'active' }, classes: [] }],
};
assert.strictEqual(
  H.capturedSurfaceGeometrySelector(genericStage, activeTinyIconSvg, null),
  genericStage.targetSelector
);
assert.strictEqual(
  H.capturedSurfaceGeometrySelector(genericStage, stickyTarget, stickyTarget.anchor),
  genericStage.targetSelector
);
assert.strictEqual(
  H.preferredSurfaceCandidate([genericStage, { ...genericStage, targetSelector: '#other' }]),
  genericStage
);
assert.strictEqual(H.preferredSurfaceCandidate([
  genericStage,
  { ...stickyTarget, targetSelector: '#unrelated-backdrop', spatialScore: 0.04,
    relatedToPrimary: false, stronglyRelatedToPrimary: false },
]), genericStage);
assert.strictEqual(H.preferredSurfaceCandidate([
  genericStage,
  { ...stickyTarget, targetSelector: '#tiny-state-layer', spatialScore: 0.02,
    stronglyRelatedToPrimary: false },
]), genericStage);
assert.strictEqual(H.preferredSurfaceCandidate([]), null);
"""
        )
        for fragment in (
            "for (const context of (Array.isArray(contexts) ? contexts : []).slice(0, 12))",
            "const selected = preferredSurfaceCandidate(candidates)",
            "const geometrySelector = capturedSurfaceGeometrySelector(primary, selected, selectedAnchor)",
            "geometrySelector,",
            "targetSelector: selected.targetSelector",
            "stateChain: selected.stateChain",
            "captureRectSurface(contexts, rect)",
            "for (const selector of SURFACE_VISUAL_SELECTORS)",
            "SURFACE_ACTIVE_SELECTOR, SURFACE_VISUAL_SELECTOR, 'canvas,video,svg'",
            "rankedCaptureEntries([...rankedElements.entries()], 12, 4)",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.source)

    def test_pointerless_visual_surface_survives_deep_hit_stack(self):
        self.run_node(
            r"""
const assert = require('assert');
const hits = Array.from({ length: 20 }, (_, index) => [
  `hit-${index}`,
  { sourcePriority: 0, score: 1 - index / 100, order: index },
]);
const visual = ['active-pointerless-scene', {
  sourcePriority: 1, score: 0.08, order: 20,
}];
const ranked = H.rankedCaptureEntries([...hits, visual], 12, 4);
assert.strictEqual(ranked.length, 12);
assert.strictEqual(ranked[0][0], 'hit-0');
assert(ranked.some(([name]) => name === 'active-pointerless-scene'));
assert.strictEqual(ranked.filter(([, meta]) => meta.sourcePriority === 0).length, 11);
const broad = ['main', {
  sourcePriority: 0, hits: 9, score: 1, surfaceScore: 0.1, order: 0,
}];
const tight = ['animated-target', {
  sourcePriority: 0, hits: 9, score: 0.6, surfaceScore: 0.75, order: 1,
}];
assert.strictEqual(
  H.rankedCaptureEntries([broad, tight], 12, 4)[0][0], 'animated-target'
);
"""
        )

    def test_surface_roles_reject_foreign_anchors_and_preserve_painted_visuals(self):
        self.run_node(
            r"""
const assert = require('assert');
const target = { id: 'sticky-target' };
const geometry = { id: 'document-geometry' };
const anchor = {
  contains(node) { return node === target; },
};
assert.strictEqual(H.surfaceAnchorOwnsRoles(anchor, target, geometry), false);
const ownedAnchor = {
  contains(node) { return node === target || node === geometry; },
};
assert.strictEqual(H.surfaceAnchorOwnsRoles(ownedAnchor, target, geometry), true);
const contaminated = {
  geometrySelector: '#document-geometry',
  targetSelector: '#sticky-target',
  anchor: { selector: '#sticky-anchor', mode: 'sticky' },
};
assert.strictEqual(H.resolvedRectAnchorMode([], contaminated, 'doc', false), 'doc');
assert.strictEqual(H.resolvedRectAnchorMode([], contaminated, 'doc', true), 'sticky');

const primary = { targetSelector: '#copy', spatialScore: 0.6, stateChain: [] };
const behindSvg = {
  targetSelector: '#behind', visualKind: 'svg', spatialScore: 0.4,
  stateChain: [], anchor: { selector: '#stage', mode: 'sticky' },
  relatedToPrimary: true, paintOrderEvidence: false,
};
assert.strictEqual(H.preferredSurfaceCandidate([primary, behindSvg]), primary);
const paintedSvg = { ...behindSvg, targetSelector: '#painted', paintOrderEvidence: true };
assert.strictEqual(H.preferredSurfaceCandidate([primary, paintedSvg]), paintedSvg);
assert.strictEqual(
  H.capturedSurfaceGeometrySelector(primary, paintedSvg, paintedSvg.anchor),
  paintedSvg.targetSelector
);
assert.strictEqual(H.surfaceAnimationRunning({ playState: 'pending' }), true);
assert.strictEqual(H.surfaceAnimationRunning({ playState: 'running' }), true);
assert.strictEqual(H.surfaceAnimationRunning({ playState: 'finished' }), false);
assert.strictEqual(H.surfaceAnimationCapturable({ playState: 'paused', currentTime: 0 }), true);
assert.strictEqual(H.surfaceAnimationCapturable({ playState: 'paused', currentTime: null }), false);
assert.strictEqual(H.surfaceAnimationCapturable({ playState: 'running' }), true);
assert.strictEqual(H.surfaceAnimationCapturable({ playState: 'finished' }), false);
const childNode = { nodeType: 1, contains() { return false; } };
const animatedNode = {
  nodeType: 1, contains(node) { return node === this || node === childNode; },
};
const broadNode = { contains(node) { return node === animatedNode || node === childNode; } };
assert.strictEqual(H.surfaceAnimationTarget({ effect: { target: animatedNode } }), animatedNode);
assert.strictEqual(H.surfaceNodeAnimationRole(animatedNode, [animatedNode]), 'exact');
assert.strictEqual(H.surfaceNodeAnimationRelation(childNode, [animatedNode]), true);
assert.strictEqual(H.surfaceNodeAnimationRole(childNode, [animatedNode]), 'inside');
assert.strictEqual(H.surfaceNodeAnimationRelation(broadNode, [animatedNode]), false);
assert.strictEqual(H.surfaceNodeAnimationRole(broadNode, [animatedNode]), 'contains');
assert.strictEqual(H.surfaceAnimationCapturable({
  playState: 'running', replaceState: 'removed',
}), false);
"""
        )
        for fragment in (
            "surfaceCandidatePaintOrderEvidence(candidate, primary, rect)",
            "selectedAnchorNode.contains(geometryOwner)",
            "resolvedSurfaceAnchorOwnership(capturedSurface)",
            "if (surface.anchor && separatedGeometry && anchorOwnership === false)",
            "effectivelyVisible(geometry), false, false, 'doc'",
            "const animationTargets = capturableSurfaceAnimationTargets()",
            "hasActiveAnimation: surfaceNodeAnimationRelation(target, animationTargets)",
            "for (const elm of animationTargets) consider(elm, true, 2)",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.source)

    def test_section_local_fixed_surfaces_capture_and_recover_flow_scope(self):
        capture_start = self.source.index("  function captureRectSurface")
        capture_end = self.source.index("  function captureDraftRectMetadata", capture_start)
        capture = self.source[capture_start:capture_end]
        self.assertIn("const stateDepthLimit = anchor ? 8 : 5;", capture)
        self.assertNotIn("if (anchor && current === anchor.node) break;", capture)
        self.assertIn("const scopeOwner = surfaceScopeOwner(selected.node, geometryOwner);", capture)
        self.assertIn("const scopeNode = scopeOwner ? surfaceFlowScope", capture)
        self.assertIn("...(scopeSelector ? { scopeSelector } : {}),", capture)

        scope_start = self.source.index("  function surfaceFlowScope(")
        scope_end = self.source.index("  function resolvedSurfaceAnchorOwnership", scope_start)
        scope = self.source[scope_start:scope_end]
        for fragment in (
            "position === 'fixed'",
            "position === 'sticky'",
            "getComputedStyle(node).pointerEvents === 'none'",
            "sectionLocalFixedScopeEligible(",
            "current.matches('section,article,[role=\"region\"],[data-stage],[data-scene]')",
            "const owner = surfaceScopeOwner(target, geometry);",
            "if (!owner) return null;",
            "captured.contains(owner)",
            "const cached = inferredSurfaceScopeCache.get(owner);",
            "cached.isConnected",
            "inferredSurfaceScopeCache.delete(owner)",
            "if (inferred) inferredSurfaceScopeCache.set(owner, inferred);",
            "const inferred = surfaceFlowScope(owner, innerHeight);",
            "surfaceScopeRectActive(rect, innerHeight)",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, scope)

        active_start = self.source.index("  function rectSurfaceActive")
        active_end = self.source.index("  const SKIP_TAGS", active_start)
        active = self.source[active_start:active_end]
        self.assertIn(
            "const scopeActive = resolvedSurfaceScopeActive(surface, target, geometry);",
            active,
        )
        self.assertIn(
            "const geometryScopeActive = resolvedSurfaceScopeActive(surface, geometry, geometry);",
            active,
        )
        self.assertIn("geometryScopeActive", active)
        self.assertIn("innerHeight, scopeActive", active)
        self.assertIn("resolvedSurfaceScopeActive(null, target, target)", active)

        tracking_start = self.source.index("  function refreshSurfaceObservation")
        tracking_end = self.source.index("  document.addEventListener('wheel'", tracking_start)
        tracking = self.source[tracking_start:tracking_end]
        self.assertIn("const capturedScopeTarget = uniqueElement(surface.scopeSelector);", tracking)
        self.assertIn("if (scopeTarget) addTrackedRole(scopeTarget, ['style'], marker);", tracking)

        navigator_start = self.source.index("  function scrollQueuedPointIntoView")
        navigator_end = self.source.index("  let queuedPointNavigationGeneration", navigator_start)
        navigator = self.source[navigator_start:navigator_end]
        self.assertIn("viewportAnchor?.active === false", navigator)
        self.assertIn("settledAnchor?.active === false", navigator)
        self.assertNotIn("rectSurfaceActive(point, primaryIndex) === false", navigator)
        review_start = self.source.index("  function jumpTo(")
        review_end = self.source.index("  // ===== BEFORE|AFTER", review_start)
        review = self.source[review_start:review_end]
        self.assertIn("va?.active === false", review)
        self.assertIn("settled?.active === false", review)
        self.assertNotIn("rectSurfaceActive(pt, primaryIndex) === false", review)

    def test_continuous_surface_animation_repositions_without_starving_visibility(self):
        for event_name in (
            "transitionrun", "transitionstart", "transitionend", "transitioncancel",
            "animationstart", "animationiteration", "animationend", "animationcancel",
        ):
            with self.subTest(event_name=event_name):
                self.assertIn(
                    f"document.addEventListener('{event_name}', wakeTrackedSurface, true)",
                    self.source,
                )

        observer = re.search(
            r"surfaceObserver = new MutationObserver\(\(records\) => \{.*?\n    \}\);",
            self.source,
            re.S,
        )
        self.assertIsNotNone(observer)
        self.assertIn("scheduleSurfacePosition()", observer.group(0))
        self.assertNotIn("notePinMotion()", observer.group(0))
        for fragment in (
            "if (S.mode !== 'feedback' || !S.pinEls.length) return;",
            "const trackedSurfaceAttributeSets = new Map()",
            "const trackedSurfaceMarkers = new Map()",
            "role.getAnimations({ subtree: true })",
            "ancestor.getAnimations()",
            "const addTrackedRole = (node, attributeNames = [], marker = null)",
            "? addTrackedRole(current, ['style'], marker)",
            "repositionMarkers(surfaceAnimationMarkers)",
            "surfaceAnimationTickTimer = setTimeout(() =>",
            "surfaceAnimationRescanTimer = setTimeout(scanSurfaceAnimations, 500)",
            "surfaceResizeObserver = new ResizeObserver",
            "surfaceIntersectionObserver = new IntersectionObserver",
            "for (const node of trackedSurfaceMarkers.keys())",
            "trackedSurfaceMarkers.has(entry.target)",
            "trackedSurfaceAttributeSets.get(node)",
            "if (S.mode !== 'feedback')",
            "queueSurfaceRebind()",
            "document.addEventListener('scroll', notePinMotion",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.source)
        self.assertNotIn("addTrackedNode(current, depth === 0 ? ['style'] : [], marker)", self.source)
        animation_driver = self.source[
            self.source.index("  function discoverSurfaceAnimationMarkers"):
            self.source.index("  function rememberSurfaceRemountParent")
        ]
        self.assertNotIn("notePinMotion", animation_driver)
        self.assertNotIn("document.getAnimations()", animation_driver)

    def test_browser_fixture_covers_pointerless_continuous_surface_animation(self):
        for fragment in (
            ".sticky-pane { transition: opacity 120ms linear; pointer-events: none; }",
            'class="sticky-pane sticky-pane-one is-active" aria-hidden="true" aria-selected="true"',
            'class="sticky-pane sticky-pane-two" aria-selected="false"',
            'id="runtime-canvas-two"',
            "qa.startSurfaceChurn = (frameCount = 240) =>",
            "stickyTarget.style.setProperty('--qa-surface-frame'",
            "qa.stopSurfaceChurn = () =>",
            "@keyframes qa-marker-pulse",
            "animation: qa-marker-pulse 1.4s linear infinite",
            "#keyframe-target.qa-animation-paused { animation-play-state: paused; }",
            'id="start-keyframe-animation"',
            "classList.remove('qa-animation-paused')",
            'id="keyframe-target"',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.qa_fixture)
        self.assertIn(
            "inactiveBoundary: !hasPositiveActivation && surfaceHasInactiveBoundary(target)",
            self.source,
        )

    def test_v0818_missing_selectors_only_reappear_at_the_capture_scroll(self):
        self.run_node(
            r"""
const assert = require('assert');
// v0.8.18 points can reference a selector removed by the agent. Their stored
// rectangle is allowed at the capture moment, but never follows a sticky scene.
assert.strictEqual(H.visualSurfaceActive(
  true, false, false, 'sticky', true, 2100, 2100, 900
), true);
assert.strictEqual(H.visualSurfaceActive(
  true, false, false, 'sticky', true, 3000, 2100, 900
), false);
"""
        )
        self.assertIn("function currentReviewPointMatches(point)", self.source)
        self.assertIn(
            "function unresolvedSurfaceActive(point, surface, reviewing = currentReviewPointMatches(point))",
            self.source,
        )
        self.assertIn(
            "if (!separatedGeometry) return unresolvedSurfaceActive(point, surface, reviewFallback);",
            self.source,
        )
        self.assertIn(
            "return reviewFallback ? unresolvedSurfaceActive(point, surface, true) : false;",
            self.source,
        )
        self.assertIn(
            "visible, hasStateSignals, stateMatches, anchorMode, reviewFallback,",
            self.source,
        )
        self.assertNotIn(
            "visible || reviewFallback, hasStateSignals, stateMatches",
            self.source,
        )
        self.assertIn(
            "effectivelyVisible(target), false, false, anchorMode, reviewFallback,",
            self.source,
        )
        self.assertIn("if (!selector) return unresolvedSurfaceActive(point, null);", self.source)
        self.assertIn("surface?.anchor?.mode || point.anchor || 'doc'", self.source)
        self.assertIn("resolvedRectAnchorMode(\n        liveModes, capturedSurface, p.anchor,", self.source)

    def test_unresolved_document_points_keep_document_geometry_active(self):
        self.run_node(
            r"""
const assert = require('assert');
assert.strictEqual(H.visualSurfaceActive(
  true, false, false, 'doc', false, 9000, 1200, 900
), true);
"""
        )
        self.assertIn("? 'sticky'\n      : 'doc'", self.source)

    def test_rectangle_edits_reuse_capture_metadata_until_geometry_changes(self):
        for fragment in (
            "rectContexts: editing ? editingRectContexts.slice()",
            "rectSurfaces: editing ? editingRectSurfaces.slice()",
            "rectMetadataRects: editing",
            "rectContexts.push(draft.rectContexts[rectIndex])",
            "rectSurfaces.push(draft.rectSurfaces[rectIndex] ?? null)",
            "captureDraftRectMetadata(S.card.draft, index",
            "draft.rectContexts[rectIndex] = rectContext",
            "draft.rectSurfaces[rectIndex] = rectSurface",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.source)

    def test_pending_fixed_text_edit_preserves_the_original_capture_frame(self):
        self.run_node(
            r"""
const assert = require('assert');
const capturedRect = { x: 20, y: 1100, w: 200, h: 80 };
const reopenedDisplayRect = { x: 20, y: 2100, w: 200, h: 80 };
const stored = H.persistedRectForDraft(
  reopenedDisplayRect,
  { ...reopenedDisplayRect },
  capturedRect
);
assert.deepStrictEqual(stored, capturedRect);
assert.strictEqual(stored.y - 1000, 100);
assert.deepStrictEqual(H.persistedRectForDraft(
  { ...reopenedDisplayRect, y: 2110 }, reopenedDisplayRect, capturedRect
), { ...reopenedDisplayRect, y: 2110 });
"""
        )
        for fragment in (
            "rectPersistedRects: editingSource",
            "persistedRectForDraft(",
            "draft.rectPersistedRects?.[rectIndex]",
            "draft.rectPersistedRects[rectIndex] = null",
            "if (!sameRectGeometry(S.card.draft.rects[index], S.drag.r0))",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.source)

    def test_mixed_document_and_sticky_rectangles_keep_independent_anchor_modes(self):
        for fragment in (
            "const anchorModes = rects.map((_rect, rectIndex) =>",
            "const fixedByRect = anchorModes.map((mode) => mode !== 'doc')",
            "geometry.fixedByRect[marker.rectIndex]",
            "editingGeometry.fixedByRect[rectIndex]",
            "anchorMode: geometry.anchorModes[rectIndex]",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.source)
        self.run_node(
            r"""
const assert = require('assert');
const modes = [
  H.pointAnchorMode([], [], false),
  H.pointAnchorMode([], ['sticky'], false),
];
assert.deepStrictEqual(modes, ['doc', 'sticky']);
assert.deepStrictEqual(modes.map((mode) => mode !== 'doc'), [false, true]);
"""
        )

    def test_queued_fixed_reopen_uses_live_display_and_original_storage_frames(self):
        self.run_node(
            r"""
const assert = require('assert');
const queued = {
  rect: { x: 20, y: 1100, w: 200, h: 80 },
  scroll: { x: 0, y: 1000 },
};
const reopenedAtScroll2000 = { x: 20, y: 2100, w: 200, h: 80 };
assert.deepStrictEqual(H.persistedRectForDraft(
  reopenedAtScroll2000,
  { ...reopenedAtScroll2000 },
  queued.rect
), queued.rect);
"""
        )
        for fragment in (
            "const editingSource = pendingEditing || queuedEditing",
            "const editingGeometry = editingSource ? correctedPointRects(editingSource)",
            "rect: { ...editingDisplayRects[editingPrimaryIndex] }",
            "editingSourceRects.map((rect) => ({ ...rect }))",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.source)

    def test_multi_rect_text_edit_preserves_the_original_last_primary_rect(self):
        self.run_node(
            r"""
const assert = require('assert');
const first = { x: 10, y: 1100, w: 80, h: 40 };
const lastPrimary = { x: 200, y: 1200, w: 120, h: 60 };
const liveDisplayRects = [
  { x: 10, y: 2100, w: 80, h: 40 },
  { x: 200, y: 2200, w: 120, h: 60 },
];
const index = H.primaryRectIndex([first, lastPrimary], lastPrimary);
assert.strictEqual(index, 1);
assert.deepStrictEqual(liveDisplayRects[index], liveDisplayRects[1]);
assert.deepStrictEqual(H.persistedRectForDraft(
  liveDisplayRects[index], { ...liveDisplayRects[index] }, lastPrimary
), lastPrimary);
assert.strictEqual(H.primaryRectIndex([first, lastPrimary], { x: 999, y: 0, w: 1, h: 1 }), 1);
assert.strictEqual(H.primaryRectIndex([first, first], first), 1);
"""
        )
        self.assertIn(
            "primaryRectIndex(editingSourceRects, editingSource.rect)",
            self.source,
        )
        self.assertIn(
            "const activeRectIndex = primaryRectIndex(rects, draft.rect)",
            self.source,
        )
        for fragment in (
            "const rectIndex = primaryRectIndex(p.rects || [p.rect], p.rect)",
            "rectIndex === primaryIndex",
            "pt.rectSurfaces?.[primaryIndex]?.scroll?.y",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.source)
        self.assertNotIn(
            "rects.findIndex((rect) => sameRectGeometry(rect, draft.rect))",
            self.source,
        )

    def test_null_surface_preserves_legacy_viewport_anchor_fallback(self):
        self.run_node(
            r"""
const assert = require('assert');
const point = {
  anchor: 'viewport',
  rectSurfaces: [null],
  rectContexts: [[{ selector: '#removed-target' }]],
};
assert.strictEqual(
  H.rectUsesLegacyViewportAnchor(point.anchor, point.rectSurfaces[0]),
  true
);
assert.strictEqual(H.pointAnchorMode([], [], true), 'unknown');
"""
        )
        self.assertIn(
            "resolvedRectAnchorMode(\n        liveModes, capturedSurface, p.anchor,",
            self.source,
        )

    def test_review_navigation_recovers_sticky_points_that_left_the_viewport(self):
        self.run_node(
            r"""
const assert = require('assert');
assert.strictEqual(
  H.reviewScrollTarget({ x: 20, y: 1200, w: 80, h: 100 }, false, 'doc', 0, 800),
  850
);
assert.strictEqual(
  H.reviewScrollTarget({ x: 983, y: -39.4, w: 61, h: 48 }, true, 'sticky', 5107, 902),
  4640.6
);
assert.strictEqual(
  H.reviewScrollTarget({ x: 983, y: 260, w: 61, h: 48 }, true, 'sticky', 4641, 902),
  null
);
assert.strictEqual(
  H.reviewScrollTarget({ x: 0, y: -30, w: 100, h: 40 }, true, 'fixed', 5000, 900),
  null
);
assert.strictEqual(
  H.reviewScrollTarget({ x: 400, y: 240, w: 100, h: 80 }, true, 'sticky', 7200, 900, 5107),
  5107
);
"""
        )
        self.assertIn("function elementViewportAnchorMode(node)", self.source)
        self.assertIn("va ? va.anchorMode : 'doc'", self.source)
        self.assertIn("const retry = reviewScrollTarget(", self.source)

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
        self.assertNotIn("S.agentWakePending || S.sentVerdicts", self.source)

    def test_review_bar_and_variant_chip_stay_compact_until_screen_edge(self):
        self.assertIn("box-sizing: border-box;", self.css)
        self.assertIn("width: max-content;", self.css)
        self.assertIn("max-width: calc(100vw - 40px);", self.css)
        self.assertIn("left: 20px;", self.css)
        self.assertIn("right: 20px;", self.css)
        self.assertIn("width: auto;", self.css)
        self.assertIn("wk-chip-dot", self.source)
        self.assertNotIn("wk-chip-flask", self.source)
        self.assertNotIn("wk-chip-cycle", self.source)

    def test_redo_can_request_a_fresh_variant_experiment(self):
        self.assertIn("let redoVariantCount", self.source)
        self.assertIn("Generate fewer variants", self.source)
        self.assertIn("Generate more variants", self.source)
        self.assertIn("Math.max(2, redoVariantCount - 1)", self.source)
        self.assertIn("Math.min(10, redoVariantCount + 1)", self.source)
        self.assertIn("variantStep.hidden = !redoVariants", self.source)
        self.assertIn("nextVerdict.redoAbcRequest", self.source)
        self.assertIn("!txt && !redoVoiceNote && !redoVariants", self.source)

    def test_current_review_rectangle_stays_above_overlapping_points(self):
        self.assertIn(".wk-pin-rect.review.current", self.css)
        self.assertIn("z-index: 3;", self.css)
        self.assertIn(".wk-pin.review.current { z-index: 4;", self.css)

    def test_review_feedback_waits_for_explicit_send_and_stays_deletable(self):
        done_start = self.source.index("doneBtn.addEventListener('click'")
        done_end = self.source.index("// The header doubles as a drag handle", done_start)
        self.assertNotIn("sendPoints", self.source[done_start:done_end])
        self.assertNotIn("sendPoints(true)", self.source)
        self.assertIn("delBtn.hidden = !queuedEditing", self.source)
        self.assertIn("queued ? 'Saved' : (activeRound ? 'Add' : 'Send')", self.source)
        self.assertIn(
            "Points are saved locally. Send them after the current verdicts finish processing.",
            self.source,
        )

    def test_unsent_feedback_points_have_a_navigable_focused_editor_list(self):
        list_start = self.source.index("  function paintFeedbackPointList()")
        list_end = self.source.index("  function closeFeedbackPointsList", list_start)
        list_source = self.source[list_start:list_end]
        self.assertIn("const points = S.points.slice().sort", list_source)
        self.assertNotIn("S.batch", list_source)
        self.assertNotIn("S.submittedPoints", list_source)
        for fragment in (
            "showPointsBtn.setAttribute('aria-haspopup', 'dialog')",
            "showPointsBtn.setAttribute('aria-controls', feedbackPanel.id)",
            "feedbackPanel.setAttribute('role', 'dialog')",
            "openQueuedPointEditor(point.id)",
            "S.points = loadQueuedPoints()",
            "openCard({ editId: point.id })",
            "feedbackPointList.querySelector('.wk-feedback-point-item')?.focus()",
            "const queuedEditParam = params.get('wk-edit-point')",
            "S.bootQueuedEdit = queuedEditParam",
            "S.bootQueuedEdit ||",
            "S.bootQueuedEdit = null;\n    maybeAutoEnterReview();",
            "physicalPath(point.page, 'after') + '?wk-edit-point='",
            "stripInternalQueryParam('wk-edit-point')",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.source)
        self.assertLess(
            self.source.index("await scrollQueuedPointIntoView(point)"),
            self.source.index("openCard({ editId: point.id })"),
        )

    def test_feedback_navigator_uses_primary_button_and_editor_has_one_rectangle(self):
        show_start = self.css.index(".wk-show-points {")
        show_end = self.css.index(".wk-show-points:hover", show_start)
        show_button = self.css[show_start:show_end]
        self.assertIn("background: var(--wk-accent);", show_button)
        self.assertIn("color: var(--wk-on-accent);", show_button)
        self.assertNotIn("background: var(--wk-paper);", show_button)

        pins_start = self.source.index("  function renderPins()")
        pins_end = self.source.index("  function correctedRect", pins_start)
        pins = self.source[pins_start:pins_end]
        self.assertIn("const editedPointId = S.card?.draft?.editId || null;", pins)
        self.assertIn("if (p.id === editedPointId || p.page !== page) continue;", pins)
        self.assertIn(
            "if (p.id === editedPointId || submittedIds.has(p.id) || p.page !== page) continue;",
            pins,
        )
        self.assertLess(
            self.source.index("S.card = cardOwner;"),
            self.source.index("renderPins();", self.source.index("S.card = cardOwner;")),
        )

    def test_feedback_point_navigation_owns_and_cleans_restored_surfaces(self):
        self.run_node(
            r"""
const assert = require('assert');
let active = false;
let owner = 0;
const begin = (generation) => { active = true; owner = generation; };
const finish = (generation) => {
  if (!H.ownedSurfaceSessionCanFinish(active, owner, generation)) return false;
  active = false;
  owner = 0;
  return true;
};

begin(1);
assert.strictEqual(finish(undefined), true);
assert.strictEqual(active, false);
begin(2);
assert.strictEqual(finish(1), false);
assert.strictEqual(active, true);
assert.strictEqual(owner, 2);
assert.strictEqual(finish(2), true);
assert.strictEqual(active, false);
assert.strictEqual(finish(2), false);
"""
        )
        navigator = self.source[
            self.source.index("  let queuedPointNavigationGeneration"):
            self.source.index("  showPointsBtn.addEventListener('click'")
        ]
        for fragment in (
            "let queuedEditorSurfaceGeneration = 0",
            "function finishQueuedEditorSurfaceSession(ownerGeneration)",
            "ownedSurfaceSessionCanFinish(\n      queuedEditorSurfacesActive",
            "finishQueuedEditorSurfaceSession();\n    closeFeedbackPointsList",
            "queuedEditorSurfaceGeneration = generation",
            "finishQueuedEditorSurfaceSession(generation);\n      return false;",
            "if (S.mode !== 'feedback' || IS_BEFORE)",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, navigator)
        self.assertLess(
            navigator.index("finishQueuedEditorSurfaceSession();"),
            navigator.index("if (!point)"),
        )
        self.assertLess(
            navigator.index("if (generation !== queuedPointNavigationGeneration)"),
            navigator.index("openCard({ editId: point.id })"),
        )
        self.assertLess(
            navigator.index("if (S.mode !== 'feedback' || IS_BEFORE)"),
            navigator.index("openCard({ editId: point.id })"),
        )

    def test_feedback_point_list_reconciles_without_losing_keyboard_focus(self):
        self.run_node(
            r"""
const assert = require('assert');
const first = { id: 'p-1', number: 1, page: '/', text: '  Fix   this  ' };
const second = { id: 'p-2', number: 2, page: '/about', text: '', voiceNote: { path: 'v.webm' } };
const signature = H.feedbackPointListSignature([first, second]);
assert.strictEqual(signature, H.feedbackPointListSignature([second, first]));
assert.strictEqual(signature, H.feedbackPointListSignature([
  { ...first, rect: { x: 10, y: 20, w: 30, h: 40 } }, second,
]));
assert.notStrictEqual(signature, H.feedbackPointListSignature([
  { ...first, text: 'Fix something else' }, second,
]));
assert.notStrictEqual(signature, H.feedbackPointListSignature([first]));
"""
        )
        for fragment in (
            "let paintedFeedbackPointListSignature = '';",
            "if (signature === paintedFeedbackPointListSignature &&",
            "feedbackPointList.childElementCount === points.length) return false;",
            "const focusedPointId = active && feedbackPointList.contains(active)",
            ".find((item) => item.dataset.pointId === focusedPointId)",
            "if (replacement) replacement.focus();",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.source)

    def test_feedback_actions_track_the_topmost_bottom_control(self):
        self.run_node(
            r"""
const assert = require('assert');
assert.strictEqual(H.feedbackToolsBottom([], 900), 14);
assert.strictEqual(H.feedbackToolsBottom([{ top: 840 }], 900), 68);
assert.strictEqual(H.feedbackToolsBottom([{ top: 840 }, { top: 760 }], 900), 148);
assert.strictEqual(H.feedbackToolsBottom([{ top: NaN }, null], 900), 14);
assert.strictEqual(H.feedbackToolsBottom([{ top: 760 }], 900, 12, 20), 152);
"""
        )
        for fragment in (
            ".wk-feedback-tools {",
            "left: 50%;",
            "max-width: calc(100vw - 32px);",
            ".wk-feedback-tools-row {",
            "flex-wrap: nowrap;",
            ".wk-feedback-panel {",
            "bottom: calc(100% + 8px);",
            "width: min(360px, calc(100vw - 32px));",
            "max-height: min(360px, 50vh);",
            "overflow-y: auto;",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.css)
        self.assertIn("...bar.querySelectorAll('.wk-abc-chip')", self.source)
        self.assertIn("scheduleFeedbackToolsPosition();", self.source)
        self.assertIn(".wk-feedback-tools-row,.wk-feedback-panel,.wk-show-points", self.source)

    def test_every_feedback_save_finishes_an_active_voice_note_first(self):
        commit_start = self.source.index("    async function commit()")
        commit_end = self.source.index("    addRectBtn.addEventListener", commit_start)
        commit_source = self.source[commit_start:commit_end]
        self.assertIn("await mic.finishAndWait()", commit_source)
        self.assertIn("if (!voiceReady) return false", commit_source)
        self.assertNotIn("Stop the recording before saving this point", self.source)

    def test_speech_done_waits_for_final_recognition_result_once(self):
        speech_start = self.source.index("  function makeMic(")
        speech_end = self.source.index("  // Voice-note mode", speech_start)
        speech_source = self.source[speech_start:speech_end]
        for fragment in (
            "let finishCycle = null",
            "activeRec.onresult",
            "activeRec.onend",
            "finishAndWait",
            "finishCycle.rec === activeRec",
            "activeRec.stop()",
            "setTimeout(() => settleFinish(activeRec, true), 3000)",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, speech_source)

        commit_start = self.source.index("    async function commit()")
        commit_end = self.source.index("    addRectBtn.addEventListener", commit_start)
        commit_source = self.source[commit_start:commit_end]
        self.assertLess(
            commit_source.index("await mic.finishAndWait()"),
            commit_source.index("draft.text = ta.value", commit_source.index("await mic.finishAndWait()")),
        )
        done_start = self.source.index("    doneBtn.addEventListener('click'")
        done_end = self.source.index("    // The header doubles", done_start)
        self.assertNotIn("finishAndWait", self.source[done_start:done_end])

    def test_space_dictation_claims_any_empty_webkit_textarea(self):
        self.assertIn("const HOTKEY_DICTATE = DS.wkHotkeyDictate || 'Space'", self.source)
        self.assertGreaterEqual(self.source.count("dictationHotkeyReady()"), 2)
        self.assertEqual(
            self.source.count("dictationHotkeyReady() { return ta.value === ''; }"),
            2,
        )
        self.assertNotIn("textTouched", self.source)
        keyboard_start = self.source.index("  // ===== keyboard")
        keyboard_source = self.source[keyboard_start:]
        for fragment in (
            "if (!holder.dictationHotkeyReady || !holder.dictationHotkeyReady()) return",
            "if (!dictationTargetAllowsActivation(t, holder.ta, editable)) return",
            "holder.micBtn.click()",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, keyboard_source)
        self.assertLess(
            keyboard_source.index("if (!holder.dictationHotkeyReady"),
            keyboard_source.index("e.preventDefault()", keyboard_source.index("if (!holder.dictationHotkeyReady")),
        )

    def test_dictation_space_preserves_controls_and_owns_its_key_repeat(self):
        self.run_node(
            r"""
const assert = require('assert');
function element(tagName, attrs = {}, parentElement = null) {
  return {
    nodeType: 1,
    tagName,
    parentElement,
    hasAttribute(name) { return Object.prototype.hasOwnProperty.call(attrs, name); },
    getAttribute(name) { return this.hasAttribute(name) ? attrs[name] : null; },
  };
}
const textarea = element('TEXTAREA');
const button = element('BUTTON');
const buttonIcon = element('SPAN', {}, button);
const link = element('A', { href: '/next' });
const customButton = element('DIV', { role: 'button' });
const focusableScene = element('CANVAS', { tabindex: '0' });
const plainPage = element('DIV');
const otherInput = element('INPUT');

for (const target of [button, buttonIcon, link, customButton, focusableScene]) {
  assert.strictEqual(H.keyboardActivationTarget(target), true, target.tagName);
  assert.strictEqual(H.dictationTargetAllowsActivation(target, textarea, false), false);
}
assert.strictEqual(H.keyboardActivationTarget(plainPage), false);
assert.strictEqual(H.dictationTargetAllowsActivation(plainPage, textarea, false), true);
assert.strictEqual(H.dictationTargetAllowsActivation(otherInput, textarea, true), false);
assert.strictEqual(H.dictationTargetAllowsActivation(textarea, textarea, true), true);

for (const holderKind of ['feedback', 'redo']) {
  const hold = { code: '' };
  const keydown = { type: 'keydown', code: 'Space', repeat: false };
  const repeat = { type: 'keydown', code: 'Space', repeat: true };
  const keyup = { type: 'keyup', code: 'Space', repeat: false };

  let value = 'typed text';
  assert.strictEqual(
    H.dictationKeyAction(keydown, 'Space', hold, value === ''),
    'pass', holderKind + ': nonempty Space must type normally'
  );
  assert.strictEqual(hold.code, '');

  value = '';
  assert.strictEqual(
    H.dictationKeyAction(keydown, 'Space', hold, value === ''),
    'activate', holderKind + ': deletion must restore dictation activation'
  );
  value = 'speech arrived';
  assert.strictEqual(
    H.dictationKeyAction(repeat, 'Space', hold, value === ''),
    'suppress', holderKind + ': activation repeat must stay consumed'
  );
  assert.strictEqual(H.dictationKeyAction(keyup, 'Space', hold, false), 'suppress');
  assert.strictEqual(hold.code, '');
  assert.strictEqual(H.dictationKeyAction(repeat, 'Space', hold, false), 'pass');
}
"""
        )
        keyboard_start = self.source.index("  // ===== keyboard")
        keyboard_source = self.source[keyboard_start:]
        self.assertLess(
            keyboard_source.index("const heldDictationAction = dictationKeyAction("),
            keyboard_source.index("if (e.repeat) return"),
        )
        self.assertIn("window.addEventListener('keyup'", keyboard_source)
        self.assertIn("window.addEventListener('blur'", keyboard_source)

    def test_ready_review_opens_without_a_confirmation_toast(self):
        self.assertIn("function maybeAutoEnterReview()", self.source)
        self.assertIn("S.reviewAutoPending = true;\n        maybeAutoEnterReview();", self.source)
        self.assertIn("enterReview({ auto: true });", self.source)
        self.assertNotIn("action: { label: 'Start review'", self.source)

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
        self.assertIn("class TestSpeechRecognition", self.qa_fixture)
        self.assertIn("this.onresult?.({ resultIndex: 0, results: [result] })", self.qa_fixture)
        self.assertIn("#sticky-target { position: sticky", self.qa_fixture)
        self.assertIn("<canvas id=\"runtime-canvas\"", self.qa_fixture)
        self.assertIn('id="sticky-target" data-state="one"', self.qa_fixture)
        self.assertIn("stickyTarget.dataset.state = second ? 'two' : 'one'", self.qa_fixture)

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
        self.assertIn("x: source.x - 12, y: source.y - 12, w: 24, h: 24", self.source)
        self.assertIn("pin.setAttribute('aria-label'", self.source)

    def test_overlay_contrast_theme_is_isolated_and_complete(self):
        for fragment in (
            "!['black', 'white'].includes(dataset.wkTheme)",
            "const OVERLAY_THEME = DS.wkTheme === 'white' ? 'white' : 'black'",
            "host.setAttribute('data-wk-theme', OVERLAY_THEME)",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.source)
        for fragment in (
            ':host([data-wk-theme="white"])',
            "--wk-on-accent: #111111",
            "color-scheme: dark",
            "color: var(--wk-on-accent)",
            "--wk-marker-line",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.css)
        self.assertNotIn("color: #fff", self.css)

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

    def test_point_before_fails_closed_for_runtime_owned_content(self):
        gate_start = self.source.index("  const UNSAFE_SWAP_TAGS")
        gate_end = self.source.index("  // The doodles are drawn", gate_start)
        gate = self.source[gate_start:gate_end]
        for fragment in (
            "'CANVAS'", "'VIDEO'", "'IFRAME'", "'IMG'",
            "current.shadowRoot", "currentKids.length !== historicalKids.length",
            "style.animationName", "getComputedStyle(current, '::before').content",
            "this element owns a versioned asset",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, gate)
        apply_start = self.source.index("  async function applySwap")
        apply_end = self.source.index("  function restoreSwap", apply_start)
        apply_source = self.source[apply_start:apply_end]
        self.assertLess(
            apply_source.index("pointSwapUnsafeReason(t.live, t.incoming)"),
            apply_source.index("styledBeforeNode(t.sel, t.incoming, context, t.live)"),
        )
        styled_start = self.source.index("  async function styledBeforeNode")
        styled_end = self.source.index("  // Keep the eye", styled_start)
        styled_source = self.source[styled_start:styled_end]
        self.assertLess(
            styled_source.index("carryState(live, source)"),
            styled_source.index("copyComputedTree(source, node)"),
        )
        self.assertIn("navSide('before');", self.source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
