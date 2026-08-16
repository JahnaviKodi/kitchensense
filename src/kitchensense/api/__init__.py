"""HTTP endpoints.

Routers here stay thin: validate, call a repository, map the result to a
response model. Anything that decides something belongs in ``domain/``, and
anything that talks to the database belongs in ``repositories/``.
"""
