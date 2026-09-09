FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir uv==0.11.29
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY packages ./packages
RUN uv sync --frozen --no-dev --link-mode copy
COPY migrations ./migrations
COPY alembic.ini ./
COPY scenarios ./scenarios
RUN useradd --create-home --uid 10001 lab
USER lab
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 LOG_FORMAT=json
CMD ["uvicorn", "commander.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
