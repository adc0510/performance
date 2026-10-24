"""Run every case script and check its trace against the case's ``EXPECT``.

Usage (from the repo root)::

    uv run python cases/run_all.py            # run all cases
    uv run python cases/run_all.py c06 c13    # only cases whose name starts with these
    uv run python cases/run_all.py --md       # also regenerate cases/TEST_CASES.md

Each case runs in its own interpreter (perf patches modules process-wide, so
cases must not share one). ``EXPECT`` is read with ``ast.literal_eval`` — this
runner never imports perf or the cases, so nothing here gets traced.

``EXPECT`` schema::

    EXPECT = {
        "summary": "one line: what code shapes this case covers",
        "exit": 0,                       # expected exit code of the script
        "checks": [
            {
                "status": "works" | "limitation" | "bug",
                "desc": "what this check demonstrates",
                "file": "main",          # which trace JSON (OUTPUT_FILE) to check
                # exactly one of the following check kinds:
                "path": ["main", "*.load", "Repo.get"],  # chain of direct children, fnmatch
                "absent": "*inner*",                     # no node matching anywhere
                "count": "*.fib", "n": 15,               # number of matching nodes
                "node": "*.parse",                       # first match (preorder), then any of:
                    "select": {"args": [1]},             # only consider nodes whose fields match
                    "args": [...], "kwargs": {...}, "result": ...,
                    "exception": "ValueError(*",         # fnmatch on the repr
                    "has": ["exception_origin"], "lacks": ["result"],
                    "raw": True,                         # compare without resolving __ref__s
                "stdout": "*TIMEOUT*",                   # fnmatch against captured stdout+stderr
                "negate": True,                          # optional: the check must FAIL
                "min_python": [3, 12],                   # optional: only applies on this Python or newer
                "max_python": [3, 11],                   # optional: only applies on this Python or older
            },
        ],
    }

Values compared via ``args``/``kwargs``/``result`` have ``{"__ref__": …}``
pool references resolved first, so expectations are written as plain values;
an expected string containing ``*`` is a wildcard (every other character is
literal; node names use fnmatch), and an
expected dict containing ``"...": True`` only has to be a subset of the actual one.
"""

import ast
import json
import re
import shutil
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "out"


def load_expect(path: Path) -> dict:
    tree = ast.parse(path.read_text())
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and any(getattr(t, "id", None) == "EXPECT" for t in stmt.targets):
            return ast.literal_eval(stmt.value)
    raise ValueError(f"{path.name}: no EXPECT")


def resolve(value, variables, depth=0):
    if depth > 50:
        return value
    if isinstance(value, dict):
        if "__ref__" in value:
            # Bare {"__ref__": r} stands for the pooled value; a rich object is
            # {"__type__": T, "__ref__": r} where r is its pooled __dict__.
            target = resolve(variables.get(value["__ref__"], value), variables, depth + 1)
            rest = {k: resolve(v, variables, depth + 1) for k, v in value.items() if k != "__ref__"}
            return {**rest, **target} if rest and isinstance(target, dict) else target
        return {k: resolve(v, variables, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, variables, depth + 1) for v in value]
    return value


def wild(text, pattern):
    """`*` matches anything (including newlines); every other character is literal."""
    regex = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(regex, text, re.DOTALL) is not None


def matches(actual, expected):
    """Equality, except an expected string containing `*` is a wildcard pattern."""
    if isinstance(expected, str) and "*" in expected and isinstance(actual, str):
        return wild(actual, expected)
    if isinstance(expected, dict) and isinstance(actual, dict):
        # {"...": True} in an expected dict allows extra keys in the actual one.
        keys = expected.keys() - {"..."}
        if "..." not in expected and keys != actual.keys():
            return False
        return keys <= actual.keys() and all(matches(actual[k], expected[k]) for k in keys)
    if isinstance(expected, list) and isinstance(actual, list):
        return len(expected) == len(actual) and all(matches(a, e) for a, e in zip(actual, expected))
    return actual == expected


