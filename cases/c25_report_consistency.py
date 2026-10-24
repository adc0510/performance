"""The printed tree's numbers agree with the hot spots for folded (×N) calls."""

import time

import perf
from _common import setup

EXPECT = {
    "new": True,
    "summary": "Printed report: a function called many times with several different children (price_order ×6 → get_customer, line_total, fraud_score) — tree self time and call counts match the hot spots",
    "checks": [
        {"status": "works", "fixes": "a folded group's self time was computed as if its merged children all started together, so overlapping was assumed and the parent's self time was inflated (267 ms shown vs 21 ms real)",
         "desc": "the self time of a folded `name ×N` row equals the hot-spots self time of that function",
         "stdout": "*price_order self: tree == hot spots*"},
        {"status": "works", "desc": "...and stays small: the parent's own work, not its children's",
         "stdout": "*price_order self below 25% of its wall: True*"},
        {"status": "works", "desc": "a single call whose children get folded keeps its correct self time too",
         "stdout": "*report self: tree == hot spots*"},
        {"status": "works", "fixes": "children merged from several folded calls showed no count (get_customer under price_order ×40 printed as one call)",
         "desc": "children merged from a folded group show their total count",
         "stdout": "*├─ __main__.get_customer ×6*"},
        {"status": "works", "desc": "...at every level below it",
         "stdout": "*└─ __main__.db_query ×12*"},
    ],
}


def db_query():
    time.sleep(0.004)


def get_customer():
    db_query()


def get_product():
    db_query()


def fraud_score():
    time.sleep(0.006)


def price_order():
    get_customer()

    def line_total():
        get_product()

    for _ in range(2):
        line_total()
    fraud_score()


def report():
    for _ in range(3):
        get_customer()
        fraud_score()


@perf.trace
def main():
    for _ in range(6):
        price_order()
    report()


if __name__ == "__main__":
    import io
    import json
    from contextlib import redirect_stdout

    setup(__file__)
    out = io.StringIO()
    with redirect_stdout(out):
        main()
    tree = out.getvalue()
    print(tree)

    root = json.load(open(f"{perf.OUTPUT_DIR}/main.json"))["trace"]
    hot = perf._aggregate(root)
    shown = {n["name"].split(" ×")[0]: n for n in perf._collapse(root["children"])}
    tree_self = getattr(perf, "_display_self", perf._self_time)  # what the printed tree uses

    def same(name):
        return abs(tree_self(shown[name]) - hot[name]["self"]) < 1e-9

    print("price_order self:", "tree == hot spots" if same("__main__.price_order") else "MISMATCH")
    print("price_order self below 25% of its wall:", tree_self(shown["__main__.price_order"]) < 0.25 * shown["__main__.price_order"]["time"])
    print("report self:", "tree == hot spots" if same("__main__.report") else "MISMATCH")
