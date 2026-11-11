# syntax=docker/dockerfile:1

FROM python:3.14.10-alpine AS base

# The bot plays audio through ffmpeg.
RUN apk add --no-cache ffmpeg

# Don't write .pyc files.
ENV PYTHONDONTWRITEBYTECODE=1

# Don't buffer stdout and stderr, so logs show up straight away and aren't
# lost if the bot crashes.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Unprivileged user the bot runs as.
ARG UID=10001
RUN adduser \
    --disabled-password \
    --gecos "" \
    --home "/nonexistent" \
    --shell "/sbin/nologin" \
    --no-create-home \
    --uid "${UID}" \
    appuser

# Install dependencies in their own layer, so it is reused until they change.
# requirements.txt is not in the repository: it is exported from poetry.lock
# before the build (see README.Docker.md).
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=bind,source=requirements.txt,target=requirements.txt \
    python -m pip install -r requirements.txt

USER appuser

# .dockerignore decides what this copies.
COPY . .

# Read settings from JELLYCHORD_* environment variables instead of config.yml.
ENV JELLYCHORD_USE_CONF_FILE=0

ENTRYPOINT ["python3", "main.py"]
