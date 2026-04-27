# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added

- **Multi-entry `hintSetList` extraction.** `cff_hints.py` now parses CFF charstrings via `afdko.otfautohint.otfFont.convertT2ToGlyphData` and emits one `autohint.v2` hint set per `hintmask` event in the source, anchored on the canonical UFO point per AFDKO's `addUfoHints` algorithm. Hint substitution is preserved through OTF → UFO → OTF on glyphs whose substitution points lie inside contours.
- **Counter-mask preservation.** `cntrmask` operators round-trip via `hstem3` / `vstem3` triplets in the `stems` array, mirroring AFDKO's `addUfoMask` triplet encoding.
- **`StdHW` / `StdVW` round-trip.** UFO has no dedicated field for these scalars, so extraction reorders `postscriptStemSnapH` / `postscriptStemSnapV` to put the CFF `StdHW` / `StdVW` value at index 0; ufo2ft picks `StdHW = StemSnapH[0]` at compile time.
- **FEA post-processing.** `_unify_feature_blocks` collapses `fontFeatures.unparse` output (one block per `(feature, script, language)` tuple) into one block per tag with internal `script` / `language` scoping. `_inline_aalt_lookups` rewrites `lookup NAME;` references inside `aalt` to inline `sub` statements (the Adobe FEA spec rejects lookup references in `aalt`; `addfeatures` errors out otherwise). `_strip_lookup_noise` and `_strip_empty_gdef` clean up artefacts. On a representative font this reduces feature-block count from 116 to one per tag.

### Changed

- **Compilation now uses the `makeotf` wrapper** instead of the deprecated `makeotfexe` binary, which is a stub on AFDKO releases scheduling its removal after March 2027 (returns exit 1 with a deprecation message and produces no output). `_compile_makeotf_hinted` resolves and subprocess-runs `makeotf`, sets `PATH` so the wrapper finds its own `tx` / `addfeatures` / `spot`, passes `features.fea` explicitly via `-ff`, and asserts the output file exists after a returncode-0 to guard against future stub regressions.
- **API rename**: `makeotfexe_path` → `makeotf_path` on `compile_otf_preserve`, `compile_otf_preserve_optimized`, `preserve_compile`, `preserve_compile_batch`. Auto-detection now resolves `makeotf` via `shutil.which`.

### Removed

- The in-house `HintExtractingDecompiler` (subclass of `SimpleT2Decompiler`) is replaced by `convertT2ToGlyphData` from `afdko.otfautohint`, which already parses stems, `startmasks`, per-pathElement masks, and counter masks correctly.

## [0.1.0] - 2026-04-03

### Added

- Initial release: extracted from ufo-widgets-gtk4 and TDKit
- `constants` module: shared PS hint constants and outline hash computation
- `extraction` module: binary font (OTF/TTF/WOFF/WOFF2) to UFO conversion with CFF hint extraction and feature cleanup
- `ps_hints` module: PS hint parsing, optimization, layer conversion, and structural validation
- `compilation` module: UFO to OTF compilation with PS hint preservation (preserve-optimized mode)
