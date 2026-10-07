# Copyright 2024-2026 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""
Turning thousands of import warnings into something a person can read.

A production Glyphs source produces warnings in bulk: a large multi-axis source reports
19 626 of them, and all but a handful are the same sentence about a different
glyph. Listing them tells the reader nothing and hides the four that matter,
which is why the import used to send the lot to the log and show only a count.

Grouping by the shape of the sentence -- numbers replaced by `N`, quoted names
by `'...'` -- collapses that to a dozen rows: "19 380x All components of the
background layer of '...' will be decomposed", and, first in the list because
they are ours and actionable, the repairs preflight made.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

#: How many example messages a group keeps. Enough to recognise the pattern,
#: few enough that the window does not become the list it replaces.
MAX_EXAMPLES = 50

_NUMBERS = re.compile(r"\d+")
_QUOTED = re.compile(r"'[^']*'")
# A dotted identifier -- `public.kern1.o_left`, `abrevegrave.alt`. glyphsLib
# names kerning classes and glyph variants in its messages without quoting
# them, so without this one sentence about seventeen missing classes stayed
# seventeen rows. The trailing "." of a sentence is not swallowed: the pattern
# requires a word character after every dot.
_DOTTED = re.compile(r"\b\w+(?:\.\w+)+\b")


@dataclass
class WarningGroup:
    """Warnings that say the same thing about different glyphs."""

    category: str
    severity: Any
    template: str
    count: int = 0
    examples: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)

    @property
    def is_repair(self) -> bool:
        """Whether this is something the import fixed, rather than noticed."""
        return self.category == "preflight"


def message_template(message: str) -> str:
    """The shape of a message, with the parts that vary taken out."""
    shape = _QUOTED.sub("'...'", message)
    shape = _DOTTED.sub("'...'", shape)
    return _NUMBERS.sub("N", shape).strip()


def summarize_warnings(warnings: list) -> list[WarningGroup]:
    """Group warnings by what they say, repairs first.

    Args:
        warnings: `ConversionWarning` objects from a conversion.

    Returns:
        One group per distinct message shape, ordered with the import's own
        repairs first and then by how many there are. Each group keeps up to
        `MAX_EXAMPLES` original messages, so the reader can see the glyphs.
    """
    groups: "OrderedDict[tuple, WarningGroup]" = OrderedDict()

    for warning in warnings:
        template = message_template(warning.message)
        key = (warning.category, getattr(warning.severity, "value", warning.severity), template)
        group = groups.get(key)
        if group is None:
            group = WarningGroup(
                category=warning.category,
                severity=warning.severity,
                template=template,
            )
            groups[key] = group
        group.count += 1
        if len(group.examples) < MAX_EXAMPLES:
            group.examples.append(warning.message)
            if warning.details:
                group.details.append(warning.details)

    return sorted(groups.values(), key=lambda g: (not g.is_repair, -g.count, g.template))


def summary_line(groups: list[WarningGroup]) -> str:
    """One line for the log, in place of every message.

    Example: `19626 warnings: 245 renamed a second layer called '...', ...`
    """
    if not groups:
        return "no warnings"
    total = sum(group.count for group in groups)
    head = ", ".join(f"{group.count} {group.template[:60]}" for group in groups[:3])
    more = len(groups) - min(3, len(groups))
    return f"{total} warnings in {len(groups)} kind(s): {head}" + (
        f", +{more} more" if more else ""
    )


__all__ = ["WarningGroup", "summarize_warnings", "summary_line", "message_template", "MAX_EXAMPLES"]
