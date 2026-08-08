# 001 — Python and FastAPI for the backend

## Decision
Python 3.12 with FastAPI.

## Reasoning
Azure AI SDKs have the strongest support in Python. FastAPI is async by
default and validates requests automatically through Pydantic.

## Alternatives rejected
- Node.js — weaker Azure AI SDK coverage
- Django — heavier than this project needs