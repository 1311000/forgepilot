# ForgePilot, in one image that can be scanned, pinned and stopped.
#
# Two stages, because the build toolchain has no business in the runtime image:
# the first installs the package (and its `web` extra) into wheels, the second
# installs *only* those wheels. That also makes the dependency set that reaches
# production exactly the set the build resolved, rather than whatever pip finds
# on the day the image is rebuilt.
#
# ⚠️ `python:3.12-slim` is pinned by **minor** version on purpose, not by
# digest. A digest pin is the right answer for a release branch; it is the wrong
# answer for this repository, because a digest that nobody updates is a silent
# five-year-old interpreter. A deployment that wants reproducibility should pin
# the digest at the point it starts publishing images.

# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /src
# Only what the build reads: the metadata and the package. Copying `tests/` here
# would put them in a layer that the runtime stage never uses.
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip wheel --wheel-dir /wheels ".[web]"


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Durable state — the serve lock, watermarks, and anything else this
    # installation is told to keep — lives on the mounted volume, not in the
    # container's writable layer. A container that loses its lock file on
    # restart is a container that can start twice.
    FORGEPILOT_HOME=/data

# ripgrep is a *runtime* dependency of the default retriever: `code_search` shells
# out to the real `rg`, and `providers/retrieve/grep.py` deliberately has no
# Python fallback (a second scanner would be a different search with different
# glob and ignore semantics). Without this the image boots and every search fails
# at the first tool call, which is exactly the kind of "works until it is used"
# gap this repository keeps a table of.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ripgrep \
    && rm -rf /var/lib/apt/lists/*

# The sandbox runs commands, and a container that runs them as root is a
# container where "the command escaped the workspace" is the only boundary left.
RUN groupadd --gid 10001 forgepilot \
    && useradd --uid 10001 --gid forgepilot --create-home --home-dir /home/forgepilot forgepilot

COPY --from=build /wheels /wheels
RUN python -m pip install --no-index --find-links /wheels "forgepilot[web]" \
    && rm -rf /wheels

# The profiles are data, not code: `--config-dir` reads them, and they are what
# `--dump-config`/`--boot` report. Shipping them in the image is what makes the
# container's composition reviewable without mounting the source tree.
COPY configs /app/configs

WORKDIR /app
RUN mkdir -p /data /workspace && chown -R forgepilot:forgepilot /data /workspace
USER forgepilot

EXPOSE 8000

# A healthcheck that asks the process, not the port: `--healthz` returns 200 only
# once the app is assembled, so a container that bound a socket but failed to
# build the registry is reported unhealthy rather than "starting" forever.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).read()"]

ENTRYPOINT ["python", "-m", "forgepilot", "--config-dir", "/app/configs"]
# `--workspace` has to be a mounted project directory: with none, chat still
# works but task mode refuses (409) — that refusal is the designed behaviour, not
# a missing default.
CMD ["--serve", "--host", "0.0.0.0", "--port", "8000", "--workspace", "/workspace"]
