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

Three main workflows, each in its own subpackage under `src/ufo_tdkit_tools/`, plus a top-level `pipeline.py` that ties them together:

1. **`extraction/`** — Binary font (OTF/TTF/WOFF/WOFF2) → in-memory UFO. Uses `ufo-extractor` for bulk work plus custom CFF hint extraction from PostScript charstrings. Optional dependency group `[extraction]`.
   - `converter.py`: Top-level pipeline (`convert_binary_to_ufo`) and FEA post-processing — `_unify_feature_blocks` (one block per tag with internal script/language scoping), `_inline_aalt_lookups` (Adobe FEA spec compliance — `aalt` rejects `lookup` references), `_strip_lookup_noise`, `_strip_empty_gdef`. Drops the auto-generated `kern` feature.
   - `cff_hints.py`: Per-glyph hint extraction. Uses `afdko.otfautohint.otfFont.convertT2ToGlyphData` to parse stems, `startmasks`, per-pathElement `masks`, and `cntr` (counter masks). Emits multi-entry `hintSetList` mirroring AFDKO's canonical `addUfoHints` algorithm: hint anchors are line endpoints or first off-curve control points; counter-masked orientations collapse into `hstem3`/`vstem3` triplets. Also fills the font-level Private dict (`BlueValues`, `OtherBlues`, `StemSnap*`, etc.) and reorders `postscriptStemSnapH/V` so the CFF `StdHW`/`StdVW` value sits at index 0 (ufo2ft picks `StdHW = StemSnapH[0]` at compile time).

2. **`ps_hints/`** — Hint parsing, optimization, analysis, validation, and layer conversion. Always available (core dep: fontTools only).
   - `parser.py`: Data models (`PSHint`, `PSHintSet`, `PSHintData`, `HintSource` enum) and parsing from UFO glyph lib entries.
   - `optimizer.py`: 5-step pipeline — remove too-wide vstems → build coverage map (ray-casting, even-odd fill) → filter small-element stems → extract vstem3 triples → resolve overlaps. See `OPTIMIZER_ALGORITHM.md` for details. Reads from and writes to processedglyphs layer only.
   - `analyzer.py`: Same logic as optimizer but non-destructive; returns issue list.
   - `converter.py`: Per-glyph helpers to move hints between the three sources (`processedglyphs` layer, default-layer `autohint.v2`, default-layer `public.postscript.hints`).
   - `batch.py`: Whole-font wrappers around `converter.py` and `optimizer.py` — `detect_font_source` (whole-font priority), `import_all_to_processed`, `export_all_from_processed`, `remove_all_hints`, `optimize_font` (vacuums stem snaps and UPM from `font.info`).
   - `validator.py`: Validate hints across an entire UFO.

3. **`compilation/`** — UFO → OTF with hint preservation. Hybrid pipeline: ufo2ft creates an unhinted OTF shell with correct metadata; `tx -t1` + `makeotf` (the wrapper, not the deprecated `makeotfexe` stub) builds a hinted OTF from the UFO; per-glyph charstrings and the Private dict hint params are merged into the shell; `_finalize_shell` then applies production glyph names and subroutinizes. Optional dependency group `[compilation]`. Supports batch processing via `ProcessPoolExecutor` (each worker spawns its own `makeotf` subprocess — no shared state). Reads hints from default-layer `autohint.v2` only — does not create or read processedglyphs (see comment in `compiler.py`, step 1.5 of `compile_otf_preserve_optimized`).

   **One glyph namespace for the merge.** The shell is compiled with `useProductionNames=False`, because `tx`/`makeotf` ignore `public.postscriptNames` and always emit source names. Renaming is deferred until after the merge and delegated to ufo2ft's own `PostProcessor(shell, ufo).process(useProductionNames=None, optimizeCFF=True)`, which is the same code a plain `compileOTF` runs (lib mapping, `uniXXXX` derivation, collision suffixes, invalid-char stripping) and which also does the `cffsubr` pass. Do not reimplement the name mapping — compiling the shell with production names is what caused issue #1 (every renamed glyph silently lost its hints).

   **Widths are not part of a charstring transfer.** A charstring's optional leading width operand encodes `width - Private.nominalWidthX` and the donor's Private dict is not merged, so `_split_width_prefix` strips the donor's prefix and splices the shell's own prefix onto the donor's body. Copying the donor program verbatim gave every hinted glyph a bogus CFF advance width (correct `hmtx`, wrong `tx -dump`).

