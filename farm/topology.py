"""The execution web of a run, extracted from the loop's own source.

The dashboard could already answer "which step is running and how long did it
take". It could not answer the question an operator actually asks when a step is
slow: *what does that step actually do?* One line labelled "Feed herd - 36.3s"
hides a fan-out of `farm/` functions and server calls, and no bar chart of phase
durations has ever made that shape visible.

So this module reads `farm/*.py` with `ast` and derives the graph:

    step  ->  the functions its `with self._step(...)` body calls
          ->  everything those functions call, transitively
          ->  the MCP tools that finally leave the process

Why static analysis instead of runtime tracing:

- **A tracer costs the farm.** `sys.setprofile` on every call inside a 70-second
  cycle is overhead on the thing being measured, and monitoring is not allowed to
  slow the loop down (the cycle budget is a hard 150s).
- **It works before the step runs.** The whole point of the pipeline view is that
  the shape of a run is visible up front, including steps this run skipped. A
  tracer can only report what already executed.
- **It is the code, not a memory of it.** The graph is re-derived from the source
  on disk whenever an mtime changes, so an edited `cycle.py` shows up on the next
  poll. There is no hand-maintained diagram to drift.

What it deliberately does not do: no import graph (an import is not an action),
no dynamic dispatch guessing, no `getattr` resolution. Every edge here comes from
a syntactically resolvable call - `self.foo()`, `module.foo()`, `foo()`, or
`self.c.call("tool")` - and anything else is left out rather than invented. The
cost of that choice is a few missing edges; the benefit is that no edge on the
screen is a guess.

Errors are contained: `graph()` never raises. A syntax error in one module costs
that module's nodes and is reported in `graph()["errors"]`, because a dashboard
panel must not be able to take the page down.
"""

import ast
import os
from typing import Any, Dict, List, Optional, Set, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))

# The client attribute that reaches the server, and the methods on it that cost a
# call. `self.c.call("collect_produce")` is the only way an action leaves this
# process, which is what makes a tool node a real boundary rather than a label.
CLIENT_ATTR = "c"
CLIENT_CALL_METHODS = {"call": "tool", "rpc": "rpc", "tool_names": "handshake"}

# A server call is two facts at once, and the graph records both: the *action*
# (`tool:sell`, the boundary where the farm actually changes) and the *code path*
# (`mcp:Client.call`, which every action without exception funnels through, picking
# up the rate limiter, the retry loop and the secret scrubber on the way).
# Recording only the action hid a fifth of the codebase; recording only the path
# lost which step does what.
CLIENT_ENTRY = {
    "call": "mcp:Client.call",
    "rpc": "mcp:Client.rpc",
    "tool_names": "mcp:Client.tool_names",
}

# Depth is bounded so one accidental recursion cannot produce an unbounded graph
# for the browser to lay out. Six hops is past the deepest real chain in farm/
# (step -> cycle method -> helper -> parse/rules -> client -> transport).
MAX_DEPTH = 6
MAX_NODES = 600

# Stable module order for summaries and inspectors: centrality to a cycle rather
# than alphabetical order. Presentation stays in dashboard/trace_explorer.*.
MODULE_ORDER = [
    "cycle", "mcp", "parse", "rules", "growth", "progress", "tokens",
    "heal", "journal", "report", "watch", "scheduler", "evidence", "release",
]


def _module_files() -> List[Tuple[str, str]]:
    out = []
    for name in sorted(os.listdir(HERE)):
        if not name.endswith(".py") or name == "__init__.py":
            continue
        out.append((name[:-3], os.path.join(HERE, name)))
    return out


def signature() -> Tuple[Tuple[str, int, int], ...]:
    """Cheap identity for the source tree, so the graph is rebuilt when it changes."""
    sig = []
    for module, path in _module_files():
        try:
            st = os.stat(path)
        except OSError:
            continue
        sig.append((module, int(st.st_mtime), int(st.st_size)))
    return tuple(sig)


class _Function(object):
    """One resolvable unit of work: a module-level function or a method."""

    __slots__ = ("module", "cls", "name", "line", "loc", "calls", "doc")

    def __init__(self, module: str, cls: Optional[str], name: str, line: int, loc: int, doc: str):
        self.module = module
        self.cls = cls
        self.name = name
        self.line = line
        self.loc = loc
        self.doc = doc
        self.calls: List[Dict[str, Any]] = []

    @property
    def qual(self) -> str:
        local = "%s.%s" % (self.cls, self.name) if self.cls else self.name
        return "%s:%s" % (self.module, local)

    @property
    def label(self) -> str:
        return "%s.%s" % (self.cls, self.name) if self.cls else self.name


