"""Minimal, fast S-expression reader/writer for KiCad files.

Representation: a list is a Python list; atoms are `Sym` (bare symbols) or `str`
(quoted strings) or int/float.  This keeps round-tripping of KiCad files lossless
enough for our purposes (we never need comments).
"""
from __future__ import annotations

import re
from typing import Any, Iterator


class Sym(str):
    """Bare (unquoted) symbol."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover
        return f"Sym({str.__repr__(self)})"


_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.S)
_NUM = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def _atom(tok: str) -> Any:
    if _NUM.match(tok):
        try:
            return int(tok) if re.fullmatch(r"[+-]?\d+", tok) else float(tok)
        except ValueError:
            pass
    return Sym(tok)


def _unescape(s: str) -> str:
    if "\\" not in s:
        return s
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


def loads(text: str) -> list:
    stack: list[list] = [[]]
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOKEN.match(text, pos)
        if not m:
            if text[pos:].strip() == "":
                break
            raise ValueError(f"bad sexpr near {text[pos:pos+40]!r}")
        pos = m.end()
        if m.group(1):
            stack.append([])
        elif m.group(2):
            done = stack.pop()
            stack[-1].append(done)
        elif m.group(3) is not None:
            stack[-1].append(_unescape(m.group(3)))
        elif m.group(4):
            stack[-1].append(_atom(m.group(4)))
    if len(stack) != 1:
        raise ValueError("unbalanced parentheses")
    root = stack[0]
    return root[0] if len(root) == 1 else root


def load(path) -> list:
    with open(path, encoding="utf-8") as f:
        return loads(f.read())


def _fmt_atom(a: Any) -> str:
    if isinstance(a, Sym):
        return str(a)
    if isinstance(a, bool):
        return "yes" if a else "no"
    if isinstance(a, int):
        return str(a)
    if isinstance(a, float):
        s = f"{a:.6f}".rstrip("0").rstrip(".")
        return "0" if s in ("-0", "") else s
    s = str(a).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


# lists whose children should be kept on one line (short leaf-ish nodes)
def dumps(node: Any, indent: int = 0) -> str:
    if not isinstance(node, list):
        return _fmt_atom(node)
    if not any(isinstance(c, list) for c in node):
        return "(" + " ".join(_fmt_atom(c) for c in node) + ")"
    pad = "\t" * (indent + 1)
    parts = []
    head = []
    i = 0
    while i < len(node) and not isinstance(node[i], list):
        head.append(_fmt_atom(node[i]))
        i += 1
    out = "(" + " ".join(head)
    for c in node[i:]:
        if isinstance(c, list):
            parts.append("\n" + pad + dumps(c, indent + 1))
        else:
            parts.append(" " + _fmt_atom(c))
    return out + "".join(parts) + "\n" + "\t" * indent + ")"


def dump(node: Any, path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(dumps(node) + "\n")


# ---------------------------------------------------------------- helpers
def S(name: str, *args) -> list:
    """Build a node: S('at', 1, 2) -> (at 1 2)."""
    return [Sym(name), *args]


def head(node) -> str | None:
    return str(node[0]) if isinstance(node, list) and node and isinstance(node[0], str) else None


def find(node: list, name: str):
    for c in node:
        if isinstance(c, list) and c and c[0] == name:
            return c
    return None


def find_all(node: list, name: str) -> Iterator[list]:
    for c in node:
        if isinstance(c, list) and c and c[0] == name:
            yield c


def walk(node) -> Iterator[list]:
    if isinstance(node, list):
        yield node
        for c in node:
            if isinstance(c, list):
                yield from walk(c)


def prop(node: list, key: str):
    """Value of (property "key" "value" ...) child."""
    for c in find_all(node, "property"):
        if len(c) > 2 and c[1] == key:
            return c[2]
    return None


def remove(node: list, name: str) -> None:
    node[:] = [c for c in node if not (isinstance(c, list) and c and c[0] == name)]
