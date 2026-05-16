# PS Hints Optimizer & Analyzer — Algorithm Reference

## Overview

The PostScript hints optimizer (`optimizer.py`) takes autohinter output — which often
contains multiple conflicting hint sets with overlapping stems — and collapses it into
a single, clean hint set suitable for final font production. The analyzer (`analyzer.py`)
detects the same issues without applying changes, for UI preview.

**Why optimize?** The Adobe autohinter (`otfautohint`) generates hint substitution — 
multiple hint sets activated at different outline points. While valid for CFF rendering,
hint substitution adds complexity and can cause rasterizer inconsistencies. Many
professional workflows prefer a single global hint set per glyph, which requires
resolving all conflicts the autohinter left behind.

---

## Pipeline

```
                         PSHintData (multiple hint sets)
                                     │
                          ┌──────────┴──────────┐
                          │  Collect unique stems │
                          └──────────┬──────────┘
                                     │
                     ┌───────────────┼───────────────┐
                     │               │               │
               Ghost hints    Triple stems    Regular stems
               (keep all)     (keep as-is)    (optimize)
                     │               │               │
                     │               │     ┌─────────┴─────────┐
                     │               │     │ Step 1: Snap-compat│
                     │               │     │ filter (20%, ≥5u)  │
                     │               │     └─────────┬─────────┘
                     │               │               │
                     │               │     ┌─────────┴─────────┐
                     │               │     │ Step 2: Accent-zone│
                     │               │     │ filter (Unicode NFD)│
                     │               │     └─────────┬─────────┘
                     │               │               │
                     │               │     ┌─────────┴─────────┐
                     │               │     │ Step 3: Coverage   │
                     │               │     │ map (ray casting)  │
                     │               │     └─────────┬─────────┘
                     │               │               │
                     │               │     ┌─────────┴─────────┐
                     │               │     │ Step 4: Small      │
                     │               │     │ element filter     │
                     │               │     └─────────┬─────────┘
                     │               │               │
                     │               │     ┌─────────┴─────────┐
                     │               │     │ Step 5: vstem3     │
                     │               │     │ detection (C(N,3)) │
                     │               │     └─────────┬─────────┘
                     │               │               │
                     │               │     ┌─────────┴─────────┐
                     │               │     │ Step 6: Overlap    │
                     │               │     │ resolution (L/R)   │
                     │               │     └─────────┬─────────┘
                     │               │               │
                     └───────────────┴───────────────┘
                                     │
                          ┌──────────┴──────────┐
                          │  Combine into single │
                          │  PSHintSet           │
                          └─────────────────────┘
```

---

## Step 1: Snap-Compatibility Filter

**Rule:** A vstem is kept iff its width lies within tolerance of **at least one**
`stemSnapV` value. Tolerance per snap is `max(5.0, snap × 0.20)` — 20% of the snap
value with a 5-unit floor.

**Why scale with snap, not UPM?** Stem width spans a huge range across weights
(Thin ≈ 20, Black ExtraWide ≈ 400) for the same UPM. A fixed absolute or UPM-based
threshold over-shoots one end and under-shoots the other. `stemSnapV` already encodes
the font's design widths, so scaling tolerance off it gives a uniform "this stem is
or isn't a design stem" criterion at every weight.

**Tolerance examples:**

| `stemSnap` value | Tolerance | Accepted range |
|---|---|---|
| 20  | 5  | [15, 25] |
| 60  | 12 | [48, 72] |
| 80  | 16 | [64, 96] |
| 200 | 40 | [160, 240] |
| 400 | 80 | [320, 480] |

**Multi-snap example (`stemSnapV = [50, 55, 60]`):**
```
Per-snap windows: [40, 60] ∪ [44, 66] ∪ [48, 72] = [40, 72]
A stem of 70 is kept (close to 60). A stem of 75 is removed.
```

**Gap example (`stemSnapV = [50, 100]`):**
```
Per-snap windows: [40, 60] ∪ [80, 120]
A stem of 70 is removed (not near any snap — likely a counter artifact).
```

**Fallback:** if `stemSnapV` is empty, use the legacy `width > UPM × 0.3` cut.

---

## Step 2: Accent-Zone Filter (Unicode NFD)

For glyphs whose codepoint NFD-decomposes into a base + combining marks, hints
falling entirely in the accent's vertical zone are removed — both H and V.

### Detection

