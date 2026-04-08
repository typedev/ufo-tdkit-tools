# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""
Structural validator for PostScript hints in UFO fonts.

Checks ``com.adobe.type.autohint.v2`` entries in each glyph's lib for
structural correctness and CFF spec compliance. Does not block compilation --
callers decide whether to abort or continue.
"""

from __future__ import annotations

import logging

from ufo_tdkit_tools.constants import (
    ADOBE_HINT_KEY_V2,
    MAX_STEMS_PER_HINTSET,
    VALID_STEM_TYPES,
)

logger = logging.getLogger(__name__)


def validate_ps_hints(ufo_path, logger=None):
    """Validate PS hint data in all glyphs of a UFO.

    Checks ``com.adobe.type.autohint.v2`` entries in each glyph's lib for
    structural correctness and CFF spec compliance.

    Args:
        ufo_path: Path to a UFO directory.
        logger: Optional logger for summary output.

    Returns:
        dict: {
            "valid": bool,               # False if any errors found
            "glyphs_checked": int,
            "glyphs_with_hints": int,
            "errors": [{"glyph": str, "message": str}, ...],
            "warnings": [{"glyph": str, "message": str}, ...],
        }
    """
    import defcon

    ufo = defcon.Font(ufo_path)
    errors = []
    warnings = []
    glyphs_with_hints = 0

    for glyph_name in ufo.keys():
        glyph = ufo[glyph_name]
        hint_data = glyph.lib.get(ADOBE_HINT_KEY_V2)
        if hint_data is None:
            continue

        glyphs_with_hints += 1

        # E1: hint data must be a dict
        if not isinstance(hint_data, dict):
            errors.append({"glyph": glyph_name, "message": "hint data is not a dict"})
            continue

        hint_set_list = hint_data.get("hintSetList")

        # E2: hintSetList must be present and a list
        if hint_set_list is None or not isinstance(hint_set_list, list):
            errors.append(
                {"glyph": glyph_name, "message": "hintSetList missing or not a list"}
            )
            continue

        # E3: hintSetList must not be empty
        if len(hint_set_list) == 0:
            errors.append({"glyph": glyph_name, "message": "hintSetList is empty"})
            continue

        for idx, hint_set in enumerate(hint_set_list):
            prefix = f"hintSetList[{idx}]"

            # E4: each element must be a dict
            if not isinstance(hint_set, dict):
                errors.append({"glyph": glyph_name, "message": f"{prefix}: not a dict"})
                continue

            # W2: pointTag should be present
            if "pointTag" not in hint_set:
                warnings.append(
                    {"glyph": glyph_name, "message": f"{prefix}: missing pointTag"}
                )

            stems = hint_set.get("stems")

            # E5: stems must be present and a list
            if stems is None or not isinstance(stems, list):
                errors.append(
                    {"glyph": glyph_name, "message": f"{prefix}: stems missing or not a list"}
                )
                continue

            stem_count = 0
            for si, stem_str in enumerate(stems):
                # E6: each stem must be a string
                if not isinstance(stem_str, str):
                    errors.append(
                        {
                            "glyph": glyph_name,
                            "message": f"{prefix}.stems[{si}]: not a string",
                        }
                    )
                    continue

                parts = stem_str.split()
                if not parts:
                    errors.append(
                        {
                            "glyph": glyph_name,
                            "message": f"{prefix}.stems[{si}]: empty string",
                        }
                    )
                    continue

                cmd = parts[0]
                params = parts[1:]

                # E7: unknown command
                if cmd not in VALID_STEM_TYPES:
                    errors.append(
                        {
                            "glyph": glyph_name,
                            "message": f"{prefix}.stems[{si}]: unknown command '{cmd}'",
                        }
                    )
                    continue

                # E8/E9: parameter count
                expected = 6 if cmd.endswith("3") else 2
                if len(params) != expected:
                    errors.append(
                        {
                            "glyph": glyph_name,
                            "message": (
                                f"{prefix}.stems[{si}]: '{cmd}' expects "
                                f"{expected} params, got {len(params)}"
                            ),
                        }
                    )
                    continue

                # E10: params must parse as numbers
                parsed_ok = True
                values = []
                for pi, p in enumerate(params):
                    try:
                        values.append(float(p))
                    except ValueError:
                        errors.append(
                            {
                                "glyph": glyph_name,
                                "message": f"{prefix}.stems[{si}]: param '{p}' is not a number",
                            }
                        )
                        parsed_ok = False
                        break

                if not parsed_ok:
                    continue

                # W1: zero-width stem
                if cmd in ("hstem", "vstem") and values[1] == 0:
                    warnings.append(
                        {
                            "glyph": glyph_name,
                            "message": (
                                f"{prefix}.stems[{si}]: zero-width stem "
                                f"({cmd} {params[0]} 0)"
                            ),
                        }
                    )

                stem_count += 1

            # E11: too many stems in one hintSet
            if stem_count > MAX_STEMS_PER_HINTSET:
                errors.append(
                    {
                        "glyph": glyph_name,
                        "message": (
                            f"{prefix}: {stem_count} stems exceeds "
                            f"CFF limit of {MAX_STEMS_PER_HINTSET}"
                        ),
                    }
                )

    result = {
        "valid": len(errors) == 0,
        "glyphs_checked": len(ufo.keys()),
        "glyphs_with_hints": glyphs_with_hints,
        "errors": errors,
        "warnings": warnings,
    }

    if logger:
        logger.info(
            f"PS hint validation: {result['glyphs_checked']} glyphs checked, "
            f"{result['glyphs_with_hints']} with hints, "
            f"{len(errors)} errors, {len(warnings)} warnings"
        )

    return result
