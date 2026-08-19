import re
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / "webkit" / "skills" / "abc" / "assets" / "widget.html"
WIDGET_RE = re.compile(
    r"<!-- ABC:widget ([A-Za-z0-9_-]+).*?<script>(.*?)</script>\s*"
    r"<!-- /ABC:widget [A-Za-z0-9_-]+ -->",
    re.DOTALL,
)


def widget_scripts(path):
    return [match.group(2).strip() for match in WIDGET_RE.finditer(path.read_text(encoding="utf-8"))]


def normalized_config(source):
    source = re.sub(r"^\s*const ID\s*=.*$", "  const ID = '<ID>';", source, flags=re.MULTILINE)
    source = re.sub(
        r"^\s*const LETTERS\s*=.*$", "  const LETTERS = '<LETTERS>';", source, flags=re.MULTILINE
    )
    return re.sub(
        r"^\s*const RELOAD\s*=.*$", "  const RELOAD = <RELOAD>;", source, flags=re.MULTILINE
    )


class ABCWidgetTests(unittest.TestCase):
    def test_checked_in_widget_copies_match_the_hardened_asset(self):
        canonical = normalized_config(widget_scripts(ASSET)[0])
        expected_counts = {
            ROOT / "examples" / "demo-site" / "index.html": 1,
            ROOT / "webkit" / "skills" / "abc" / "evals" / "files" / "one-experiment-live.html": 1,
            ROOT / "webkit" / "skills" / "abc" / "evals" / "files" / "two-experiments-live.html": 2,
        }
        for path, expected_count in expected_counts.items():
            with self.subTest(path=str(path.relative_to(ROOT))):
                scripts = widget_scripts(path)
                self.assertEqual(len(scripts), expected_count)
                for source in scripts:
                    self.assertEqual(normalized_config(source), canonical)

    def test_widget_rejects_invalid_variants_and_survives_unavailable_storage(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable")
        source = widget_scripts(ASSET)[0]
        result = subprocess.run(
            [node, "-"],
            input=self._node_harness(source, storage_throws=True, reload_mode=False),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_reload_scroll_state_is_scoped_to_page_and_experiment(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable")
        source = widget_scripts(ASSET)[0].replace(
            "const RELOAD  = false;", "const RELOAD  = true;"
        )
        result = subprocess.run(
            [node, "-"],
            input=self._node_harness(source, storage_throws=False, reload_mode=True),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_widget_leaves_an_incompatible_existing_global_untouched(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable")
        source = widget_scripts(ASSET)[0]
        result = subprocess.run(
            [node, "-"],
            input=self._node_harness(
                source,
                storage_throws=False,
                reload_mode=False,
                incompatible_manager=True,
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @staticmethod
    def _node_harness(source, storage_throws, reload_mode, incompatible_manager=False):
        prefix = r'''
const warnings = [];
console.warn = (...parts) => warnings.push(parts.join(' '));
class Element {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.listeners = {};
    this.style = {};
    this.attributes = {};
    this.hidden = false;
    this.dataset = {};
    this.textContent = '';
  }
  appendChild(child) { this.children.push(child); child.parent = this; return child; }
  append(...children) { children.forEach((child) => this.appendChild(child)); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] === undefined ? null : this.attributes[name]; }
  closest(selector) {
    if (selector !== '[data-abc-scope]') return null;
    let node = this;
    while (node) {
      if (node.attributes && node.attributes['data-abc-scope'] !== undefined) return node;
      node = node.parent;
    }
    return null;
  }
  getBoundingClientRect() { return { top: 100, bottom: 400, height: 300 }; }
  querySelectorAll(selector) { return selector === '[data-abc-only]' ? this.variants || [] : []; }
  dispatchEvent(event) { this.lastEvent = event; return true; }
}
const head = new Element('head');
const body = new Element('body');
const scope = new Element('section');
scope.setAttribute('data-abc-scope', 'hero');
scope.variants = ['A', 'B'].map((letter) => {
  const node = new Element('div');
  node.setAttribute('data-abc-only', letter);
  return node;
});
scope.variants[0].style.display = 'flex';
scope.variants[1].style.display = 'grid';
scope.variants.forEach((node) => scope.appendChild(node));
const nestedScope = new Element('section');
nestedScope.setAttribute('data-abc-scope', 'nested');
const nestedVariant = new Element('div');
nestedVariant.setAttribute('data-abc-only', 'B');
nestedVariant.style.display = 'inline-grid';
nestedScope.appendChild(nestedVariant);
scope.appendChild(nestedScope);
scope.variants.push(nestedVariant);
const localData = new Map();
const sessionData = new Map();
const storage = (data) => ({
  getItem(key) { return data.has(key) ? data.get(key) : null; },
  setItem(key, value) { data.set(key, String(value)); },
  removeItem(key) { data.delete(key); },
});
global.window = global;
global.document = {
  head,
  body,
  readyState: 'complete',
  createElement(tag) { return new Element(tag); },
  querySelector(selector) { return selector === '[data-abc-scope="hero"]' ? scope : null; },
};
global.location = { pathname: '/demo', search: '?abc-hero=AB', href: 'http://localhost/demo?abc-hero=AB' };
global.innerHeight = 800;
global.scrollY = 125;
global.requestAnimationFrame = (callback) => { callback(); return 1; };
global.addEventListener = () => {};
global.scrollTo = () => {};
global.CustomEvent = class CustomEvent { constructor(name, options) { this.name = name; this.detail = options.detail; } };
'''
        if storage_throws:
            prefix += r'''
Object.defineProperty(window, 'localStorage', { get() { throw new Error('blocked'); } });
Object.defineProperty(window, 'sessionStorage', { get() { throw new Error('blocked'); } });
'''
        else:
            prefix += r'''
window.localStorage = storage(localData);
window.sessionStorage = storage(sessionData);
'''
        if incompatible_manager:
            prefix += r'''
window.__abc = { owner: 'host-page' };
'''
            suffix = r'''
if (window.__abc.owner !== 'host-page') throw new Error('existing global was replaced');
if (body.children.some((child) => child.className === 'abc-switch')) throw new Error('widget was added');
if (!warnings.some((warning) => warning.includes('incompatible'))) throw new Error('missing compatibility warning');
'''
            return prefix + "\n" + source + "\n" + suffix
        suffix = r'''
const instance = window.__abc.get('hero');
if (!instance) throw new Error('widget did not register');
if (instance.current !== 'A') throw new Error('multi-character URL variant was accepted');
if (scope.variants[0].style.display !== 'flex') throw new Error('visible inline display was not preserved');
if (scope.variants[1].style.display !== 'none') throw new Error('inactive outer variant was not hidden');
if (nestedVariant.style.display !== 'inline-grid') throw new Error('nested experiment was changed');
if (instance.set('AB') !== false || instance.current !== 'A') throw new Error('invalid manager variant was accepted');
if (instance.set('b') !== true || instance.current !== 'B') throw new Error('valid manager variant was rejected');
if (scope.variants[0].style.display !== 'none') throw new Error('old outer variant was not hidden');
if (scope.variants[1].style.display !== 'grid') throw new Error('restored inline display was lost');
if (nestedVariant.style.display !== 'inline-grid') throw new Error('nested experiment changed after a switch');
const button = body.children.find((child) => child.className === 'abc-switch');
if (!button || !button.attributes['aria-label']) throw new Error('switcher has no accessible name');
'''
        if reload_mode:
            suffix += r'''
instance.set('A');
button.listeners.click({ altKey: false });
if (sessionData.get('abc:/demo:hero:scroll') !== '125') throw new Error('scroll state is not scoped');
if (sessionData.has('abc:scroll')) throw new Error('legacy global scroll key leaked');
'''
        else:
            suffix += r'''
const buttonCount = body.children.filter((child) => child.className === 'abc-switch').length;
if (buttonCount !== 1) throw new Error('unexpected button count');
'''
        return prefix + "\n" + source + "\n" + suffix


if __name__ == "__main__":
    unittest.main(verbosity=2)
