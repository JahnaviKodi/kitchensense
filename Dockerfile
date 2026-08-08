FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .
COPY src/ ./src/
ENV PYTHONPATH=/app/src
CMD ["uvicorn", "kitchensense.main:app", "--host", "0.0.0.0", "--port", "8000"]