1. Resolve the glyph's primary codepoint. Prefer `glyph.unicodes[0]`; fall back to
   `fontTools.agl.toUnicode(glyph.name.split(".", 1)[0])` to handle suffixed names
   (`dcaron.alt`, `aacute.sc`). Adobe PUA codepoints (U+E000–U+F8FF, supplementary
   PUAs) are treated as "no codepoint" — these come from legacy names like `cyrbreve`
   that don't represent a real precomposed character.

2. Apply `unicodedata.normalize("NFD", chr(cp))`. If the result has length ≥ 2 the
   glyph is an accented composite. Every combining mark (`category == "Mn"`) is
   classified by its canonical combining class (`unicodedata.combining()`):
   - CCC ∈ {200, 202, 218, 220, 222, 233, 240} → below-accent
     (cedilla CCC 202, ogonek CCC 202, dot below CCC 220, comma below CCC 220, …)
   - CCC ∈ {214, 216, 228, 230, 232, 234} → above-accent
     (acute, breve, caron, dieresis, macron, … all CCC 230)

   Name-substring (`"BELOW"`) is not used — it misses CEDILLA and OGONEK which
   sit below but have no "BELOW" token in their Unicode name.

3. A glyph can carry **both** above and below marks (Vietnamese `ợ` = horn + dot
   below). Both zones get cut independently.

### Zone bounds

The cut threshold comes from the base glyph's actual outline when possible —
this handles the ascender/xHeight distinction automatically:

```
Above zone = base_glyph.bounds.yMax + 5
Below zone = base_glyph.bounds.yMin − 5
```

For `dcaron`: base = `d`, yMax ≈ ascender → caron cut, d preserved.
For `aacute`: base = `a`, yMax ≈ xHeight → acute cut, a preserved.

**Soft-dotted bases (i, j, ї, ј, …)** require special handling: the bare base
glyph (e.g., `i`) carries a tittle that gets *replaced* by the accent in the
composite (`idieresis`, `afii10104`). Using `i.bounds.yMax` would place the
threshold above the accent and miss it. For these bases (Unicode `Soft_Dotted`
property — Latin i/j and variants, Cyrillic і/ј, Greek yot) the threshold is
`info.xHeight + 5` regardless of whether the base glyph exists.

**Fallbacks** when the base glyph isn't in the UFO:
- Above: `info.capHeight + 5` for Lu base; `info.xHeight + 5` for soft-dotted
  Ll bases; `info.ascender + 5` for other Ll/Lt (safer than xHeight — never
  clips an ascender on `dcaron`, `lcaron`, etc.).
- Below: `−5` (just below baseline).

### Cut criteria

- **hstem**: removed when `hstem.position ≥ above_y` or `hstem.end ≤ below_y`.
  Direct test — hstem coordinates are Y.
