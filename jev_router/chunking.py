"""Lossless chunking extracted from the original context_router prototype."""
import re
from pathlib import Path

LEVELS = {
    "full": "This chunk directly answers the current query or contains the exact fact/code it needs; show it verbatim.",
    "long": "This chunk is clearly relevant to the query; keep it in full, dropping only unrelated boilerplate.",
    "short": "This chunk is only tangentially related; a one-line gist is all the query needs.",
    "hide": "This chunk is irrelevant to the current query; do not show it at all.",
}
_SHOWN = {"full", "long"}  # levels that retain a chunk's full content

# A top-level def/class/decorator, or a module-level CONSTANT assignment, starts a new chunk.
_BOUNDARY = re.compile(r"^(def |async def |class |@|[A-Z_][A-Z0-9_]*\s*=)")
_METHOD = re.compile(r"^\s{4}(async )?def ")
# Claude Code's Read tool prefixes every line with its line number and a tab.
_LINENO = re.compile(r"^\s*\d+(?:\t|│|: )")
_MAX_CHUNK_LINES = 150


def _split_long(cid, lines, key):
    """A class body can run hundreds of lines; split it at method boundaries so Jev can keep one
    method without keeping the whole class."""
    if len(lines) <= _MAX_CHUNK_LINES:
        return [(cid, "".join(lines))]
    out, cur, n = [], [], 0
    for line in lines:
        if cur and len(cur) >= 20 and _METHOD.match(key(line)):
            out.append((f"{cid}#{n}", "".join(cur))); cur, n = [], n + 1
        cur.append(line)
    out.append((f"{cid}#{n}" if n else cid, "".join(cur)))
    return out


def chunk_python_text(text, *, prefix=None):
    """Split python source into labeled chunks: 'header' (docstring/imports up to the first
    top-level definition) then one chunk per top-level def/class/constant, long classes split at
    methods. Accepts Read-tool output: line-number prefixes are ignored for boundary detection and
    kept in the chunk text. With `prefix`, ids are namespaced 'prefix:name'.
    Returns an ordered list of (chunk_id, text)."""
    lines = text.splitlines(keepends=True)
    key = (lambda ln: _LINENO.sub("", ln, count=1)) if lines and _LINENO.match(lines[0]) else (lambda ln: ln)
    tag = (prefix + ":") if prefix else ""
    chunks, cur_id, cur = [], tag + "header", []
    for line in lines:
        src = key(line)
        if _BOUNDARY.match(src):
            if cur:
                chunks.extend(_split_long(cur_id, cur, key))
            m = re.match(r"^(?:async\s+)?(?:def|class)\s+(\w+)", src) or re.match(r"^([A-Z_][A-Z0-9_]*)\s*=", src)
            cur_id = tag + (m.group(1) if m else src.strip()[:40])
            cur = [line]
        else:
            cur.append(line)
    if cur:
        chunks.extend(_split_long(cur_id, cur, key))
    return chunks


def chunk_python(path, *, prefix=None):
    """chunk_python_text over a file on disk."""
    return chunk_python_text(Path(path).read_text(), prefix=prefix)


def chunk_corpus(paths):
    """Chunk several files into one corpus, ids namespaced by file stem (Fig 4's multi-file
    retrieval / big-grep scenario)."""
    out = []
    for p in paths:
        out.extend(chunk_python(p, prefix=Path(p).stem))
    return out


def chunk_generic(text, *, prefix="", window=30):
    """Fixed line-window chunks for non-python retrieval (command output, yaml/json/md, greps).
    Returns [(id, text)]."""
    lines = text.splitlines(keepends=True)
    tag = (prefix + ":") if prefix else ""
    out = []
    for i in range(0, len(lines), window):
        out.append((f"{tag}block{i // window}", "".join(lines[i:i + window])))
    return out or [(tag + "block0", text)]


_GREP_LINE = re.compile(r"^([^\s:][^:]*):\d+[:-]")


def is_grep(text):
    lines = text.splitlines()
    return bool(lines) and sum(bool(_GREP_LINE.match(ln)) for ln in lines) >= len(lines) // 2


def chunk_grep(text, *, window=30):
    """Group grep/ripgrep `path:line:match` output by file, so Jev keeps or drops whole files'
    hits; runs longer than `window` lines are split. Non-grep-shaped text falls back to windows."""
    lines = text.splitlines(keepends=True)
    if not is_grep(text):
        return chunk_generic(text, window=window)
    out, cur, cur_file = [], [], None
    for line in lines:
        m = _GREP_LINE.match(line)
        f = m.group(1) if m else cur_file
        if cur and (f != cur_file or len(cur) >= window):
            out.append((f"{Path(cur_file or 'grep').name}#{len(out)}", "".join(cur))); cur = []
        cur_file = f
        cur.append(line)
    if cur:
        out.append((f"{Path(cur_file or 'grep').name}#{len(out)}", "".join(cur)))
    return out


# unittest "=====\nFAIL: name" and pytest "____ name ____" failure separators.
_FAILURE = re.compile(r"(?m)^(?:={20,}\n(?=(?:FAIL|ERROR): )|_{5,} .+ _{5,}$|-{20,}\n(?=Ran \d+ tests? in ))")


def chunk_log(text, *, window=30, max_block=150):
    """Split test/build logs at failure separators so each traceback is one chunk; else line windows."""
    starts = [m.start() for m in _FAILURE.finditer(text)]
    if len(starts) < 2:
        return chunk_generic(text, window=window)
    out, bounds = [], [0, *starts, len(text)]
    for i, (a, b) in enumerate(zip(bounds, bounds[1:])):
        block = text[a:b]
        if not block:
            continue
        lines = block.splitlines(keepends=True)
        for j in range(0, len(lines), max_block):
            out.append((f"log{i}" + (f"#{j // max_block}" if len(lines) > max_block else ""), "".join(lines[j:j + max_block])))
    return out


def chunk_any(text, path=""):
    """Chunk retrieved text by kind: python -> by def/class/const; else fixed line windows."""
    if path.endswith(".py"):
        return chunk_python_text(text)
    stem = Path(path).stem if path else ""
    return chunk_generic(text, prefix=stem)


def _gist(text):
    body = [ln for ln in text.splitlines() if ln.strip()]
    if not body:
        return ""
    head = body[0].rstrip()
    hidden = len(text.splitlines()) - 1
    return f"{head}\n    # … ({hidden} lines elided)\n" if hidden > 0 else head + "\n"

