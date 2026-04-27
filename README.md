# ufo-tdkit-tools

PS hints extraction, optimization, and preserve-mode compilation for UFO fonts.

## Features

- **extraction** -- Convert binary fonts (OTF/TTF/WOFF/WOFF2) to UFO with full PS hint preservation (multi-entry `hintSetList`, counter triplets `hstem3`/`vstem3`, font-level Private dict including `StdHW`/`StdVW`) and FEA post-processing (one feature block per tag, inlined `aalt` for spec compliance).
- **ps_hints** -- Parse, optimize, analyze, and validate PostScript hints in UFO fonts; move hints between processedglyphs / glyph lib / default layers.
- **compilation** -- Compile UFO back to OTF preserving PS hints (via AFDKO `makeotf` + per-glyph charstring merge + `cffsubr` subroutinization). Parallel batch via `ProcessPoolExecutor`.

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
    optimize=False,       # set True to run the ps_hints optimizer
)
assert result.success
print(result.glyphs_with_hints, "/", result.glyphs_total,
      "autohinted=", result.autohinted)
```

If the input has no hints, the pipeline runs `afdko.otfautohint`
automatically (`result.autohinted is True`).

For the lower-level entry points (`extraction.convert_binary_to_ufo`,
`compilation.compile_otf_preserve_optimized`, the `ps_hints` parser /
optimizer / validator / batch wrappers), see [`docs/API.md`](docs/API.md).

## Round-trip fidelity

OTF → UFO → OTF preserves declared `hstem`/`vstem` positions and widths byte-for-byte, hint substitution between drawing operations, counter-mask grouping, and font-level Private dict scalars. The one structural exception is hint substitution that fires *between* subpaths (a `hintmask` immediately before a `moveto`, common on disconnected glyphs like `i`, `j`, dieresis-bearing letters): the `autohint.v2` format has no anchor for these and AFDKO's own autohint produces the same flattening.

## License

Apache-2.0
