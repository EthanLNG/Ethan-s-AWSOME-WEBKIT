---
name: abc
description: Build and manage lettered variations (A/B/C/…) of one feature or visual change on a landing page, toggleable live via a small button fixed at the page's bottom-left. Use whenever the user invokes /ABC in any casing ("/ABC 10" means ten variations), asks for multiple versions/options/takes of a change ("make a few versions of the hero", "try 3 options for this button"), prefixes a request with variant letters ("A move it up", "AB tighten spacing", "in B and C swap the icon"), or asks to keep/save a winning variation and remove the rest. Also applies whenever a page already carries ABC experiment markers (data-abc attributes or ABC: comments) and the request touches that page.
---

# /ABC - lettered variations with a live switcher

The user often wants to *see* several takes on one change side by side before
committing. This skill turns one request into N lettered variations living on
the same page, switchable in the browser with a small bottom-left button, then
lets them iterate on specific letters and finally keep exactly one - leaving
zero trace of the experiment.

Each experiment is **scoped to the section it changes.** Its switcher only
appears while that section is in view, and it carries an id so a page can host
several independent experiments at once (a hero take *and* a pricing take)
without their buttons fighting over the corner. This matters because the user
runs experiments in different parts of a page in parallel - a switcher that
showed globally, or that toggled the wrong section, would be worse than none.

The whole system rests on one contract: **every byte the experiment adds is
marked** (and tagged with the experiment id), so "keep C, delete the rest" is
a mechanical operation on *that* experiment, never archaeology. Follow the
marker conventions exactly - they are what makes the cleanup lossless.

## Lifecycle

1. **Generate** - build N variations + the switcher widget.
2. **Iterate** - letter-addressed tweaks ("B lower it 10px"), any number of rounds.
3. **Finalize** - the user picks a winner: fold it into the page, delete everything else.

## 1. Reading the request

- `/ABC <request>` with no letters or count → **4 variations, A–D** (the default).
- `/ABC <N> <request>` (a bare integer right after /ABC) → N variations, first
  N letters (`/ABC 10` → A–J).
- Explicit per-letter directions ("A filled blue, B outline only") → build
  exactly those, nothing invented beyond what each letter asks.

**Pick the scope element first.** The change lives somewhere - a hero, a pricing
block, a nav. Find the element that contains it and give the experiment a short
id from it (`hero`, `pricing`, `ring-callout`). Tag that element
`data-abc-scope="<id>"` (add a wrapper only if there's genuinely no element to
use). Everything the experiment does is scoped under it, so the rest of the page
- including other experiments - is untouched.

**Scope to the smallest stable box that holds every variation - not reflexively
the enclosing `<section>`.** The scope element does double duty: it's the CSS
scope *and* the switcher's visibility trigger (the widget shows the button only
while that element's box is what you're looking at). So its box has to be the
part of the page the change is actually visible in. A `<section>` is the right
scope when the change reads across the whole of it (a hero's headline, a pricing
grid). It is the **wrong** scope when the experiment is one element inside a long
section - a callout at the top of a ten-step scroller, a badge on the first card
of a fifty-row table - because the button then hangs around for screens of page
where there is nothing to compare. Walk *up* from the changed element only until
you reach a node that (a) contains all the variant markup, (b) has a stable,
non-collapsed layout box, and (c) is on screen roughly when the change is. Stop
there.

Two things not to be fooled by when judging (b) and (c): a wrapper with no
height of its own (all children floated/absolute) gives a zero-height box the
observer can never see, so keep walking up; and a box whose *children* are moved
by transforms still measures at its own untransformed position, which is usually
what you want - the parked/rest position of the thing. If nothing satisfies all
three, wrap the variants in a `data-abc-scope` div of your own rather than
settling for the whole section.

**One experiment per section, several per page are fine.** Check the page for
existing markers (`grep -n 'ABC:'`) and note their ids.
- A fresh `/ABC <request>` whose change falls in a section that has **no** live
  experiment → just build it, even if other sections have live experiments.
- If it targets a section that **already** has a live experiment → that's a
  conflict on *that* section; tell the user and ask whether to finalize/abandon
  it first.
- `/ABC <N>` (bare count) with a live experiment → **resize** it: add fresh
  takes for the new letters (continuing the subtle→bold arc) or delete the
  dropped letters' fenced code, and update `LETTERS` in that widget's config.
  If more than one experiment is live, resize the current one (§5) and say
  which; if that's ambiguous, ask which section.

When directions aren't given per letter, make the N variations **genuinely
different approaches** to the request - vary the mechanism (scale, weight,
placement, style, motion), not micro-jitters of one idea. A useful default
ordering: A closest to the current design, last letter boldest. All the
project's normal design conventions still bind inside variations (brand
palette, typography rules, asset conventions, etc.).

## 2. Marker conventions (the contract)