def _first_string(node: ast.Call) -> Optional[str]:
    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    return None


def _summary(node: ast.AST) -> str:
    """The first sentence of a docstring, which is the only prose worth showing."""
    doc = ast.get_docstring(node) or ""
    doc = " ".join(doc.split())
    if not doc:
        return ""
    cut = doc.find(". ")
    if cut > 0:
        doc = doc[: cut + 1]
    return doc[:160]


class _ModuleReader(ast.NodeVisitor):
    """Collects functions, their calls, and the module's farm-local imports."""

    def __init__(self, module: str):
        self.module = module
        self.imports: Dict[str, str] = {}     # local alias -> farm module
        self.functions: Dict[str, _Function] = {}
        self.steps: List[Dict[str, Any]] = []  # cycle.py's `with self._step(...)`
        self._cls: Optional[str] = None
        self._fn: Optional[_Function] = None
        self._known = {name for name, _ in _module_files()}

    # -- imports ---------------------------------------------------------
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if (node.module or "").split(".")[-1] in ("farm", "") or node.level:
            for alias in node.names:
                if alias.name in self._known:
                    self.imports[alias.asname or alias.name] = alias.name
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            tail = alias.name.split(".")[-1]
            if alias.name.startswith("farm.") and tail in self._known:
                self.imports[alias.asname or tail] = tail
        self.generic_visit(node)

    # -- definitions -----------------------------------------------------
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        outer, self._cls = self._cls, node.name
        self.generic_visit(node)
        self._cls = outer

    def _visit_function(self, node: Any) -> None:
        # A nested def is part of its parent's body, not a separately callable
        # unit, so its calls are attributed to the enclosing function.
        if self._fn is not None:
            self.generic_visit(node)
            return
        line = node.lineno
        end = getattr(node, "end_lineno", line) or line
        fn = _Function(self.module, self._cls, node.name, line, max(1, end - line + 1), _summary(node))
        self.functions[fn.qual] = fn
        self._fn = fn
        self.generic_visit(node)
        self._fn = None

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    # -- calls -----------------------------------------------------------
    def visit_With(self, node: ast.With) -> None:
        """`with self._step("name")` marks the body as one pipeline step."""
        for item in node.items:
            call = item.context_expr
            if not isinstance(call, ast.Call):
                continue
            if not _is_self_attr(call.func, "_step"):
                continue
            name = _first_string(call)
            if not name:
                continue
            body_calls: List[Dict[str, Any]] = []
            for stmt in node.body:
                body_calls.extend(_calls_in(stmt, self.module, self.imports, self._cls))
            self.steps.append({"name": name, "line": node.lineno, "calls": body_calls})
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self._fn is not None:
            self._fn.calls.extend(_describe_call(node, self.module, self.imports, self._cls))
            if _is_self_attr(node.func, "_skip"):
                name = _first_string(node)
                if name:
                    self.steps.append({"name": name, "line": node.lineno, "calls": []})
        self.generic_visit(node)


def _is_self_attr(func: ast.AST, attr: str) -> bool:
    return (
        isinstance(func, ast.Attribute)
        and func.attr == attr
        and isinstance(func.value, ast.Name)
        and func.value.id == "self"
    )


