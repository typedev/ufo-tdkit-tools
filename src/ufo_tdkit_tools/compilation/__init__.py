"""UFO to OTF compilation with PS hint preservation.

Requires the ``compilation`` extra: ``pip install ufo-tdkit-tools[compilation]``

Exception: :mod:`legacy_kern` (``add_legacy_kern`` and friends) post-processes
an already-compiled binary and needs only fontTools, so it works on a bare
install.
"""


def __getattr__(name):
    """Lazy imports for optional compilation dependencies."""
    _compiler_names = {
        "compile_otf_preserve",
        "compile_otf_preserve_optimized",
        "generate_goadb",
        "is_preserve_mode",
        "prepare_processedglyphs",
    }
    _legacy_kern_names = {
        "add_legacy_kern",
        "build_kern_table",
        "flatten_gpos_kern",
    }
    _preserve_names = {
        "BatchCompileResult",
        "PreserveCompileResult",
        "preserve_compile",
        "preserve_compile_batch",
    }
    if name in _compiler_names:
        from ufo_tdkit_tools.compilation import compiler

        return getattr(compiler, name)
    if name in _legacy_kern_names:
        from ufo_tdkit_tools.compilation import legacy_kern

        return getattr(legacy_kern, name)
    if name in _preserve_names:
        from ufo_tdkit_tools.compilation import preserve

        return getattr(preserve, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "add_legacy_kern",
    "build_kern_table",
    "flatten_gpos_kern",
    "compile_otf_preserve",
    "compile_otf_preserve_optimized",
    "generate_goadb",
    "is_preserve_mode",
    "prepare_processedglyphs",
    "BatchCompileResult",
    "PreserveCompileResult",
    "preserve_compile",
    "preserve_compile_batch",
]