- **vstem**: point-based detection on the stem's vertical edges. For
  each vstem the optimizer collects all contour points (decomposing
  components first via `DecomposingRecordingPen`) whose X falls within
  ±3 units of the stem's left edge (`stem.position`) or right edge
  (`stem.position + stem.width`). These points sit on the vertical edges
  of the stroke the vstem represents. If **every** such point's Y
  coordinate falls in the accent zone (`≥ above_y − 5` or `≤ below_y + 5`),
  the stem belongs to the accent and is removed.

  Three regimes are handled by the same rule:

  * **Separate-contour accents** — decomposed `Adieresis` dots: only
    the dot contour has points at the stem's X edges; all Y values lie
    above capHeight → remove.
  * **Integrated accents** — `Ccedilla`/`Aogonek` where the cedilla or
    ogonek is fused with the base contour: the accent's vertical edges
    sit at X values where the base body has no points (e.g. the cedilla
    in DIN's Ccedilla has on-curve points at X=314 and X=399, while the
    C body's nearest X positions are 306, 346, 385, 461). All edge points
    are below baseline → remove.
  * **Base-body stems under any accent** — `Iacute`'s I-body vstem
    (X 86 to 201): the I body has on-curve points at the same X spanning
    from baseline to capHeight. Not all in the accent zone → keep.

  Earlier attempts (X-bbox containment, edge-matching on per-contour
  bboxes) misfired on integrated accents because they have no separate
  bbox. The point-based test works without needing the accent to be its
  own contour.

This is the primary line of defense against accent stems. The geometric small-element
filter (Step 4) still runs as a fallback for non-Unicode and AGL-unknown glyphs.

---

## Step 3: Coverage Map (Filled Height Metric)

When a `glyph` object is provided, the optimizer builds a **coverage map** — for each
vstem, it measures how much actual filled contour exists at the stem's X position.

### Ray Casting + Even-Odd Fill

A vertical ray is cast through the stem's center (`x = position + width/2 + 0.5`).
The ray intersects contour segments (lines, quadratics, cubics), producing a sorted
list of Y-crossing coordinates. The even-odd fill rule pairs consecutive crossings:

```
Stem center at X=85:

Ray hits contour at Y = [0, 50, 200, 700]
                          │     │
Fill pairs:  [0─50]  [200─700]
             filled   filled
             ▔▔▔▔▔   ▔▔▔▔▔▔▔

Total coverage = 50 + 500 = 550 units
```

**Why not bounding box?** A bounding box would report 700 units here, counting the
unfilled gap between 50 and 200. The ray casting method correctly handles cases where
a horizontal crossbar passes through the stem's X range at a different Y level — only
the actually filled portions count.

### Segment Extraction

Contour points are converted to segment tuples:
- 2 points → line segment
- 3 points → quadratic Bezier (TrueType)
- 4 points → cubic Bezier (PostScript)

For composite glyphs (components only), a `DecomposingRecordingPen` is used to
expand all components into flat contours before segment extraction.

---

## Step 4: Small Element Filtering (geometric fallback)

This step still runs after the Unicode-based accent filter, primarily catching:
- Glyphs without Unicode codepoints (custom alternates, ligatures)
- Glyphs whose name isn't in AGL (`cyrabreve`, `palochka.alt`)
- Composite-glyph leg artifacts that aren't accents (Cyrillic Д, Ц legs)


Stems belonging to accent marks, descender legs, or other minor glyph elements should
not participate in vstem3 detection or overlap resolution — they produce false positives.

### Candidate Selection

A stem is a **candidate for removal** if its filled height is less than 40% of the
maximum filled height among all vstems. Only alphabetic glyphs (Unicode categories
Lu, Ll, Lt) are filtered.

### Two-Pass Removal

A candidate is removed only if one of two conditions is met:

#### Pass 1: Accent Zone Detection

The glyph's vertical space is divided into zones using font metrics:

```
Uppercase (Lu):                    Lowercase (Ll, Lt):

  ─── accent zone ───                ─── accent zone ───
  ═══ capHeight ═════                ═══ xHeight ════════
  ─── main body  ────                ─── main body  ────
  ═══ baseline (0) ══                ═══ baseline (0) ══
  ─── accent zone ───                ─── accent zone ───
```

For each candidate stem, the coverage is split into **main body zone** `[0, reference_y]`
and **accent zones** (above reference or below baseline). If more than 70% of the stem's
coverage falls in accent zones, it is classified as an accent stem and removed.

**Example — decomposed idieresis (lowercase):**
```
xHeight = 500

Stem 1: vstem 195 82  (main stroke of i)
  Ray crossings: Y = [0, 500]
  Main zone [0, 500]: 500 units (100%)
  Accent zone: 0 units (0%)
  → NOT accent, keep

Stem 2: vstem 175 50  (left dot)
  Ray crossings: Y = [560, 610]
  Main zone [0, 500]: 0 units (0%)
  Accent zone: 50 units (100%)
  → ACCENT (100% > 70%), remove

Stem 3: vstem 260 50  (right dot)
  → Same analysis as left dot → ACCENT, remove
```

**Example — cedilla hook (lowercase):**
```
xHeight = 500

Hook stem coverage:
  Ray crossings: Y = [-180, -50]
  Main zone [0, 500]: 0 units
  Accent zone (below 0): 130 units (100%)
  → ACCENT, remove
```

#### Pass 2: Touch-Based Filtering

If the candidate is NOT in the accent zone, it is checked for spatial proximity to
main stems. The stem is removed if it overlaps or touches (within 3-unit tolerance)
any main stem:

```
touch condition: (cand.position - 3) < main.end  AND  (main.position - 3) < cand.end
```

**Example — Cyrillic De (Д, uppercase):**
```
capHeight = 700

Stem 1: vstem  30 85  (left vertical, coverage=680)  → main
Stem 2: vstem 365 85  (right vertical, coverage=670) → main
Stem 3: vstem  10 40  (left leg, coverage=120)        → candidate (120/680 = 18%)
Stem 4: vstem 435 40  (right leg, coverage=110)       → candidate

Stem 3 accent check:
  Coverage from Y = [-80, 40]
  Main zone [0, 700]: 40 units (33%)
  Accent zone: 80 units (67%)
  → Not accent (67% < 70%)

Stem 3 touch check:
  Stem 1: position=30, end=115
  Stem 3: position=10, end=50
  (10 - 3) < 115 AND (30 - 3) < 50 → YES, touches Stem 1
  → REMOVE (leg touches main vertical)

Stem 4: same analysis → touches Stem 2 → REMOVE
```

**Example — Cyrillic Soft Sign (Ь, uppercase):**
```
capHeight = 700

Stem 1: vstem  30 85  (left vertical, coverage=680)  → main
Stem 2: vstem 340 82  (bowl right edge, coverage=270) → candidate (270/680 = 40%)

Stem 2 accent check:
  Coverage from Y = [0, 350]
  Main zone [0, 700]: 350 units (100%)
  → Not accent

Stem 2 touch check:
  Stem 1: position=30, end=115
  Stem 2: position=340, end=422
  (340 - 3) < 115? → 337 < 115? → NO
  → Does not touch → KEEP
```

### Composite Glyph Filtering

For composite glyphs (components, no contours), a different strategy is used: the
base component (tallest by bounding box height) is identified, and its processed
layer hints are read. Composite vstems that don't match any base vstem (within
position ±3, width ±5 tolerance) are classified as accent vstems and removed.

