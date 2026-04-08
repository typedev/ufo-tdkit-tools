# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""
Core conversion pipeline: binary OTF/TTF/WOFF/WOFF2 -> UFO.

Uses ufo-extractor for the bulk of conversion (outlines, font info,
kerning, groups, TT instructions, anchors, features), then adds
custom PS hint extraction from CFF charstrings.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fontTools.ttLib import TTFont

from ufo_tdkit_tools.extraction.warnings import ConversionWarning, WarningSeverity

logger = logging.getLogger(__name__)

# Metadata keys stored in font.lib
LIB_KEY_CONVERTED = "com.typedev.fontRover.convertedFromBinary"
LIB_KEY_ORIGINAL_PATH = "com.typedev.fontRover.originalBinaryPath"
LIB_KEY_ORIGINAL_FORMAT = "com.typedev.fontRover.originalFormat"


@dataclass
class ConversionResult:
    """Result of binary -> UFO conversion."""

    font: Any  # fontParts RFont
    original_path: Path
    temp_dir: tempfile.TemporaryDirectory
    warnings: list[ConversionWarning] = field(default_factory=list)
    is_cff: bool = False
    glyph_count: int = 0
    hint_count: int = 0


def convert_binary_to_ufo(
    binary_path: Path | str,
    progress_callback: Callable[[int, str], None] | None = None,
) -> ConversionResult:
    """Convert a binary font file to an in-memory UFO.

    Steps:
    1. Open binary with fontTools TTFont
    2. Detect variable fonts (warn/refuse)
    3. Create temp directory + defcon Font
    4. Run extractor.extractUFO for outlines, info, kerning, features, etc.
    5. If CFF: extract per-glyph PS hints
    6. Verify font-level hint data
    7. Store metadata in font.lib
    8. Return ConversionResult

    Args:
        binary_path: Path to .otf/.ttf/.woff/.woff2 file.
        progress_callback: Optional (step, message) callback for UI progress.

    Returns:
        ConversionResult with loaded fontParts RFont and any warnings.

    Raises:
        FileNotFoundError: If binary_path doesn't exist.
        ValueError: If file format is unsupported.
        RuntimeError: If conversion fails critically.
    """
    binary_path = Path(binary_path).resolve()
    if not binary_path.exists():
        raise FileNotFoundError(f"Font file not found: {binary_path}")

    warnings: list[ConversionWarning] = []
    suffix = binary_path.suffix.lower()
    if suffix not in (".otf", ".ttf", ".woff", ".woff2"):
        raise ValueError(f"Unsupported font format: {suffix}")

    # ── Step 1: Open with fontTools ──────────────────────────────────────
    if progress_callback:
        progress_callback(0, "Opening binary font...")

    logger.info(f"Opening binary font: {binary_path}")
    tt_font = TTFont(str(binary_path))

    # ── Step 2: Check for variable fonts ─────────────────────────────────
    is_variable = "fvar" in tt_font
    if is_variable:
        warnings.append(
            ConversionWarning(
                category="general",
                severity=WarningSeverity.WARNING,
                message="Variable font detected -- extracting default instance only",
                details="Variable font decomposition to multi-master designspace "
                "is not supported yet. Only the default instance will be extracted.",
            )
        )
        logger.warning("Variable font detected, extracting default instance only")

    # Detect format
    is_cff = "CFF " in tt_font or "CFF2" in tt_font

    # ── Step 3: Create temp directory ────────────────────────────────────
    if progress_callback:
        progress_callback(1, "Preparing conversion...")

    temp_dir = tempfile.TemporaryDirectory(prefix="fontrover_import_")
    temp_ufo_path = os.path.join(temp_dir.name, f"{binary_path.stem}.ufo")

    # ── Step 4: Run extractor (without features -- we do our own) ───────
    if progress_callback:
        progress_callback(2, "Extracting font data (outlines, info, kerning)...")

    logger.info("Running ufo-extractor...")

    try:
        import defcon

        dest_font = defcon.Font()
        _run_extractor(str(binary_path), dest_font, warnings)
        # Save to disk so fontParts can open it
        dest_font.save(temp_ufo_path)
    except Exception as e:
        temp_dir.cleanup()
        raise RuntimeError(f"Extraction failed: {e}") from e

    # ── Step 5: Open with fontParts ──────────────────────────────────────
    if progress_callback:
        progress_callback(3, "Loading converted UFO...")

    try:
        import fontParts.world as fp

        ufo_font = fp.OpenFont(temp_ufo_path, showInterface=False)
    except Exception as e:
        temp_dir.cleanup()
        raise RuntimeError(f"Failed to open converted UFO: {e}") from e

    glyph_count = len(ufo_font)
    hint_count = 0

    # ── Step 6: Extract CFF per-glyph hints ──────────────────────────────
    if is_cff and "CFF2" not in tt_font:
        if progress_callback:
            progress_callback(4, "Extracting PostScript hints...")

        logger.info("Extracting CFF per-glyph hints...")
        try:
            from ufo_tdkit_tools.extraction.cff_hints import (
                extract_cff_hints,
                verify_font_level_hints,
            )

            hint_count, hint_warnings = extract_cff_hints(
                tt_font, ufo_font, progress_callback
            )
            warnings.extend(hint_warnings)

            # Verify font-level hint data
            info_warnings = verify_font_level_hints(tt_font, ufo_font)
            warnings.extend(info_warnings)

            if hint_count > 0:
                logger.info(f"Extracted PS hints for {hint_count} glyphs")
        except Exception as e:
            logger.error(f"PS hint extraction failed: {e}", exc_info=True)
            warnings.append(
                ConversionWarning(
                    category="hints",
                    severity=WarningSeverity.ERROR,
                    message=f"PS hint extraction failed: {e}",
                )
            )

    # ── Step 7: Extract and clean features ──────────────────────────────
    if progress_callback:
        progress_callback(5, "Extracting OpenType features...")

    _extract_clean_features(tt_font, ufo_font, warnings)

    # ── Step 8: Validate font info ───────────────────────────────────────
    if progress_callback:
        progress_callback(6, "Validating font info...")

    _validate_font_info(ufo_font, warnings)

    # ── Step 9: Store metadata ───────────────────────────────────────────
    ufo_font.lib[LIB_KEY_CONVERTED] = True
    ufo_font.lib[LIB_KEY_ORIGINAL_PATH] = str(binary_path)
    ufo_font.lib[LIB_KEY_ORIGINAL_FORMAT] = suffix

    # Close the TTFont to free memory
    tt_font.close()

    if progress_callback:
        progress_callback(7, "Done!")

    logger.info(
        f"Conversion complete: {binary_path.name} -> UFO "
        f"({glyph_count} glyphs, {hint_count} hinted)"
    )

    return ConversionResult(
        font=ufo_font,
        original_path=binary_path,
        temp_dir=temp_dir,
        warnings=warnings,
        is_cff=is_cff,
        glyph_count=glyph_count,
        hint_count=hint_count,
    )


