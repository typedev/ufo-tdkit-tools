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

2. **`ps_hints/`** — Hint parsing, optimization, analysis, validation, and layer conversion. Always available (core dep: fontTools only).
   - `parser.py`: Data models (`PSHint`, `PSHintSet`, `PSHintData`, `HintSource` enum) and parsing from UFO glyph lib entries.
   - `optimizer.py`: 5-step pipeline — remove too-wide vstems → build coverage map (ray-casting, even-odd fill) → filter small-element stems → extract vstem3 triples → resolve overlaps. See `OPTIMIZER_ALGORITHM.md` for details.
   - `analyzer.py`: Same logic as optimizer but non-destructive; returns issue list.
   - `converter.py`: Move hints between the processedglyphs layer, glyph lib, and default layer.
   - `validator.py`: Validate hints across an entire UFO.

3. **`compilation/`** — UFO → OTF with hint preservation. Hybrid pipeline: ufo2ft creates an unhinted OTF shell, then tx+makeotfexe create a hinted OTF, per-glyph charstrings are merged, and cffsubr subroutinizes the result. Optional dependency group `[compilation]`. Supports batch processing via `ProcessPoolExecutor` (functions are pure/stateless).

**`constants.py`** — Shared lib keys (`com.adobe.type.autohint.v2`, `public.postscript.hints`, etc.), processed layer names, validation constants, and `compute_outline_hash()` (must match AFDKO's HashPointPen algorithm).

## Key Design Patterns

- **Lazy imports** via `__getattr__` in `__init__.py` for optional modules (extraction, compilation)
- **Hint source priority**: PROCESSED_LAYER > AUTOHINT_V2 > PUBLIC_PS — the processedglyphs layer is the canonical hint storage
- **Dataclasses** for all data structures and result objects
- **Stateless functions** for compilation — enables safe parallel processing

## Code Style

- Line length: 100 chars (ruff)
- Python >=3.10, uses `from __future__ import annotations`
- Google-style docstrings
- Copyright header: `# Copyright 2024 Alexander Lubovenko` + Apache 2.0
- Class-based test organization (`class TestXxx:`)
