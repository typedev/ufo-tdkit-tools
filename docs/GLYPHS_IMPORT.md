# Glyphs Import — .glyphs / .glyphspackage to UFO

`ufo_tdkit_tools.glyphs` converts a Glyphs.app source into UFO masters — plus a
`.designspace` when the source has more than one master — written to a
directory the caller chooses. The conversion itself is `glyphsLib`'s; this
package is a layer around it that repairs real-world sources before glyphsLib
sees them and restores what glyphsLib drops after. Most rules below exist
because a real source broke the import.

It moved here from Font-Rover (`font_rover.glyphs_import`), which keeps the UI
— source picker, destination dialog, progress window — and re-exports this
package. Like the binary importer (`ufo_tdkit_tools.extraction`), it has no GUI
dependency, so TDKit, scripts and CI can use it.

Requires the `glyphs` extra: `pip install ufo-tdkit-tools[glyphs]`. Importing
the package does not need glyphsLib; the converter imports it when it runs.

Scale it has been run at: a 216-master, six-axis, 148 MB `.glyphspackage`
(720 glyphs) converts in about two and a half minutes into 216 UFOs and a
designspace of 657 sources, 441 of them layer sources.

## Module Structure

```
ufo_tdkit_tools/glyphs/
├── __init__.py         # Public API (lazy re-exports)
├── converter.py        # inspect / plan / convert (progress callback, cancel)
├── fast_inspect.py     # cheap read: masters and axes, no glyphs parsed
├── format4.py          # Glyphs 4 sources: plist rewritten to format 3 (experimental)
├── axis_maps.py        # drops a user->design axis map no master fits
├── warning_summary.py  # thousands of warnings -> a dozen groups
├── preflight.py        # repairs that make real sources convertible
├── corners.py          # bake corner/cap components, then disarm the pass
├── ps_hints.py         # Glyphs PS hints -> com.adobe.type.autohint.v2
├── stem_snaps.py       # zero / repeated stems dropped from postscriptStemSnapH/V
└── tt_hints.py         # restores the TT delta amounts glyphsLib drops
```

`ConversionWarning` and `WarningSeverity` come from
`ufo_tdkit_tools.extraction.warnings`, so both importers report warnings in one
shape.

## Quick Start

```python
from ufo_tdkit_tools.glyphs import (
    convert_glyphs_to_ufos,
    default_output_dir,
    describe_existing,
    inspect_glyphs_source,
    plan_output_paths,
)

source = "Family.glyphs"

# What is in there? (cheap — parsing only)
info = inspect_glyphs_source(source)
print(info.family_name, info.master_count, info.glyph_count, info.axes)

# Where would it go, and would anything be replaced?
out_dir = default_output_dir(source)          # <source dir>/Family
existing = describe_existing(plan_output_paths(source, out_dir))

result = convert_glyphs_to_ufos(source, out_dir)
print(result.open_path)        # the .designspace, or the single UFO
print(result.ufo_paths)        # every master written
print(result.warnings)         # what glyphsLib could not carry over
```

## Command line

```bash
ufo-tdkit-tools glyphs2ufo [-o DIR] [--force] [--apply-corners] [--dry-run] [-v|-q] SOURCE...
```

A thin wrapper over the API below (`cli._cmd_glyphs2ufo`):

- **Destination**: `default_output_dir()` (a folder named after the source,
  beside it); `-o DIR` is used as is for one source and as `DIR/<stem>` for
  several.