def _run_extractor(
    source_path: str,
    dest_font: Any,
    warnings: list[ConversionWarning],
) -> None:
    """Run ufo-extractor for outlines, info, kerning (NOT features).

    Features are extracted separately via _extract_clean_features()
    to allow post-processing and cleanup.
    """
    try:
        from extractor import extractUFO

        extractUFO(
            source_path,
            dest_font,
            doGlyphs=True,
            doInfo=True,
            doKerning=True,
            doFeatures=False,  # Features handled separately
        )
    except ImportError:
        raise RuntimeError(
            "ufo-extractor is not installed. "
            "Install with: uv pip install ufo-extractor"
        )


def _extract_clean_features(
    tt_font: Any,
    ufo_font: Any,
    warnings: list[ConversionWarning],
) -> None:
    """Extract OpenType features with minimal, safe cleanup.

    Only two transformations are applied:
    1. Remove kern feature + orphaned lookups (kern is auto-generated
       by fontmake/ufo2ft from UFO kerning data)
    2. Fix aalt blocks in FEA text: strip script/language statements
       (invalid per OpenType spec, fontFeatures bug #64) and deduplicate
       resulting identical aalt blocks

    All other features are left exactly as fontFeatures produces them,
    preserving script/language targeting for locl, liga, ssXX, etc.
    """
    try:
        from fontFeatures.ttLib import unparse
    except ImportError:
        warnings.append(
            ConversionWarning(
                category="features",
                severity=WarningSeverity.INFO,
                message="fontFeatures not available, features not extracted",
            )
        )
        return

    try:
        ff = unparse(tt_font)
    except Exception as e:
        warnings.append(
            ConversionWarning(
                category="features",
                severity=WarningSeverity.ERROR,
                message=f"Feature decompilation failed: {e}",
            )
        )
        return

    # ── Step 1: Remove kern + orphaned lookups ───────────────────────────
    removed_routines: set[int] = set()
    if "kern" in ff.features:
        for ref in ff.features["kern"]:
            if hasattr(ref, "routine") and ref.routine is not None:
                removed_routines.add(id(ref.routine))
        del ff.features["kern"]
        logger.info("Stripped kern feature (auto-generated from UFO kerning)")

    # Remove orphaned routines (only referenced by kern)
    if removed_routines:
        still_referenced: set[int] = set()
        for tag, refs in ff.features.items():
            for ref in refs:
                if hasattr(ref, "routine") and ref.routine is not None:
                    still_referenced.add(id(ref.routine))
        orphaned = removed_routines - still_referenced
        if orphaned:
            ff.routines = [r for r in ff.routines if id(r) not in orphaned]
            logger.info(f"Removed {len(orphaned)} orphaned lookups from kern")

    # ── Step 2: Serialize to FEA ─────────────────────────────────────────
    try:
        fea_text = ff.asFea()
    except Exception as e:
        warnings.append(
            ConversionWarning(
                category="features",
                severity=WarningSeverity.ERROR,
                message=f"Feature serialization failed: {e}",
            )
        )
        return

    # ── Step 3: Fix FEA text ───────────────────────────────────────────
    fea_text = _fix_languagesystem_order(fea_text)
    fea_text = _fix_aalt_blocks(fea_text)

    if fea_text and fea_text.strip():
        ufo_font.features.text = fea_text
        warnings.append(
            ConversionWarning(
                category="features",
                severity=WarningSeverity.WARNING,
                message="OpenType features extracted (kern stripped, review recommended)",
                details="kern feature removed (auto-generated from UFO kerning). "
                "aalt cleaned (script/language removed per OpenType spec). "
                "All other features preserved with original script/language targeting.",
            )
        )
        logger.info(f"Features extracted ({len(fea_text)} chars)")
    else:
        logger.info("No features to extract")


