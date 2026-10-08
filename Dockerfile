# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml ./
COPY cloudcost ./cloudcost
# The optional CA secret is used only in managed/proxied build environments.
# It is not copied into the image. Normal GitHub runners use system trust.
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir '.[aws]'

FROM python:3.12-slim-bookworm AS runtime

ARG VERSION=0.2.0
LABEL org.opencontainers.image.title="CloudCost" \
      org.opencontainers.image.description="Monthly unbilled cloud cost monitor with SQLite and a read-only SPA" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/geekdada/cloudcost"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN groupadd --gid 10001 cloudcost && \
    useradd --uid 10001 --gid cloudcost --no-create-home --home-dir /data cloudcost && \
    mkdir /data && chown cloudcost:cloudcost /data
COPY --from=builder /opt/venv /opt/venv

WORKDIR /data
USER 10001:10001
EXPOSE 8765
VOLUME ["/data"]
ENTRYPOINT ["cloudcost"]
CMD ["serve", "-c", "/data/config.toml", "--host", "0.0.0.0", "--monitor"]
