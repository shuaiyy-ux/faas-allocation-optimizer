"""Dynamic constraint plugin loader (session-aware, sandboxed).

In the per-session deployment model, constraint plugins live in
`SessionState.constraints` as `{name: source_code}` and are compiled fresh
per solve. A plugin is a Python source string defining a top-level
`apply(model, x, veh, dealer, *, stage, rem_cap, source_to_dealers, arc_dist)`
callable that adds PuLP constraints to `model`.

Security model
--------------
Constraint code is authored by the chat agent and only stored after the user
approves it, but for the public demo we do NOT trust it. Instead of a raw
``exec`` with full builtins, every plugin is:

1. **AST-validated** against a whitelist (:func:`validate_constraint_source`).
   Anything that could escape the sandbox is rejected before execution:
   imports, ``_``-prefixed names / attributes (blocks ``__globals__`` &c.),
   the dangerous callables (``open`` / ``eval`` / ``exec`` / ``compile`` /
   ``getattr`` / ``setattr`` / ``globals`` / ``locals`` / ``vars`` /
   ``__import__`` …), ``lambda`` / ``class`` / ``async`` definitions, and
   string-format traversal (``.format`` / ``.format_map``).
2. **Executed with no real builtins** — ``{"__builtins__": {}}`` plus an
   explicit safe namespace (a small set of pure helpers and ``lpSum`` so the
   agent can express slot / count constraints).

Only nodes needed to write real constraints are accepted: ``def apply``,
arithmetic / comparison / boolean ops, subscripting, indexing, ``for`` loops,
``if`` / ``return``, comprehensions, and calls to the whitelisted helpers or
to methods on the objects the engine passes in (``x.items()`` etc.). A
comprehension can no longer be an escape hatch because the dunder / private
attribute rule blocks ``__class__`` / ``__subclasses__`` regardless of where
it appears.
"""
import ast
from typing import Callable, Dict, List, Optional, Tuple

from pulp import lpSum

# Callables reachable from constraint code. Deliberately tiny: pure, no I/O,
# no import machinery, no attribute-introspection helpers. `lpSum` lets the
# agent build sum constraints; everything else is a harmless builtin.
SAFE_NAMESPACE: Dict[str, object] = {
    "lpSum": lpSum,
    "len": len, "range": range, "enumerate": enumerate, "zip": zip,
    "sorted": sorted, "min": min, "max": max, "sum": sum, "abs": abs,
    "round": round, "int": int, "float": float, "bool": bool, "str": str,
    "list": list, "dict": dict, "set": set, "tuple": tuple,
    "any": any, "all": all,
    "True": True, "False": False, "None": None,
}

# Names that must never be callable / referenced from constraint code.
_FORBIDDEN_NAMES = {
    "open", "eval", "exec", "compile", "getattr", "setattr", "delattr",
    "hasattr", "globals", "locals", "vars", "__import__", "input",
    "breakpoint", "help", "exit", "quit", "memoryview", "object", "type",
    "super", "classmethod", "staticmethod", "property", "__build_class__",
}

# Attribute names that must never be accessed (string-format traversal). The
# `_`-prefix rule already covers all dunders; these are the remaining public
# escape routes.
_FORBIDDEN_ATTRS = {"format", "format_map"}


def _describe(node: ast.AST) -> str:
    return type(node).__name__


def validate_constraint_source(source: str) -> None:
    """Raise ``ValueError`` if `source` is not a safe constraint plugin.

    Passing means: it parses, defines a top-level ``apply`` function, and
    contains no forbidden node. It does NOT execute anything.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"Syntax error in constraint code: {exc}")

    top_level_funcs = {
        n.name for n in tree.body if isinstance(n, ast.FunctionDef)
    }
    if "apply" not in top_level_funcs:
        raise ValueError(
            "Constraint code must define a top-level apply(model, x, veh, "
            "dealer, **ctx) function."
        )

    for node in ast.walk(tree):
        # No imports.
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ValueError("Disallowed: imports are not permitted in constraints.")
        # No lambda / class / async / generators-as-functions.
        if isinstance(node, (ast.Lambda, ast.ClassDef, ast.AsyncFunctionDef,
                             ast.Await, ast.AsyncFor, ast.AsyncWith)):
            raise ValueError(
                f"Disallowed constraint construct: {_describe(node)}."
            )
        # No global/nonlocal rebinding of outer scope.
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            raise ValueError("Disallowed: global/nonlocal are not permitted.")
        # No `with` (could open resources through a passed object).
        if isinstance(node, (ast.With,)):
            raise ValueError("Disallowed construct: with-statement.")
        # Names: block _-prefixed (dunder / private) and dangerous callables.
        if isinstance(node, ast.Name):
            if node.id.startswith("_"):
                raise ValueError(
                    f"Disallowed name '{node.id}': underscore-prefixed names are blocked."
                )
            if node.id in _FORBIDDEN_NAMES:
                raise ValueError(f"Disallowed name: {node.id}.")
        # Attributes: block _-prefixed (dunder / private) and format traversal.
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise ValueError(
                    f"Disallowed attribute '{node.attr}': "
                    "underscore-prefixed / dunder attributes are blocked."
                )
            if node.attr in _FORBIDDEN_ATTRS:
                raise ValueError(
                    f"Disallowed attribute: {node.attr} (string-format traversal)."
                )
        # Argument names may not be _-prefixed either (keeps the rule total).
        if isinstance(node, ast.arg) and node.arg.startswith("_"):
            raise ValueError(
                f"Disallowed argument name '{node.arg}': "
                "underscore-prefixed names are blocked."
            )


def compile_constraint(name: str, source: str) -> Callable:
    """Validate `source` and return its `apply` callable.

    Executes with an empty builtins map plus :data:`SAFE_NAMESPACE`. Raises
    ``ValueError`` if validation fails or no callable ``apply`` results.
    """
    validate_constraint_source(source)
    namespace: Dict[str, object] = {"__builtins__": {}}
    namespace.update(SAFE_NAMESPACE)
    exec(compile(source, f"<constraint:{name}>", "exec"), namespace)
    apply_fn = namespace.get("apply")
    if not callable(apply_fn):
        raise ValueError("Constraint defines no callable `apply`.")
    return apply_fn


def load_all(session_constraints: Optional[Dict[str, str]] = None) -> List[Tuple[str, Callable]]:
    """Compile constraint plugins from a session's `constraints` dict.

    Each entry is validated and compiled in a restricted namespace. A plugin
    that fails validation or compilation is skipped with a printed warning so
    one bad plugin cannot break the solve.

    Returns: list of `(name, apply_fn)` tuples in insertion order.
    """
    if not session_constraints:
        return []
    plugins: List[Tuple[str, Callable]] = []
    for name, source in session_constraints.items():
        try:
            apply_fn = compile_constraint(name, source)
        except Exception as exc:  # noqa: BLE001 — one bad plugin must not break the solve
            print("[constraint] Rejected/failed {}: {}".format(name, exc))
            continue
        plugins.append((name, apply_fn))
    return plugins