def _fix_languagesystem_order(fea_text: str) -> str:
    """Ensure 'languagesystem DFLT dflt' is first if present.

    The OpenType FEA spec requires DFLT dflt to be the first
    languagesystem statement. fontFeatures may output them in
    arbitrary order depending on the font's ScriptList.
    """
    import re

    lines = fea_text.split("\n")
    ls_lines = []
    other_lines = []
    first_non_ls_idx = None

    for i, line in enumerate(lines):
        if re.match(r"\s*languagesystem\s+", line):
            ls_lines.append(line)
        else:
            if first_non_ls_idx is None and ls_lines:
                first_non_ls_idx = i
            other_lines.append(line)

    if not ls_lines or len(ls_lines) < 2:
        return fea_text

    # Sort: DFLT dflt first, then alphabetical
    dflt_line = None
    rest = []
    for line in ls_lines:
        if re.match(r"\s*languagesystem\s+DFLT\s+dflt\s*;", line):
            dflt_line = line
        else:
            rest.append(line)

    if dflt_line is None:
        return fea_text  # No DFLT dflt -- nothing to fix

    sorted_ls = [dflt_line] + rest
    # Rebuild: languagesystem block + rest of file
    return "\n".join(sorted_ls + other_lines)


def _fix_aalt_blocks(fea_text: str) -> str:
    """Fix aalt feature blocks: strip script/language, deduplicate.

    The OpenType spec says aalt has no script/language sensitivity,
    but fontFeatures (bug #64) emits script/language inside aalt blocks.
    This causes fontTools compilation errors.

    Fix: remove script/language lines from aalt blocks, then deduplicate
    resulting identical blocks.
    """
    import re

    block_pattern = re.compile(
        r"(feature\s+aalt\s*\{)(.*?)(\}\s*aalt\s*;)",
        re.DOTALL,
    )

    # First pass: strip script/language from aalt blocks
    def _clean_aalt_body(match: re.Match) -> str:
        prefix = match.group(1)
        body = match.group(2)
        suffix = match.group(3)
        # Remove script/language lines
        cleaned_lines = []
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("script ") or stripped.startswith("language "):
                continue
            cleaned_lines.append(line)
        return prefix + "\n".join(cleaned_lines) + suffix

    fea_text = block_pattern.sub(_clean_aalt_body, fea_text)

    # Second pass: deduplicate identical aalt blocks
    seen_aalt: set[str] = set()
    removals: list[tuple[int, int]] = []

    for match in block_pattern.finditer(fea_text):
        body = match.group(2)
        normalized = " ".join(body.split())
        if normalized in seen_aalt:
            removals.append((match.start(), match.end()))
        else:
            seen_aalt.add(normalized)

    if removals:
        result = []
        last_end = 0
        for start, end in removals:
            result.append(fea_text[last_end:start])
            last_end = end
        result.append(fea_text[last_end:])
        fea_text = "".join(result)
        # Clean up blank lines
        fea_text = re.sub(r"\n{3,}", "\n\n", fea_text)

    return fea_text


def _validate_font_info(ufo_font, warnings: list[ConversionWarning]) -> None:
    """Validate font info completeness after extraction."""
    info = ufo_font.info

    # Critical fields
    if not info.unitsPerEm:
        info.unitsPerEm = 1000
        warnings.append(
            ConversionWarning(
                category="info",
                severity=WarningSeverity.WARNING,
                message="unitsPerEm was missing, defaulted to 1000",
            )
        )

    if not info.familyName:
        # Try to derive from font file name
        original_path = ufo_font.lib.get(LIB_KEY_ORIGINAL_PATH, "")
        if original_path:
            info.familyName = Path(original_path).stem
        warnings.append(
            ConversionWarning(
                category="info",
                severity=WarningSeverity.INFO,
                message="familyName was missing, derived from filename",
            )
        )

    # Check optional but important fields
    missing = []
    if info.ascender is None:
        missing.append("ascender")
    if info.descender is None:
        missing.append("descender")
    if info.xHeight is None:
        missing.append("xHeight")
    if info.capHeight is None:
        missing.append("capHeight")

    if missing:
        warnings.append(
            ConversionWarning(
                category="info",
                severity=WarningSeverity.INFO,
                message=f"Missing font info fields: {', '.join(missing)}",
            )
        )
