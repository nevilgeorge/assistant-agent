FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
# Keep the host bundle flat: runtime paths remain stable across repository moves.
COPY deploy/production/compose.prod.yaml deploy/production/Caddyfile \
     deploy/production/apply.sh deploy/production/install-s3fs.sh \
     deploy/production/systemd/assistant-app.service \
     deploy/production/systemd/assistant-daily-logs.service \
     deploy/production/runtime/app-host.sh deploy/production/runtime/daily-logs-host.sh \
     deploy/production/runtime/recover-daily-logs.sh \
     deploy/production/runtime/daily-logs-probe.py ./deploy/
RUN pip install --no-cache-dir .
RUN useradd --uid 10001 --create-home appuser
USER appuser
EXPOSE 8000
CMD ["uvicorn", "assistant_agent.web:app", "--host", "0.0.0.0", "--port", "8000"]
