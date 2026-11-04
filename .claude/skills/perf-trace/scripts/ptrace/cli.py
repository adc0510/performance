"""Read perf trace files (JSON written by the `perf` tracer) without loading them whole into context.

Standard library only -- runs with any python3 (3.9+), perf need not be installed.

browse
    ls DIR                              trace files in DIR, newest first
    summary TRACE                       overview: totals, perf overhead, hot spots, errors
    tree TRACE [--id N] [--depth D] [--min-pct P] [--no-merge]
    hot TRACE [--sort self|wall|calls|cumul]
    find TRACE PATTERN [--errors]       calls by name (substring or glob)
    node TRACE ID [--frames]            one call: args, result, exception, children
    path TRACE ID                       the chain of calls from the root to node ID
    where [TRACE] ID|NAME --root DIR    source file:line of a node's function
    locals TRACE ID                     TRACE_LOCALS of one call, line by line
    diff TRACE_A TRACE_B                per-function time, before vs after
data and state
    value TRACE LITERAL [--locals] [--contains]   every place a value appears, first one first
    flow TRACE ID                       where a call's arguments came from and where its result went
    diverge TRACE ID_A ID_B             value + structure diff of two calls, down to the first difference
    vdiff TRACE_A TRACE_B [--fn NAME] [--k K]     the same, across two traces
    at TRACE ID LINE [--hit K]          all locals right after the K-th execution of a line
    iters TRACE ID [--line L]           one row per loop iteration
    mutations TRACE [--fn NAME]         calls that changed objects they were handed
    lines TRACE ID                      the function's source with hit counts and values per line
exceptions
    errors TRACE                        each exception: origin, fate, cause chain, translation
    retries TRACE                       repeated attempts of a failing call
    exc-flow TRACE                      exceptions raised and caught over and over
    unfinished TRACE                    calls still running when the trace closed
time and concurrency
    critical-path TRACE [--id N]        the chain of calls that set the wall time
    timeline TRACE [--id N]             text Gantt of a call's children, concurrency, idle stretches
    serial TRACE                        independent work done one after another
    nplus1 TRACE                        one call per item where only one argument changes
    dupes TRACE                         the same call with equal arguments, repeated
    cache TRACE                         lru_cache hit rate, misses for already-seen arguments
    scaling TRACE [--fn NAME]           how time grows with input size (log-log exponent)
    outliers TRACE                      calls far slower than the same function's median
    gens TRACE                          generators: active vs wall, closed early
    payload TRACE                       functions moving the biggest values
many traces
    stats DIR [--root NAME]             p50/p95 per function, what drives the slow traces
    sdiff TRACE_A TRACE_B               structural diff: calls/exceptions that appeared or vanished
    group DIR [--key PATH]              traces sharing an id: same input again, or different requests?
fixing and verifying
    to-test TRACE ID --root SRC [--mock-leaves] [-o FILE]   pytest reproducing one captured call
    snapshot TRACE --fn NAME --root SRC [-o FILE]           golden-master tests over every captured call
    replay TRACE ID --root SRC [--mock leaves|children|none] [--keep NAME] [--pdb] [--locals] [-o FILE]
                                        run ONE captured call again on its own, compare with the recording
understanding and hygiene
    types TRACE [--fn NAME]             observed argument/return types per function
    touched TRACE --root SRC [--unreached]   files and functions this run executed
    sequence TRACE [--id N] [--depth D] Mermaid sequence diagram
    html TRACE [-o FILE] [--no-values]  one offline HTML page: call tree, call graph, functions, flame
    secrets TRACE [--emails]            credentials that escaped redaction, with config to stop it
    advise TRACE                        capture settings for the next trace

Node IDs (#N) are preorder positions in the trace's tree: stable for a given file, so an ID printed
by one command can be passed to another.
"""

from __future__ import annotations

import argparse

