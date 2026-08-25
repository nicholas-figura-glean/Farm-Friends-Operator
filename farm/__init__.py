"""Deterministic Farm Friends operator.

The strategy rules that used to live in an LLM prompt live in rules.py.
The LLM is only involved when watch.py raises an anomaly.
"""

__all__ = ["mcp", "parse", "rules", "cycle", "watch", "report"]
