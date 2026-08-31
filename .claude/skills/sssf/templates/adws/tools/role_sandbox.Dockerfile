FROM oven/bun:1.4.0-debian@sha256:5bb0f9be3a1a36a03e27c9a9dd894a3b1ad26657155c7df4dda771e17bf872ef AS bun

FROM ghcr.io/astral-sh/uv:0.8.22-python3.13-bookworm-slim@sha256:c4a67221d74ad160ddf4e114804bda0f8dd2d2e1aa5c16e0817cf8530ff8f5f6

COPY --from=bun /usr/local/bin/bun /usr/local/bin/bun

RUN apt-get update \
    && apt-get install --yes --no-install-recommends bash ca-certificates coreutils diffutils findutils grep procps sed \
    && rm -rf /var/lib/apt/lists/*

ENV UV_NO_PROGRESS=1
