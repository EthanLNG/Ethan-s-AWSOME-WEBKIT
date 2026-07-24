#!/bin/bash
# config-get.sh — the ONE place shell scripts read webkit config from.
#
# Every other script in this kit (claim-color.sh, release-color.sh,
# open-preview.sh, the preview server's shell callers, …) gets its palette /
# ports / lock dir / browser mode by calling THIS script. Config resolution
# lives in exactly one place, and a project only ever edits webkit.config.json
# — never the scripts.
#
# Config resolution order:
#   1. $WK_CONFIG                          explicit override (tests, odd layouts)
#   2. <kit>/webkit.config.json            the installed, filled-in config
#   3. <kit>/webkit.config.template.json   fallback so a fresh clone works
#                                          out-of-the-box for a quick demo,
#                                          before install fills in real values
#
# Usage:
#   config-get.sh get <dot.path>    scalar lookup — e.g. `get browser.mode`,
#                                   `get lock_dir`, `get palette.0.port`
#                                   (numeric path parts index into lists)
#   config-get.sh palette           one line per color, in claim order:
#                                   "slug emoji port"
#   config-get.sh color <emoji>     look up one color by emoji: "slug port"
#
# Missing key / unknown emoji / unreadable or broken JSON: one-line message on
# stderr, exit 1. Only real values ever land on stdout, so callers can safely
# do  foo="$(config-get.sh get bar)"  under `set -e`.
#
# stdlib-only by design: bash + python3 (json). No jq, no Node.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

if [ -n "${WK_CONFIG:-}" ]; then
  config="$WK_CONFIG"
elif [ -f "$script_dir/../webkit.config.json" ]; then
  config="$script_dir/../webkit.config.json"
else
  config="$script_dir/../webkit.config.template.json"
fi

# One python3 -c does all the JSON work. Python string literals below are
# double-quoted exclusively so the whole program can sit in a single-quoted
# bash string.
exec python3 -c '
import json, sys

path = sys.argv[1]
args = sys.argv[2:]

def die(msg):
    sys.stderr.write("config-get.sh: %s\n" % msg)
    sys.exit(1)

try:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
except OSError as e:
    die("cannot read config %s (%s)" % (path, e))
except ValueError as e:
    die("invalid JSON in %s (%s)" % (path, e))

cmd = args[0] if args else ""

if cmd == "get" and len(args) == 2:
    node, seen = cfg, []
    for part in args[1].split("."):
        seen.append(part)
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            die("no key %r in %s" % (".".join(seen), path))
    if isinstance(node, (dict, list)):
        die("%r is not a scalar — use a deeper dot-path" % args[1])
    if isinstance(node, bool):
        print("true" if node else "false")
    elif node is None:
        print("null")
    else:
        print(node)

elif cmd == "palette" and len(args) == 1:
    pal = cfg.get("palette")
    if not isinstance(pal, list) or not pal:
        die("no palette[] in %s" % path)
    for c in pal:
        try:
            print(c["slug"], c["emoji"], c["port"])
        except (TypeError, KeyError):
            die("palette entry %r must have slug/emoji/port" % (c,))

elif cmd == "color" and len(args) == 2:
    pal = cfg.get("palette")
    if not isinstance(pal, list):
        die("no palette[] in %s" % path)
    for c in pal:
        if isinstance(c, dict) and c.get("emoji") == args[1]:
            try:
                print(c["slug"], c["port"])
            except KeyError:
                die("palette entry %r must have slug/port" % (c,))
            sys.exit(0)
    die("%r is not a palette color (config: %s)" % (args[1], path))

else:
    die("usage: config-get.sh get <dot.path> | palette | color <emoji>")
' "$config" "$@"