def _describe_call(
    node: ast.Call, module: str, imports: Dict[str, str], cls: Optional[str]
) -> List[Dict[str, Any]]:
    """Resolve one call site to zero or more graph targets.

    The shapes that matter, in the order they occur in farm/: a server call, a
    method on self, a call into another farm module, and a module-level function
    in this file. Anything else (stdlib, builtin, dynamic dispatch) resolves to
    nothing rather than to a guess.
    """
    func = node.func
    # self.c.call("tool", ...) / self.c.rpc(...) / self.c.tool_names()
    if isinstance(func, ast.Attribute) and func.attr in CLIENT_CALL_METHODS:
        owner = func.value
        if (
            isinstance(owner, ast.Attribute)
            and owner.attr == CLIENT_ATTR
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "self"
        ) or (isinstance(owner, ast.Name) and owner.id in ("client", "c")):
            kind = CLIENT_CALL_METHODS[func.attr]
            out = [{"target": CLIENT_ENTRY[func.attr], "kind": "call", "line": node.lineno}]
            if kind == "handshake":
                out.append({"target": "tool:tools/list", "kind": "tool", "line": node.lineno})
            else:
                tool = _first_string(node)
                if tool:
                    prefix = "tool:" if kind == "tool" else "rpc:"
                    out.append({"target": prefix + tool, "kind": "tool", "line": node.lineno})
            return out
    if isinstance(func, ast.Attribute):
        owner = func.value
        # self.method(...)
        if isinstance(owner, ast.Name) and owner.id == "self" and cls:
            if func.attr.startswith("_step") or func.attr == "_skip":
                return []
            return [{"target": "%s:%s.%s" % (module, cls, func.attr), "kind": "call",
                     "line": node.lineno}]
        # rules.foo(...), parse.foo(...), progress.foo(...)
        if isinstance(owner, ast.Name) and owner.id in imports:
            return [{"target": "%s:%s" % (imports[owner.id], func.attr), "kind": "call",
                     "line": node.lineno}]
        return []
    # bare foo(...) - a module-level function in this file
    if isinstance(func, ast.Name):
        return [{"target": "%s:%s" % (module, func.id), "kind": "call", "line": node.lineno}]
    return []


def _calls_in(
    stmt: ast.AST, module: str, imports: Dict[str, str], cls: Optional[str]
) -> List[Dict[str, Any]]:
    out = []
    for node in ast.walk(stmt):
        if isinstance(node, ast.Call):
            out.extend(_describe_call(node, module, imports, cls))
    return out


def _read_modules() -> Tuple[Dict[str, _Function], List[Dict[str, Any]], List[str], Dict[str, int]]:
    functions: Dict[str, _Function] = {}
    steps: List[Dict[str, Any]] = []
    errors: List[str] = []
    module_loc: Dict[str, int] = {}
    for module, path in _module_files():
        try:
            with open(path, encoding="utf-8") as fh:
                source = fh.read()
            tree = ast.parse(source, filename=path)
        except (OSError, SyntaxError, ValueError) as exc:
            errors.append("%s: %s" % (module, str(exc)[:120]))
            continue
        module_loc[module] = source.count("\n") + 1
        reader = _ModuleReader(module)
        reader.visit(tree)
        functions.update(reader.functions)
        steps.extend(reader.steps)
    return functions, steps, errors, module_loc


