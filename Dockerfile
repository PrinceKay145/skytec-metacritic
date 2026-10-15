FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.5.1 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

EXPOSE 8000
# hosts such as Render set $PORT; exec keeps uvicorn as PID 1 so it receives stop signals
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
