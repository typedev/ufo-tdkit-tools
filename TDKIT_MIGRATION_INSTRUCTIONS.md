# TDKit Migration Instructions: ufo-tdkit-tools integration

These instructions should be executed from the TDKit project directory by Claude Code.

## Context

A new package `ufo-tdkit-tools` was created at `/home/alexander/WORK/ufo-tdkit-tools/`.
It contains PS hints extraction, optimization, and preserve-mode compilation code
that was extracted from TDKit and ufo-widgets-gtk4.

TDKit should now use this package as a dependency instead of its internal copies.

## What was extracted FROM TDKit

### From `src/tdkit/compilers/otf_ttf.py` — these functions:
- `is_preserve_mode()` (~line 421)
- `_compile_ufo2ft_shell()` (~line 433)
- `_compile_makeotf_hinted()` (~line 468)
- `generate_goadb()` (~line 567)
- `prepare_processedglyphs()` + helpers `_decompose_glyph`, `_ensure_hint_ref_point`, `_deep_copy_hint_data` (~line 606-767)
- `compile_otf_preserve()` (~line 770)
- `compile_otf_preserve_optimized()` (~line 794)

**NOT extracted** (keep in TDKit): `compileOTF_afdko`, `compileTTF_ufo2ft`, `pshint_ufo_afdko`, `parse_pshint_commands`

### From `src/tdkit/compilers/preserve.py` — entire file

### From `src/tdkit/hints/ps_hints.py` — only `validate_ps_hints()` function (lines 28-179)
**NOT extracted**: `getPSHintsGlyphData`, `ProcessedHashMapParser`, `main()`, constants — all stay

## New import paths

| Old import | New import |
|---|---|
| `tdkit.compilers.otf_ttf.is_preserve_mode` | `ufo_tdkit_tools.compilation.is_preserve_mode` |
| `tdkit.compilers.otf_ttf.compile_otf_preserve` | `ufo_tdkit_tools.compilation.compile_otf_preserve` |
| `tdkit.compilers.otf_ttf.compile_otf_preserve_optimized` | `ufo_tdkit_tools.compilation.compile_otf_preserve_optimized` |
| `tdkit.compilers.otf_ttf.prepare_processedglyphs` | `ufo_tdkit_tools.compilation.prepare_processedglyphs` |
| `tdkit.compilers.otf_ttf.generate_goadb` | `ufo_tdkit_tools.compilation.generate_goadb` |
| `tdkit.compilers.preserve.preserve_compile` | `ufo_tdkit_tools.compilation.preserve_compile` |
| `tdkit.compilers.preserve.preserve_compile_batch` | `ufo_tdkit_tools.compilation.preserve_compile_batch` |
| `tdkit.compilers.preserve.PreserveCompileResult` | `ufo_tdkit_tools.compilation.PreserveCompileResult` |
| `tdkit.compilers.preserve.BatchCompileResult` | `ufo_tdkit_tools.compilation.BatchCompileResult` |
| `tdkit.hints.ps_hints.validate_ps_hints` | `ufo_tdkit_tools.ps_hints.validate_ps_hints` |

## Step-by-step changes

### 1. pyproject.toml
- Add `"ufo-tdkit-tools[compilation]"` to `dependencies` list
- Add `[tool.uv.sources]` section (or append to existing):
  ```toml
  [tool.uv.sources]
  ufo-tdkit-tools = { path = "../ufo-tdkit-tools" }
  ```

### 2. src/tdkit/builder/worker.py
Find line ~29 with imports from `tdkit.compilers.otf_ttf`.
Split into two imports — extracted functions from new package, rest stays:
```python
# Extracted to ufo-tdkit-tools
from ufo_tdkit_tools.compilation import (
    compile_otf_preserve,
    compile_otf_preserve_optimized,
    is_preserve_mode,
)
# Remaining TDKit-only functions
from tdkit.compilers.otf_ttf import compileOTF_afdko, compileTTF_ufo2ft, pshint_ufo_afdko
```

### 3. src/tdkit/builder/td_builder.py
Same pattern as worker.py — find preserve-related imports, redirect to new package.

### 4. src/tdkit/compilers/otf_ttf.py (MOST DELICATE)
- REMOVE extracted functions (~520 lines): is_preserve_mode, _compile_ufo2ft_shell, _compile_makeotf_hinted, generate_goadb, prepare_processedglyphs + 3 helpers, compile_otf_preserve, compile_otf_preserve_optimized
- ADD re-exports near the top:
  ```python
  # Re-exports from ufo-tdkit-tools (preserve compilation)
  from ufo_tdkit_tools.compilation.compiler import (
      is_preserve_mode,
      compile_otf_preserve,
      compile_otf_preserve_optimized,
      prepare_processedglyphs,
      generate_goadb,
  )
  ```
- KEEP: compileOTF_afdko, compileTTF_ufo2ft, pshint_ufo_afdko, parse_pshint_commands
- Clean up imports that were only used by extracted functions (shutil, tempfile may become unused)

### 5. src/tdkit/compilers/preserve.py
Replace entire content with re-exports:
```python
"""Backward compatibility — preserve API moved to ufo-tdkit-tools."""
from ufo_tdkit_tools.compilation.preserve import (
    PreserveCompileResult,
    BatchCompileResult,
    preserve_compile,
    preserve_compile_batch,
)

__all__ = [
    "PreserveCompileResult",
    "BatchCompileResult",
    "preserve_compile",
    "preserve_compile_batch",
]
```

### 6. src/tdkit/hints/ps_hints.py
- Add import near top (after existing constants): `from ufo_tdkit_tools.ps_hints.validator import validate_ps_hints`
- DELETE the local `validate_ps_hints()` function definition (~lines 28-179)
- KEEP everything else: constants, getPSHintsGlyphData, ProcessedHashMapParser, main()

## Verification

```bash
uv sync
uv run python -c "
from tdkit.compilers.otf_ttf import is_preserve_mode, compile_otf_preserve_optimized
from tdkit.compilers.preserve import preserve_compile
from tdkit.hints.ps_hints import validate_ps_hints
from tdkit.builder.worker import build_instance_worker
print('All TDKit imports OK')
"
uv run pytest tests/ -v
```

## Threading safety note

The preserve compiler functions are stateless (string paths + scalar params).
ProcessPoolExecutor in worker.py serializes function references by module path.
Changing from `tdkit.compilers.otf_ttf` to `ufo_tdkit_tools.compilation.compiler`
is transparent — Python resolves the function at import time in each worker process.
No changes to threading model needed.

## Function signatures

The migration originally aimed to preserve signatures byte-for-byte. As of the
makeotf compatibility fix, the `makeotfexe_path` keyword has been renamed to
`makeotf_path` because the underlying binary changed (recent AFDKO releases
ship `makeotfexe` as a deprecation stub with no compilation behaviour).
TDKit callers passing `makeotfexe_path=...` need to update the kwarg name.

```python
compile_otf_preserve_optimized(
    ufo_path, otf_path, logger=None, pshash_rebuild=False,
    tx_path=None, makeotf_path=None, stats=None
) -> bool

compile_otf_preserve(
    ufo_path, otf_path, logger=None, pshash_rebuild=False,
    tx_path=None, makeotf_path=None
) -> bool

is_preserve_mode(pshinter) -> bool

preserve_compile(ufo_path, otf_path, logger=None, tx_path=None, makeotf_path=None) -> PreserveCompileResult

preserve_compile_batch(input_dir, output_dir, logger=None, parallel=False, workers=None, on_progress=None) -> BatchCompileResult

validate_ps_hints(ufo_path, logger=None) -> dict
```
