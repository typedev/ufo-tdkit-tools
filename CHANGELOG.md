# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Fixed

- **Preserve mode dropped the hints of every production-renamed glyph** ([#1](https://github.com/typedev/ufo-tdkit-tools/issues/1)). The per-glyph charstring merge matches by glyph name, but the two halves of the compile used different name sets: the ufo2ft shell was built with production names (`uni0100`) while the `tx -t1` + `makeotf` donor ignores `public.postscriptNames` and keeps source names (`Amacron`). Every renamed glyph failed the `glyph_name not in hinted_cs` test and was silently skipped — on DIN 2014 that is 488 of 829 glyphs per face, shipping OTFs with 32.5 % of glyphs hinted against 99.3 % for the same sources built with the external autohinter. The shell is now compiled with `useProductionNames=False` so both sides share the UFO's source namespace, and renaming is deferred to after the merge, delegated to ufo2ft's own `PostProcessor` (same code path a plain `compileOTF` runs: lib mapping, `uniXXXX` derivation, collision suffixes, invalid-character stripping) which also performs the `cffsubr` subroutinization. New e2e regression test `test_production_renamed_glyphs_keep_hints`.
- **Merged charstrings reported a bogus advance width in the CFF.** A Type 2 charstring may open with a width operand encoded as `width - Private.nominalWidthX`, and the merge copies charstrings between two fonts with unrelated Private dicts (here: shell `nominalWidthX=500 / defaultWidthX=600`, donor `0 / 0`). Every transferred glyph therefore decoded to a wrong CFF width — `tx -dump -6` reported `1100 width` for a 600-unit glyph — while `hmtx` stayed correct, so layout engines were unaffected but CFF-level consumers (`tx`, subsetters, CFF→CFF2 conversion) were not. The new `_split_width_prefix` helper strips the donor's width prefix and splices the shell's own onto the donor's body. This defect was previously masked by the name bug (it only affected the third of glyphs that were merged at all). New e2e regression test `test_transferred_charstrings_keep_their_width`.
- **Silent hint loss now surfaces in the log.** The merge warns when the shell and donor name sets disagree on more than 5 % of glyphs (with a sample of the unmatched names), and when hinted donor glyphs have no shell counterpart. `stats` gained `donor_hinted` and `name_mismatch` alongside `hints_transferred` / `total_glyphs`.

- **Preserve mode could fail to compile UFOs with features (platform-dependent).** `_compile_makeotf_hinted` passed the UFO's `features.fea` to the donor compile via `-ff`; on some platforms `makeotfexe` aborts with `std::system_error` whenever `-ff` is present — even for an empty file (reproduced deterministically on Fedora 44 / AFDKO 4.0.3, and reported intermittently under parallel load on AFDKO 5.0.0; not reproduced on 5.0.0 here across ~250 serial and oversubscribed-parallel invocations). When the abort fires, `_compile_makeotf_hinted` returns `False`, so `compile_otf_preserve_optimized` returns `False` and `process_font` reports `success=False` / `"OTF compilation failed"` — a hard failure, not a silent unhinted output. (A caller that catches that `False` and falls back to a plain ufo2ft compile is what turns it into silently unhinted output downstream.) The donor font exists only as a source of hinted charstrings and PrivateDict hint parameters — its GSUB/GPOS are discarded by the per-glyph merge — so the donor is now compiled with **no features file at all** (wrapper auto-discovery is moot: the tx-emitted `.ps` sits alone in a temp directory). Output features are unaffected: they come from the ufo2ft shell. Removing `-ff` also skips donor feature compilation that the merge discards anyway (~4% of the makeotf step on a 600-glyph / 42 KB feature file here). New e2e regression test compiles a hinted UFO **with** features and asserts the hints actually reach the output CFF (`test_hints_survive_with_features_present`).

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
