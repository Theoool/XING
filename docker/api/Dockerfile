ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------- 精简运行时基座
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONPATH=/app/src \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        curl ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------- 编译构建基座
FROM runtime AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /uvx /bin/

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------- 依赖层
FROM builder AS deps
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project

FROM builder AS deps-prod
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-default-groups

# ---------------------------------------------------------------- 开发镜像
FROM builder AS dev
COPY --from=deps /opt/venv /opt/venv
COPY docker/api/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh
COPY . .
EXPOSE 8000
ENTRYPOINT ["tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "xing.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--reload", "--reload-dir", "/app/src", "--log-level", "info"]

# ---------------------------------------------------------------- 生产镜像
FROM runtime AS prod
COPY --from=deps-prod /opt/venv /opt/venv
COPY docker/api/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin appuser
COPY --chown=1000:1000 src ./src
COPY --chown=1000:1000 alembic ./alembic
COPY --chown=1000:1000 alembic.ini pyproject.toml README.md ./
USER appuser
EXPOSE 8000
ENTRYPOINT ["tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "xing.main:app", "--host", "0.0.0.0", "--port", "8000"]
