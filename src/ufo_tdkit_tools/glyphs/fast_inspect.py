# Copyright 2024-2026 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""
Reading a Glyphs source's shape without reading its glyphs.

The destination dialog needs a handful of numbers — how many masters, which
axes, how many glyphs — and nothing else. Getting them through
`glyphsLib.load()` means parsing every glyph in the file: measured at 3.3 s for
a 14 MB source and **44 s** for a large multi-axis source, twice over, while the dialog
sits there looking hung.

Everything the dialog shows lives in one place. A `.glyphspackage` keeps it in
`fontinfo.plist` beside a directory of one file per glyph; a single `.glyphs`
file has the same structure with the glyphs inline. Reading that part alone
takes 8 ms.

The parsing itself is still glyphsLib's: the plist goes through its own
`Parser` into a real `GSFont`, so field names, type coercion and custom
parameters behave exactly as they do in a full load. Only the glyphs are
missing, and only the caller who does not need them comes here.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# A glyph whose name starts with one of these is a part, not a letter: Glyphs
# draws corners and caps by placing them as components.
_PART_PREFIXES = ("_corner.", "_cap.", "_segment.", "_brush.")

#: `corner_component_count` when the source was read the cheap way. The parts
#: are visible in the glyph names; how many times they are *placed* is not,
#: and the conversion reports the real number when it is done.
COUNT_UNKNOWN = -1


def _load_plist(path: Path) -> Any:
    import openstep_plist

    with path.open("r", encoding="utf-8") as handle:
        return openstep_plist.load(handle, use_numbers=True)


def _font_from_data(data: dict) -> Any:
    """Build a GSFont from plist data, glyphs left out."""
    from glyphsLib.classes import GSFont
    from glyphsLib.parser import Parser

    from .format4 import FORMAT_NOTES_ATTR, normalize_format4

    data = dict(data)
    data["glyphs"] = []
    notes = normalize_format4(data)
    font = GSFont()
    Parser(current_type=GSFont).parse_into_object(font, data)
    setattr(font, FORMAT_NOTES_ATTR, notes)
    return font


def quick_inspect(source_path: Path | str):
    """The summary alone; see :func:`read_source` when the font is needed too."""
    return read_source(source_path)[0]


def read_source(source_path: Path | str):
    """Read a Glyphs source's masters, axes and instances, skipping its glyphs.

    Args:
        source_path: Path to a `.glyphs` file or `.glyphspackage` directory.

    Returns:
        `(info, font)`. The info's `corner_component_count` is either 0 or
        `COUNT_UNKNOWN` (parts are present, count unknown), and its hint counts
        are `COUNT_UNKNOWN` — those need the glyphs. The font is a real
        `GSFont` with no glyphs in it: small enough to keep while a dialog is
        open, and enough to work out the filenames a conversion would write.

    Raises:
        FileNotFoundError: the source does not exist.
        Exception: whatever the plist parser raises for an unreadable source.
    """
    from .converter import GlyphsSourceInfo
    from .format4 import source_format_version

    source = Path(source_path).resolve()
    if not source.exists():
        raise FileNotFoundError(f"Glyphs source not found: {source}")

    if source.is_dir():
        data = _load_plist(source / "fontinfo.plist")
        glyph_names = _package_glyph_names(source)
    else:
        data = _load_plist(source)
        glyph_names = [g.get("glyphname", "") for g in data.get("glyphs", [])]

    font = _font_from_data(data)

    has_parts = any(name.startswith(_PART_PREFIXES) for name in glyph_names)

    info = GlyphsSourceInfo(
        path=source,
        family_name=font.familyName or source.stem,
        master_count=len(font.masters),
        glyph_count=len(glyph_names),
        instance_count=len(font.instances),
        axes=[(a.name, a.axisTag) for a in font.axes],
        corner_component_count=COUNT_UNKNOWN if has_parts else 0,
        ps_hint_count=COUNT_UNKNOWN,
        tt_hint_count=COUNT_UNKNOWN,
        format_version=source_format_version(font),
    )
    return info, font


def _package_glyph_names(package: Path) -> list[str]:
    """Glyph names of a .glyphspackage, from its order file or its directory."""
    order = package / "order.plist"
    if order.exists():
        try:
            names = _load_plist(order)
            if isinstance(names, list):
                return [str(n) for n in names]
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Could not read %s: %s", order, exc)

    glyphs_dir = package / "glyphs"
    if glyphs_dir.is_dir():
        # A glyph file is named after the glyph, with uppercase letters
        # escaped ("A_.glyph"); the name is only needed to spot parts.
        return [p.stem.replace("_", "") for p in glyphs_dir.glob("*.glyph")]
    return []


def plan_output_filenames(gs_font: Any) -> list[str]:
    """The UFO filenames a conversion of this font would write.

    Reproduces `glyphsLib`'s own naming so the dialog can say which files a
    destination already holds without building a designspace — that build is
    the expensive half of a conversion (21 s where the parse is 3 s), and it
    used to run again on every change of destination.

    Layer sources (brace layers) write into the UFO of the master they belong
    to, so there is one filename per master.
    """
    from glyphsLib.builder.constants import UFO_FILENAME_CUSTOM_PARAM
    from glyphsLib.util import build_ufo_path

    family = gs_font.familyName or "Unnamed"
    names: list[str] = []
    seen: dict[str, int] = {}

    for master in gs_font.masters:
        explicit = master.customParameters[UFO_FILENAME_CUSTOM_PARAM]
        if explicit:
            name = str(explicit)
        else:
            name = Path(build_ufo_path("", family, master.name)).name

        if name in seen:
            # glyphsLib disambiguates same-named masters with " #n".
            seen[name] += 1
            stem = Path(name).stem
            name = f"{stem} #{seen[name]}.ufo"
        else:
            seen[name] = 0
        names.append(name)

    return names


__all__ = ["quick_inspect", "read_source", "plan_output_filenames", "COUNT_UNKNOWN"]
