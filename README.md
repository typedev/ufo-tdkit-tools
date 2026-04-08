# ufo-tdkit-tools

PS hints extraction, optimization, and preserve-mode compilation for UFO fonts.

## Features

- **extraction** -- Convert binary fonts (OTF/TTF/WOFF/WOFF2) to UFO with CFF hint preservation and feature cleanup
- **ps_hints** -- Parse, optimize, and validate PostScript hints in UFO fonts
- **compilation** -- Compile UFO back to OTF preserving PS hints (via AFDKO makeotf + per-glyph charstring merge)

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

## License

Apache-2.0
