"""``python -m perf``: attach perf to a Python process that is already running.

    python -m perf attach <pid> pkg.mod:func [--calls N] [--seconds S] [--out DIR] [--set KEY=VALUE ...]
    python -m perf detach <pid> [pkg.mod:func]

``attach`` asks the target to trace the next calls of ``pkg.mod:func`` (see
:func:`perf.attach`), then waits, listing each trace as it is written, until
the budget is used up (default: 1 call) or you press Ctrl-C (which detaches).

The request reaches the target one of two ways (``--via``):

* ``signal`` -- the program called ``perf.enable_attach()`` at startup. Works
  on any Python, needs no special rights. Picked automatically when the target
  is listening.
* ``remote`` -- Python 3.14+ ``sys.remote_exec`` (PEP 768): nothing prepared
  in the target, but this must run on the same Python minor version as the
  target and needs ptrace rights over it (with ``kernel.yama.ptrace_scope=1``,
  the Linux default, that means ``sudo``).

Either way the target only receives a JSON request (target, budget, config);
the traces are written by the target itself, into ``--out`` (default
``./perf_traces``, resolved here), so it must be writable by the target.
"""

import argparse
import ast
import json
import os
import signal
import sys
import time
import uuid
from pathlib import Path

import perf


def _target_uid(pid: int) -> int:
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return os.getuid() if hasattr(os, "getuid") else 0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    try:  # an exited process its parent hasn't reaped yet still answers kill(0)
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return state not in ("Z", "X")


def _private_write(path: Path, text: str, uid: int) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    if hasattr(os, "geteuid") and os.geteuid() == 0 and uid != 0:
        os.chown(tmp, uid, -1)  # the target (not root) must read it
    os.replace(tmp, path)


def _control_dir(uid: int) -> Path:
    directory = perf._attach_dir(uid)
    if not directory.exists():
        directory.mkdir(mode=0o700)
        if hasattr(os, "geteuid") and os.geteuid() == 0 and uid != 0:
            os.chown(directory, uid, -1)
    if not perf._attach_dir_trusted(directory) and not (hasattr(os, "geteuid") and os.geteuid() == 0):
        sys.exit(f"perf: {directory} is not private to this user; remove it and retry")
    _sweep(directory)
    return directory


def _sweep(directory: Path) -> None:
    """Remove what processes that died without cleaning up left behind
    (markers, requests, status files are all named <pid>.*)."""
    for p in directory.iterdir():
        pid = p.name.split(".", 1)[0]
        if pid.isdigit() and not _alive(int(pid)):
            try:
                p.unlink()
            except OSError:
                pass


_REMOTE_SCRIPT = """\
def _perf_attach_main():
    import sys
    path = {perf_parent!r}
    added = False
    if "perf" not in sys.modules and path not in sys.path:
        sys.path.insert(0, path)
        added = True
    try:
        import perf
    except BaseException as e:
        import json, os
        with open({status!r}, "w") as f:
            json.dump({{"id": {rid!r}, "state": "error", "error": "cannot import perf in the target: %r" % (e,)}}, f)
        return
    finally:
        if added:
            try:
                sys.path.remove(path)
            except ValueError:
                pass
    handle = getattr(perf, "_handle_attach_request", None)
    if handle is None:
        import json
        with open({status!r}, "w") as f:
            json.dump({{"id": {rid!r}, "state": "error", "error": "the target's perf (%s) predates live attach" % getattr(perf, "__file__", "?")}}, f)
        return
    handle({request!r})
try:
    _perf_attach_main()
finally:
    del _perf_attach_main
    try:
        __import__("os").unlink({script!r})
    except OSError:
        pass
"""


