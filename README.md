# ufo-tdkit-tools

PS hints extraction, optimization, and preserve-mode compilation for UFO fonts.

## Features

- **extraction** -- Convert binary fonts (OTF/TTF/WOFF/WOFF2) to UFO with full PS hint preservation (multi-entry `hintSetList`, counter triplets `hstem3`/`vstem3`, font-level Private dict including `StdHW`/`StdVW`) and FEA post-processing (one feature block per tag, inlined `aalt` for spec compliance).
- **ps_hints** -- Parse, optimize, analyze, and validate PostScript hints in UFO fonts; move hints between processedglyphs / glyph lib / default layers.
- **compilation** -- Compile UFO back to OTF preserving PS hints (via AFDKO `makeotf` + per-glyph charstring merge, then production glyph names + `cffsubr` subroutinization). Parallel batch via `ProcessPoolExecutor`.

## Installation

```bash
pip install ufo-tdkit-tools                    # core (constants, ps_hints parser)
pip install ufo-tdkit-tools[extraction]        # + binary font conversion
pip install ufo-tdkit-tools[compilation]       # + OTF compilation with hints
pip install ufo-tdkit-tools[all]               # everything
```

## Quick start

The simplest path is the `process_font` pipeline — any input (binary or
UFO, hinted or not) becomes a hinted OTF + a clean UFO:

```python
from ufo_tdkit_tools import process_font

result = process_font(
    "input.otf",          # OTF / TTF / WOFF / WOFF2 / UFO
    "out.otf",
    "out.ufo",
    hint_source="auto",   # priority: processed > v2 > public_ps
    autohint="fill",      # "fill" | "all" | "off"
    optimize=False,       # set True to run the ps_hints optimizer
)
assert result.success
print(result.glyphs_with_hints, "/", result.glyphs_total,
      "autohinted=", result.autohinted_count,
      "in otf=", result.otf_glyphs_hinted)
```

Glyphs the chosen source does not hint are autohinted individually with
`afdko.otfautohint` — a partially hinted master (base forms hinted,
composites not) comes out fully hinted, with the authored hints kept.
`autohint="all"` re-hints everything instead, `autohint="off"` leaves the
gaps alone.

For the lower-level entry points (`extraction.convert_binary_to_ufo`,
`compilation.compile_otf_preserve_optimized`, the `ps_hints` parser /
optimizer / validator / batch wrappers), see [`docs/API.md`](docs/API.md).

## Command line

Installing the package (`uv sync` / `pip install`) provides the
`ufo-tdkit-tools` console script (also runnable as
`python -m ufo_tdkit_tools`). It wraps `process_font` for batch builds:

```bash
# Re-hint/optimize a batch of OTFs in place (temp UFO created & discarded)
ufo-tdkit-tools optimize-otf --in-place *.otf

# Or write fresh <stem>.otf + <stem>.ufo pairs into a directory
ufo-tdkit-tools optimize-otf -o build/ Sans-Regular.otf Sans-Bold.otf

# Skip the ps_hints optimizer (autohint + compile only)
ufo-tdkit-tools optimize-otf --in-place --no-optimize *.otf

# Keep only the authored hints, never call the autohinter
ufo-tdkit-tools optimize-otf -o build/ --autohint off Src.ufo
```

Every run prints one machine-parseable summary line and exits non-zero if
any input failed — convenient inside a build log:

```
optimized=36 autohinted=0 failed=0
```

`--in-place` only rewrites `.otf` inputs (atomically, so a mid-pipeline
failure never corrupts the source). UFO and other binary inputs use `-o DIR`.

## Round-trip fidelity

OTF → UFO → OTF preserves declared `hstem`/`vstem` positions and widths byte-for-byte, hint substitution between drawing operations, counter-mask grouping, and font-level Private dict scalars. The one structural exception is hint substitution that fires *between* subpaths (a `hintmask` immediately before a `moveto`, common on disconnected glyphs like `i`, `j`, dieresis-bearing letters): the `autohint.v2` format has no anchor for these and AFDKO's own autohint produces the same flattening.

Metadata (OS/2, `head`, `name`) also round-trips: `fsType`, weight/width class, vendor ID, Unicode/code-page ranges, PANOSE, typo/win/hhea metrics, sub/superscript and strikeout, `head.macStyle`/`head.flags`, and all name records. The `OS/2.fsSelection` flags `USE_TYPO_METRICS` (bit 7), `WWS` (bit 8) and `OBLIQUE` (bit 9) are restored during extraction — `ufo-extractor` (≤ 0.8.1) drops them, so they are re-read straight from the source `OS/2` table.

## License

Apache-2.0
