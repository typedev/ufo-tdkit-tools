"""ufo-tdkit-tools: PS hints extraction, optimization, and compilation for UFO fonts."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    # Single source of truth: the version in pyproject.toml, via package metadata.
    __version__ = _pkg_version("ufo-tdkit-tools")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"

from .constants import (
    ADOBE_HINT_KEY_V2,
    ADOBE_HINT_KEY_V1,
    PUBLIC_PS_HINT_KEY,
    PROCESSED_LAYER_NAME,
    PROCESSED_LAYER_NAME_ALT,
    VALID_STEM_TYPES,
    MAX_STEMS_PER_HINTSET,
    compute_outline_hash,
)

__all__ = [
    "__version__",
    "ADOBE_HINT_KEY_V2",
    "ADOBE_HINT_KEY_V1",
    "PUBLIC_PS_HINT_KEY",
    "PROCESSED_LAYER_NAME",
    "PROCESSED_LAYER_NAME_ALT",
    "VALID_STEM_TYPES",
    "MAX_STEMS_PER_HINTSET",
    "compute_outline_hash",
    "process_font",
    "ProcessResult",
    "add_legacy_kern",
]


def __getattr__(name):
    if name in ("process_font", "ProcessResult"):
        from .pipeline import ProcessResult, process_font

        return {"process_font": process_font, "ProcessResult": ProcessResult}[name]
    if name == "add_legacy_kern":
        # fontTools-only; usable without the compilation extra
        from .compilation.legacy_kern import add_legacy_kern

        return add_legacy_kern
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
