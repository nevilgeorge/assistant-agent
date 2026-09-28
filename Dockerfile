FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
COPY deploy/compose.prod.yaml deploy/Caddyfile ./deploy/
RUN pip install --no-cache-dir .
RUN useradd --uid 10001 --create-home appuser
USER appuser
EXPOSE 8000
CMD ["uvicorn", "assistant_agent.web:app", "--host", "0.0.0.0", "--port", "8000"]
