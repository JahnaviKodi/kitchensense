"""Pure business logic. Nothing in this package performs I/O.

Modules here must not import sqlalchemy, fastapi or httpx. The import ban is
enforced by ``tests/test_domain_purity.py``.
"""
