---
name: webkit-setup
description: Start or reuse a webkit design-iteration session in a project that vendors webkit/ (Ethan's AWESOME WEBKIT). Use at session start and whenever the user asks to launch, open, preview, show, or test the website, or says "set up the webkit", "start a webkit session", "start the preview session", or "start the feedback session" — claiming the agent color, starting the stamping preview server, and opening the configured external desktop browser all happen through this.
---

# webkit-setup

This project vendors Ethan's AWESOME WEBKIT.

Read `webkit/SETUP.md` and follow it exactly, top to bottom.

The canonical instructions live there, not here; do not improvise, skip steps,
or substitute your own commands for the ones it gives. In particular, never
replace `webkit/scripts/open-preview.sh` with an agent in-app browser, Claude
preview pane, IDE webview, or generic browser tool: the script opens and reuses
the external browser chosen by `browser.app_name`.

Report the claimed color, port, and preview URL when done.