The section element carries `data-abc-scope="<id>"` (static, marks the
experiment root) and, at runtime, `data-abc="X"` (the current letter, set by
the widget). Variant styles key on **both**, so they only ever apply inside
this experiment's section. Everything the experiment touches is fenced with
`ABC:` markers that include the id, so several experiments' markers never blur
together:

| What | Marker |
|---|---|
| Variant-specific CSS | `/* ABC:hero:B */ … rules … /* /ABC:hero:B */` |
| Variant-specific HTML | `<!-- ABC:hero:B --> … <!-- /ABC:hero:B -->` |
| Variant-specific JS | `/* ABC:hero:B */ … /* /ABC:hero:B */` |
| Shared groundwork this experiment needs | same fences with `ABC:hero:base` |
| The switcher | `<!-- ABC:widget hero --> … <!-- /ABC:widget hero -->` (from the asset) |

**CSS** - scope every variant rule under the section's scope + letter:

```css
/* ABC:hero:B */
[data-abc-scope="hero"][data-abc="B"] .cta { background: #fff; color: var(--blue); }
/* /ABC:hero:B */
```

Place variant blocks *after* the base rules they override - scoping wins on
specificity now, but at finalize the prefix is stripped and only source order
keeps the winner winning.

**HTML** - a variant that needs its own element gets `data-abc-only="B"` (space
-separate letters to share: `data-abc-only="B D"`). Put it **inside the scope
section** - the widget only toggles `data-abc-only` elements within its own
section - and inside `<!-- ABC:hero:B -->` fences so deletion stays grep-able.
It's toggled via inline `display`, so it works for any display type.

**JS** - branch on the **scope element's** live value *at use time*, not at
init, so switching without a reload behaves:

```js
/* ABC:hero:C */
const s = document.querySelector('[data-abc-scope="hero"]');
if (s.dataset.abc === 'C') el.classList.add('pulse');
/* /ABC:hero:C */
```

The scope element's `data-abc` is the source of truth; the widget also
dispatches `abc:change` (bubbling, detail `{id, variant}`) on it every switch if
something needs to react.

**`ABC:<id>:base`** is for groundwork every variant in this experiment builds on
(a wrapper, a `position: relative`, a shared keyframe). At finalize it is *kept*
(fences stripped); everything letter-marked is kept only for the winner. Note
`data-abc-scope` itself is base-level scaffolding - it's an attribute on an
existing element, removed at finalize, not something to fence.

One naming rule inside variant code: never give your own classes, ids, or
keyframes an `abc` stem (`cta-pulse`, not `abc-pulse`). Winner code survives
finalize, and the leftover check greps for the stem - it's reserved for
scaffolding that gets deleted (fences, `data-abc*`, the widget).

## 3. The switcher widget

Copy `assets/widget.html` (relative to this SKILL.md) verbatim to the end of
`<body>`, then edit only the `ABC:config` block:

- `ID` - the experiment id; **must equal** the section's `data-abc-scope`
  value. Also rename the two `ABC:widget hero` comment fences to your id.
- `LETTERS` - one char per live variation, e.g. `'ABCD'` or `'ABCDEFGHIJ'`.
- `RELOAD` - set `true` when variants involve draw-in scenes / scroll-triggered
  animation that can't re-render on a live switch; each click then reloads with
  `?abc-<id>=X` (scroll position is preserved). Default `false` = instant
  in-place switching, right for most CSS/layout tweaks.

For a second experiment on the same page, paste another copy with its own id -
the shared manager inside is guarded (`window.__abc || …`), so pasting it more
than once is safe; only the config differs.

What it gives you for free: a ~36px circle at the bottom-left showing the
current letter (click = next, Alt/Option-click = previous), that **only appears while its
section is the one in view** and, when two sections are on screen at once,
**stacks above the other's button instead of overlapping it**. Plus persistence
per (page path + id) via localStorage, an `?abc-<id>=X` URL override for
deep-link QA, and a `window.__abc` manager for headless checks
(`window.__abc.get('hero')` → `.current`, `.set('B')`, `.letters`;
`window.__abc.list()` → all live ids).

Don't restyle or reposition it per page - the user's muscle memory depends on
it looking the same everywhere. It's dev-only chrome; it disappears at finalize.

## 4. After generating

- QA at least: page loads clean; the button cycles through *all* letters; each
  letter visibly differs; `?abc-<id>=<last letter>` deep-link works. With a
  second experiment live, confirm the two buttons never overlap and each drives
  only its own scope.
- **Prove the button's range, don't eyeball it.** Scroll a few hundred px at a
  time from before the change to well past it, logging `btn.hidden` at each
  stop. The window where it's visible must bracket the change and close soon
  after - a button still showing screens later means the scope is too big
  (§1); one that never shows means the scope box is zero-height or off-path.
  Fix the scope and re-run the sweep; don't paper over it by restyling the
  widget.
