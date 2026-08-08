# KitchenSense

An autonomous agent that reduces household food waste. Reads a photographed
grocery receipt, predicts which items are likely to be wasted, and sends one
message with a recipe before they are.

## Running locally

    pip install -e ".[dev]"
    uvicorn kitchensense.main:app --reload

Then open http://localhost:8000/health

## Project structure

    src/kitchensense/api/       HTTP endpoints
    src/kitchensense/domain/    pure business logic, no I/O
    src/kitchensense/models/    data models
    docs/diagrams/              process flow figures
    docs/decisions/             technical decision records
    infra/                      Azure infrastructure as code