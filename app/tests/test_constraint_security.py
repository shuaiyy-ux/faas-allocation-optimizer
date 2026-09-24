"""Security + functional tests for the sandboxed constraint loader.

The chat agent can draft constraint plugins (small PuLP-constraint functions)
that the user approves. For the public demo they are NOT trusted: every plugin
passes an AST whitelist (constraints.validate_constraint_source) and runs with
an empty builtins map. These tests prove:

- a spread of malicious payloads are rejected (imports, dunder/private access,
  the dangerous callables, lambda, format-string traversal, comprehension
  escapes);
- legitimate constraints (the shapes the agent/tests actually produce) still
  validate, compile, and take effect in the solver.
"""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine  # noqa: E402
from constraints import (  # noqa: E402
    compile_constraint,
    load_all,
    validate_constraint_source,
)
from state import SessionState  # noqa: E402


# ── Malicious payloads (>= 8 distinct) — all must be rejected ──────────

MALICIOUS = {
    "top_level_import": "import os\ndef apply(model, x, veh, dealer, **ctx):\n    return True\n",
    "dunder_import_call": (
        "def apply(model, x, veh, dealer, **ctx):\n"
        "    return __import__('os').system('id')\n"
    ),
    "open_file": (
        "def apply(model, x, veh, dealer, **ctx):\n"
        "    return open('/etc/passwd').read()\n"
    ),
    "eval_call": "def apply(model, x, veh, dealer, **ctx):\n    return eval('1+1')\n",
    "exec_call": "def apply(model, x, veh, dealer, **ctx):\n    exec('y=1')\n    return y\n",
    "getattr_call": (
        "def apply(model, x, veh, dealer, **ctx):\n"
        "    return getattr(model, 'objective')\n"
    ),
    "globals_call": (
        "def apply(model, x, veh, dealer, **ctx):\n"
        "    globals()['pwn'] = 1\n"
        "    return True\n"
    ),
    "dunder_attr_bases": (
        "def apply(model, x, veh, dealer, **ctx):\n"
        "    return ().__class__.__bases__\n"
    ),
    "comprehension_subclasses": (
        "def apply(model, x, veh, dealer, **ctx):\n"
        "    return [c for c in ().__class__.__base__.__subclasses__()]\n"
    ),
    "format_traversal": (
        "def apply(model, x, veh, dealer, **ctx):\n"
        "    return '{0.__class__}'.format(model)\n"
    ),
    "lambda_inside": (
        "def apply(model, x, veh, dealer, **ctx):\n"
        "    f = lambda: 0\n"
        "    return f()\n"
    ),
    "private_attr": (
        "def apply(model, x, veh, dealer, **ctx):\n"
        "    return model._problem\n"
    ),
}


@pytest.mark.parametrize("name", sorted(MALICIOUS))
def test_malicious_constraint_rejected(name):
    with pytest.raises(ValueError):
        validate_constraint_source(MALICIOUS[name])


@pytest.mark.parametrize("name", sorted(MALICIOUS))
def test_malicious_constraint_never_compiles(name):
    with pytest.raises(ValueError):
        compile_constraint(name, MALICIOUS[name])


def test_missing_apply_is_rejected():
    with pytest.raises(ValueError, match="apply"):
        validate_constraint_source("x = 1\n")


def test_load_all_skips_malicious_and_keeps_legit():
    plugins = load_all({
        "bad": MALICIOUS["dunder_import_call"],
        "good": "def apply(model, x, veh, dealer, **ctx):\n    return True\n",
    })
    names = [n for n, _ in plugins]
    assert names == ["good"]


# ── Legitimate constraints — must validate + compile ──────────────────

LEGIT = {
    "trivial": "def apply(model, x, veh, dealer, **ctx):\n    return True\n",
    "ban_dealer": (
        "def apply(model, x, veh, dealer, *, stage, rem_cap, source_to_dealers, arc_dist):\n"
        "    for key, var in x.items():\n"
        "        vin, code = key\n"
        "        if code == 'FD15':\n"
        "            model += var == 0\n"
    ),
    "cap_with_lpsum": (
        "def apply(model, x, veh, dealer, *, stage, rem_cap, source_to_dealers, arc_dist):\n"
        "    for code in rem_cap:\n"
        "        here = [x[key] for key in x if key[1] == code]\n"
        "        if here:\n"
        "            model += lpSum(here) <= 3\n"
    ),
}


@pytest.mark.parametrize("name", sorted(LEGIT))
def test_legit_constraint_validates_and_compiles(name):
    validate_constraint_source(LEGIT[name])  # no raise
    fn = compile_constraint(name, LEGIT[name])
    assert callable(fn)


def _session_with(constraints):
    sess = SessionState(
        client_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        fleet_df=engine.load_baseline_fleet(),
    )
    sess.constraints = dict(constraints)
    return sess


def test_legit_constraint_takes_effect_in_solver():
    """A constraint forcing every assignment to zero must drive n_assigned to 0,
    proving the compiled plugin actually runs inside the ILP."""
    baseline = engine.solve_weekly_batch(n_batch=8, seed=3, session=_session_with({}))
    assert baseline["n_assigned"] > 0  # control: some cars get placed

    block_all = (
        "def apply(model, x, veh, dealer, *, stage, rem_cap, source_to_dealers, arc_dist):\n"
        "    if stage == 1:\n"
        "        for key, var in x.items():\n"
        "            model += var == 0\n"
    )
    constrained = engine.solve_weekly_batch(
        n_batch=8, seed=3, session=_session_with({"block_all": block_all}),
    )
    assert constrained["n_assigned"] == 0


def test_malicious_constraint_is_inert_in_solver():
    """A malicious plugin is skipped, so the solve behaves as if unconstrained."""
    baseline = engine.solve_weekly_batch(n_batch=8, seed=3, session=_session_with({}))
    with_bad = engine.solve_weekly_batch(
        n_batch=8, seed=3,
        session=_session_with({"bad": MALICIOUS["open_file"]}),
    )
    assert with_bad["n_assigned"] == baseline["n_assigned"]
