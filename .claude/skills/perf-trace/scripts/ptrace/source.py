"""Map trace node names to definitions in a source tree (for to-test, snapshot, touched)."""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "site-packages", "__pycache__", ".tox", "build", "dist", ".mypy_cache"}


def py_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            if f.endswith(".py"):
                yield Path(dirpath) / f


@dataclass
class Defn:
    module: str
    qualname: str  # "func", "Class.method", "outer.<locals>.inner"
    file: str
    line: int
    end: int
    kind: str  # "function" | "class"
    is_async: bool = False
    binding: str = ""  # "classmethod" | "staticmethod" | "" (decorator on a method)

    @property
    def importable(self) -> bool:
        return "<locals>" not in self.qualname

    @property
    def top(self) -> str:
        return self.qualname.split(".")[0]


def module_name(root: Path, f: Path) -> str:
    rel = f.resolve().relative_to(root.resolve()).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if parts and parts[0] == "src" and len(parts) > 1:  # src layout: src/pkg/mod.py imports as pkg.mod
        parts = parts[1:]
    return ".".join(parts)


class Index:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.defs: list[Defn] = []
        self.by_qual: dict[str, list[Defn]] = {}
        self.files: dict[str, list[Defn]] = {}
        self._props: set = set()
        for f in py_files(self.root):
            try:
                tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
            except (SyntaxError, OSError, ValueError):
                continue
            mod = module_name(self.root, f)
            self._visit(tree.body, mod, str(f), "")
        for d in self.defs:
            self.by_qual.setdefault(d.qualname, []).append(d)
            self.files.setdefault(d.file, []).append(d)

    def _visit(self, body, mod, file, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = prefix + node.name
                deco = {d.id if isinstance(d, ast.Name) else d.attr for d in node.decorator_list
                        if isinstance(d, (ast.Name, ast.Attribute))}  # functools.cached_property, x.setter
                binding = next((b for b in ("classmethod", "staticmethod") if b in deco), "")
                if deco & {"property", "cached_property", "setter", "deleter"}:
                    binding = "property"
                elif deco & {"contextmanager", "asynccontextmanager"}:
                    binding = "contextmanager"
                q_def = Defn(mod, q, file, node.lineno, node.end_lineno or node.lineno, "function",
                             isinstance(node, ast.AsyncFunctionDef), binding)
                if binding == "property" and (file, q) in self._props:
                    continue  # the setter/deleter of a property already indexed by its getter
                if binding == "property":
                    self._props.add((file, q))
                self.defs.append(q_def)
                self._visit(node.body, mod, file, q + ".<locals>.")
            elif isinstance(node, ast.ClassDef):
                q = prefix + node.name
                self.defs.append(Defn(mod, q, file, node.lineno, node.end_lineno or node.lineno, "class"))
                self._visit(node.body, mod, file, q + ".")
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.AsyncWith)):  # defs under `if TYPE_CHECKING`, try/except imports
                for part in ("body", "orelse", "finalbody"):
                    self._visit(getattr(node, part, []) or [], mod, file, prefix)
                for h in getattr(node, "handlers", []) or []:
                    self._visit(h.body, mod, file, prefix)

    def candidates(self, name: str) -> list[Defn]:
        """Every definition a trace node name could refer to."""
        name = re.sub(r" ×\d+$", "", name)
        if name.endswith((".setter", ".deleter")):
            name = name.rsplit(".", 1)[0]
        candidates: list[Defn] = []
        parts = name.split(".")
        for i in range(len(parts) - 1, -1, -1):  # longest module prefix first
            mod, qual = ".".join(parts[:i]), ".".join(parts[i:])
            for d in self.by_qual.get(qual, []):
                if mod in ("", "__main__") or d.module == mod or d.module.endswith("." + mod):
                    candidates.append(d)
            if candidates:
                break
        return candidates

    def lookup(self, name: str, file_hint: str | None = None, prefer: list[str] | tuple = ()) -> Defn | None:
        """Definition for a trace node name: 'mod.func', 'Class.method', '__main__.func', 'mod.outer.<locals>.inner'."""
        candidates = self.candidates(name)
        for f in ([file_hint] if file_hint else []) + list(prefer):
            hinted = [d for d in candidates if Path(d.file).resolve() == Path(f).resolve()]
            if hinted:
                return hinted[0]
        return candidates[0] if len(candidates) >= 1 else None

    def files_by_names(self, names) -> list[str]:
        """Files ranked by how many of these node names they could define, most first: where a
        `__main__.func` or a module-less `Class.method` that several files define most likely
        lives is the file that also defines the trace's other functions."""
        score: dict[str, int] = {}
        for nm in set(names):
            for f in {d.file for d in self.candidates(nm)}:
                score[f] = score.get(f, 0) + 1
        return sorted(score, key=lambda f: -score[f])

    def lookup_class(self, cls: str, near: Defn | None = None) -> Defn | None:
        found = [d for d in self.by_qual.get(cls, []) if d.kind == "class"]
        if near:
            same = [d for d in found if d.file == near.file]
            if same:
                return same[0]
        return found[0] if found else None


def trace_files(tr, index: Index | None = None) -> list[str]:
    """Source files the trace itself names (TRACE_LOCALS sources, exception origins), most mentioned first:
    the best guess for where a `__main__.func` or `Class.method` without a module lives. With an index,
    then the files under it that define the most of the trace's function names."""
    from collections import Counter
    c = Counter()
    for n in tr.nodes:
        f = tr.source_file(n)
        if f:
            c[f] += 1
        o = n.get("exception_origin") or {}
        if o.get("file"):
            c[o["file"]] += 1
    named = [f for f, _ in c.most_common()]
    if index is None:
        return named
    return named + [f for f in index.files_by_names(n["name"] for n in tr.nodes) if f not in named]