- **Overwrite check** from the cheap read (`fast_inspect.read_source()` +
  `plan_output_filenames()`, plus `<stem>.designspace` when there is more
  than one master — the rule Font-Rover's dialog uses). Anything already there
  makes the source fail unless `--force`. A single-master source with brace
  layers also writes a `.designspace`; that one is not predicted.
- **Cancel**: the conversion runs in a worker thread and Ctrl-C sets the
  `cancel` event, so `ConversionCancelled` cleans up what the run wrote.
  Exit code 130. A second Ctrl-C aborts without waiting for the checkpoint.
- **Warnings**: glyphsLib's logger stops propagating to stderr for the run;
  the converter's own handler still collects every record, and the CLI prints
  `summarize_warnings()` groups (`-v` adds examples).
- **Output**: one summary line on stdout, `converted=N failed=K`
  (`planned=N` for `--dry-run`, `cancelled=1` added on Ctrl-C); exit 1 if any
  source failed.

## Conversion Pipeline

1. `load_glyphs_source()` (`format4.py`) parses the source (a file, or a
   directory for `.glyphspackage`) — `glyphsLib.load()` plus the format-4
   rewrite below — and `preflight_source()` repairs what glyphsLib would
   refuse (see **Preflight** below). The parse is ours alone — the source file
   is never written.
2. `glyphsLib.to_designspace(font, write_skipexportglyphs=True)` builds a
   DesignSpace document whose sources carry in-memory UFOs.
3. Sources are deduplicated by filename — a Glyphs **brace layer** becomes a
   separate designspace source that shares one UFO with the master it sits
   between, so the same filename appears more than once and must be written once.
4. Each UFO is written with `glyphsLib.clean_ufo(path)` first, which **removes an
   existing UFO at that path** so a stale `.glif` cannot survive into the new
   master. This is why the caller must check `plan_output_paths()` and confirm.
5. The `.designspace` is written **only when the document has more than one
   source**. For a single master glyphsLib produces a degenerate document — an
   axis whose minimum, default and maximum are all equal, with the source pinned
   at a location outside it — so no document is better than that one.
6. The optional passes run per unique font, still in memory: corner components
   are baked (`apply_corners`), hand-made PostScript hints translated
   (`import_ps_hints`) and TrueType delta amounts restored
   (`restore_tt_delta_settings`). They work on the in-memory ufoLib2 fonts, which
   is why they live here rather than in a plugin — see **Extra passes**.
7. `open_path` points at the `.designspace`, or at the single UFO.

## What Gets Extracted

| Data | Result |
|------|--------|
| Outlines, components, anchors | Full |
| Kerning and groups | Full, as `public.kern1.*` / `public.kern2.*` |
| `kern` / `mark` / `mkmk` / `curs` / `GDEF` | Not written as FEA **by design** — generated at compile time from the kerning, groups and anchors above; `lib` carries the writer list |
| Hand-written features, classes, prefixes | Full, into `features.fea` |
| Axes and their mapping | DesignSpace axes with `<map>` |
| Instances | DesignSpace instances |
| Masters' metrics, alignment zones, stems | `fontinfo.plist` (zones → blue values; stems cleaned, see **Stem snaps**) |
| Glyph colour labels | `public.markColor` |
| Background layers | UFO layer `public.background` |
| Brace (intermediate) layers | Layer-based DesignSpace sources, **sparse** (only the glyphs that have the layer), named by coordinates (`{400, 100}`) — supported |
| Other named layers (backups, e.g. dated ones) | UFO layers under their raw name, in the UFO of their associated master; no master prefix, layer order not kept |
| Layers with no associated master, or unnamed non-brace/bracket layers | **Dropped by glyphsLib, silently** (its warning is gated on `minimize_glyphs_diffs`, which the import does not set) — treated as junk by design |
| Bracket layers | DesignSpace rules **plus** extra `*.BRACKET.*` glyphs, visible in the grid |
| Smart components | **Decomposed and interpolated** at the component's location (`glyphsLib.builder.smart_components`) |
| Custom parameters | `com.schriftgestaltung.*` keys in `lib` |

## What Does Not Come Across

Everything below is stored faithfully by glyphsLib but is *not* applied to the
outlines or translated into UFO-native data. None of it is lost from the UFO.

- **Features Glyphs marks *automatic*** (`aalt` and friends) arrive as an empty
  stub carrying a `# automatic` comment — Glyphs does not cache the generated
  code in the source, so there is nothing to carry. `features.fea` holds the
  hand-written features only.
- **Corner and cap components** are not applied unless asked — the switch
  described under **Extra passes** bakes them.
- **TrueType hints** are not translated: they stay under Glyphs' own key
  (`com.schriftgestaltung.hints`), where a consumer can read them directly
  (Font-Rover's TT Hints overlay does). The importer contributes one thing, `restore_tt_delta_settings()` in `tt_hints.py`: it puts back
  `GSHint.settings`, the field holding a delta's per-ppm amounts, which
  `glyphsLib.builder.hints.to_ufo_hints` does not copy. It is written in Glyphs'
  own shape under Glyphs' own field name, narrowed to the entry belonging to this
  master (found through `lib["com.schriftgestaltung.fontMasterOrder"]`, the
  master index glyphsLib records — also right for brace layers sharing a UFO),
  and a round-trip back to `.glyphs` ignores it. **Glyphs authors deltas
  per instance**, so a master sitting at a location no delta was written for gets
  none — a real case, reported as a note rather than passed over.
- **PostScript hints** *do* come across now, always — see **Extra passes**.

⚠ Testing smart components with a synthetic fixture: the part mapping
(`userData.PartSelection`, Glyphs 2 format) must be on the **default** layer too,
otherwise glyphsLib sees one master and silently leaves the component unexpanded.

## Preflight

Glyphs.app tolerates things glyphsLib refuses outright, and production sources
carry them — refused *during* the conversion, minutes in, after the parse.
`preflight.py` (`preflight_source()`) repairs a parse we own, never writes to the
file on disk, and reports every repair as a conversion warning (category
`preflight`, listed first in the import report). Each repair exists because a
real source stopped the import:

| Defect | What it does | Repair |
|---|---|---|
| The same custom parameter twice (`fsType` at font level, `Axis Values` on an instance) | `RuntimeError: More than one value for this customParameter` | Keep the first value, drop the repeats |
| A `glyphOrder` custom parameter listing the same name twice | Converts fine; the **UFO** is then unreadable — `ValueError: Duplicate glyph names are not allowed` from the fontParts getter, so the font opens empty | Keep each name's first position, drop the repeats |
| A component naming a glyph that does not exist — typically left behind in a background layer | `MissingComponentError` while decomposing that layer | Remove the component |
| Two layers of one master with the same name (dated backups taken in the same minute) | `KeyError: "glyph named 'A' already exists"` — glyphsLib names a background layer `<layer name>.background`, so same-named layers collide | Rename the second ` #1`, the way glyphsLib already renames duplicate outline layers (`builder/layers.py`); no drawing is lost |
| A cubic segment with three off-curves | `GlifLibError: too many offcurve points before curve point` | Remove only duplicates of the point they sit on. **Distinct** points cannot be dropped without changing the outline: in a background or a backup layer the whole contour is removed; in a master, brace or bracket layer the import stops here with `UnwritableOutlineError` naming glyph and layer, instead of minutes later on the write. A closed path is walked from after its last on-curve, so a run wrapping across the start of the node list is caught too. Long quadratic (`qcurve`) runs are legal and untouched |

**The glyphOrder repeat is the one that does not announce itself.** The
conversion is byte-identical to plain glyphsLib and the font opens with **no
glyphs**, because fontParts normalizes the order on *read* and raises. The repair
keeps the written UFO clean, but a consumer should not rely on it: any tool
can write this list unvalidated, so code that opens UFOs with fontParts is
safer reading the order around the normalizer (Font-Rover does, in
`utils/glyph_order.safe_glyph_order()`).

**⚠ `GSLayer.background` is a property that *creates* a background layer when
there is none** (`glyphsLib/classes.py`). Walking `.background` once gave every
layer in a font an empty background and turned a clean source into the
same-name collision above. Read `_background`, which is `None` when absent. A test
pins this.

Problems the preflight does *not* repair still reach the caller, because they are
the source's to fix rather than ours: duplicate designspace locations from
inconsistently named brace layers, duplicate glyph layer names, and kerning
referring to groups that no longer exist all arrive as warnings.

## Glyphs 4 format (experimental)

Glyphs 4 saves `.formatVersion = 4`. glyphsLib (6.15 included) reads formats 2
and 3 and branches on `format_version == 3`, so a format-4 file goes to the
format-2 readers and dies on the first node with `TypeError: expected string
or bytes-like object, got 'list'` — in `fast_inspect`'s cheap read as well as
the conversion.

Structurally format 4 is format 3 with a few changes, so `format4.py` rewrites
the **plist** (before glyphsLib parses it, in `load_glyphs_source()` and in
`fast_inspect`) and glyphsLib then reads it as format 3:

| Format 4 | What glyphsLib would do | Rewrite |
|---|---|---|
| `.formatVersion = 4` | format-2 readers, crash | set to 3 |
| No top-level `familyName`; only `properties -> familyNames` | "Unnamed font" — also names every UFO | copy the `dflt` value |
| No instance `name`; only `properties -> styleNames` | every instance "Regular"; they collide in the axis mapping | copy the `dflt` value |
| Node type `ct` on curve points | corner curve point (smooth flag lost) | read as `cs` — the handles seen so far are collinear |

Everything else format 4 may have changed goes through unnoticed, so the import
report always opens with a "Glyphs 4 format source: import is experimental"
note listing what was rewritten, and the source summary shows
"Glyphs 4 format (experimental)" (`GlyphsSourceInfo.format_version`). The
notes ride on the parsed `GSFont` (`FORMAT_NOTES_ATTR`) and `preflight_source()`
puts them first, so they arrive whoever parsed the font.

A key-path comparison of one format-4 source against nine format-3 ones found
only `familyName` structurally missing; the new keys (stroke attributes,
annotations, a component's `anchor`, instance `id`) are format-3 features or
ignored. **Remove this module once glyphsLib reads format 4** —
`test_plain_glyphslib_cannot_read_format_4` fails on that day.

## Axis mappings the masters contradict

Without "Axis Location" parameters glyphsLib derives each axis's user -> design
`<map>` from the **instances**: user value from `weightClass` / `widthClass`,
design value from `axesValues`. Glyphs itself does not — it exports the design
coordinates as they are. When the classes contradict the coordinates (every
"Condensed" instance at a different width), each instance redefines the last
(glyphsLib logs "redefines the mapping") and the survivor can send every user
value to one design value. The document opens, but **nothing interpolates it**:
`DesignSpaceInterpolator` maps min/default/max through the map, gets one point,
and every location renders the default master.

`axis_maps.repair_axis_maps(doc)` runs right after `to_designspace()` and drops a
map that fails any of: strictly rising outputs; covering every source's design
coordinate; the axis default landing on the default source (the one with
`copyLib`). The axis then spans the sources' design coordinates with no map —
what Glyphs would export — and the report says so (category `preflight`, with
the old map in the details). A sound map is left untouched. Source and instance
locations are design coordinates and do not change.

It was found on a source whose instance names and classes were swapped against
their coordinates — Glyphs' own `instanceInterpolations` cache confirmed the
coordinates, so the names are what is wrong there; the import cannot fix that,
only keep the designspace usable.

## Extra passes

Corners and PS hints run while the fonts are still the `ufoLib2` objects `to_designspace()`
produced. That location is not a preference: `CornerComponentsFilter` fails on a
fontParts `RFont` (`'tuple' object has no attribute 'defaultLayer'`) and on a
defcon `Font` (`'Point' object has no attribute 'type'`), so doing this after
import would need a fontParts→ufoLib2→back round-trip plus undo, dirty-flag and
all-sources bookkeeping. Here it is one call.

**Only the destructive one is a choice.** Baking corners rewrites outlines and
cannot be undone, so it is an argument (`apply_corners`), worth offering only
when the source actually has corner components
(`GlyphsSourceInfo.has_corner_components`). Importing hints adds a lib key and changes no outline, so it
happens unconditionally, exactly as importing a binary font brings its CFF hints
across without asking; the source summary reports how many the file holds.

### Apply corner components (`corners.py`) — on request

Runs `glyphsLib.filters.cornerComponents.CornerComponentsFilter`, then **disarms
the pass** — and that second half is what makes it correct:

1. `Corner` / `Cap` entries are dropped from each glyph's
   `com.schriftgestaltung.hints`;
2. the `cornerComponents` entry is removed from
   `lib["com.github.googlei18n.ufo2ft.filters"]` (`eraseOpenCorners` stays — it
   is a different pass).

Without it the filter is not idempotent: the placement data survives the bake, so
a second run applies the same corner again (4 points → 7 → 10), and ufo2ft would
apply every corner a *second* time at compile, silently doubling them in the
exported font.

One-way by nature: the corner stops being a component — a UI should say so
before offering it.

### Import PostScript hints (`ps_hints.py`) — always

Writes `com.adobe.type.autohint.v2` — the key `ps_hints` and the compiler read
and the binary importer writes — from the structures Glyphs keeps under
`com.schriftgestaltung.hints`:

- `horizontal: true` → `hstem`, `false` → `vstem`. A Glyphs *horizontal* hint
  constrains Y, which is what `hstem` means.
- `Stem` takes position and width from `place: [pos, width]`.
- `TopGhost` / `BottomGhost` carry no `place`: the position comes from the node
  named by `origin: [contour, point]`, and the width is Adobe's ghost marker,
  **−20 top / −21 bottom**.
- **⚠ A closed contour is rotated by one on the way into a UFO**, so a Glyphs
  node index cannot be read straight out of the UFO: in Glyphs the starting
  node of a closed contour is stored last, and glyphsLib does
  `nodes.insert(0, nodes.pop())` (`builder/paths.py`). Reading the index
  unrotated silently lands on the **previous** node. An open contour is not
  rotated. Test on an outline whose points all have distinct Y — on a square
  the wrong index gives the right answer. `ufo_point_index()` does the
  rotation; anything else resolving Glyphs node indices (`tt_hints.py`,
  Font-Rover's TT hints overlay) has to do the same.
- `TT*` and component hints are skipped.
- One flat hint set per glyph, anchored to the first on-curve point (named
  `hintRef0000` if it has no name). Glyphs' list has no hint-replacement
  structure, so more sets would claim information the source does not hold.
- `id` is written empty, which disables the staleness check — the outline is
  exactly what the designer hinted, and claiming a hash only invites it to go
  stale on the first edit.

The hints land in the **default layer**, not in `processedglyphs`. That layer
is the autohinter's output; promoting hints into it is
`ps_hints.batch.import_all_to_processed()`'s job (and `process_font` does it
when it needs the buffer).

### Stem snaps (`stem_snaps.py`) — always

Glyphs 3 keeps the stem *definitions* on the font and one value per definition
on each master, **0 where the master has no value of its own**. UFOs merged
into one Glyphs file come out exactly that way: each UFO adds its stems as new
definitions (named `hStem0` / `vStem0`, repeated), the other masters get 0 in
those slots, and a master can lose every value. glyphsLib
(`builder/masters.py`) copies the list as is, so the UFO got
`postscriptStemSnapH = [0, 0, 67, 41, 0, 0, 0, 0, 0, 0, 0, 0]`.

`clean_stem_snaps()` drops the zeros and repeats and **keeps the order**,
reported as a repair. Never sort: ufo2ft takes `StdHW` / `StdVW` from index 0
of the list as given (it sorts only the `StemSnap*` arrays), so sorting makes
the thinnest stem the standard one. `process_font` cleans the same way at
build time (`pipeline._clean_stem_snaps`, which also applies the CFF limit of
12 entries). A master whose list
was **all zeros** gets the field unset and a `stems` warning: the source holds
nothing to restore, and the values have to come from wherever the master was
merged from. Nothing else in the master's info is touched.

**Merging UFOs into one Glyphs file loses more than stems**, and none of it is
the import's to restore — it is absent from the `.glyphs`: per-UFO naming
(`postscriptFontName`, `openTypeNamePreferred*`, `styleMapFamilyName`, the
`name` records), OS/2 (`weightClass`, `widthClass`, `vendorID`, `panose`,
unicode / code page ranges, `fsType` — glyphsLib then writes its default
`[3]`), PostScript `BlueScale` / `BlueShift` / `BlueFuzz`, `gasp`, and zones of
size 0. Glyphs keeps naming and OS/2 data on **instances**; a merged source has
none, so glyphsLib derives `styleMap*` from the master names. Text with
non-ASCII characters can arrive already replaced by U+FFFD in the `.glyphs`.


## API Reference

### `convert_glyphs_to_ufos(source_path, output_dir, progress_callback=None, apply_corners=False, font=None, cancel=None)`

**Args:**
- `source_path` — `.glyphs` file or `.glyphspackage` directory
- `output_dir` — destination; created if absent
- `progress_callback` — `(step, total, message)`; `total <= 0` means the phase
  has nothing to count and the UI should pulse. ⚠️ The binary importer keeps
  its own two-argument `(step, message)` callback — the two are not the same
  protocol.
- `apply_corners` — bake corner and cap components into the outlines
- `font` — an already-parsed `GSFont` to convert instead of reading the source.
  The converter **takes it over and mutates it** (`to_designspace` does), so it
  is good for exactly one conversion.
- `cancel` — anything with `is_set()`. Polled before the parse, after it, after
  the repairs, after the build and at the head of every master written; raises
  `converter.ConversionCancelled` (with `removed`, the paths it deleted) rather
  than returning a result. It is not re-exported from the package.

Hand-made PostScript hints are translated unconditionally; there is no argument
for it, and `ps_hints_imported` on the result reports what it did.

**Returns:** `GlyphsConversionResult`

**Raises:** `FileNotFoundError` (missing source), `ValueError` (destination inside
the source), plus whatever `glyphsLib` raises for an unreadable source.

### `inspect_glyphs_source(source_path)` → `GlyphsSourceInfo`

Parses without converting: exact, and slow (a full parse). Kept for scripts and
tests. **An interactive UI should use `fast_inspect.read_source()` instead.**

### `fast_inspect.read_source(source_path)` → `(GlyphsSourceInfo, GSFont)`

The cheap read: glyph counts from `order.plist`, everything else from
`fontinfo.plist` through glyphsLib's parser. Counts it cannot know (hints,
corner placements) are `COUNT_UNKNOWN`. `quick_inspect()` returns the info
alone. The `GSFont` has no glyphs and is what `plan_output_filenames()` takes.

### `fast_inspect.plan_output_filenames(gs_font)` → `list[str]`

The UFO filenames a conversion of that font would write, reproducing
glyphsLib's naming (`UFO Filename` custom parameter, ` #n` for same-named
masters). Verified equal to `to_designspace`'s own answer on a 216-master
source, without building anything.

### `plan_output_paths(source_path, output_dir)` → `list[Path]`

The paths a conversion would write: UFOs first, then the `.designspace` when one
would be written. Pair with `describe_existing()` for the overwrite check.

### `GlyphsSourceInfo`

```python
@dataclass
class GlyphsSourceInfo:
    path: Path
    family_name: str
    master_count: int
    glyph_count: int
    instance_count: int
    axes: list[tuple[str, str]]   # (name, tag)
    corner_component_count: int   # corner/cap placements, not _corner.* glyphs
    ps_hint_count: int
    tt_hint_count: int
    format_version: int = 3       # 4 is read experimentally (format4.py)

    @property
    def is_multi_master(self) -> bool: ...
    @property
    def has_corner_components(self) -> bool: ...
    @property
    def has_ps_hints(self) -> bool: ...
```

### `GlyphsConversionResult`

```python
@dataclass
class GlyphsConversionResult:
    source_path: Path
    output_dir: Path
    ufo_paths: list[Path]
    designspace_path: Path | None     # None for a single master
    open_path: Path                   # the DS, or the lone UFO
    warnings: list[ConversionWarning]   # preflight repairs first, then glyphsLib's
    glyph_count: int
    master_count: int
    corners_applied: int               # glyphs whose outlines the bake changed
    ps_hints_imported: int             # glyphs given Adobe hint data
    tt_deltas_restored: int            # TTDelta hints given their per-ppm amounts

    @property
    def is_designspace(self) -> bool: ...
```

## Dependencies

The `glyphs` extra: `glyphsLib>=6.0.0,<7` and `ufoLib2>=0.16.0`. The upper bound
is deliberate — `ps_hints` / `tt_hints` assume how glyphsLib rotates a closed
path's nodes, and `corners` uses `glyphsLib.filters.cornerComponents`, both
internals. Exercised on glyphsLib 6.12.7 and 6.15.0.
