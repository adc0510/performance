"""Tests for the perf-trace skill's `replay` command.

Each run builds a small throwaway project, records real traces of it with perf,
then drives `perf_trace.py replay` exactly as the skill does and checks what it
prints and returns. Standard library only:

    PYTHONPATH=src python -m unittest tests.test_skill_replay -v

perf must be importable by the running Python (PYTHONPATH=src from the repo root).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / ".claude" / "skills" / "perf-trace" / "scripts" / "perf_trace.py"
SRC = REPO / "src"

FILES = {
    "shop/__init__.py": "",
    "shop/db.py": '''
        import os

        # Replay fakes these: if one runs for real under a replay, it raises. (Inline,
        # not a helper function: a traced helper would make these non-leaves.)

        def fetch_price(sku):
            if os.environ.get("REPLAY_FORBID_DB"):
                raise RuntimeError("real fetch_price called during replay")
            return {"A": 10.0, "B": 2.5, "C": 99.0}[sku]

        def fetch_stock(sku):
            if os.environ.get("REPLAY_FORBID_DB"):
                raise RuntimeError("real fetch_stock called during replay")
            return {"A": 5, "B": 0, "C": 1}[sku]

        async def fetch_rate(cur):
            if os.environ.get("REPLAY_FORBID_DB"):
                raise RuntimeError("real fetch_rate called during replay")
            return {"EUR": 0.9, "USD": 1.0}[cur]
    ''',
    "shop/models.py": '''
        from dataclasses import dataclass, field
        from typing import Optional

        @dataclass
        class Line:
            sku: str
            qty: int

        @dataclass
        class Order:
            id: int
            lines: list = field(default_factory=list)
            coupon: Optional[str] = None

        class OutOfStock(Exception):
            pass
    ''',
    "shop/pricing.py": '''
        import time
        from .db import fetch_price, fetch_stock, fetch_rate
        from .models import Order, OutOfStock

        class Pricer:
            def __init__(self, tax):
                self.tax = tax

            def line_total(self, line):
                if fetch_stock(line.sku) < line.qty:
                    raise OutOfStock(f"{line.sku}: wanted {line.qty}")
                return fetch_price(line.sku) * line.qty

            def total(self, order):
                subtotal = sum(self.line_total(l) for l in order.lines)
                if order.coupon == "HALF":
                    subtotal = subtotal / 3  # BUG: should be / 2
                return round(subtotal * (1 + self.tax), 2)

            @classmethod
            def default(cls, tax=0.2):
                return cls(tax).tax

            @property
            def rate(self):
                return self.tax * 100

        async def convert(amount, cur):
            return round(amount * await fetch_rate(cur), 2)

        def depth(n):
            return 0 if n == 0 else 1 + depth(n - 1)

        def options(name, *, upper=False, sep="-"):
            out = sep.join(name.split())
            return out.upper() if upper else out

        def handle(orders, tax=0.1):
            p = Pricer(tax)
            out = []
            for o in orders:
                try:
                    out.append(p.total(o))
                except OutOfStock as e:
                    out.append(str(e))
            return out

        def label(conn, order_id):
            return f"order-{order_id}"

        def store(conn, order_id):
            conn.execute("select ?", (order_id,))
            return order_id

        def count(items):
            return len(items)

        def slow(x):
            time.sleep(1)
            return x

        def numbers(n):
            yield from range(n)

        def save(writer, value):
            writer.write(value)
            return value
    ''',
    # A driver script: runs the program when imported. Replay must never import it.
    "drive.py": '''
        import asyncio, sqlite3, sys
        import perf
        from shop import pricing
        from shop.models import Order, Line

        with open("drive_runs.txt", "a") as f:
            f.write("ran\\n")

        class Writer:  # defined in this script
            def write(self, v):
                return None

        perf.OUTPUT_DIR = "traces"
        perf.PRINT_REPORT = False

        def run(name, fn, *args, **kwargs):
            perf.OUTPUT_FILE = name
            return perf.trace(fn)(*args, **kwargs)

        run("handle", pricing.handle, [Order(1, [Line("A", 2), Line("C", 1)], coupon="HALF"),
                                       Order(2, [Line("B", 1)]), Order(3, [Line("A", 1)])])
        perf.OUTPUT_FILE = "convert"
        asyncio.run(perf.trace(pricing.convert)(12.5, "EUR"))
        run("depth", pricing.depth, 4)
        run("options", pricing.options, "a b c", upper=True, sep="_")
        run("classmethod", pricing.Pricer.default, 0.3)
        run("classmethod_nested", lambda: pricing.Pricer.default(0.4))
        p = pricing.Pricer(0.25)
        run("property", lambda: p.rate)
        conn = sqlite3.connect(":memory:")
        run("label", pricing.label, conn, 7)
        run("count", pricing.count, [conn, 1, "x"])
        run("save", pricing.save, Writer(), 5)
        run("numbers", lambda: list(pricing.numbers(3)))
        try:
            perf.OUTPUT_FILE = "slow"
            perf.trace(pricing.slow, TIMEOUT=0.05)(1)
        except Exception:
            pass
        perf.OUTPUT_FILE = "nocapture"
        perf.trace(pricing.depth, CAPTURE_IO=False)(2)
    ''',
    "store_drive.py": '''
        import perf
        from shop import pricing

        perf.OUTPUT_DIR = "traces"
        perf.PRINT_REPORT = False
        perf.OUTPUT_FILE = "store"
        import sqlite3
        perf.trace(pricing.store)(sqlite3.connect(":memory:"), 9)
    ''',
}


def _env(**extra) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(SRC), env.get("PYTHONPATH", "")) if p)
    env.pop("PYTHONSAFEPATH", None)
    env.update(extra)
    return env


def _write_project(root: Path) -> None:
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")


def _nodes(trace_path: Path) -> list:
    """Preorder list of a trace's nodes: list index == the skill's node id."""
    out = []

    def walk(n):
        out.append(n)
        for c in n.get("children", []):
            walk(c)

    walk(json.loads(trace_path.read_text())["trace"])
    return out


class ReplayTests(unittest.TestCase):
    root: Path

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="perf_replay_test_")
        cls.root = Path(cls.tmp) / "proj"
        _write_project(cls.root)
        for script in ("drive.py", "store_drive.py"):
            proc = subprocess.run([sys.executable, script], cwd=cls.root, env=_env(), capture_output=True, text=True)
            if proc.returncode:
                raise RuntimeError(f"{script} failed:\n{proc.stdout}\n{proc.stderr}")
        cls.traces = cls.root / "traces"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ── helpers ──────────────────────────────────────────────────────────

    def node_id(self, trace: str, name: str, nth: int = 0, **fields) -> int:
        matches = [i for i, n in enumerate(_nodes(self.traces / f"{trace}.json"))
                   if n["name"].endswith(name) and all(n.get(k) == v for k, v in fields.items())]
        self.assertGreater(len(matches), nth, f"no node {name!r} #{nth} in {trace}")
        return matches[nth]

    def replay(self, trace: str, node, *args, root: Path | None = None, stdin: str | None = None,
               forbid_db: bool = True, python: str | None = sys.executable) -> subprocess.CompletedProcess:
        root = root or self.root
        cmd = [sys.executable, str(SKILL), "replay", str(self.traces / f"{trace}.json"), str(node), "--root", str(root)]
        if python:
            cmd += ["--python", python]
        env = _env(**({"REPLAY_FORBID_DB": "1"} if forbid_db else {}))
        proc = subprocess.run(cmd + list(args), cwd=root, env=env, capture_output=True, text=True, input=stdin, timeout=60)
        proc.out = proc.stdout + proc.stderr  # type: ignore[attr-defined]
        return proc

    def copy_project(self) -> Path:
        dst = Path(tempfile.mkdtemp(prefix="perf_replay_copy_", dir=self.tmp)) / "proj"
        shutil.copytree(self.root, dst, ignore=shutil.ignore_patterns("traces", "__pycache__"))
        return dst

    def edit(self, root: Path, rel: str, old: str, new: str) -> None:
        path = root / rel
        text = path.read_text()
        self.assertIn(old, text)
        path.write_text(text.replace(old, new))

    def assertSame(self, proc):
        self.assertEqual(proc.returncode, 0, proc.out)
        self.assertIn("SAME as recorded", proc.out)
        self.assertNotIn("DIVERGED", proc.out)

    # ── replays that must reproduce the recording ────────────────────────

    def test_method_with_rebuilt_self_and_dataclass_args(self):
        p = self.replay("handle", self.node_id("handle", "Pricer.total"), forbid_db=False)
        self.assertSame(p)
        self.assertIn("returned 43.63", p.out)

    def test_mock_children_isolates_the_function(self):
        p = self.replay("handle", self.node_id("handle", "Pricer.total"), "--mock", "children")
        self.assertSame(p)
        self.assertIn("faked Pricer.line_total: called 2x", p.out)

    def test_leaf_fakes_reach_from_import_copies(self):
        # pricing did `from .db import fetch_price`: patching shop.db alone would miss
        # it, and REPLAY_FORBID_DB makes the real one raise.
        p = self.replay("handle", self.node_id("handle", "Pricer.line_total"))
        self.assertSame(p)
        self.assertIn("faked fetch_stock: called 1x", p.out)
        self.assertIn("faked fetch_price: called 1x", p.out)

    def test_exception_reproduced(self):
        p = self.replay("handle", self.node_id("handle", "Pricer.line_total", 2))
        self.assertSame(p)
        self.assertIn("raised   OutOfStock: B: wanted 1", p.out)

    def test_exception_propagating_through_caller(self):
        # default --mock leaves: total's callee line_total isn't a leaf, so it (and the
        # DB below it) runs for real and raises OutOfStock up through total
        p = self.replay("handle", self.node_id("handle", "Pricer.total", 1), forbid_db=False)
        self.assertSame(p)
        self.assertIn("raised   OutOfStock: B: wanted 1", p.out)

    def test_async_function(self):
        p = self.replay("convert", 0)
        self.assertSame(p)
        self.assertIn("faked fetch_rate: called 1x", p.out)

    def test_recursion_never_fakes_the_function_itself(self):
        p = self.replay("depth", 0, "--mock", "children")
        self.assertSame(p)
        self.assertIn("returned 4", p.out)
        self.assertNotIn("faked depth", p.out)

    def test_whole_request_for_real(self):
        p = self.replay("handle", 0, "--mock", "none", forbid_db=False)
        self.assertSame(p)
        self.assertIn("[43.63, 'B: wanted 1', 11.0]", p.out)

    def test_keyword_only_arguments(self):
        p = self.replay("options", 0)
        self.assertSame(p)
        self.assertIn("returned A_B_C", p.out)

    def test_classmethod_root_call(self):
        # a root classmethod call records no `cls`: its first argument is the real one
        p = self.replay("classmethod", self.node_id("classmethod", "Pricer.default"))
        self.assertSame(p)
        self.assertIn("returned 0.3", p.out)

    def test_classmethod_nested_call_and_init_never_faked(self):
        # nested: `cls` IS recorded ({"__class__": ...}) and must be dropped; and the
        # leaf Pricer.__init__ must run for real (a fake would leave the object empty)
        p = self.replay("classmethod_nested", self.node_id("classmethod_nested", "Pricer.default"))
        self.assertSame(p)
        self.assertIn("not faked: Pricer.__init__: a dunder", p.out)

    def test_property(self):
        p = self.replay("property", self.node_id("property", "Pricer.rate"))
        self.assertSame(p)
        self.assertIn("returned 25.0", p.out)

    # ── replays that must notice a change ────────────────────────────────

    def test_fix_is_detected_with_first_difference(self):
        root = self.copy_project()
        self.edit(root, "shop/pricing.py", "subtotal / 3  # BUG: should be / 2", "subtotal / 2")
        p = self.replay("handle", self.node_id("handle", "Pricer.total"), "--mock", "children", root=root)
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn("DIFFERENT from recorded", p.out)
        self.assertIn("first difference: result: recorded 43.63, now 65.45", p.out)

    def test_changed_callee_arguments_reported_as_diverged(self):
        root = self.copy_project()
        self.edit(root, "shop/pricing.py", "return fetch_price(line.sku) * line.qty", "return fetch_price(line.sku.lower()) * line.qty")
        p = self.replay("handle", self.node_id("handle", "Pricer.line_total"), root=root)
        self.assertIn("DIVERGED: fetch_price call 1: args[0]: recorded 'A', now 'a'", p.out)
        self.assertIn("but faked calls got different arguments", p.out)

    def test_extra_callee_call_beyond_the_recording(self):
        root = self.copy_project()
        self.edit(root, "shop/pricing.py", "if fetch_stock(line.sku) < line.qty:",
                  "fetch_stock(line.sku)\n        if fetch_stock(line.sku) < line.qty:")
        p = self.replay("handle", self.node_id("handle", "Pricer.line_total"), root=root)
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn("fetch_stock was called 2 times; the recording has 1 call(s)", p.out)

    def test_new_exception_is_different(self):
        root = self.copy_project()
        self.edit(root, "shop/pricing.py", "    def total(self, order):\n", "    def total(self, order):\n        raise KeyError('new')\n")
        p = self.replay("handle", self.node_id("handle", "Pricer.total"), "--mock", "children", root=root)
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn("raised   KeyError", p.out)

    def test_exception_gone_is_different(self):
        root = self.copy_project()
        self.edit(root, "shop/pricing.py", "raise OutOfStock(", "return 0 or OutOfStock(")
        p = self.replay("handle", self.node_id("handle", "Pricer.line_total", 2), root=root)
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn("recorded OutOfStock", p.out)

    # ── looking inside ───────────────────────────────────────────────────

    def test_locals_writes_a_trace_with_the_functions_locals(self):
        out = Path(self.tmp) / "replay_locals.json"
        p = self.replay("handle", self.node_id("handle", "Pricer.total"), "--mock", "children", "--trace-out", str(out), "--locals")
        self.assertSame(p)
        root = json.loads(out.read_text())["trace"]
        self.assertEqual(root["name"], "Pricer.total")
        if sys.version_info < (3, 12):  # TRACE_LOCALS needs sys.monitoring: perf says so, the trace has no locals
            self.assertIn("TRACE_LOCALS needs Python 3.12+", p.out)
            self.assertNotIn("locals", root)
            return
        text = json.dumps(root.get("locals"))
        self.assertIn("subtotal", text)
        self.assertIn("119.0", text)

    def test_pdb_stops_on_first_line(self):
        p = self.replay("handle", self.node_id("handle", "Pricer.total"), "--mock", "children", "--pdb",
                        stdin="n\nn\np subtotal\nc\n")
        self.assertIn("-> subtotal = sum(self.line_total(l) for l in order.lines)", p.out)
        self.assertIn("(Pdb) 119.0", p.out.replace("(Pdb) > ", "\n(Pdb) > "))
        self.assertIn("SAME as recorded", p.out)

    def test_script_kept_with_o_runs_standalone_and_can_be_edited(self):
        script = Path(self.tmp) / "kept_replay.py"
        p = self.replay("options", 0, "-o", str(script), "--no-run")
        self.assertEqual(p.returncode, 0, p.out)
        run = subprocess.run([sys.executable, str(script)], cwd=self.root, env=_env(), capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("SAME as recorded", run.stdout)
        script.write_text(script.read_text().replace("'a b c'", "'x y'"))
        run = subprocess.run([sys.executable, str(script)], cwd=self.root, env=_env(), capture_output=True, text=True)
        self.assertEqual(run.returncode, 1)
        self.assertIn("first difference: result: recorded 'A_B_C', now 'X_Y'", run.stdout)

    # ── values the trace could not keep ──────────────────────────────────

    def test_uncaptured_argument_unused_still_replays(self):
        p = self.replay("label", 0)
        self.assertSame(p)
        self.assertIn("warning: <Connection> was not captured", p.out)

    def test_uncaptured_argument_used_fails_naming_it(self):
        p = self.replay("store", 0)
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn("replay: this value was not captured (<Connection>)", p.out)

    def test_uncaptured_value_inside_a_list_still_compiles(self):
        # the placeholder sits inside a list literal: a `# TODO` comment would break the line
        p = self.replay("count", 0)
        self.assertSame(p)
        self.assertIn("returned 3", p.out)

    # ── safety and refusals ──────────────────────────────────────────────

    def test_callee_defined_in_a_script_is_not_faked_and_script_is_not_rerun(self):
        runs = (self.root / "drive_runs.txt").read_text().count("ran")
        p = self.replay("save", 0)
        # The Writer instance (a script's class, default repr) can't be rebuilt, so the
        # call fails on it -- naming it -- instead of importing drive.py to fake write().
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn("this value was not captured (<Writer>)", p.out)
        self.assertIn("not faked: Writer.write", p.out)
        self.assertIn("a script: importing it would run it", p.out)
        self.assertEqual((self.root / "drive_runs.txt").read_text().count("ran"), runs, "replay re-ran drive.py")

    def test_function_defined_in_a_script_is_refused(self):
        p = self.replay("save", self.node_id("save", "Writer.write"))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("runs code when imported", p.out)

    def test_call_cut_off_by_timeout_is_refused(self):
        p = self.replay("slow", 0)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("did not finish on its own", p.out)

    def test_call_without_captured_arguments_is_refused(self):
        p = self.replay("nocapture", 0)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no captured arguments", p.out)

    def test_generator_is_refused(self):
        p = self.replay("numbers", self.node_id("numbers", "numbers"))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("generator", p.out)

    # ── to-test shares the call builder: the shapes fixed for replay ─────

    def to_test(self, trace: str, node) -> str:
        proc = subprocess.run([sys.executable, str(SKILL), "to-test", str(self.traces / f"{trace}.json"), str(node),
                               "--root", str(self.root)], capture_output=True, text=True, env=_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        compile(proc.stdout, f"<to-test {trace}>", "exec")  # generated code must at least parse
        return proc.stdout

    def test_to_test_property_reads_the_attribute_on_self(self):
        src = self.to_test("property", self.node_id("property", "Pricer.rate"))
        self.assertIn("result = self.rate", src)
        self.assertNotIn("the instance", src)

    def test_to_test_root_classmethod_keeps_its_argument(self):
        src = self.to_test("classmethod", self.node_id("classmethod", "Pricer.default"))
        self.assertRegex(src, r"Pricer\.default\(\w+\)")

    def test_to_test_with_uncaptured_value_in_a_list_parses(self):
        self.to_test("count", 0)

    # ── interpreter choice ───────────────────────────────────────────────

    def test_default_python_is_the_projects_venv(self):
        root = self.copy_project()
        venv_bin = root / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").symlink_to(sys.executable)
        p = self.replay("options", 0, root=root, python=None)
        self.assertSame(p)
        self.assertIn(f"$ {venv_bin / 'python'} ", p.out)


if __name__ == "__main__":
    unittest.main()
