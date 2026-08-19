#!/bin/bash
# config-get.sh - the ONE place shell scripts read webkit config from.
#
# Every other script in this kit (claim-color.sh, release-color.sh,
# open-preview.sh, the preview server's shell callers, …) gets its palette /
# ports / lock dir / browser mode by calling THIS script. Config resolution
# lives in exactly one place, and a project only ever edits webkit.config.json
# - never the scripts.
#
# Config resolution order:
#   1. $WK_CONFIG                          explicit override (tests, odd layouts)
#   2. <kit>/webkit.config.json            the installed, filled-in config
#   3. <kit>/webkit.config.template.json   fallback so a fresh clone works
#                                          out-of-the-box for a quick demo,
#                                          before install fills in real values
#
# Usage:
#   config-get.sh get <dot.path>    scalar lookup - e.g. `get browser.mode`,
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
import json, os, re, stat, sys, unicodedata

path = sys.argv[1]
args = sys.argv[2:]

def die(msg):
    sys.stderr.write("config-get.sh: %s\n" % msg)
    sys.exit(1)

def reject_constant(value):
    raise ValueError("non-standard JSON constant: %s" % value)

def reject_surrogates(value):
    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError("JSON contains an unpaired Unicode surrogate")
    elif isinstance(value, list):
        for item in value:
            reject_surrogates(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            reject_surrogates(key)
            reject_surrogates(item)

maximum = 1024 * 1024
descriptor = None
try:
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        die("config must be a regular file, not a link or special path")
    if before.st_size > maximum:
        die("config exceeds 1 MB")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    opened = os.fstat(descriptor)
    chunks, remaining = [], maximum + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, 65536))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    after_read = os.fstat(descriptor)
    current = os.lstat(path)
except OSError as e:
    die("cannot read config %s (%s)" % (path, e))
finally:
    if descriptor is not None:
        os.close(descriptor)

def signature(value):
    return (
        value.st_dev, value.st_ino, value.st_mode, value.st_size,
        getattr(value, "st_mtime_ns", int(value.st_mtime * 1000000000)),
    )

if (
    not stat.S_ISREG(opened.st_mode)
    or not stat.S_ISREG(current.st_mode)
    or signature(opened) != signature(before)
    or signature(opened) != signature(after_read)
    or signature(opened) != signature(current)
):
    die("config changed while it was read")
data = b"".join(chunks)
if len(data) > maximum:
    die("config exceeds 1 MB")
try:
    cfg = json.loads(data.decode("utf-8"), parse_constant=reject_constant)
    reject_surrogates(cfg)
except (UnicodeDecodeError, ValueError) as e:
    die("invalid JSON in %s (%s)" % (path, e))

if not isinstance(cfg, dict):
    die("config root in %s must be an object" % path)

grace_seconds = cfg.get("grace_seconds", 180)
if (
    isinstance(grace_seconds, bool)
    or not isinstance(grace_seconds, int)
    or not 30 <= grace_seconds <= 86400
):
    die("grace_seconds must be an integer from 30 through 86400")
cfg["grace_seconds"] = grace_seconds

def validated_palette():
    pal = cfg.get("palette")
    if not isinstance(pal, list) or not pal:
        die("no palette[] in %s" % path)

    slugs, emojis, ports = set(), set(), set()
    for index, color in enumerate(pal):
        label = "palette[%d]" % index
        if not isinstance(color, dict):
            die("%s must be an object" % label)

        missing = [key for key in ("slug", "emoji", "port") if key not in color]
        if missing:
            die("%s must have slug/emoji/port" % label)

        slug = color["slug"]
        emoji = color["emoji"]
        port = color["port"]
        if not isinstance(slug, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", slug) is None:
            die("%s.slug must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}" % label)
        bidi_controls = {"LRE", "RLE", "PDF", "LRO", "RLO", "LRI", "RLI", "FSI", "PDI"}
        if (
            not isinstance(emoji, str)
            or not 1 <= len(emoji) <= 32
            or any(
                ch.isspace()
                or unicodedata.category(ch) == "Cc"
                or ch in "<>&\"\u0027`"
                or unicodedata.bidirectional(ch) in bidi_controls
                for ch in emoji
            )
        ):
            die("%s.emoji must be a short token without whitespace, control characters, bidi controls, or markup" % label)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            die("%s.port must be an integer from 1 through 65535" % label)

        if slug in slugs:
            die("duplicate palette slug %r" % slug)
        if emoji in emojis:
            die("duplicate palette emoji %r" % emoji)
        if port in ports:
            die("duplicate palette port %r" % port)
        slugs.add(slug)
        emojis.add(emoji)
        ports.add(port)
    return pal

# Validate once for every operation. This keeps all shell callers on the same
# safe palette contract, including callers that only request another key.
palette = validated_palette()

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
        die("%r is not a scalar - use a deeper dot-path" % args[1])
    if isinstance(node, str) and any(
        unicodedata.category(ch) == "Cc" or ch in "\u2028\u2029" for ch in node
    ):
        die("%r contains control or newline characters" % args[1])
    if isinstance(node, bool):
        print("true" if node else "false")
    elif node is None:
        print("null")
    else:
        print(node)

elif cmd == "palette" and len(args) == 1:
    for c in palette:
        print(c["slug"], c["emoji"], c["port"])

elif cmd == "color" and len(args) == 2:
    for c in palette:
        if c["emoji"] == args[1]:
            print(c["slug"], c["port"])
            sys.exit(0)
    die("%r is not a palette color (config: %s)" % (args[1], path))

else:
    die("usage: config-get.sh get <dot.path> | palette | color <emoji>")
' "$config" "$@"
