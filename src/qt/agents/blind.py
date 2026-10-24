"""Blinding for the agent test set (runtime plan 8.3): the Playbook pack and the card text without the course's
worked examples, so an agent decides a test case from the rules and not from the answer printed next to them.

Same method as the spike's blind Playbook v3 (spike/BLINDING-v3.md): the "examples from the course" sections and the
cards' "how it would work on ..." paragraphs go; in every other line only the sentence or the parenthesis that names
a case company goes, and the rule itself stays.
"""
from __future__ import annotations

import re

# The companies of the test cases, in English and in Hebrew, and the words that point at them.
TERMS = ("BA", "Boeing", "בואינג", "BLDR", "Builders FirstSource", "AXON", "Axon", "אקסון", "BBY", "Best Buy",
         "בסט ביי", "ERJ", "Embraer", "AVAV", "AeroVironment", "TDG", "TransDigm", "ATRO", "Astronics",
         "Spirit AeroSystems", "737", "A&D", "חלל והגנה", "אירוספייס")

_LATIN = [t for t in TERMS if re.fullmatch(r"[A-Za-z0-9&\- ]+", t)]
_OTHER = [t for t in TERMS if t not in _LATIN]
TERM_RE = re.compile("|".join([rf"(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])" for t in _LATIN]
                              + [re.escape(t) for t in _OTHER]))
EXAMPLE_HEADING = re.compile(r"^(#{2,4}) .*(דוגמאות מהקורס|Examples from the course)")
WORKED = re.compile(r"^\*\*(איך זה היה עובד|ולמה לא)")
PAREN = re.compile(r"\([^()]*\)")
SENTENCE = re.compile(r"(?<=[.!?;])\s+")


def _clean_line(line: str) -> str:
    line = PAREN.sub(lambda m: "" if TERM_RE.search(m.group(0)) else m.group(0), line)
    if not TERM_RE.search(line):
        return line
    kept = [s for s in SENTENCE.split(line) if not TERM_RE.search(s)]
    out = " ".join(kept).rstrip()
    return "" if re.fullmatch(r"[\s\-*\d.|#>]*", out) else out


def blind(text: str) -> tuple[str, int]:
    """(the text without the examples and the case names, how many lines were cut or changed)."""
    out, changed, skip_level, skip_para = [], 0, None, False
    for line in text.splitlines():
        h = re.match(r"^(#{1,6}) ", line)
        if skip_level is not None:
            if h and len(h.group(1)) <= skip_level:
                skip_level = None
            else:
                changed += 1
                continue
        m = EXAMPLE_HEADING.match(line)
        if m:
            skip_level, changed = len(m.group(1)), changed + 1
            continue
        if skip_para:
            if line.strip():
                changed += 1
                continue
            skip_para = False
        if WORKED.match(line):
            skip_para, changed = True, changed + 1
            continue
        new = _clean_line(line)
        if new != line:
            changed += 1
            if not new:
                continue
        out.append(new)
    return "\n".join(out), changed


def leaks(text: str) -> list[str]:
    """The case names still in a text (a blinded pack should have none)."""
    return sorted({m.group(0) for m in TERM_RE.finditer(text)})
