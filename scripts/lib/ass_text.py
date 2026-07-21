from __future__ import annotations

import unicodedata
from collections.abc import Iterable


ASS_LINE_BREAK = r"\N"


def escape_ass_fragment(text: str) -> str:
    """Return user text that cannot be interpreted as ASS syntax.

    Backslashes and braces have active meaning to libass.  Full-width glyphs
    preserve their visible intent without relying on renderer-specific escape
    behavior.  User-supplied control and line-separator characters become
    ordinary spaces; callers may add line breaks only through
    :func:`join_ass_lines`.
    """
    if type(text) is not str:
        raise TypeError("ASS text fragments must be strings")

    escaped: list[str] = []
    for character in text:
        if character == "\\":
            escaped.append("＼")
        elif character == "{":
            escaped.append("｛")
        elif character == "}":
            escaped.append("｝")
        elif unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            escaped.append(" ")
        else:
            escaped.append(character)
    return "".join(escaped)


def join_ass_lines(lines: Iterable[str]) -> str:
    """Escape user-owned line fragments and add pipeline-owned ASS breaks."""
    fragments = list(lines)
    if not fragments:
        raise ValueError("At least one ASS text line is required")
    return ASS_LINE_BREAK.join(escape_ass_fragment(fragment) for fragment in fragments)
