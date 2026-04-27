# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

ufo-tdkit-tools is a Python library for PostScript hints extraction, optimization, and compilation in UFO fonts. Extracted from TDKit and ufo-widgets-gtk4 as a standalone dependency.

## Commands

```bash
uv sync                                          # Install all deps (including dev)
uv run pytest tests/ -v                          # Run all tests
uv run pytest tests/test_parser.py::TestParseStem -v  # Run a specific test class
uv run ruff check src/ tests/                    # Lint
uv run ruff format src/ tests/                   # Auto-format
uv build                                         # Build wheel
```

## Architecture

Three main workflows, each in its own subpackage under `src/ufo_tdkit_tools/`:

1. **`extraction/`** — Binary font (OTF/TTF/WOFF/WOFF2) → in-memory UFO. Uses `ufo-extractor` for bulk work plus custom CFF hint extraction from PostScript charstrings. Optional dependency group `[extraction]`.
   - `converter.py`: Top-level pipeline (`convert_binary_to_ufo`) and FEA post-processing — `_unify_feature_blocks` (one block per tag with internal script/language scoping), `_inline_aalt_lookups` (Adobe FEA spec compliance — `aalt` rejects `lookup` references), `_strip_lookup_noise`, `_strip_empty_gdef`. Drops the auto-generated `kern` feature.
   - `cff_hints.py`: Per-glyph hint extraction. Uses `afdko.otfautohint.otfFont.convertT2ToGlyphData` to parse stems, `startmasks`, per-pathElement `masks`, and `cntr` (counter masks). Emits multi-entry `hintSetList` mirroring AFDKO's canonical `addUfoHints` algorithm: hint anchors are line endpoints or first off-curve control points; counter-masked orientations collapse into `hstem3`/`vstem3` triplets. Also fills the font-level Private dict (`BlueValues`, `OtherBlues`, `StemSnap*`, etc.) and reorders `postscriptStemSnapH/V` so the CFF `StdHW`/`StdVW` value sits at index 0 (ufo2ft picks `StdHW = StemSnapH[0]` at compile time).

2. **`ps_hints/`** — Hint parsing, optimization, analysis, validation, and layer conversion. Always available (core dep: fontTools only).
   - `parser.py`: Data models (`PSHint`, `PSHintSet`, `PSHintData`, `HintSource` enum) and parsing from UFO glyph lib entries.
   - `optimizer.py`: 5-step pipeline — remove too-wide vstems → build coverage map (ray-casting, even-odd fill) → filter small-element stems → extract vstem3 triples → resolve overlaps. See `OPTIMIZER_ALGORITHM.md` for details.
   - `analyzer.py`: Same logic as optimizer but non-destructive; returns issue list.
   - `converter.py`: Move hints between the processedglyphs layer, glyph lib, and default layer.
   - `validator.py`: Validate hints across an entire UFO.

3. **`compilation/`** — UFO → OTF with hint preservation. Hybrid pipeline: ufo2ft creates an unhinted OTF shell with correct metadata; `tx -t1` + `makeotf` (the wrapper, not the deprecated `makeotfexe` stub) builds a hinted OTF from the UFO; per-glyph charstrings and the Private dict hint params are merged into the shell; `cffsubr` subroutinizes the result. Optional dependency group `[compilation]`. Supports batch processing via `ProcessPoolExecutor` (each worker spawns its own `makeotf` subprocess — no shared state).

**`constants.py`** — Shared lib keys (`com.adobe.type.autohint.v2`, `public.postscript.hints`, etc.), processed layer names, validation constants, and `compute_outline_hash()` (must match AFDKO's HashPointPen algorithm).

## Hint round-trip fidelity

The OTF → UFO → OTF round-trip preserves:

- All declared `hstem`/`vstem` positions and widths byte-for-byte.
- Hint substitution (`hintmask` operators) for glyphs whose substitution points are inside contours, via multi-entry `hintSetList`.
- Counter-mask grouping (`cntrmask` → `hstem3`/`vstem3` triplets).
- Font-level Private dict values including `StdHW`/`StdVW` (via the `postscriptStemSnapH/V[0]` convention).

Known limitation: hint substitutions that fire **between subpaths** (a `hintmask` immediately before a `moveto`, common on disconnected glyphs like `i`, `j`, dieresis-bearing letters) are not representable in the `autohint.v2` format and degrade to a single hint set. AFDKO's own autohint output exhibits the same limitation.

## Key Design Patterns

- **Lazy imports** via `__getattr__` in `__init__.py` for optional modules (extraction, compilation)
- **Hint source priority**: PROCESSED_LAYER > AUTOHINT_V2 > PUBLIC_PS — the processedglyphs layer is the canonical hint storage
- **Dataclasses** for all data structures and result objects
- **Stateless functions** for compilation — enables safe parallel processing
- **AFDKO Python API over subprocess** where possible — extraction calls `afdko.otfautohint` Python functions in-process; compilation still subprocesses `tx` and `makeotf` because they're external binaries

## Code Style

- Line length: 100 chars (ruff)
- Python >=3.10, uses `from __future__ import annotations`
- Google-style docstrings
- Copyright header: `# Copyright 2024 Alexander Lubovenko` + Apache 2.0
- Class-based test organization (`class TestXxx:`)
