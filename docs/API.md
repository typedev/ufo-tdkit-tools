# API Reference

`ufo-tdkit-tools` exposes one high-level entry point (`process_font`) and a set
of lower-level modules for callers who need finer control. Most consumers
should only need `process_font`.

## Contents

- [Installation & extras](#installation--extras)
- [`process_font` — main entry point](#process_font--main-entry-point)
- [`ProcessResult`](#processresult)
- [Top-level constants](#top-level-constants)
- [Sub-module APIs](#sub-module-apis)
  - [`extraction` — binary → UFO](#extraction--binary--ufo)
  - [`compilation` — UFO → OTF](#compilation--ufo--otf)
  - [`ps_hints.parser` — data models & parsing](#ps_hintsparser--data-models--parsing)
  - [`ps_hints.batch` — whole-font wrappers](#ps_hintsbatch--whole-font-wrappers)
  - [`ps_hints.converter` — per-glyph helpers](#ps_hintsconverter--per-glyph-helpers)
  - [`ps_hints.optimizer` — hint optimization](#ps_hintsoptimizer--hint-optimization)
  - [`ps_hints.validator` — structural validation](#ps_hintsvalidator--structural-validation)

---

## Installation & extras

```bash
pip install ufo-tdkit-tools                 # core: constants, ps_hints parser/optimizer/validator
pip install ufo-tdkit-tools[extraction]     # + binary → UFO  (pulls afdko, ufo-extractor, defcon)
pip install ufo-tdkit-tools[compilation]    # + UFO → OTF     (pulls afdko, ufo2ft, cffsubr, defcon)
pip install ufo-tdkit-tools[all]            # everything
```

`process_font` requires both `extraction` and `compilation` extras (i.e.
`[all]`). The core package alone is enough for parsing, optimizing, and
validating hints in UFOs you already have.

---

## `process_font` — main entry point

```python
from ufo_tdkit_tools import process_font, ProcessResult

result: ProcessResult = process_font(
    input_path,                      # Path | str — .otf/.ttf/.woff/.woff2/.ufo
    output_otf,                      # Path | str
    output_ufo,                      # Path | str
    *,
    hint_source="auto",              # "auto" | "v2" | "processed" | "public_ps"
    autohint="fill",                 # "fill" | "all" | "off"
    optimize=False,                  # run the ps_hints optimizer
    logger_=None,                    # Optional[logging.Logger]
)
```

Takes any binary font or UFO and writes a hinted OTF + a normalized UFO
side-by-side. The pipeline is:

1. **Load** the input. Binary formats are extracted to a temporary UFO via
   `extraction.convert_binary_to_ufo`. UFO inputs are copied into a working
   directory so the original is never mutated.
2. **Resolve hint source.** See [Hint source resolution](#hint-source-resolution).
3. **Autohint the gaps.** Every drawable glyph the chosen source does not
   hint is passed to `afdko.otfautohint.hintFiles` (`glyphList` restricted to
   exactly those glyphs), and its hints join the authored ones in the
   `processedglyphs` buffer. See [Autohint modes](#autohint-modes).
   `result.autohinted` records whether the autohinter ran,
   `result.autohinted_count` how many glyphs it hinted.
4. **Optimize** (when `optimize=True`). Hints are routed through the
   `processedglyphs` layer for the optimizer, then exported back to default
   `autohint.v2`.
5. **Normalize** all hints into default-layer `com.adobe.type.autohint.v2`
   (the only source the compiler reads).
6. **Layer hygiene.** A `processedglyphs` layer that was on the input is
   preserved. One created by the pipeline (autohint buffer or optimizer
   buffer) is removed before save.
7. **Save** the UFO and **compile** the OTF
   (`compilation.compile_otf_preserve_optimized`). Both halves of the compile
   keep the UFO's source glyph names so the per-glyph merge can match them;
   production names (`public.postscriptNames`) and `cffsubr` subroutinization
   are applied to the merged font at the end, via ufo2ft's `PostProcessor`.

### Hint source resolution

| Argument        | Behaviour                                                                               |
| --------------- | --------------------------------------------------------------------------------------- |
| `"auto"`        | UFO: priority `processed > v2 > public_ps`. Nothing anywhere → autohint everything.      |
|                 | Binary: hints are always read from extracted CFF; if extraction yielded none → autohint.|
| `"v2"`          | UFO only. Read default-layer `com.adobe.type.autohint.v2`. 0 hinted glyphs → fail.       |
| `"processed"`   | UFO only. Read `com.adobe.type.processedglyphs` layer. 0 hinted glyphs → fail.           |
| `"public_ps"`   | UFO only. Read default-layer `public.postscript.hints`. 0 hinted glyphs → fail.          |

The pipeline picks **one** source for the entire font. Per-glyph mixed
sources are not supported; lower-priority sources are silently dropped from
the output. Glyphs the chosen source does not cover are handled by
`autohint`, not by falling back to another source.

### Autohint modes

| `autohint` | Glyphs hinted by the source | Glyphs without hints             |
| ---------- | --------------------------- | -------------------------------- |
| `"fill"` (default) | kept as authored     | autohinted                       |
| `"all"`    | discarded and re-hinted     | autohinted                       |
| `"off"`    | kept as authored            | left unhinted                    |

`"off"` also turns "no hints anywhere" into a failure instead of hinting the
whole font. Note that `detect_font_source` reports a source as soon as **one**
glyph has hints, which is why partially hinted masters (base forms hinted,
composites not) need `"fill"` rather than the old whole-font gate.

### Coverage matrix

| Input            | Hints?                | `hint_source`            | Pipeline does                | `autohinted` |
| ---------------- | --------------------- | ------------------------ | ---------------------------- | ------------ |
| OTF/TTF/WOFF     | yes                   | (ignored)                | extract → preserve           | False        |
| OTF/TTF/WOFF     | no                    | (ignored)                | extract → autohint → compile | True         |
| UFO              | yes (all glyphs)      | `auto`                   | preserve by priority         | False        |
| UFO              | yes (some glyphs)     | `auto`                   | preserve + autohint the rest | True         |
| UFO              | no                    | `auto`                   | autohint → compile           | True         |
| UFO              | yes (matching)        | `v2`/`processed`/`public_ps` | preserve from that source | False        |
| UFO              | no (matching source)  | `v2`/`processed`/`public_ps` | **fail** (`No hints found`) | —          |

### Examples

```python
from pathlib import Path
from ufo_tdkit_tools import process_font

# Round-trip a hinted OTF, no optimization
r = process_font("Sans-Regular.otf", "out.otf", "out.ufo")
assert r.success and not r.autohinted
print(r.glyphs_with_hints, "/", r.glyphs_total)

# Build an OTF + UFO from an unhinted UFO (autohint kicks in automatically)
r = process_font("DesignSrc.ufo", "Build.otf", "Build.ufo", hint_source="auto")
assert r.autohinted

# Force the public.postscript.hints source on a UFO; fail if missing
r = process_font(ufo, otf_out, ufo_out, hint_source="public_ps")
if not r.success:
    raise RuntimeError(r.error)

# Optimize hints during compile
r = process_font(ufo, otf_out, ufo_out, optimize=True)
print(r.optimized_count, "glyphs optimized")
```

---

## `ProcessResult`

```python
@dataclass
class ProcessResult:
    success: bool
    hint_source_used: str | None = None   # "autohint_v2" / "processedglyphs" / "public_ps"
    glyphs_total: int = 0
    glyphs_with_hints: int = 0
    optimized: bool = False               # was the optimizer run?
    optimized_count: int = 0              # # of glyphs optimized
    autohinted: bool = False              # did otfautohint run?
    autohinted_count: int = 0             # # of glyphs it hinted
    otf_glyphs_hinted: int = 0            # hinted charstrings in the output OTF
    otf_glyphs_total: int = 0             # glyphs in the output OTF
    error: str | None = None              # set on failure
```

`hint_source_used` reflects the authored source that drove the compile. When
there was none (nothing hinted anywhere, or `autohint="all"`) it is
`"processedglyphs"`, the layer the autohinter writes to. To distinguish
"preserved hints" from "generated hints", check `autohinted` /
`autohinted_count` — with `autohint="fill"` a font can have both.

`otf_glyphs_hinted` / `otf_glyphs_total` come from the compiler and describe
what actually reached the binary; they are the numbers to assert on in a
build-level regression guard.

---

## Top-level constants

Imported directly from `ufo_tdkit_tools`:

| Symbol                       | Value                                      | Meaning                                          |
| ---------------------------- | ------------------------------------------ | ------------------------------------------------ |
| `ADOBE_HINT_KEY_V2`          | `"com.adobe.type.autohint.v2"`             | Adobe v2 hint key in `glyph.lib`.                |
| `ADOBE_HINT_KEY_V1`          | `"com.adobe.type.autohint"`                | Legacy v1 hint key (read-only fallback).         |
| `PUBLIC_PS_HINT_KEY`         | `"public.postscript.hints"`                | UFO-spec PS hint key in `glyph.lib`.             |
| `PROCESSED_LAYER_NAME`       | `"com.adobe.type.processedglyphs"`         | Adobe processed-glyphs layer name.               |
| `PROCESSED_LAYER_NAME_ALT`   | `"glyphs.com.adobe.type.processedglyphs"`  | On-disk alternative name.                        |
| `VALID_STEM_TYPES`           | `{"hstem", "vstem", "hstem3", "vstem3"}`   | Allowed stem operator names.                     |
| `MAX_STEMS_PER_HINTSET`      | `96`                                       | CFF spec limit.                                  |

```python
def compute_outline_hash(glyph) -> str: ...
```
Compute the hash AFDKO uses to detect outline changes since hinting (matches
`HashPointPen`). Returns SHA-512 hex for ≥128-char data, otherwise the raw
data string.

---

## Sub-module APIs

### `extraction` — binary → UFO

`pip install ufo-tdkit-tools[extraction]`

```python
from ufo_tdkit_tools.extraction.converter import convert_binary_to_ufo

result = convert_binary_to_ufo(
    binary_path,                               # Path | str — .otf/.ttf/.woff/.woff2
    progress_callback=None,                    # Optional[Callable[[int, str], None]]
)
# result is a ConversionResult dataclass:
#   font: fontParts RFont (already loaded, in `temp_dir`)
#   original_path: Path of the source binary
#   temp_dir: tempfile.TemporaryDirectory  (caller must cleanup())
#   warnings: list[ConversionWarning]
#   is_cff: bool
#   glyph_count: int
#   hint_count: int
```

The font lives inside `temp_dir`. Persist by calling `result.font.save("path.ufo")`,
then `result.temp_dir.cleanup()`.

Variable fonts produce a warning and only the default instance is extracted.

### `compilation` — UFO → OTF

`pip install ufo-tdkit-tools[compilation]`

Two layers of API. The lower-level `compiler` module is what `process_font`
calls. The higher-level `preserve` module is a convenience wrapper that
includes a hint-presence pre-check and supports parallel batch processing.

#### Single-file compilation

```python
from ufo_tdkit_tools.compilation.compiler import compile_otf_preserve_optimized

ok: bool = compile_otf_preserve_optimized(
    ufo_path,                  # str
    otf_path,                  # str
    logger=None,
    pshash_rebuild=False,      # rebuild processedglyphs layer before compile
    tx_path=None,              # absolute path to AFDKO tx (auto-detected)
    makeotf_path=None,         # absolute path to AFDKO makeotf (auto-detected)
    stats=None,                # optional dict, see below
)
```

Reads hints from default-layer `com.adobe.type.autohint.v2` only. Returns
`True` on success.

When `stats` is a dict it is filled with merge counters:

| Key                 | Meaning                                                             |
| ------------------- | ------------------------------------------------------------------- |
| `hints_transferred` | Hinted charstrings merged into the shell.                            |
| `total_glyphs`      | Glyphs in the shell CFF.                                             |
| `donor_hinted`      | Glyphs carrying hints in the `makeotf` donor.                        |
| `name_mismatch`     | Shell glyphs with no donor counterpart (should be 0; a large value means the two compiles disagree on glyph names). |

```python
from ufo_tdkit_tools.compilation.preserve import preserve_compile, PreserveCompileResult

r: PreserveCompileResult = preserve_compile(
    ufo_path,                  # str
    otf_path,                  # str
    logger=None,
    tx_path=None,
    makeotf_path=None,
)
# PreserveCompileResult:
#   ufo_path, otf_path, success, error, warnings,
#   hints_found, hints_transferred, skipped
```

`preserve_compile` returns `success=False` and `skipped=True` if the UFO
contains no `autohint.v2` data. Use `process_font` if you want autohinting in
that case.

#### Parallel batch compilation

```python
from ufo_tdkit_tools.compilation.preserve import preserve_compile_batch

batch = preserve_compile_batch(
    input_dir,                 # str — folder with *.ufo
    output_dir,                # str — folder for *.otf (created if missing)
    logger=None,
    parallel=False,            # use ProcessPoolExecutor
    workers=None,              # default cpu_count() - 2
    on_progress=None,          # Optional[Callable[[ufo_name, status], None]]
)
# BatchCompileResult: results, successful, failed, skipped_count, total, to_dict()
```

### `ps_hints.parser` — data models & parsing

Always available (core dep: fontTools only).

```python
from ufo_tdkit_tools.ps_hints.parser import (
    HintSource, PSHint, PSHintSet, PSHintData,
    parse_ps_hints, parse_stem,
    get_available_sources, get_source_counts, get_glyph_hint_status,
    build_point_map, build_point_map_from_layer,
    reload_processed_layer,
)
```

#### `HintSource`

```python
class HintSource(Enum):
    PROCESSED_LAYER = "processedglyphs"
    AUTOHINT_V2     = "autohint_v2"
    PUBLIC_PS       = "public_ps"
```

#### `PSHint`

A single stem (or counter triplet, or ghost hint).

```python
@dataclass
class PSHint:
    type: str                                # "hstem", "vstem", "hstem3", "vstem3"
    position: float
    width: float
    pairs: list[tuple[float, float]] | None  # for *stem3*: 3 (pos, width) pairs
    raw: str

    is_horizontal: bool                      # property
    is_vertical:   bool                      # property
    is_triple:     bool                      # property — *stem3
    is_ghost:      bool                      # property — width in (-20, -21)
    is_top_ghost:  bool                      # width == -20
    is_bottom_ghost: bool                    # width == -21
    end:           float                     # property — position + width
```

#### `PSHintSet`

A group of stems active for one section of the outline (one `hintmask`).

```python
@dataclass
class PSHintSet:
    point_tag: str | None                    # e.g. "hintRef0000"
    point_coords: tuple[float, float] | None
    stems: list[PSHint]
    index: int

    hstems:      list[PSHint]                # property
    vstems:      list[PSHint]                # property
    ghost_hints: list[PSHint]                # property
```

#### `PSHintData`

The whole hint payload for one glyph.

```python
@dataclass
class PSHintData:
    source: HintSource
    hint_sets: list[PSHintSet]
    flex_points: list[str]
    id_hash: str | None
    is_stale: bool                           # current outline hash != id_hash
    format_version: str
    errors: list[str]

    total_stems: int                         # property — unique across all sets
    has_hint_substitution: bool              # property — len(hint_sets) > 1
```

#### Parsing

```python
parse_ps_hints(glyph, source: HintSource, font=None) -> PSHintData
parse_stem(stem_str: str) -> PSHint | None     # e.g. "hstem 0 52"

# Discover what's available for a glyph:
get_available_sources(glyph, font=None) -> list[HintSource]

# Whole-font counts:
get_source_counts(font) -> dict[str, int]      # {"processed": N, "v2": N, "public_ps": N}

# Single-glyph status string ("processed" / "v2" / "public_ps" / None):
get_glyph_hint_status(glyph, font=None) -> str | None

# Point-name maps used by hintRef parsing:
build_point_map(glyph) -> dict[str, tuple[float, float]]
build_point_map_from_layer(glyph_set, glyph_name: str) -> dict[str, tuple[float, float]]

# Refresh fontParts in-memory layer view after on-disk mutation:
reload_processed_layer(font, glyph_names: list[str]) -> None
```

### `ps_hints.batch` — whole-font wrappers

Whole-font versions of the per-glyph helpers. These iterate the default
layer for you.

```python
from ufo_tdkit_tools.ps_hints.batch import (
    detect_font_source, count_glyphs_with_source,
    import_all_to_processed, export_all_from_processed,
    remove_all_hints, optimize_font,
)

detect_font_source(font) -> HintSource | None
    # Whole-font priority: processed > v2 > public_ps. None if no hints.

count_glyphs_with_source(font, source: HintSource) -> int

import_all_to_processed(font, source: HintSource | str) -> int
    # source: HintSource enum or "v2" / "public_ps" / "processed".

export_all_from_processed(font, target: HintSource | str) -> int

remove_all_hints(font, source: HintSource | str) -> int

optimize_font(font) -> dict[str, int]
    # {"optimized": N, "skipped": N}
    # Reads hints from processedglyphs, runs ps_hints.optimizer.optimize_hints,
    # writes optimized hints back to processedglyphs. Caller is responsible for
    # importing the source into processedglyphs first.
```

### `ps_hints.converter` — per-glyph helpers

```python
from ufo_tdkit_tools.ps_hints.converter import (
    import_to_processed, export_from_processed, remove_hints,
)

import_to_processed(glyph, font, source: str) -> bool
export_from_processed(glyph, font, target: str) -> bool
remove_hints(glyph, font, source: str) -> bool
    # source/target: "v2" | "public_ps" | "processed"
```

### `ps_hints.optimizer` — hint optimization

```python
from ufo_tdkit_tools.ps_hints.optimizer import optimize_hints, apply_optimized_hints

new_hd = optimize_hints(
    hint_data,                  # PSHintData (from parser)
    glyph_width,                # float
    stem_snap_v=None,           # Optional[list[float]]
    stem_snap_h=None,           # Optional[list[float]]
    upm=1000,
    glyph=None,                 # Optional[fontParts glyph] for contour-based coverage
) -> PSHintData

apply_optimized_hints(glyph, font, hint_data: PSHintData) -> bool
    # Writes the optimized hint data back to the processedglyphs layer.
```

The optimizer reads from and writes to the **processedglyphs layer only** —
this is by design. Use `ps_hints.batch.optimize_font` for the whole-font
flow, or wrap with `import_all_to_processed` / `export_all_from_processed`.

See `OPTIMIZER_ALGORITHM.md` for the algorithm description.

### `ps_hints.validator` — structural validation

```python
from ufo_tdkit_tools.ps_hints.validator import validate_ps_hints

report = validate_ps_hints(ufo_path, logger=None)
# {
#     "valid": bool,                        # False if any errors
#     "glyphs_checked": int,
#     "glyphs_with_hints": int,
#     "errors":   [{"glyph": str, "message": str}, ...],
#     "warnings": [{"glyph": str, "message": str}, ...],
# }
```

Checks each glyph's `com.adobe.type.autohint.v2` for CFF spec compliance:
unknown stem operators, too many stems per set, bad coordinate types, etc.

---

## Common patterns

### Inspect hint data of a UFO

```python
import defcon
from ufo_tdkit_tools.ps_hints.parser import HintSource, parse_ps_hints
from ufo_tdkit_tools.ps_hints.batch import detect_font_source

font = defcon.Font("MyFont.ufo")
source = detect_font_source(font)            # priority: processed > v2 > public_ps
if source is None:
    print("no hints")
else:
    for glyph in font:
        hd = parse_ps_hints(glyph, source, font=font)
        print(glyph.name, hd.total_stems, "stems",
              "stale" if hd.is_stale else "fresh")
```

### Count hinted glyphs in a compiled OTF

`cffsubr` runs as the final compile step, which moves hint operators
**inside** subroutines. Naive `cs.program` scans miss them. Desubroutinize
first:

```python
from fontTools.ttLib import TTFont
from fontTools import subset

HINT_OPS = {"hstem", "vstem", "hstemhm", "vstemhm", "hintmask", "cntrmask"}

def count_hinted_in_otf(otf_path: str) -> tuple[int, int]:
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
    hinted = sum(
        1 for n in cs.keys()
        if (cs[n].decompile() or True)
        and any(isinstance(op, str) and op in HINT_OPS for op in cs[n].program)
    )
    total = len(cs.keys())
    f.close()
    return hinted, total
```

### Strip all hints from a UFO

```python
import fontParts.world as fp
from ufo_tdkit_tools.ps_hints.batch import remove_all_hints
from ufo_tdkit_tools.ps_hints.parser import HintSource

font = fp.OpenFont("MyFont.ufo", showInterface=False)
for src in (HintSource.AUTOHINT_V2, HintSource.PUBLIC_PS, HintSource.PROCESSED_LAYER):
    remove_all_hints(font, src)
font.save()
```

### Validate before compile

```python
from ufo_tdkit_tools.ps_hints.validator import validate_ps_hints

report = validate_ps_hints("MyFont.ufo")
if not report["valid"]:
    for e in report["errors"]:
        print(f"  {e['glyph']}: {e['message']}")
```
