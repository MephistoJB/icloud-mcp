FROM ghcr.io/astral-sh/uv:0.11.30 AS uv

FROM python:3.12.13-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ ./src/
COPY run.py README.md ./
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12.13-slim
ARG VERSION=dev
LABEL org.opencontainers.image.source="https://github.com/MephistoJB/icloud-mcp" \
      org.opencontainers.image.description="Hardened iCloud MCP server" \
      org.opencontainers.image.version="$VERSION"
RUN groupadd --system --gid 10001 icloud-mcp \
    && useradd --system --uid 10001 --gid icloud-mcp --home-dir /app --shell /usr/sbin/nologin icloud-mcp
WORKDIR /app
COPY --from=builder --chown=icloud-mcp:icloud-mcp /app/.venv /app/.venv
COPY --from=builder --chown=icloud-mcp:icloud-mcp /app/src /app/src
COPY --from=builder --chown=icloud-mcp:icloud-mcp /app/run.py /app/run.py
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8000
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8000')+'/health', timeout=3)"]
ENTRYPOINT ["python", "run.py"]
CMD ["--http"]
