# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Conversion warning types for binary font import."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class WarningSeverity(Enum):
    """Severity level of a conversion warning."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class ConversionWarning:
    """A single warning/note produced during binary -> UFO conversion."""

    category: str  # "features", "hints", "info", "general"
    severity: WarningSeverity
    message: str
    details: str | None = None
