# Copyright 2026 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""
Reading a Glyphs 4 source (`.formatVersion = 4`) — experimental.

glyphsLib knows formats 2 and 3 and branches on `format_version == 3`, so a
format-4 file falls into the format-2 readers and dies on the first node with
`TypeError: expected string or bytes-like object, got 'list'`. Structurally a
format-4 file is format 3 with a few changes, so the plist is rewritten into
format 3 *before* glyphsLib parses it:

- `.formatVersion` becomes 3, which routes every reader and builder to the
  format-3 branches.
- The top-level `familyName` is gone; the family lives only in
  `properties -> familyNames`. glyphsLib does not look there and calls the font
  "Unnamed font", which also names every UFO. The default-language value is
  copied back to `familyName`.
- An instance has no `name` either; its style lives only in
  `properties -> styleNames`, and glyphsLib names every such instance
  "Regular" -- which also makes them collide in the axis mapping. The
  default-language style name is copied back to `name`.
- A new node type `ct` appears on curve points. On the sources seen so far its
  handles are collinear, i.e. a smooth point, so it is read as `cs`. Without
  the rewrite glyphsLib reads it as a corner curve point (the shape is the
  same, the smooth flag is lost).

Anything else format 4 changed goes through unnoticed, which is why every
import of such a source says so in its report.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ufo_tdkit_tools.extraction.warnings import ConversionWarning, WarningSeverity

FORMAT_4 = 4

#: Format-4 node types and the format-3 type they are read as.
_NODE_TYPE_MAP = {"ct": "cs"}

#: Set on a GSFont read from a format-4 source: the warnings describing what
#: the rewrite did, picked up by `preflight_source()` for the import report.
FORMAT_NOTES_ATTR = "_font_rover_format_notes"


def _default_localized(properties: Any, key: str) -> str | None:
    """The `dflt` value of a localized property, else its first value."""
    if not isinstance(properties, list):
        return None
    for entry in properties:
        if not isinstance(entry, dict) or entry.get("key") != key:
            continue
        if "value" in entry:
            return str(entry["value"])
        values = entry.get("values") or []
        for item in values:
            if isinstance(item, dict) and item.get("language") == "dflt":
                return str(item.get("value"))
        if values and isinstance(values[0], dict) and "value" in values[0]:
            return str(values[0]["value"])
    return None


def _rewrite_nodes(obj: Any) -> int:
    """Rewrite format-4 node types in place everywhere below `obj`; return the count."""
    count = 0
    stack = [obj]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            nodes = item.get("nodes")
            if isinstance(nodes, list):
                for i, node in enumerate(nodes):
                    if (
                        isinstance(node, (list, tuple))
                        and len(node) >= 3
                        and node[2] in _NODE_TYPE_MAP
                    ):
                        rewritten = list(node)
                        rewritten[2] = _NODE_TYPE_MAP[node[2]]
                        nodes[i] = rewritten
                        count += 1
            stack.extend(v for k, v in item.items() if k != "nodes")
        elif isinstance(item, list):
            stack.extend(item)
    return count


def normalize_format4(data: dict) -> list[ConversionWarning]:
    """Rewrite a format-4 plist into format 3 in place.

    Args:
        data: The top-level plist dict of a `.glyphs` file (or a package's
            `fontinfo.plist` with the glyphs merged in).

    Returns:
        The warnings for the import report; empty when the data is not format 4
        and nothing was touched.
    """
    if data.get(".formatVersion") != FORMAT_4:
        return []

    data[".formatVersion"] = 3
    details = [
        "glyphsLib reads formats 2 and 3 only; this source was read as format 3. "
        "Check the result against the source in Glyphs."
    ]

    if not data.get("familyName"):
        family = _default_localized(data.get("properties"), "familyNames")
        if family:
            data["familyName"] = family
            details.append(f"Family name taken from the localized family names: {family}.")

    named = 0
    for instance in data.get("instances") or []:
        if isinstance(instance, dict) and not instance.get("name"):
            style = _default_localized(instance.get("properties"), "styleNames")
            if style:
                instance["name"] = style
                named += 1
    if named:
        details.append(f"{named} instance name(s) taken from the localized style names.")

    tangents = _rewrite_nodes(data.get("glyphs", []))
    if tangents:
        details.append(f"{tangents} tangent node(s) ('ct') read as smooth curve points.")

    return [
        ConversionWarning(
            category="preflight",
            severity=WarningSeverity.WARNING,
            message="Glyphs 4 format source: import is experimental",
            details=" ".join(details),
        )
    ]


def load_glyphs_source(source_path: "Path | str"):
    """`glyphsLib.load()` that also reads format 4.

    The format-4 notes are left on the font under `FORMAT_NOTES_ATTR`, so they
    reach the import report whoever parsed the font.
    """
    import openstep_plist
    from glyphsLib.classes import GSFont
    from glyphsLib.parser import Parser, load_glyphspackage

    path = os.fspath(source_path)
    if os.path.isdir(path):
        data = load_glyphspackage(path)
    else:
        with open(path, "r", encoding="utf-8") as handle:
            data = openstep_plist.load(handle, use_numbers=True)

    notes = normalize_format4(data)
    font = GSFont()
    Parser(current_type=GSFont).parse_into_object(font, data)
    setattr(font, FORMAT_NOTES_ATTR, notes)
    return font


def source_format_version(font: Any) -> int:
    """The format the source was saved in: 4 for a rewritten one, else the font's own."""
    if getattr(font, FORMAT_NOTES_ATTR, None):
        return FORMAT_4
    return getattr(font, "format_version", 2)