4. **`pipeline.py`** — Single public entry point (`process_font`). Takes any binary or UFO input and produces an OTF + UFO pair. Resolves hint source per `hint_source` arg (auto-detect or explicit), fills hint gaps per `autohint` arg, optionally runs the optimizer, normalizes hints into default `autohint.v2`, then compiles. Explicit `hint_source` (`v2` / `processed` / `public_ps`) with no hints anywhere still fails. Layer hygiene rule: a processedglyphs layer present on input survives to output; one created by the pipeline (as a working buffer or by the autohinter) is removed before save, together with the autohinter's `data/com.adobe.type.processedHashMap`. Returns `ProcessResult` dataclass.

   **Autohinting is per glyph, not per font.** `detect_font_source` reports a source as soon as *one* glyph has hints, so a partially hinted master (the norm for FontLab sources: base forms hinted, composites not) used to be treated as fully hinted. `autohint="fill"` (default) runs `afdko.otfautohint.hintFiles` with `glyphList` restricted to the drawable glyphs the source does not cover, and their hints join the authored ones in the processedglyphs buffer — the autohinter leaves the layer's other entries and the default layer untouched, so authored hints survive. `"all"` re-hints everything and discards authored hints; `"off"` leaves gaps unhinted and turns "no hints anywhere" into a failure. `ProcessResult.autohinted` / `.autohinted_count` report what happened; `.otf_glyphs_hinted` / `.otf_glyphs_total` report what reached the binary.

**`constants.py`** — Shared lib keys (`com.adobe.type.autohint.v2`, `public.postscript.hints`, etc.), processed layer names, validation constants, and `compute_outline_hash()` (must match AFDKO's HashPointPen algorithm).

## Hint round-trip fidelity

The OTF → UFO → OTF round-trip preserves:

- All declared `hstem`/`vstem` positions and widths byte-for-byte.
- Hint substitution (`hintmask` operators) for glyphs whose substitution points are inside contours, via multi-entry `hintSetList`.
- Counter-mask grouping (`cntrmask` → `hstem3`/`vstem3` triplets).
- Font-level Private dict values including `StdHW`/`StdVW` (via the `postscriptStemSnapH/V[0]` convention).

Known limitation: hint substitutions that fire **between subpaths** (a `hintmask` immediately before a `moveto`, common on disconnected glyphs like `i`, `j`, dieresis-bearing letters) are not representable in the `autohint.v2` format and degrade to a single hint set. AFDKO's own autohint output exhibits the same limitation.

## Inspecting hints in compiled OTFs

`compilation/compiler.py` always subroutinizes (via ufo2ft's `PostProcessor`, backed by `cffsubr`) as the final step. After subroutinization most glyphs reduce to a `callsubr` / `callgsubr` wrapper, and the actual hint operators (`hstem`, `vstem`, `hstemhm`, `vstemhm`, `hintmask`, `cntrmask`) live **inside the called subroutine, not at the top level of the charstring**.

Any code that audits hint presence by scanning `charstring.program` will undercount and report false "hint loss" unless it desubroutinizes first. Use this:

```python
from fontTools.ttLib import TTFont
from fontTools import subset

HINT_OPS = {"hstem", "vstem", "hstemhm", "vstemhm", "hintmask", "cntrmask"}

def count_hinted_glyphs(otf_path: str) -> tuple[int, int]:
    f = TTFont(otf_path)
    options = subset.Options()
    options.desubroutinize = True
    options.notdef_outline = True
    options.glyph_names = True
    options.layout_features = ["*"]
    options.name_IDs = ["*"]
    options.name_languages = ["*"]
    options.notdef_glyph = True
    sub = subset.Subsetter(options=options)
    sub.populate(glyphs=f.getGlyphOrder())
    sub.subset(f)
    cs = f["CFF "].cff.topDictIndex[0].CharStrings
    hinted = 0
    for n in cs.keys():
        cs[n].decompile()
        if any(isinstance(op, str) and op in HINT_OPS for op in cs[n].program):
            hinted += 1
    f.close()
    return hinted, len(cs.keys())
```

The same caveat applies to byte-level diffs of individual charstrings: `cs.program` for a subroutinized glyph may look like `[N, M, 'callsubr']` even when the underlying glyph carries a full set of stems and hint substitutions.

## Key Design Patterns

- **Lazy imports** via `__getattr__` in `__init__.py` for optional modules (extraction, compilation, pipeline)
- **Hint source priority**: PROCESSED_LAYER > AUTOHINT_V2 > PUBLIC_PS — used by `pipeline.process_font(hint_source="auto")` and `ps_hints.batch.detect_font_source`
- **Single source per font** — `process_font` picks one hint source for the whole font; per-glyph mixed sources are not supported (the lower-priority sources are ignored). Glyphs the chosen source does not hint are filled in by the autohinter (`autohint` arg), not by another source
- **Optimizer works only in processedglyphs** — by design; `pipeline.py` imports the chosen source into processedglyphs as a working layer, then exports back to default `autohint.v2`
- **Compiler reads only default-layer `autohint.v2`** — pipeline always normalizes here before compile
- **Dataclasses** for all data structures and result objects
- **Stateless functions** for compilation — enables safe parallel processing
- **AFDKO Python API over subprocess** where possible — extraction calls `afdko.otfautohint` Python functions in-process; compilation still subprocesses `tx` and `makeotf` because they're external binaries

## Code Style

- Line length: 100 chars (ruff)
- Python >=3.10, uses `from __future__ import annotations`
- Google-style docstrings
- Copyright header: `# Copyright 2024 Alexander Lubovenko` + Apache 2.0
- Class-based test organization (`class TestXxx:`)
