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

```python
from ufo_tdkit_tools.extraction import convert_binary_to_ufo
from ufo_tdkit_tools.ps_hints import optimize_hints, parse_ps_hints
from ufo_tdkit_tools.compilation import preserve_compile

# Extract: OTF -> UFO with hints
result = convert_binary_to_ufo("input.otf", "output.ufo")

# Optimize hints in UFO
# ... (see ps_hints module docs)

# Compile: UFO -> OTF preserving hints
result = preserve_compile("output.ufo", "optimized.otf")
```

## Round-trip fidelity

OTF → UFO → OTF preserves declared `hstem`/`vstem` positions and widths byte-for-byte, hint substitution between drawing operations, counter-mask grouping, and font-level Private dict scalars. The one structural exception is hint substitution that fires *between* subpaths (a `hintmask` immediately before a `moveto`, common on disconnected glyphs like `i`, `j`, dieresis-bearing letters): the `autohint.v2` format has no anchor for these and AFDKO's own autohint produces the same flattening.

## License

Apache-2.0