def _deliver(pid: int, request: dict, via: str) -> tuple:
    """Write `request` for `pid` and make the target read it. Returns
    (status path, how it was delivered)."""
    uid = _target_uid(pid)
    directory = _control_dir(uid)
    request_path = directory / f"{pid}.{request['id']}.request.json"
    status_path = directory / f"{pid}.{request['id']}.status.json"
    listening = _listening_signal(directory, pid)
    if via == "auto":
        via = "signal" if listening else "remote"
    if via == "signal":
        if not listening:
            sys.exit(f"perf: pid {pid} isn't listening -- call perf.enable_attach() at its startup, or use --via remote (Python 3.14+)")
        _private_write(request_path, json.dumps(request), uid)
        os.kill(pid, listening)
        return status_path, request_path, "signal"
    if not hasattr(sys, "remote_exec"):
        sys.exit(
            f"perf: sys.remote_exec needs Python 3.14+ (this is {sys.version.split()[0]}). "
            "Run this CLI with the target's Python 3.14, or call perf.enable_attach() in the target."
        )
    _private_write(request_path, json.dumps(request), uid)
    script = directory / f"{pid}.{request['id']}.py"
    perf_parent = str(Path(perf.__file__).resolve().parent.parent)
    _private_write(
        script,
        _REMOTE_SCRIPT.format(perf_parent=perf_parent, status=str(status_path), rid=request["id"], request=str(request_path), script=str(script)),
        uid,
    )
    try:
        sys.remote_exec(pid, str(script))
    except PermissionError as e:
        script.unlink(missing_ok=True)
        request_path.unlink(missing_ok=True)
        sys.exit(
            f"perf: not allowed to attach to pid {pid} ({e}).\n"
            "  sys.remote_exec needs ptrace rights: run with sudo, or set kernel.yama.ptrace_scope=0,\n"
            "  or call perf.enable_attach() in the target and retry (no rights needed)."
        )
    except Exception as e:
        script.unlink(missing_ok=True)
        request_path.unlink(missing_ok=True)
        sys.exit(f"perf: sys.remote_exec failed for pid {pid}: {type(e).__name__}: {e}")
    return status_path, request_path, "remote"


def _listening_signal(directory: Path, pid: int):
    """The signal `pid` handles for attach requests, or None when it never
    called perf.enable_attach() -- or the marker is a dead process's and the
    pid now belongs to another one (signalling that would kill it)."""
    marker = directory / f"{pid}.listening"
    try:
        info = json.loads(marker.read_text())
    except (OSError, ValueError):
        return None
    start = info.get("start")
    if start is not None and start != perf._process_start(pid):
        try:
            marker.unlink()  # stale
        except OSError:
            pass
        return None
    return int(info.get("signal") or signal.SIGUSR1)


def _read_status(path: Path, request_id: str) -> dict | None:
    try:
        status = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return status if status.get("id") == request_id else None


def _wait_status(path: Path, request_id: str, pid: int, timeout: float, states: tuple) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _read_status(path, request_id)
        if status is not None and status.get("state") in states:
            return status
        if not _alive(pid):
            return None
        time.sleep(0.05)
    return None


def _withdraw(request_path: Path, status_path: Path, request_id: str) -> dict | None:
    """Cancel a request the target hasn't answered, so it can't act on it
    later (a remote_exec script still runs when the main thread wakes, but
    finds no request). If the target took it in the meantime, its status."""
    try:
        request_path.unlink()
    except FileNotFoundError:  # the target already read it: wait for the answer
        time.sleep(0.5)
        status = _read_status(status_path, request_id)
        if status is not None:
            return status
    except OSError:
        pass
    _cleanup(status_path)
    return None


def _parse_value(text: str):
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text


def _cleanup(*paths: Path) -> None:
    for p in paths:
        try:
            p.unlink()
        except OSError:
            pass


