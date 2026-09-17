#!/usr/bin/env python3
"""Read saved instructions explicitly, without burying them in scene metadata."""

import argparse
import json
import stat
import sys
from pathlib import Path


MAX_BYTES = 2 * 1024 * 1024
MISSING = object()


def load_document(path):
    path = Path(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("input must be a regular JSON file")
    with path.open("rb") as source:
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("input exceeds the 2 MiB feedback limit")
    document = json.loads(raw.decode("utf-8"))
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError("expected a version-1 feedback or verdict document")
    kind = document.get("kind")
    if kind == "feedback_update":
        raise ValueError("feedback_update is a wake-up marker; read live feedback.json")
    if kind not in ("feedback", "verdicts"):
        raise ValueError("expected feedback.json or verdicts.json")
    key = "points" if kind == "feedback" else "verdicts"
    items = document.get(key)
    id_key = "id" if kind == "feedback" else "pointId"
    if not isinstance(items, list) or len(items) > 500:
        raise ValueError("expected at most 500 point records")
    ids = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("point record must be an object")
        point_id = item.get(id_key)
        if not isinstance(point_id, str) or not point_id or point_id in ids:
            raise ValueError("point ids must be nonempty and unique")
        ids.add(point_id)
    return document, items, id_key


def instruction_fields(kind):
    return ("text", "voiceNote", "abcRequest") if kind == "feedback" else (
        "redoText", "redoVoiceNote", "redoAbcRequest"
    )


def text_state(value):
    if value is MISSING:
        return "MISSING FIELD"
    if not isinstance(value, str):
        return "INVALID TYPE"
    if not value.strip():
        return "EMPTY ({} characters)".format(len(value))
    return "PRESENT ({} characters)".format(len(value))


def render(document, items, id_key, point_id=None, context=False):
    lines = ["Saved {} instructions; batch {}; round {}".format(
        document["kind"], document.get("batchId"), document.get("round")
    )]
    text_key, voice_key, abc_key = instruction_fields(document["kind"])
    if point_id is None:
        lines.append("Inventory only. Read EACH id separately with --point ID before acting.")
        for item in items:
            lines.append("Point {} | {} | {}={} | {}={} | {}={}".format(
                item.get("number", item.get("verdict", "?")), item[id_key],
                text_key, text_state(item.get(text_key, MISSING)),
                voice_key, "PRESENT" if item.get(voice_key) else "none",
                abc_key, "PRESENT" if item.get(abc_key) else "none",
            ))
        return "\n".join(lines) + "\n"

    matches = [item for item in items if item[id_key] == point_id]
    if not matches:
        raise ValueError("point id was not found in this saved document")
    item = matches[0]
    lines.append("Point id: " + point_id)
    if context:
        if document["kind"] != "feedback":
            raise ValueError("context belongs to the point in feedback.json")
        for key in ("context", "rectContexts", "rectSurfaces", "uiState", "abcState"):
            value = item.get(key, MISSING)
            lines.append(key + ": " + ("MISSING FIELD" if value is MISSING else
                                       json.dumps(value, ensure_ascii=False)))
        return "\n".join(lines) + "\n"

    for key in ("number", "revision", "page", "status", "verdict", "chosenLetter",
                "rect", "rects", "viewport", "scroll", "anchor"):
        if key in item:
            lines.append(key + ": " + json.dumps(item[key], ensure_ascii=False))
    value = item.get(text_key, MISSING)
    lines.append(text_key + ": " + text_state(value))
    if isinstance(value, str):
        lines.extend(["BEGIN SAVED USER INSTRUCTION", value, "END SAVED USER INSTRUCTION"])
    for key in (voice_key, abc_key):
        lines.append(key + ": " + json.dumps(item.get(key), ensure_ascii=False))
    lines.append("End of point. Context is separate: repeat with --context if needed.")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", help="saved feedback.json or verdicts.json")
    parser.add_argument("--point", help="exact opaque point id from the inventory")
    parser.add_argument("--context", action="store_true", help="read only this point's scene metadata")
    args = parser.parse_args(argv)
    if args.context and not args.point:
        parser.error("--context requires --point")
    try:
        document, items, id_key = load_document(args.document)
        output = render(document, items, id_key, args.point, args.context)
    except (OSError, ValueError, UnicodeError) as error:
        print("read-feedback.py: " + str(error), file=sys.stderr)
        return 2
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