def with_text(trace):
    """A written trace with each TRACE_LOCALS entry's source text filled back
    in (``entry["text"]``) from the top-level "sources" map, and its "changed"
    edits replayed into full "set" values (perf.expand_locals), in place."""
    import perf

    perf.expand_locals(trace)
    sources = trace.get("sources", {})
    for node in walk(trace["trace"]):
        lines = sources.get(node.get("source"), {}).get("lines", {})
        for entry in node.get("locals", []):
            if str(entry.get("line")) in lines:
                entry["text"] = lines[str(entry["line"])]
    return trace


def walk(node):
    yield node
    for child in node.get("children", []):
        yield from walk(child)


def find_path(node, names):
    if not fnmatch(node["name"], names[0]):
        return False
    if len(names) == 1:
        return True
    return any(find_path(child, names[1:]) for child in node.get("children", []))


def run_check(check, traces, output):
    """Returns (ok, detail)."""
    if "stdout" in check:
        return wild(output, check["stdout"]), ""
    trace = traces.get(check.get("file", "main"))
    if trace is None:
        return False, f"no trace file {check.get('file', 'main')}.json"
    root, variables = trace["trace"], trace["variables"]
    if "path" in check:
        return find_path(root, check["path"]), ""
    if "absent" in check:
        hits = [n["name"] for n in walk(root) if fnmatch(n["name"], check["absent"])]
        return not hits, f"found {hits[:3]}" if hits else ""
    if "count" in check:
        n = sum(1 for node in walk(root) if fnmatch(node["name"], check["count"]))
        return n == check["n"], f"count={n}"
    if "node" in check:
        # `select` narrows which node is checked, e.g. {"args": [0.02, "x"]}.
        select = check.get("select", {})
        node = next(
            (n for n in walk(root) if fnmatch(n["name"], check["node"])
             and all(matches(resolve(n.get(k, "<missing>"), variables), v) for k, v in select.items())),
            None,
        )
        if node is None:
            return False, "node not found"
        for key in ("args", "kwargs", "result", "locals_dropped"):
            if key in check:
                actual = node.get(key, "<missing>")
                if not check.get("raw"):
                    actual = resolve(actual, variables)
                if not matches(actual, check[key]):
                    return False, f"{key}={json.dumps(actual, default=str)[:300]}"
        if "exception" in check and not matches(node.get("exception", ""), check["exception"]):
            return False, f"exception={node.get('exception')!r}"
        for key in check.get("has", []):
            if key not in node:
                return False, f"missing key {key}"
        for key in check.get("lacks", []):
            if key in node:
                return False, f"unexpected key {key}={json.dumps(resolve(node[key], variables), default=str)[:200]}"
        return True, ""
    return False, "unknown check kind"


def run_case(path: Path):
    expect = load_expect(path)
    out_dir = OUT / path.stem
    shutil.rmtree(out_dir, ignore_errors=True)
    # -X faulthandler: if a case ever crashes the interpreter, its output.txt
    # holds the stack of every thread at the crash.
    proc = subprocess.run(
        [sys.executable, "-X", "faulthandler", path.name], cwd=HERE, capture_output=True, text=True, timeout=120
    )
    output = proc.stdout + proc.stderr
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "output.txt").write_text(output)
    traces = {p.stem: json.loads(p.read_text()) for p in out_dir.glob("*.json")}
    results = []
    expected_exit = expect.get("exit", 0)
    if proc.returncode != expected_exit:
        results.append(({"status": "works", "desc": f"exit code {expected_exit}"}, False, f"exit={proc.returncode}"))
    py = list(sys.version_info[:2])
    for check in expect["checks"]:
        if py < check.get("min_python", [0]) or py > check.get("max_python", [99]):
            results.append((check, True, "n/a"))  # not applicable on this Python
            continue
        ok, detail = run_check(check, traces, output)
        if check.get("negate"):
            ok, detail = not ok, detail or "matched, but negate=True"
        results.append((check, ok, detail))
    return expect, results


