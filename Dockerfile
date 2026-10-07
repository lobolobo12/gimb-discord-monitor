FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY bot.py ./

RUN useradd --uid 10001 --no-create-home app && mkdir -p data && chown app data
USER app
VOLUME /app/data
CMD ["/app/.venv/bin/python", "bot.py", "run"]