---

## Step 5: vstem3 Detection

**Critical:** vstem3 detection runs BEFORE overlap resolution. If it ran after,
the overlap step would eliminate valid triple candidates.

### Adobe Type 1 Constraints (T1_SPEC.pdf pp.53-54)

A valid `vstem3` (or `hstem3`) requires exactly three stems satisfying:

1. **Equal outer widths:** `|s0.width - s2.width| ≤ 1.0`
2. **Equal center-to-center spacing:** `|(c1 - c0) - (c2 - c1)| ≤ 1.0`
3. **No pairwise overlaps** between any two of the three stems

### Combinatorial Search

All `C(N, 3)` combinations of remaining vstems are tested. Typical N < 10, so the
cost is negligible (~120 combinations max).

When multiple valid triples exist, the one with stems closest to stemSnap values
(lowest sum of snap distances) is selected.

**Example — Cyrillic Sha (Ш):**
```
stemSnapV = [82]

Vstems after filtering:
  s0: vstem  30 82   (left vertical)
  s1: vstem 220 82   (center vertical)
  s2: vstem 410 82   (right vertical)

Check (s0, s1, s2):
  Outer widths: |82 - 82| = 0 ≤ 1.0  ✓
  Centers: c0=71, c1=261, c2=451
  Spacing: (261-71) = 190, (451-261) = 190
  |190 - 190| = 0 ≤ 1.0  ✓
  No overlaps  ✓

  → Valid vstem3: "vstem3 30 82 220 82 410 82"
  → All 3 stems removed from regular pool
  → Any stem touching/overlapping the triple also removed
```

### Cleanup After vstem3

After a triple is detected, stems that touch or overlap any component of the triple
are also removed. This prevents a stem like a trailing hook that shares a boundary
with the rightmost triple stem from surviving into the overlap resolution phase.

---

## Step 6: Overlap Resolution

### Vertical Stems

Remaining vstems are classified as **left** or **right** based on glyph center:
```
center = glyph_width / 2.0
left:  stem center < glyph center
right: stem center >= glyph center
```

**Centric detection:** If any left stem overlaps any right stem, all stems are treated
as centric (typical for narrow glyphs like `i`, `j`, `l`).

#### Same-Side Overlap Groups

Overlapping stems on each side are grouped. For each group, one stem is kept using
a 4-tier priority:

| Priority | Condition | Action |
|----------|-----------|--------|
| 1 | Coverage difference > 2× and > 100 units | Pick by combined score |
| 2 | Different snap distances (diff > 0.5 units) | Pick closest to stemSnap |
| 3 | Mixed widths (diff > 15% or > 10 units) | Pick closest to stemSnap |
| 4 | Fallback | Leftmost (left side) / Rightmost (right side) |

#### Centric Overlap Groups

For centric glyphs, all overlapping stems are grouped together (no L/R split) and
resolved by `_pick_best_stem()`.