def describe(check):
    """The exact assertion a check makes, as one short markdown string."""
    def code(value):
        text = json.dumps(value, ensure_ascii=False)
        text = text if len(text) <= 160 else text[:157] + "…"
        return "`" + text.replace("|", "\\|").replace("`", "'") + "`"

    where = f" in `{check['file']}.json`" if check.get("file", "main") != "main" else ""
    if "stdout" in check:
        text = f"output matches {code(check['stdout'])}"
    elif "path" in check:
        text = "path " + " → ".join(f"`{n}`" for n in check["path"]) + where
    elif "absent" in check:
        text = f"no node matching `{check['absent']}`{where}"
    elif "count" in check:
        text = f"exactly {check['n']} node(s) matching `{check['count']}`{where}"
    elif "node" in check:
        parts = [f"first `{check['node']}`{where}"]
        for key in ("args", "kwargs", "result", "locals_dropped"):
            if key in check:
                parts.append(f"{key} = {code(check[key])}")
        if "exception" in check:
            parts.append(f"exception ~ {code(check['exception'])}")
        if check.get("has"):
            parts.append("has " + ", ".join(f"`{k}`" for k in check["has"]))
        if check.get("lacks"):
            parts.append("lacks " + ", ".join(f"`{k}`" for k in check["lacks"]))
        if check.get("raw"):
            parts.append("(refs not resolved)")
        text = "; ".join(parts)
    else:
        text = "—"
    return ("**NOT** " if check.get("negate") else "") + text


def md_text(text):
    """Prose for a table cell: `<marker>`-style text outside code spans would be
    swallowed as HTML by Markdown viewers, and `|` would split the cell."""
    parts = text.split("`")
    for i in range(0, len(parts), 2):  # even parts are outside backticks
        parts[i] = parts[i].replace("<", "&lt;").replace(">", "&gt;")
    return "`".join(parts).replace("|", "\\|")


def whats_new(rows):
    """The "What's new" section: new cases, new checks in existing cases, and
    the checks that verify bug fixes — driven by `new` / `fixes` in EXPECT."""
    new_cases = [(p, e, r) for p, e, r in rows if e.get("new")]
    new_checks = [(p, c) for p, e, r in rows if not e.get("new") for c, _, _ in r if c.get("new")]
    fixes = [(p, i, c, ok) for p, e, r in rows for i, (c, ok, _) in enumerate(r, 1) if c.get("fixes")]
    if not (new_cases or new_checks or fixes):
        return []
    md = ["## What's new since the last review", ""]
    if fixes:
        md += ["### Fixed: bugs and removed limitations (each verified by a check)", "", "| what was wrong | verified by | result |", "|---|---|---|"]
        for path, i, check, ok in fixes:
            md.append(f"| {md_text(check['fixes'])} | [{path.stem}](#{path.stem}) check {i} | {'✅' if ok else '❌'} |")
        md.append("")
    if new_cases:
        md += [f"### New cases ({len(new_cases)})", "", "| case | covers | checks | result |", "|---|---|---|---|"]
        for path, expect, results in new_cases:
            bad = sum(1 for _, ok, _ in results if not ok)
            md.append(f"| [{path.stem}](#{path.stem}) | {md_text(expect['summary'])} | {len(results)} | {'✅' if not bad else f'❌ {bad}'} |")
        md.append("")
    if new_checks:
        md += ["### New checks in existing cases", ""]
        for path, check in new_checks:
            md.append(f"- [{path.stem}](#{path.stem}): `{check['status']}`: {md_text(check['desc'])}")
        md.append("")
    limits = [(p, c) for p, e, r in rows for c, _, _ in r
              if c["status"] == "limitation" and (e.get("new") or c.get("new"))]
    if limits:
        md += [f"### New limitations documented ({len(limits)})", "",
               "Known gaps the new cases pinned down; each needs a decision (accept, document in the skill, or fix):", ""]
        for path, check in limits:
            md.append(f"- [{path.stem}](#{path.stem}): {md_text(check['desc'])}")
        md.append("")
    return md