from . import browse, codegen, exceptions, html, multi, replay, timing, understand, values


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="perf_trace.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def cmd(name, fn, *positional, **kw):
        p = sub.add_parser(name)
        for arg in positional:
            p.add_argument(arg)
        p.set_defaults(_run=fn)
        return p

    # browse (unchanged interface)
    p = cmd("ls", browse.cmd_ls, "dir"); p.add_argument("-n", type=int, default=20); p.add_argument("-r", "--recursive", action="store_true")
    p = cmd("summary", browse.cmd_summary, "trace"); p.add_argument("-n", type=int, default=12)
    p = cmd("tree", browse.cmd_tree, "trace"); p.add_argument("--id"); p.add_argument("--depth", type=int, default=6)
    p.add_argument("--min-pct", type=float, default=1.0); p.add_argument("--no-merge", action="store_true")
    p = cmd("hot", browse.cmd_hot, "trace"); p.add_argument("-n", type=int, default=25)
    p.add_argument("--sort", choices=["self", "wall", "calls", "cumul"], default="self")
    p = cmd("find", browse.cmd_find, "trace", "pattern"); p.add_argument("-n", type=int, default=30); p.add_argument("--errors", action="store_true")
    p = cmd("node", browse.cmd_node, "trace", "id"); p.add_argument("--width", type=int, default=400)
    p.add_argument("-n", type=int, default=30); p.add_argument("--frames", action="store_true")
    p = cmd("locals", browse.cmd_locals, "trace", "id"); p.add_argument("--width", type=int, default=200)
    cmd("path", browse.cmd_path, "trace", "id")
    p = sub.add_parser("where"); p.add_argument("trace", nargs="?", default=None); p.add_argument("target")
    p.add_argument("--root", default="."); p.set_defaults(_run=browse.cmd_where)
    p = cmd("diff", browse.cmd_diff, "a", "b"); p.add_argument("-n", type=int, default=25)

    # data and state
    p = cmd("value", values.cmd_value, "trace", "value"); p.add_argument("--locals", action="store_true")
    p.add_argument("--contains", action="store_true", help="substring match for strings"); p.add_argument("-n", type=int, default=40)
    p = cmd("flow", values.cmd_flow, "trace", "id"); p.add_argument("-n", type=int, default=10)
    p = cmd("diverge", values.cmd_diverge, "trace", "a", "b"); p.add_argument("--depth", type=int, default=3); p.add_argument("--width", type=int, default=50)
    p = cmd("vdiff", values.cmd_vdiff, "a", "b"); p.add_argument("--fn"); p.add_argument("--k", type=int, default=0)
    p.add_argument("--depth", type=int, default=3); p.add_argument("--width", type=int, default=50)
    p = cmd("at", values.cmd_at, "trace", "id"); p.add_argument("line", type=int); p.add_argument("--hit", type=int, default=1)
    p.add_argument("--width", type=int, default=200)
    p = cmd("iters", values.cmd_iters, "trace", "id"); p.add_argument("--line", type=int); p.add_argument("--width", type=int, default=40)
    p = cmd("mutations", values.cmd_mutations, "trace"); p.add_argument("--fn"); p.add_argument("-n", type=int, default=40)
    p = cmd("lines", values.cmd_lines, "trace", "id"); p.add_argument("--width", type=int, default=80)

    # exceptions
    cmd("errors", exceptions.cmd_errors, "trace")
    cmd("retries", exceptions.cmd_retries, "trace")
    p = cmd("exc-flow", exceptions.cmd_exc_flow, "trace"); p.add_argument("--min", type=int, default=3)
    cmd("unfinished", exceptions.cmd_unfinished, "trace")

    # time and concurrency
    p = cmd("critical-path", timing.cmd_critical_path, "trace"); p.add_argument("--id"); p.add_argument("--depth", type=int, default=12)
    p.add_argument("--min-pct", type=float, default=1.0)
    p = cmd("timeline", timing.cmd_timeline, "trace"); p.add_argument("--id"); p.add_argument("--width", type=int, default=80)
    p.add_argument("-n", type=int, default=30); p.add_argument("--no-merge", action="store_true"); p.add_argument("--min-pct", type=float, default=2.0)
    p = cmd("serial", timing.cmd_serial, "trace"); p.add_argument("--min-pct", type=float, default=2.0); p.add_argument("-n", type=int, default=10)
    p = cmd("nplus1", timing.cmd_nplus1, "trace"); p.add_argument("--min", type=int, default=3); p.add_argument("--min-pct", type=float, default=1.0)
    p = cmd("dupes", timing.cmd_dupes, "trace"); p.add_argument("-n", type=int, default=20)
    cmd("cache", timing.cmd_cache, "trace")
    p = cmd("scaling", timing.cmd_scaling, "trace"); p.add_argument("--fn"); p.add_argument("-n", type=int, default=10)
    p.add_argument("--min-exp", type=float, default=1.4)
    p = cmd("outliers", timing.cmd_outliers, "trace"); p.add_argument("--min", type=int, default=5); p.add_argument("--factor", type=float, default=5.0)
    p.add_argument("-n", type=int, default=10)
    p = cmd("gens", timing.cmd_gens, "trace"); p.add_argument("-n", type=int, default=30)
    p = cmd("payload", timing.cmd_payload, "trace"); p.add_argument("-n", type=int, default=15)
    p.add_argument("--sort", choices=["out", "in", "capture"], default="out")

    # many traces
    p = cmd("stats", multi.cmd_stats, "dir"); p.add_argument("--root"); p.add_argument("-r", "--recursive", action="store_true")
    p.add_argument("-n", type=int, default=20); p.add_argument("--sort", choices=["p95", "p50", "corr", "tail"], default="tail")
    p = cmd("sdiff", multi.cmd_sdiff, "a", "b"); p.add_argument("-n", type=int, default=30)
    p = cmd("group", multi.cmd_group, "dir"); p.add_argument("--key"); p.add_argument("-r", "--recursive", action="store_true")

    # fixing and verifying
    p = cmd("to-test", codegen.cmd_to_test, "trace", "id"); p.add_argument("--root", default=".")
    p.add_argument("--mock-leaves", action="store_true"); p.add_argument("-o", "--output")
    p = cmd("snapshot", codegen.cmd_snapshot, "trace"); p.add_argument("--fn", required=True); p.add_argument("--root", default=".")
    p.add_argument("-n", type=int, default=25); p.add_argument("--keep-todo", action="store_true"); p.add_argument("-o", "--output")
    p = cmd("replay", replay.cmd_replay, "trace", "id"); p.add_argument("--root", default=".")
    p.add_argument("--mock", choices=["leaves", "children", "none"], default="leaves",
                   help="fake the call's direct leaf callees (default), all direct callees, or nothing, from their recorded results")
    p.add_argument("--keep", action="append", metavar="NAME", help="never fake this callee (repeatable, substring)")
    p.add_argument("--pdb", action="store_true", help="stop in pdb on the function's first line (interactive terminal)")
    p.add_argument("--locals", action="store_true", help="record the replay as a new trace with the function's locals")
    p.add_argument("--trace-out", metavar="FILE", help="where to write the replay trace (default TRACE.replay-ID.json)")
    p.add_argument("--python", help="the project's interpreter (default ROOT/.venv/bin/python, else this one)")
    p.add_argument("-o", "--output", help="keep the generated replay script here (edit its arguments, run it again)")
    p.add_argument("--no-run", action="store_true", help="only write the script (-o)")

    # understanding and hygiene
    p = cmd("types", understand.cmd_types, "trace"); p.add_argument("--fn")
    p = cmd("touched", understand.cmd_touched, "trace"); p.add_argument("--root", default="."); p.add_argument("--unreached", action="store_true")
    p = cmd("sequence", understand.cmd_sequence, "trace"); p.add_argument("--id"); p.add_argument("--depth", type=int, default=2)
    p.add_argument("--min-pct", type=float, default=1.0); p.add_argument("--width", type=int, default=60)
    p.add_argument("--by", choices=["function", "module"], default="function", help="one lifeline per function (default) or per module")
    p = cmd("secrets", understand.cmd_secrets, "trace"); p.add_argument("--emails", action="store_true"); p.add_argument("-n", type=int, default=30)
    cmd("advise", understand.cmd_advise, "trace")
    p = cmd("html", html.cmd_html, "trace"); p.add_argument("-o", "--output", help="default: the trace path with .html")
    p.add_argument("--no-values", action="store_true", help="leave out arguments, results and locals: structure and timing only (exception messages stay)")
    return ap


def main(argv=None):
    a = _parser().parse_args(argv)
    try:
        a._run(a)
    except BrokenPipeError:  # piped into head
        pass