#### Combined Scoring (`_pick_best_stem`)

When both stemSnap and coverage data are available:

```
score = snap_distance - (normalized_coverage × 5.0)

where normalized_coverage = coverage / max_coverage ∈ [0, 1]
```

Lower score wins. This means:
- Snap difference > 10 units: snap always wins
- Snap difference < 5 units: coverage can break the tie
- Full coverage is worth ~5 font units of snap advantage

### Horizontal Stems

Simpler than vstems: no L/R split, no coverage-based resolution. Overlapping hstems
are grouped and resolved purely by stemSnap proximity.

---

## Analyzer (`analyzer.py`)

The analyzer detects the same issues as the optimizer but without applying changes.
It returns a list of `(pattern_tag, detail_string)` tuples for UI display.

### Pattern Tags

| Tag | Description |
|-----|-------------|
| `snap-incompatible-v` | vstem width not within ~20% of any stemSnap value |
| `centric-overlap` | Cross-side vstem overlap (narrow glyphs) |
| `overlap-v` | Same-side vstem overlap |
| `overlap-h` | Horizontal stem overlap |
| `potential-vstem3` | 3 vstems matching Adobe stem3 constraints |

### vstem3 Detection in Analyzer

The analyzer uses the same `C(N, 3)` combinatorial search with identical Adobe Type 1
constraints and stemSnap-based scoring. The best candidate (if any) is reported as a
`potential-vstem3` issue.

---

## Data Flow: Parser → Optimizer → Writer

```
UFO Font
  │
  ├─ processedglyphs layer (from otfautohint)
  │   └── glyph.lib["com.adobe.type.autohint.v2"]
  │       ├── hintSetList: [{pointTag, stems}, ...]
  │       ├── id: outline hash
  │       └── flexList: [point_names]
  │
  ▼
parser.parse_ps_hints(glyph, source, font)
  │
  ▼
PSHintData
  ├── hint_sets: [PSHintSet, ...]  (multiple sets = hint substitution)
  ├── source: HintSource.PROCESSED_LAYER
  ├── id_hash, is_stale, flex_points
  │
  ▼
optimizer.optimize_hints(hint_data, glyph_width, snap_v, snap_h, upm, glyph)
  │
  ▼
PSHintData (single optimized hint set)
  │
  ▼
optimizer.apply_optimized_hints(glyph, font, optimized_data)
  │
  ▼
processedglyphs layer glyph.lib updated
```

---

## Constants and Thresholds

| Constant | Value | Used in |
|----------|-------|---------|
| Snap tolerance (per snap) | `max(5, snap × 0.20)` | Step 1 |
| Snap fallback (no stemSnapV) | `width > UPM × 0.3` | Step 1 |
| Accent-zone buffer | 5 units around base bbox / metric | Step 2 |
| Small element threshold | 40% of max coverage | Step 4 |
| Accent zone threshold (geometric) | 70% of coverage outside main body | Step 4 |
| Touch tolerance | 3.0 units | Step 4 |
| Composite match tolerance | position ±3, width ±5 | Step 4 (composites) |
| vstem3 width tolerance | 1.0 unit | Step 5 |
| vstem3 spacing tolerance | 1.0 unit | Step 5 |
| Coverage significance | >2× ratio AND >100 units diff | Step 6 |
| Mixed width threshold | >15% or >10 units | Step 6 |
| Snap distance significance | >0.5 units difference | Step 6 |
| Coverage weight in scoring | 5.0 font units equivalent | Step 6 |

---

## Edge Cases

### Glyphs Without Unicode
Non-alphabetic glyphs or glyphs without unicode values bypass small-element filtering
entirely. All stems participate in vstem3 detection and overlap resolution.

### Missing Font Metrics
If `capHeight` or `xHeight` is `None`, accent zone detection is skipped. Only the
touch-based filtering remains active for small-element candidates.

### No Glyph Object (Backward Compatibility)
When `glyph=None`, the optimizer falls back to position-based logic (no coverage map,
no accent zone detection, no touch filtering). This preserves backward compatibility
with callers that don't pass the glyph object.

### Ghost Hints
Ghost hints (width -20 or -21) are separated at the start and preserved unconditionally.
They represent edge hints for top/bottom alignment zones and never conflict with
regular stems.

### Existing Triples
If the autohinter already produced `vstem3` or `hstem3` hints, they are preserved
as-is and excluded from regular optimization.
