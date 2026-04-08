"""Binary font (OTF/TTF/WOFF/WOFF2) to UFO conversion with CFF hint extraction.

Requires the ``extraction`` extra: ``pip install ufo-tdkit-tools[extraction]``
"""

from .warnings import ConversionWarning, WarningSeverity


def __getattr__(name):
    """Lazy imports for optional extraction dependencies."""
    _converter_names = {"ConversionResult", "convert_binary_to_ufo"}
    if name in _converter_names:
        from ufo_tdkit_tools.extraction import converter

        return getattr(converter, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ConversionResult",
    "convert_binary_to_ufo",
    "ConversionWarning",
    "WarningSeverity",
]