def cmd_attach(args: argparse.Namespace) -> int:
    pid = args.pid
    if not _alive(pid):
        sys.exit(f"perf: no process {pid}")
    config = {}
    for item in args.set or ():
        key, sep, value = item.partition("=")
        if not sep:
            sys.exit(f"perf: --set expects KEY=VALUE, got {item!r}")
        config[key] = _parse_value(value)
    unknown = set(config) - perf._CONFIG_KEYS
    if unknown:
        sys.exit(f"perf: unknown config key(s) {sorted(unknown)}; valid: {', '.join(sorted(perf._CONFIG_KEYS))}")
    out = Path(args.out).resolve()
    config.setdefault("OUTPUT_DIR", str(out))  # an OUTPUT_FILE is then a name inside it
    config.setdefault("PRINT_REPORT", False)  # the target's stdout isn't ours to read
    calls = args.calls if args.calls is not None else (None if args.seconds else 1)
    if calls == 0:
        calls = None
    request = {"id": uuid.uuid4().hex, "action": "attach", "target": args.target, "calls": calls, "seconds": args.seconds, "config": config}
    out_dir = Path(config["OUTPUT_DIR"]) if config.get("OUTPUT_DIR") else None
    seen: set = set(out_dir.glob("*.json")) if out_dir is not None and out_dir.is_dir() else set()
    status_path, request_path, via = _deliver(pid, request, args.via)
    status = _wait_status(status_path, request["id"], pid, args.connect_timeout, ("attached", "error", "done"))
    if status is None:
        status = _withdraw(request_path, status_path, request["id"])
    if status is None:
        if not _alive(pid):
            hint = "it exited"
        elif via == "remote":
            hint = (
                "sys.remote_exec only runs once the target's MAIN thread executes Python code; "
                "a main thread parked in join()/wait()/a long C call never picks it up. "
                "Call perf.enable_attach() in the target and retry (a signal wakes it)"
            )
        else:
            hint = "its main thread may be stuck in C code that ignores signals"
        print(f"perf: pid {pid} did not answer within {args.connect_timeout:g}s -- request withdrawn.\n  {hint}", file=sys.stderr)
        return 1
    if status["state"] == "error":
        _cleanup(status_path)
        print(f"perf: attach failed in pid {pid}: {status.get('error')}", file=sys.stderr)
        return 1
    budget = " and ".join(
        part for part in (f"{calls} call{'s' * (calls != 1)}" if calls else "", f"{args.seconds:g}s" if args.seconds else "") if part
    ) or "until Ctrl-C"
    where = config.get("OUTPUT_DIR") or config.get("OUTPUT_FILE")
    print(f"perf: attached to {status.get('target')} in pid {pid} (via {via}); tracing {budget} -> {where}", file=sys.stderr)
    if not args.wait:
        return 0

    def report_new() -> None:
        if out_dir is None or not out_dir.is_dir():
            return
        for p in sorted(out_dir.glob("*.json"), key=lambda q: q.stat().st_mtime):
            if p not in seen:
                seen.add(p)
                print(p)

    try:
        while True:
            report_new()
            status = _read_status(status_path, request["id"]) or status
            if status.get("state") == "done":
                break
            if not _alive(pid):
                print(f"perf: pid {pid} exited", file=sys.stderr)
                return 1
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("perf: detaching...", file=sys.stderr)
        detach = {"id": uuid.uuid4().hex, "action": "detach", "target": args.target}
        detach_status, detach_request, _ = _deliver(pid, detach, via)
        if _wait_status(detach_status, detach["id"], pid, args.connect_timeout, ("detached", "error")) is None:
            _withdraw(detach_request, detach_status, detach["id"])
        # traced calls still running finish on their own; give them a moment
        _wait_status(status_path, request["id"], pid, 2.0, ("done",))
        status = _read_status(status_path, request["id"]) or status
        _cleanup(detach_status)
    report_new()
    _cleanup(status_path)
    print(f"perf: done -- {status.get('traced', '?')} traced call(s) ({status.get('reason') or 'detached'})", file=sys.stderr)
    return 0


def cmd_detach(args: argparse.Namespace) -> int:
    request = {"id": uuid.uuid4().hex, "action": "detach", "target": args.target}
    status_path, request_path, _ = _deliver(args.pid, request, args.via)
    status = _wait_status(status_path, request["id"], args.pid, args.connect_timeout, ("detached", "error"))
    if status is None:
        status = _withdraw(request_path, status_path, request["id"])
    if status is None:
        print(f"perf: pid {args.pid} did not answer", file=sys.stderr)
        return 1
    _cleanup(status_path)
    if status["state"] == "error":
        print(f"perf: detach failed: {status.get('error')}", file=sys.stderr)
        return 1
    print(f"perf: detached {', '.join(status.get('stopped') or []) or 'nothing (no live attach)'}", file=sys.stderr)
    return 0


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m perf", description="Attach perf to a running Python process.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("pid", type=int, help="process id of the running Python program")
        p.add_argument("--via", choices=("auto", "signal", "remote"), default="auto",
                       help="signal: target called perf.enable_attach(); remote: Python 3.14 sys.remote_exec (default: signal if listening, else remote)")
        p.add_argument("--connect-timeout", type=float, default=10.0, metavar="S", help="how long to wait for the target to answer (default 10)")

    a = sub.add_parser("attach", help="trace the next calls of a function in a running process")
    common(a)
    a.add_argument("target", help="function to trace: pkg.mod:func or pkg.mod:Class.method (module must already be imported there)")
    a.add_argument("--calls", type=int, help="number of calls to trace (default 1; 0 = no limit)")
    a.add_argument("--seconds", type=float, help="stop after this long (with no --calls: trace every call in the window)")
    a.add_argument("--out", default="perf_traces", help="directory the target writes traces to (default ./perf_traces)")
    a.add_argument("--set", action="append", metavar="KEY=VALUE", help="perf config for the traced calls, e.g. --set TRACE_LOCALS=True (repeatable)")
    a.add_argument("--no-wait", dest="wait", action="store_false", help="return once attached instead of waiting for the traces")
    a.set_defaults(func=cmd_attach)

    d = sub.add_parser("detach", help="stop a live attach early")
    common(d)
    d.add_argument("target", nargs="?", help="only this function (default: every live attach)")
    d.set_defaults(func=cmd_detach)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
