#!/usr/bin/env python3
"""Checks for farm/topology.py - the call graph the 3D pipeline view draws.

A visualisation is only worth looking at if it cannot lie, and this graph is
derived rather than authored, so the risk is not that it looks wrong: it is that
it looks plausible while missing edges. These checks pin it to facts that are
independently true of the loop:

- every step the cycle declares must exist as a node, in execution order
- the steps that talk to the server must reach the tools they actually name
- the steps that are pure arithmetic must reach no server tool at all
- every server call must route through the one MCP client method
- the graph must be deterministic, bounded, and cheap enough to build on a poll

Run: python3 deploy/test_topology.py
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from farm import progress, topology  # noqa: E402

CHECKS = []


def ok(passed: bool, label: str, detail: str = "") -> None:
    CHECKS.append((bool(passed), label, detail if not passed else ""))


def main() -> int:
    started = time.time()
    graph = topology.graph()
    build_s = time.time() - started

    nodes = {node["id"]: node for node in graph["nodes"]}
    steps = {step["name"]: step for step in graph["steps"]}
    edges = graph["edges"]
    out_edges = {}
    for edge in edges:
        out_edges.setdefault(edge["source"], set()).add(edge["target"])

    ok(not graph["errors"], "every farm module parses", str(graph["errors"]))

    # -- the pipeline itself ------------------------------------------------
    declared = [step["name"] for step in progress.STEPS]
    ok(list(steps.keys()) == declared,
       "the graph's steps are progress.STEPS, in execution order",
       "%s != %s" % (list(steps.keys()), declared))
    for name in declared:
        ok("step:%s" % name in nodes, "step node exists: %s" % name)

    # -- the boundary ------------------------------------------------------
    # These pairings are the loop's contract with the server. If a rename in
    # cycle.py breaks one, the 3D view would quietly stop showing that call.
    expected_tools = {
        "collect": "collect_produce",
        "read": "list_farm",
        "feed": "feed_animals",
        "sell": "sell",
        "adopt": "adopt_animal",
        "buy_feed": "buy_feed",
        "board": "leaderboard",
        "offers": "propose_trade",
        "trades": "respond_to_trade",
        "harvest": "harvest",
        "tools": "tools/list",
    }
    for step, tool in expected_tools.items():
        found = steps.get(step, {}).get("tools") or []
        ok(tool in found, "step %s reaches server tool %s" % (step, tool), "tools=%s" % found)

    # The interesting negative: planning and bookkeeping cost nothing on the wire.
    # That claim is the whole argument for the Python loop, and here it is a
    # property of the graph rather than a sentence in a README.
    ok(not (steps.get("plan", {}).get("tools") or []),
       "plan makes no server call: expansion is arithmetic",
       str(steps.get("plan", {}).get("tools")))
    ok(not (steps.get("finish", {}).get("tools") or []),
       "recording a run makes no server call",
       str(steps.get("finish", {}).get("tools")))
    ok((steps.get("plan", {}).get("functions") or 0) >= 5,
       "plan still reaches real work: the rules fan-out",
       str(steps.get("plan", {}).get("functions")))

    # -- structure ---------------------------------------------------------
    tool_nodes = [n for n in graph["nodes"] if n["kind"] == "tool"]
    ok(len(tool_nodes) >= 10, "every server tool the loop names is a node",
       "found %d" % len(tool_nodes))
    ok(all(n["steps"] for n in tool_nodes),
       "no server tool is orphaned from the step that calls it")
    ok("mcp:Client.call" in nodes,
       "the transport boundary is in the graph, not abstracted away")
    ok("mcp:Client._post" in out_edges.get("mcp:Client.rpc", set()),
       "the client's own call chain is followed into the transport")
    ok(nodes.get("rules:expansion_plan") is not None,
       "the joint feed/adoption solve is a node")
    ok(nodes["rules:expansion_plan"]["loc"] > 20,
       "node size carries real lines of code",
       str(nodes.get("rules:expansion_plan", {}).get("loc")))
    ok(nodes["rules:expansion_plan"]["doc"] != "",
       "documented functions carry their first sentence for the inspector")

    modules = {m["name"] for m in graph["modules"]}
    for expected in (
        "cycle", "rules", "mcp", "parse", "growth",
        "analysis", "claims", "ledger", "policy",
    ):
        ok(expected in modules, "module represented: %s" % expected, str(sorted(modules)))

    # Every non-step node must be reachable from some step, or it is decoration.
    unreachable = [n["id"] for n in graph["nodes"] if n["kind"] != "step" and not n["steps"]]
    ok(not unreachable, "every node is reachable from a step", str(unreachable[:5]))

    # Every edge must land on a node that exists (a dangling edge would draw a
    # line to the origin, which reads as a real relationship).
    dangling = [e for e in edges if e["source"] not in nodes or e["target"] not in nodes]
    ok(not dangling, "no dangling edges", str(dangling[:3]))
    ok(all(e["source"] != e["target"] for e in edges), "no self-loops")

    kinds = {e["kind"] for e in edges}
    ok(kinds <= {"step", "call", "tool"}, "edge kinds are the three the renderer knows",
       str(kinds))

    # -- honesty -----------------------------------------------------------
    # Nothing invented: every function node must name a real file and a line
    # inside it, so the inspector's "cycle.py:412" can always be opened.
    bad_lines = []
    for node in graph["nodes"]:
        if node["kind"] != "func":
            continue
        path = os.path.join(topology.HERE, node["module"] + ".py")
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            bad_lines.append(node["id"])
            continue
        if not (1 <= node["line"] <= len(lines)):
            bad_lines.append(node["id"])
            continue
        source = lines[node["line"] - 1]
        tail = node["label"].split(".")[-1]
        if "def %s" % tail not in source:
            bad_lines.append("%s -> %s" % (node["id"], source.strip()[:60]))
    ok(not bad_lines, "every function node points at its own def line",
       str(bad_lines[:3]))

    # -- cost and stability ------------------------------------------------
    ok(build_s < 0.75, "the graph builds fast enough to serve on demand",
       "%.3fs" % build_s)
    ok(json.dumps(topology.graph(), sort_keys=True) == json.dumps(graph, sort_keys=True),
       "two builds of unchanged source are byte-identical")

    first = topology.cached_graph()
    started = time.time()
    for _ in range(50):
        topology.cached_graph()
    cached_s = (time.time() - started) / 50
    ok(topology.cached_graph() is first, "the cache returns the same object")
    ok(cached_s < 0.01, "a cached read is cheap enough for a 2s poll",
       "%.4fs" % cached_s)

    ok(not graph["stats"]["truncated"], "the real graph fits inside the node cap")
    ok(graph["stats"]["functions"] >= 40, "the fan-out is actually the codebase",
       str(graph["stats"]))
    payload = len(json.dumps(graph, separators=(",", ":")))
    ok(payload < 120_000, "the payload is small enough to fetch once", "%d bytes" % payload)

    # A monitoring module must never be able to break the page: prove the
    # error path returns a usable empty graph instead of raising.
    real_dir, topology.HERE = topology.HERE, "/nonexistent-farm-dir"
    try:
        broken = topology.graph()
        ok(broken["nodes"] == [] and "stats" in broken,
           "an unreadable source tree degrades to an empty graph, not an exception")
    except Exception as exc:  # noqa: BLE001
        ok(False, "an unreadable source tree degrades to an empty graph, not an exception",
           str(exc))
    finally:
        topology.HERE = real_dir

    failures = [c for c in CHECKS if not c[0]]
    for passed, label, detail in CHECKS:
        print("  %-4s %s%s" % ("ok" if passed else "FAIL", label,
                               ("  [%s]" % detail) if detail else ""))
    print()
    if failures:
        print("TOPOLOGY TEST FAILED: %d of %d checks" % (len(failures), len(CHECKS)))
        return 1
    print("TOPOLOGY TEST PASSED: %d checks (build %.0fms, %d nodes, %d edges)"
          % (len(CHECKS), build_s * 1000, len(graph["nodes"]), len(edges)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
