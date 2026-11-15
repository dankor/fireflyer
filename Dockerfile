# Two images from one file:
#   portal (the default: `docker build .`) — what a deployment runs: the portal
#          and its drivers, no test tools, a user that isn't root, and
#          `python -m fireflyer.portal` (one process, no reloader).
#   dev    (docker compose builds it: `target: dev`) — the test tools too; the
#          compose mounts the source over it and reloads on a change.
FROM python:3.13-slim AS base

WORKDIR /app
ENV PYTHONUNBUFFERED=1

# The package and what pyproject's metadata names (LICENSE, README.md). The
# install is editable in both images: the app reads the starter dashboard from
# demo/ beside the package, which a site-packages install would leave behind.
COPY pyproject.toml LICENSE README.md ./
COPY fireflyer ./fireflyer
COPY demo ./demo

EXPOSE 8000


FROM base AS dev

RUN pip install --no-cache-dir -e ".[test,portal]"

# Bind 0.0.0.0 so the editor is reachable from the host. (The local
# `python -m fireflyer.web` entrypoint stays on 127.0.0.1.)
CMD ["uvicorn", "fireflyer.web.app:app", "--host", "0.0.0.0", "--port", "8000"]


FROM base AS portal

# The code stays root's and read-only to the app; it writes only data/ (a
# local dataset folder or the made-up certificate, when those are used) and
# /tmp (inline datasets' Parquet).
RUN pip install --no-cache-dir -e ".[portal]" \
    && useradd --system --uid 10001 --no-create-home --home-dir /app fireflyer \
    && mkdir -p /app/data && chown fireflyer /app/data
USER 10001

CMD ["python", "-m", "fireflyer.portal"]
