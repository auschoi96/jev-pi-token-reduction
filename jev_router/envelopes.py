"""Lossless host envelope parsing. Only source lines become scoreable chunks."""
import re

_OPEN = re.compile(r"\A(?P<prefix>(?:<path>[^\r\n]*</path>\r?\n<type>file</type>\r?\n)?<(?P<tag>content|file)>\r?\n)")
_LINE = re.compile(r"^(\d+): ")


def opencode_read(text):
    """Return prefix, numbered body, suffix, first source line; reject unknown shapes.

    The wrapper, pagination footer, and reminders are kept verbatim. Contiguous
    source numbering is checked rather than inferred from positions in the wrapper.
    """
    match = _OPEN.match(text)
    if not match:
        raise ValueError("Unsupported OpenCode read envelope")
    lines = text[match.end():].splitlines(keepends=True)
    body, first = [], None
    for line in lines:
        numbered = _LINE.match(line)
        if not numbered:
            break
        number = int(numbered[1])
        if first is None:
            first = number
        if first < 1 or number != first + len(body):
            raise ValueError("Noncontiguous OpenCode read lines")
        body.append(line)
    suffix = "".join(lines[len(body):])
    close = re.search(r"(?m)^</" + match["tag"] + r">(?:\r?$)", suffix)
    if not body or close is None or re.search(r"(?m)^\d+: ", suffix[:close.start()]):
        raise ValueError("Incomplete OpenCode read envelope")
    return match["prefix"], "".join(body), suffix, first
