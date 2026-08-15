"""Proves the join() fix doesn't poison a long-lived sender for later use.

A naive fix ("join() sets one shared shutdown flag") would permanently
disable retries for every future send after the first join() call -- fatal
for a sender meant to be reused across many traces over a process's
lifetime. This sends a blob, joins, then sends ANOTHER blob and confirms it
still retries/succeeds normally.
"""

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from collector import start_collector  # noqa: E402
from sender import EndpointSender  # noqa: E402

LAB_DIR = Path(__file__).parent
SPILL_DIR = LAB_DIR / "lab_spill_reuse"


def make_blob(name: str, content: dict) -> Path:
    SPILL_DIR.mkdir(exist_ok=True)
    path = SPILL_DIR / name
    path.write_text(json.dumps(content))
    return path


def main() -> None:
    shutil.rmtree(SPILL_DIR, ignore_errors=True)

    server, store = start_collector()
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    sender = EndpointSender(url, tags=["lab"])

    # First send + join -- this is the call that, with the buggy shared-Event
    # version, would have set a permanent shutdown flag.
    path1 = make_blob("blob1.json", {"n": 1})
    sender.send_blob_from_file("ref-1", path1, "trace-a")
    sender.join(timeout=10)

    first_send_ok = sender.blobs_sent == 1 and not path1.exists()

    # Second send, AFTER join() already ran once -- must still retry/succeed
    # normally, not be silently dead on arrival.
    path2 = make_blob("blob2.json", {"n": 2})
    sender.send_blob_from_file("ref-2", path2, "trace-b")
    sender.join(timeout=10)

    second_send_ok = sender.blobs_sent == 2 and not path2.exists()

    checks = {
        "first_send_succeeded": first_send_ok,
        "second_send_after_join_still_succeeded": second_send_ok,
        "no_abandoned_sends": sender.blobs_abandoned == 0,
        "collector_received_both_blobs": len(store.blobs.get("trace-a", {})) == 1
        and len(store.blobs.get("trace-b", {})) == 1,
    }
    result = {
        "label": "sender reuse across multiple join() calls",
        "blobs_sent": sender.blobs_sent,
        "blobs_abandoned": sender.blobs_abandoned,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
    }
    print(json.dumps(result))
    server.shutdown()
    shutil.rmtree(SPILL_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
