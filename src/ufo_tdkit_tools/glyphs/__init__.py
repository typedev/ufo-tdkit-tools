# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Glyphs.app source import -- converts .glyphs / .glyphspackage to UFO.

Multi-master sources become UFO masters plus a .designspace; a single-master
source becomes one UFO. The conversion itself is glyphsLib's; this package
repairs real-world sources before glyphsLib sees them and restores what
glyphsLib drops after. See ``docs/GLYPHS_IMPORT.md``.

Requires the ``glyphs`` extra: ``pip install ufo-tdkit-tools[glyphs]``.
Importing this package does not need glyphsLib; the converter imports it
when it runs.
"""

_EXPORTS = {
    "apply_corner_components": "corners",
    "UnwritableOutlineError": "preflight",
    "preflight_source": "preflight",
    "glyphs_hints_to_stems": "ps_hints",
    "import_ps_hints": "ps_hints",
    "ufo_point_index": "ps_hints",
    "GLYPHS_EXTENSIONS": "converter",
    "GlyphsConversionResult": "converter",
    "GlyphsSourceInfo": "converter",
    "convert_glyphs_to_ufos": "converter",
    "default_output_dir": "converter",
    "describe_existing": "converter",
    "format_source_summary": "converter",
    "inspect_glyphs_source": "converter",
    "is_glyphs_source": "converter",
    "plan_output_paths": "converter",
}


def __getattr__(name):
    """Lazy imports, so importing the package stays cheap."""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(f"{__name__}.{module_name}"), name)


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))


__all__ = list(_EXPORTS)