def _merge_steps(raw: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """One entry per step name. `harvest` appears as both a step and a skip."""
    merged: Dict[str, List[Dict[str, Any]]] = {}
    for item in raw:
        merged.setdefault(item["name"], []).extend(item["calls"])
    return merged


def _tool_label(node_id: str) -> str:
    return node_id.split(":", 1)[1]


def graph() -> Dict[str, Any]:
    """Nodes and edges for the whole pipeline, derived from farm/*.py.

    Shape (stable, and every field is used by the dashboard):

        nodes: [{id, kind: step|func|tool, label, module, qual, line, loc,
                 doc, steps: [step names that reach it], depth, fan}]
        edges: [{source, target, kind: step|call|tool}]
    """
    try:
        return _graph()
    except Exception as exc:  # noqa: BLE001 - a panel must never take the page down
        return {"nodes": [], "edges": [], "steps": [], "modules": [],
                "errors": ["topology failed: %s" % str(exc)[:160]], "stats": {}}


def _graph() -> Dict[str, Any]:
    functions, raw_steps, errors, module_loc = _read_modules()
    steps = _merge_steps(raw_steps)

    nodes: Dict[str, Dict[str, Any]] = {}
    edges: Set[Tuple[str, str, str]] = set()
    reach: Dict[str, Set[str]] = {}        # node id -> step names that reach it
    depth_of: Dict[str, int] = {}

    def add_node(node_id: str, **fields: Any) -> Dict[str, Any]:
        node = nodes.get(node_id)
        if node is None:
            node = {"id": node_id, "steps": [], "depth": 0, "fan": 0}
            node.update(fields)
            nodes[node_id] = node
        return node

    # Steps in the order the loop runs them, which is the order they were parsed.
    step_order = [item["name"] for item in raw_steps]
    seen: Set[str] = set()
    ordered_steps = [n for n in step_order if not (n in seen or seen.add(n))]

    for index, name in enumerate(ordered_steps):
        add_node("step:%s" % name, kind="step", label=name, module="cycle",
                 qual="cycle:Cycle.run", line=0, loc=0, doc="", order=index)

    for name in ordered_steps:
        root = "step:%s" % name
        # Breadth-first from the step body so `depth` is hops from the step, which
        # is what the 3D view uses to place a node in the web.
        frontier: List[Tuple[str, int]] = []
        for call in steps.get(name, []):
            frontier.append((call["target"], 1))
            target = call["target"]
            if target.startswith("tool:") or target.startswith("rpc:"):
                edges.add((root, target, "tool"))
            elif target in functions:
                edges.add((root, target, "step"))
        visited: Set[str] = set()
        while frontier:
            target, depth = frontier.pop(0)
            if depth > MAX_DEPTH or len(nodes) >= MAX_NODES:
                continue
            key = "%s@%d" % (target, depth)
            if key in visited:
                continue
            visited.add(key)

            if target.startswith("tool:") or target.startswith("rpc:"):
                node = add_node(target, kind="tool", label=_tool_label(target),
                                module="mcp", qual=target, line=0, loc=0,
                                doc="server call" if target.startswith("tool:") else "JSON-RPC method")
            else:
                fn = functions.get(target)
                if fn is None:
                    continue          # stdlib, builtin, or unresolvable: not invented
                node = add_node(target, kind="func", label=fn.label, module=fn.module,
                                qual=fn.qual, line=fn.line, loc=fn.loc, doc=fn.doc)

            reach.setdefault(target, set()).add(name)
            prior = depth_of.get(target)
            depth_of[target] = depth if prior is None else min(prior, depth)

            fn = functions.get(target)
            if fn is None:
                continue
            for call in fn.calls:
                child = call["target"]
                if child == target:
                    continue          # self-recursion adds no structure to look at
                if child.startswith("tool:") or child.startswith("rpc:"):
                    edges.add((target, child, "tool"))
                    frontier.append((child, depth + 1))
                elif child in functions:
                    edges.add((target, child, "call"))
                    frontier.append((child, depth + 1))

    # Drop edges whose endpoints never got a node (depth or node cap).
    live_edges = [
        {"source": s, "target": t, "kind": k}
        for s, t, k in sorted(edges)
        if s in nodes and t in nodes
    ]
    for edge in live_edges:
        nodes[edge["source"]]["fan"] += 1

    for node_id, node in nodes.items():
        node["steps"] = sorted(reach.get(node_id, set()))
        node["depth"] = depth_of.get(node_id, 0)

    modules_used = sorted({n["module"] for n in nodes.values()},
                          key=lambda m: (MODULE_ORDER.index(m) if m in MODULE_ORDER else 99, m))
    module_rows = [
        {
            "name": m,
            "nodes": sum(1 for n in nodes.values() if n["module"] == m and n["kind"] == "func"),
            "loc": module_loc.get(m, 0),
        }
        for m in modules_used
    ]

    step_rows = []
    for index, name in enumerate(ordered_steps):
        reached = [n for n in nodes.values() if name in (n["steps"] or [])]
        step_rows.append({
            "name": name,
            "order": index,
            "functions": sum(1 for n in reached if n["kind"] == "func"),
            "tools": sorted(_tool_label(n["id"]) for n in reached if n["kind"] == "tool"),
            "modules": sorted({n["module"] for n in reached if n["kind"] == "func"}),
        })

    node_rows = sorted(nodes.values(), key=lambda n: (n["kind"], n["id"]))
    return {
        "nodes": node_rows,
        "edges": live_edges,
        "steps": step_rows,
        "modules": module_rows,
        "errors": errors,
        "stats": {
            "functions": sum(1 for n in node_rows if n["kind"] == "func"),
            "tools": sum(1 for n in node_rows if n["kind"] == "tool"),
            "edges": len(live_edges),
            "modules": len(module_rows),
            "source_loc": sum(module_loc.values()),
            "max_depth": max([n["depth"] for n in node_rows] or [0]),
            "truncated": len(nodes) >= MAX_NODES,
        },
    }


_CACHE: Dict[str, Any] = {"signature": None, "graph": None}


def cached_graph() -> Dict[str, Any]:
    """`graph()`, rebuilt only when a farm/*.py mtime or size changes.

    The dashboard polls every 2 seconds; re-parsing 4,400 lines of Python 1,800
    times an hour to produce a byte-identical answer is waste, and the mtime
    check is what keeps an edit to cycle.py visible on the next poll anyway.
    """
    sig = signature()
    if _CACHE["signature"] != sig or _CACHE["graph"] is None:
        _CACHE["graph"] = graph()
        _CACHE["signature"] = sig
    return _CACHE["graph"]