- Reopen the preview as usual: `webkit/scripts/open-preview.sh "<url>?abc-hero=A"`
  (add the site's jump/anchor for the changed section - e.g. `#pricing` or its
  jump param - so the section is on screen and its button shows).
- In the reply, give **one line per letter** describing that take - the user
  decides from the browser, the lines are their map. Example:
  - `A - same button, 20% larger and bolder`
  - `B - outline style, fills on hover`
  - `C - lifted with a hard shadow, arrow on hover`
  - `D - pulses gently, adds a "no app" subline`

## 5. Iterating - who a message is for

When only one experiment is live, letters are unambiguous. When several are,
also track a **current experiment** - the section just generated or last
addressed - and resolve letters within it. The user names a section
("in the pricing one, B …", "the hero experiment") to switch which is current;
that section stays current until they name another. If a letter is genuinely
ambiguous across live experiments and they gave no section, ask which - a wrong
guess edits the wrong part of the page.

- A leading letter or letter-run addresses those variants: `B lower it 10px`,
  `AB move the button up`, `in A and B, move it up 10px`. Lowercase counts
  (`b lower it`) - users type fast. "All"/"both" (in whatever language the
  user writes) address every live letter **in the current experiment** for
  that message.
- The addressed set becomes the **current target** and stays current until the
  user names other letters. A letterless follow-up ("actually 6px") applies to
  the current target.
- No target chosen yet (right after generating) and the request touches the
  experimented element → it's a change to every take: put it in the experiment's
  `ABC:<id>:base` block (one place all variants see, survives finalize) rather
  than duplicating it into N letter blocks - unless a variant needs its own
  version of it - and say what you did in the first line of the reply.
- A request about something unrelated to the experiment is just a normal page
  edit - no letters involved, current target unchanged.
- A leading capital can be English ("A button that pulses") rather than an
  address. If the letter is a live variant and the sentence reads as an
  instruction, treat it as the address; when you had to judge, open the reply
  by stating the interpretation ("Applied to B:") so a miss costs one message.

Edits to variant X happen **only inside X's fenced blocks** (extending them as
needed). Never let a tweak for B leak into A's block or the shared page.

**Sign-off:** during a live experiment, end every reply with the current target
letters immediately before the session color emoji - `B 🔵`, `AB 🔵` - with the
emoji still the literal last character. If more than one experiment is live,
prefix the current section id so it's unambiguous - `hero B 🔵`. No target yet →
just the emoji, as always. After the last experiment is finalized → back to
just the emoji.

## 6. Finalizing - "save C, delete the others"

Finalize acts on **one experiment** (identify which by id - the one the user is
looking at / just named). Touch only that id's markers; any other experiment
stays live and untouched. When the user picks a winner (letter W of experiment
`hero`):

1. Delete that experiment's widget block (`ABC:widget hero` fences, everything
   between).
2. Delete every `ABC:hero:*` fenced block for letters ≠ W, including their
   `data-abc-only` elements.
3. Unwrap W: strip W's fences; in W's rules replace the
   `[data-abc-scope="hero"][data-abc="W"]` prefix with the section's own stable
   selector (its class/id, e.g. `.hero`) - this keeps the look exactly where it
   was *and* local to that section, and source order now keeps it winning.
   Delete `data-abc-only` attributes on kept elements; unwrap
   `dataset.abc === 'W'` conditionals keeping their bodies.
4. Strip `ABC:hero:base` fences, keeping the content but **likewise collapsing
   its `[data-abc-scope="hero"]` prefix to the section selector** (`.hero`), then
   remove the `data-abc-scope="hero"` attribute from the section. Both the base
   and winner rules were scoped through that attribute, so once it's gone their
   selectors must fall back to the section's own - otherwise the kept styles
   stop matching. (No class/id to collapse to? Drop the prefix entirely - fine
   when the inner selector is unique to that section.)
5. **Verify zero leftovers for this id:** `grep -rni "abc" <the page's files>` -
   every experiment token contains the stem (`ABC:`, `data-abc`, `abc-switch`,
   `abc:change`, `__abc`). Expect no `hero` hits; hits from *other* live
   experiments are fine (judge any unrelated coincidental content match on its
   merits). When the last experiment is finalized, expect no hits at all.
6. QA the section renders identically to how variant W looked, with that
   button gone, then reopen the preview (no `?abc-hero` param) for the user.

"Abandon" (no winner) is the same with step 3 skipped and `ABC:hero:base` blocks
deleted too - the section returns to its pre-experiment state.

**Never merge to a target branch with markers live.** If the user asks to merge while
*any* experiment is live, flag it and ask whether to finalize first.

## Evals

Functional evals live in `evals/evals.json` (fixtures in `evals/files/`),
covering generate/iterate/second-experiment/finalize. The fixture pages are
synthetic RTL Hebrew examples. The skill itself is language- and site-agnostic.
