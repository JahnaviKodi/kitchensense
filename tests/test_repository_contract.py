"""Cross-tenant reads are impossible by construction — checked by construction.

The repositories are the only door onto the kitchen record, and the rule is
that ``household_id`` is a required keyword argument on every method. A rule
in a docstring lasts until the first hurried pull request, so it is asserted
here instead: add a method that takes household from ambient state, or accepts
it positionally where it could be swapped with another UUID, and this fails.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest

from kitchensense.repositories import inventory as inventory_repositories
from kitchensense.repositories.inventory import (
    InventoryEventRepository,
    InventorySnapshotRepository,
)

REPOSITORIES = [InventoryEventRepository, InventorySnapshotRepository]

SOURCE = Path(inventory_repositories.__file__).read_text(encoding="utf-8")


def public_methods(cls: type) -> list[tuple[str, Any]]:
    return [
        (name, member)
        for name, member in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]


def _cases() -> list[tuple[type, str, Any]]:
    return [
        (cls, name, member) for cls in REPOSITORIES for name, member in public_methods(cls)
    ]


CASES = _cases()
IDS = [f"{cls.__name__}.{name}" for cls, name, _ in CASES]


def test_the_repositories_expose_methods_to_check() -> None:
    assert len(CASES) >= 10, "introspection found suspiciously few methods to check"


@pytest.mark.parametrize(("cls", "name", "method"), CASES, ids=IDS)
def test_household_id_is_a_required_keyword_argument(
    cls: type, name: str, method: Any
) -> None:
    parameter = inspect.signature(method).parameters.get("household_id")

    assert parameter is not None, f"{cls.__name__}.{name} does not take a household_id"
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, (
        f"{cls.__name__}.{name} takes household_id positionally; it must be "
        "keyword-only so it cannot be confused with another UUID at the call site"
    )
    assert parameter.default is inspect.Parameter.empty, (
        f"{cls.__name__}.{name} defaults household_id; a default is an ambient "
        "tenant, which is the thing this design exists to prevent"
    )


@pytest.mark.parametrize(("cls", "name", "method"), CASES, ids=IDS)
def test_no_public_method_takes_positional_arguments(
    cls: type, name: str, method: Any
) -> None:
    """Everything after ``self`` is keyword-only, so call sites read as prose."""
    positional = [
        parameter.name
        for parameter in inspect.signature(method).parameters.values()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        and parameter.name != "self"
    ]

    assert not positional, f"{cls.__name__}.{name} accepts {positional} positionally"


def test_every_select_of_a_table_goes_through_the_scoping_helper() -> None:
    """The household filter cannot be forgotten if there is one place to put it.

    Requiring the argument only guarantees it was *passed*. This checks it is
    also *used*: no method builds its own ``select`` and quietly omits the
    predicate.
    """
    tree = ast.parse(SOURCE)
    unscoped: list[str] = []

    for definition in ast.walk(tree):
        if not isinstance(definition, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if definition.name == "_scoped":
            continue
        for node in ast.walk(definition):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "select"
            ):
                unscoped.append(definition.name)

    assert not unscoped, (
        f"{sorted(set(unscoped))} build a select() outside _scoped(); compose onto "
        "_scoped() instead so the household predicate is always applied"
    )
