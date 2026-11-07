# syntax=docker/dockerfile:1

FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv

# Build against the base image's interpreter so the venv is valid in the runtime stage.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, so source edits don't invalidate this layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.13-slim

# The slim base ships no zoneinfo database; scheduling and log timestamps need Eastern time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# Fixed UID/GID 999, so the host can `chown 999:999` the data directory before first run.
RUN groupadd --system --gid 999 app \
    && useradd --system --uid 999 --gid app --no-create-home app \
    && mkdir /data \
    && chown app:app /data

WORKDIR /app

COPY --from=builder /app/.venv ./.venv
COPY --from=builder /app/src ./src

# The watchlist and the team crosswalk are baked in and edited by PR (spec §7.1, §7.5).
# `teams.CROSSWALK_PATH` resolves to /app/teams_crosswalk.yaml through the editable install of /app/src.
COPY watchlist.yaml teams_crosswalk.yaml ./

# The commit the image was built from, recorded on every run. CI passes it; local builds leave it empty.
ARG IMAGE_SHA=""

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    TZ=America/New_York \
    DATA_DIR=/data \
    IMAGE_SHA=${IMAGE_SHA}

USER app

VOLUME /data

# `health` fails once the serve heartbeat is over 8 days old (spec §9.1); the start period covers
# the first loop tick at boot, which writes the heartbeat on a fresh volume.
HEALTHCHECK --interval=5m --start-period=1m CMD ["cbb-digest", "health"]

ENTRYPOINT ["cbb-digest"]
CMD ["serve"]
