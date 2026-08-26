#!/usr/bin/env python3
"""Focused checks for the architecture payload's runtime projection.

The browser keeps imports and calls in separate lenses. These tests prevent the API
from quietly collapsing those semantics or drawing a direct module-to-tool shortcut
that skips the shared MCP boundary.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from farm import architecture, topology  # noqa: E402


def check(condition, label):
    if not condition:
        raise AssertionError(label)
    print("  ok  ", label)


def fixture_graph():
    return {
        "nodes": [
            {"id": "step:collect", "kind": "step", "label": "collect", "module": "cycle", "steps": []},
            {"id": "cycle:Cycle.collect", "kind": "func", "label": "Cycle.collect", "module": "cycle", "steps": ["collect"]},
            {"id": "mcp:Client.call", "kind": "func", "label": "Client.call", "module": "mcp", "steps": ["collect"]},
            {"id": "parse:state", "kind": "func", "label": "state", "module": "parse", "steps": ["collect"]},
            {"id": "tool:collect_produce", "kind": "tool", "label": "collect_produce", "module": "mcp", "steps": ["collect"]},
        ],
        "edges": [
            {"source": "step:collect", "target": "cycle:Cycle.collect", "kind": "step"},
            {"source": "cycle:Cycle.collect", "target": "mcp:Client.call", "kind": "call"},
            {"source": "cycle:Cycle.collect", "target": "parse:state", "kind": "call"},
            {"source": "cycle:Cycle.collect", "target": "tool:collect_produce", "kind": "tool"},
        ],
        "steps": [
            {"name": "collect", "order": 0, "modules": ["cycle", "mcp", "parse"],
             "tools": ["collect_produce"]}
        ],
        "errors": [],
        "stats": {"functions": 3, "tools": 1, "edges": 4},
    }


def main():
    original = topology.cached_graph
    topology.cached_graph = fixture_graph
    try:
        runtime = architecture._runtime_topology()
    finally:
        topology.cached_graph = original

    edges = {(edge["source"], edge["target"], edge["kind"]): edge for edge in runtime["edges"]}
    check(("cycle", "mcp", "call") in edges, "cross-module calls remain directional")
    check(("cycle", "parse", "call") in edges, "multiple called subsystems survive aggregation")
    check(("mcp", "tool:collect_produce", "tool") in edges,
          "external tools are anchored at the shared MCP boundary")
    check(("cycle", "tool:collect_produce", "tool") not in edges,
          "semantic tool shortcuts do not skip the MCP boundary")
    check(all(edge["source"] != edge["target"] for edge in runtime["edges"]),
          "intra-module function calls do not clutter the subsystem map")
    check(edges[("mcp", "tool:collect_produce", "tool")]["steps"] == ["collect"],
          "runtime edges preserve the stages that can reach them")
    check(runtime["steps"][0] == {
        "name": "collect", "order": 0, "modules": ["cycle", "mcp", "parse"],
        "tools": ["collect_produce"]
    }, "run-stage drill-down metadata remains stable")

    live = architecture._runtime_topology()
    check(isinstance(live["edges"], list) and isinstance(live["steps"], list),
          "live topology degrades to a stable payload shape")
    print("architecture payload: PASS")


if __name__ == "__main__":
    main()
