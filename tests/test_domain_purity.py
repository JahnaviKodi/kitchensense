"""The domain package stays pure, and this is what keeps it that way.

``domain/`` holds the fold that every feature and every training set is built
on. The moment it can reach a database or an HTTP client, testing it requires
one, and the temptation to "just fetch the missing bit here" turns a pure
function into a query. Import bans are cheap to state and expensive to
violate by accident, so state them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

DOMAIN = Path(__file__).resolve().parents[1] / "src" / "kitchensense" / "domain"

FORBIDDEN_ROOTS = {
    "sqlalchemy",
    "alembic",
    "fastapi",
    "httpx",
    "requests",
    "psycopg",
    "asyncpg",
    "starlette",
    "pydantic",
}

DOMAIN_MODULES = sorted(DOMAIN.glob("*.py"))


def _imported_roots(source: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_there_are_domain_modules_to_check() -> None:
    assert DOMAIN_MODULES, "expected at least one module in src/kitchensense/domain"


@pytest.mark.parametrize("module", DOMAIN_MODULES, ids=lambda path: path.name)
def test_domain_module_imports_nothing_that_does_io(module: Path) -> None:
    offenders = _imported_roots(module.read_text(encoding="utf-8")) & FORBIDDEN_ROOTS

    assert not offenders, (
        f"{module.name} imports {sorted(offenders)}; domain/ must stay free of I/O"
    )


@pytest.mark.parametrize("module", DOMAIN_MODULES, ids=lambda path: path.name)
def test_domain_module_does_not_reach_back_into_the_app(module: Path) -> None:
    """Nor may it depend on the layers built on top of it."""
    imported = {
        name
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.module
        for name in [node.module]
    }
    banned = {
        name
        for name in imported
        if name.startswith(("kitchensense.models", "kitchensense.repositories", "kitchensense.db"))
    }

    assert not banned, f"{module.name} imports {sorted(banned)}; the dependency points inwards"