def write_markdown(rows):
    import platform

    counts = {}
    for _, _, results in rows:
        for check, _, _ in results:
            counts[check["status"]] = counts.get(check["status"], 0) + 1
    total = sum(counts.values())
    failed = sum(1 for _, _, results in rows for _, ok, _ in results if not ok)
    na = sum(1 for _, _, results in rows for _, _, d in results if d == "n/a")
    md = [
        "# perf test cases",
        "",
        f"Generated by `uv run python cases/run_all.py --md` on Python {platform.python_version()} "
        f"({platform.system()}): **{len(rows)} cases, {total} checks, {failed} failing** "
        f"({na} not applicable on this Python version — see each check's *(Python …)* scope).",
        "",
        "Every check runs the case script in its own interpreter, then asserts on the trace JSON it wrote "
        "(or on its stdout). Status meanings:",
        "",
        "- `works`: perf traces and captures this as you'd expect.",
        "- `limitation`: a known blind spot or lossy representation; the check pins down exactly what you get instead.",
        "- `bug`: known wrong behaviour; the check fails once it is fixed, so update it then.",
        "",
        "Assertions: names are fnmatch patterns; a path is a chain of direct children starting at the root; "
        "values are compared after resolving `{\"__ref__\"}` pool refs (unless marked); `*` inside an expected "
        "string is a wildcard; `\"...\": true` in an expected dict allows extra keys.",
        "",
    ]
    md += whats_new(rows)
    md += [
        "## Overview",
        "",
        "🆕 = new since the last review · 🔧 = check verifies a bug fix",
        "",
        "| case | covers | works | limitation | bug | result |",
        "|---|---|---|---|---|---|",
    ]
    for path, expect, results in rows:
        n = {s: sum(1 for c, _, _ in results if c["status"] == s) for s in ("works", "limitation", "bug")}
        bad = sum(1 for _, ok, _ in results if not ok)
        mark = " 🆕" if expect.get("new") else ""
        md.append(f"| [{path.stem}](#{path.stem}){mark} | {md_text(expect['summary'])} | {n['works']} | {n['limitation']} | {n['bug']} "
                  f"| {'✅ pass' if not bad else f'❌ {bad} failing'} |")
    for path, expect, results in rows:
        md += ["", f"## {path.stem}{' 🆕' if expect.get('new') else ''}", "", f"{expect['summary']}.", "",
               f"Source: [`{path.name}`]({path.name}) · run alone: `uv run python cases/run_all.py {path.stem[:3]}`", "",
               "| # | status | behaviour | asserted | result |", "|---|---|---|---|---|"]
        for i, (check, ok, detail) in enumerate(results, 1):
            result = ("➖ n/a" if detail == "n/a" else "✅") if ok else f"❌ {detail}".replace("|", "\\|")
            desc = md_text(check["desc"])
            scope = [f"Python ≥ {'.'.join(map(str, check['min_python']))}"] if "min_python" in check else []
            scope += [f"Python ≤ {'.'.join(map(str, check['max_python']))}"] if "max_python" in check else []
            if scope:
                desc = f"*({', '.join(scope)})* " + desc
            if check.get("new") or expect.get("new"):
                desc = "🆕 " + desc
            if check.get("fixes"):
                desc = "🔧 " + desc + "<br>*Fixed:* " + md_text(check["fixes"])
            md.append(f"| {i} | {check['status']} | {desc} | {describe(check)} | {result} |")
    (HERE / "TEST_CASES.md").write_text("\n".join(md) + "\n")


def main(argv):
    write_md = "--md" in argv
    prefixes = [a for a in argv if not a.startswith("--")]
    cases = sorted(p for p in HERE.glob("c[0-9]*.py") if not prefixes or p.stem.startswith(tuple(prefixes)))
    failed = 0
    rows = []
    for path in cases:
        expect, results = run_case(path)
        rows.append((path, expect, results))
        bad = [r for r in results if not r[1]]
        failed += len(bad)
        print(f"{'FAIL' if bad else 'ok  '} {path.stem}: {expect['summary']}")
        for check, ok, detail in results:
            if not ok:
                print(f"     ✗ [{check['status']}] {check['desc']}  ({detail})")
    na = sum(1 for _, _, results in rows for _, _, d in results if d == "n/a")
    print(f"\n{failed} failing check(s), {na} not applicable on Python {sys.version_info[0]}.{sys.version_info[1]}")
    if write_md:
        write_markdown(rows)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
