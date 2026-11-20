FROM ghcr.io/astral-sh/uv:0.11.25@sha256:1e3808aa9023d0980e7c15b1fa7c1ac16ff35925780cf5c459858b2d693f01a9 AS uv

FROM node:24-bookworm-slim@sha256:dc26ae92c9fb15cf6b1d63e20f0944b2efe0f115b20e00be48fb23fceb061c54 AS node

FROM node AS frontend
WORKDIR /build
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN npm install --global pnpm@11.22.0 && pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm run build

FROM node AS egress-download
ADD --checksum=sha256:4e1e447449e3cc653dae956208588722b5c8fe8ce894f633adc756a851d1ec46 \
    https://github.com/stripe/smokescreen/releases/download/v0.1.0/smokescreen_v0.1.0_linux_amd64.tar.gz /tmp/smokescreen.tar.gz
RUN tar -xzf /tmp/smokescreen.tar.gz -C /tmp

FROM scratch AS detector-egress
COPY --from=egress-download /tmp/smokescreen_v0.1.0_linux_amd64/smokescreen /smokescreen
USER 65532:65532
EXPOSE 4750
ENTRYPOINT ["/smokescreen"]

FROM python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    UV_PYTHON_DOWNLOADS=never uv sync --locked --no-dev --no-install-project --no-cache
COPY src ./src
COPY --from=frontend /build/dist ./frontend/dist
RUN groupadd --gid 10001 signalwatch && useradd --uid 10001 --gid signalwatch --create-home signalwatch
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/src PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER signalwatch
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "signalwatch.main:app", "--host", "0.0.0.0", "--port", "8000"]
