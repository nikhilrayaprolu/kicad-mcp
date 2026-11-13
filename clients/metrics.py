"""Aggregate evaluation metrics for a design driven through the MCP server.

usage: pixi run python clients/metrics.py designs/<proj>
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

root = Path(sys.argv[1])
calls = [json.loads(l) for l in (root / "client_calls.jsonl").read_text().splitlines() if l.strip()]
by_tool = defaultdict(lambda: {"n": 0, "s": 0.0, "bytes": 0, "errors": 0})
for c in calls:
    t = by_tool[c["tool"]]
    t["n"] += 1; t["s"] += c["s"]; t["bytes"] += c["resp_bytes"]; t["errors"] += int(c.get("is_error", False))
text_bytes = sum(c["resp_bytes"] for c in calls if c["tool"] not in ("render_view", "place", "route"))
print(f"tool calls: {len(calls)}  total server time: {sum(c['s'] for c in calls):.0f}s  "
      f"response bytes (excl. images): ~{text_bytes}  (~{text_bytes//4} tokens)")
print(f"{'tool':18s} {'n':>4s} {'sec':>8s} {'resp_B':>9s} {'err':>4s}")
for k, v in sorted(by_tool.items(), key=lambda kv: -kv[1]["n"]):
    print(f"{k:18s} {v['n']:4d} {v['s']:8.1f} {v['bytes']:9d} {v['errors']:4d}")
d = json.loads((root / "design.json").read_text())
print(f"\ncomponents {len(d['components'])}, constraints {len(d['constraints'])}, netclasses {len(d['netclasses'])}")
print("components by block:", dict(Counter(c['block'] for c in d['components'].values())))
for f in ("erc.json", "drc.json"):
    p = root / "out" / f
    if p.exists():
        j = json.loads(p.read_text())
        if f == "erc.json":
            v = [x for s in j.get("sheets", []) for x in s.get("violations", [])]
        else:
            v = j.get("violations", [])
        print(f"{f}: {Counter(x['severity'] for x in v)}  unconnected={len(j.get('unconnected_items', []))} "
              f"parity={len(j.get('schematic_parity', []))}")
