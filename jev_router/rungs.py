"""Deterministic middle rungs of the visibility ladder; no model call, exact text stays recoverable.

short = an outline (signatures / per-file match counts / error lines).
long  = the chunk minus comments, docstring bodies, blank lines, and runs of passing-test lines.
Each returns (text, elided); elided=False means the rung equals the original.
"""
import re

from .chunking import _LINENO, is_grep

_SIGNATURE = re.compile(r"^\s*(?:@|async\s+def\s|def\s|class\s)")
_DEF = re.compile(r"^\s*(?:async\s+def|def|class)\s")
_DOC = re.compile(r"""^\s*[rRbBuU]?(\"\"\"|''')""")
_COMMENT = re.compile(r"^\s*#")
_GREP = re.compile(r"^([^\s:][^:]*)[:-](\d+)[:-]")
_SIGNAL = re.compile(r"error|fail|exception|traceback|assert|warning|^\s*File \"", re.I)
_PASSING = re.compile(r"(?:\.\.\.\s*ok|\bPASSED\b|^ok)\s*$")
MAX_OUTLINE = 30


def log_key(text):
    """Identity of a traceback block apart from its test name, line numbers, and literal values."""
    lines = text.splitlines()
    head = next((i for i, ln in enumerate(lines) if ln.startswith(("FAIL: ", "ERROR: ")) or ln.startswith("_____")), None)
    if head is None:
        return None
    body = "\n".join(ln for ln in lines[head + 1:] if not set(ln.strip()) <= {"-", "=", "_"})
    body = re.sub(r"'[^'\n]*'|\"[^\"\n]*\"", "S", body)
    return re.sub(r"\d+", "N", body).strip() or None


def duplicate(text, first, ref):
    """One line: the repeated traceback's header, its first occurrence, and the exact-text reference."""
    lines = text.splitlines(keepends=True)
    head = next((ln for ln in lines if ln.startswith(("FAIL: ", "ERROR: ")) or ln.startswith("_____")), lines[0])
    return head.rstrip("\r\n") + f"  [Jev: same traceback as {first}; jev_expand hidden_ref={ref}]\n"


def kind_of(tool_name, source, body):
    if tool_name.lower() == "read" and source.endswith(".py"):
        return "python"
    return "grep" if is_grep(body) else "text"


def render(level, text, kind):
    if level == "hide":
        return "", bool(text)
    if level not in {"short", "long"}:
        return text, False
    lines = text.splitlines(keepends=True)
    fn = {"python": (_py_short, _py_long), "grep": (_grep_short, _grep_long), "text": (_text_short, _text_long)}[kind]
    out = fn[0 if level == "short" else 1](lines)
    if out == lines or not out:
        return text, False
    shown = "".join(out)
    return (shown if shown.endswith(("\n", "\r")) else shown + "\n"), True


def _code(line):
    return _LINENO.sub("", line, count=1)


def _py_short(lines):
    first = next((k for k, ln in enumerate(lines) if _SIGNATURE.match(_code(ln))), None)
    if first is None:
        body = [ln for ln in lines if ln.strip()]
        return body[:1] + [ln for ln in body[1:] if _code(ln).startswith(("import ", "from "))][:MAX_OUTLINE]
    out, k = [], first
    while k < len(lines) and _code(lines[k]).lstrip().startswith("@"):
        out.append(lines[k]); k += 1
    while k < len(lines):
        out.append(lines[k]); k += 1
        if _code(out[-1]).split("#", 1)[0].rstrip().endswith(":"):
            break
    j = k
    while j < len(lines) and not _code(lines[j]).strip():
        j += 1
    if j < len(lines) and _DOC.match(_code(lines[j])):
        out.append(lines[j]); k = j + 1
    out.extend([ln for ln in lines[k:] if _DEF.match(_code(ln))][:max(0, MAX_OUTLINE - len(out))])
    return out


def _py_long(lines):
    out, in_doc, prev = [], None, ""
    for line in lines:
        code = _code(line)
        stripped = code.strip()
        if in_doc:
            if in_doc in stripped:
                in_doc = None
            continue
        if not stripped or _COMMENT.match(code):
            continue
        m = _DOC.match(code)
        if m and (prev.endswith(":") or not prev):
            out.append(line)
            rest = stripped[len(m.group(0).strip()):]
            if m.group(1) not in rest:
                in_doc = m.group(1)
            continue
        out.append(line)
        prev = code.split("#", 1)[0].rstrip()
    return out


def _grep_groups(lines):
    groups = {}
    for line in lines:
        m = _GREP.match(line)
        groups.setdefault(m.group(1) if m else "", []).append((m.group(2) if m else None, line))
    return groups


def _grep_short(lines):
    out = []
    for path, hits in _grep_groups(lines).items():
        nums = [n for n, _ in hits if n]
        head = ", ".join(nums[:10]) + (", …" if len(nums) > 10 else "")
        out.append(f"{path or '(output)'}: {len(hits)} lines" + (f" (lines {head})" if nums else "") + "\n")
    return out


def _grep_long(lines, keep=5):
    out = []
    for path, hits in _grep_groups(lines).items():
        out.extend(line for _, line in hits[:keep])
        if len(hits) > keep:
            out.append(f"… (+{len(hits) - keep} more lines from {path or 'output'})\n")
    return out


def _text_short(lines):
    body = [ln for ln in lines if ln.strip()]
    if not body:
        return []
    signal = [ln for ln in body[1:-1] if _SIGNAL.search(ln)][:8]
    return [body[0], *signal, *([body[-1]] if len(body) > 1 else [])]


def _text_long(lines):
    out, run, last = [], [], None
    def flush():
        out.extend(run) if len(run) < 3 else out.append(f"… ({len(run)} passing/repeated lines elided)\n")
        run.clear()
    for line in lines:
        if not line.strip():
            continue
        if _PASSING.search(line.rstrip()) or line == last:
            run.append(line)
        else:
            flush()
            out.append(line)
        last = line
    flush()
    return out
