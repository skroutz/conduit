# syntax=docker/dockerfile:1
#
# Conduit MCP Server — hardened, multi-arch runtime image.
#
# Multi-stage build:
#   * builder  — full Python toolchain (pip, compilers) to install the package
#                and its dependencies. Discarded; never shipped.
#   * runtime  — Google distroless: no shell, no package manager, no apt.
#                Runs as the unprivileged "nonroot" user (UID 65532).
#
# The builder's Python minor version MUST match the distroless runtime's
# (Debian 13 distroless ships CPython 3.13) so that compiled wheels such as
# pydantic-core (a fastmcp dependency) carry a matching ABI tag (cp313).

# ---------------------------------------------------------------------------
# Stage 1: builder
# ---------------------------------------------------------------------------
# python:3.13-slim (digest pinned for reproducibility / supply-chain integrity)
FROM python@sha256:c33f0bc4364a6881bed1ec0cc2665e6c53c87a43e774aaeab88e6f17af105e4f AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /src

# Copy only what is needed to build the package (keeps layers small and the
# build cache stable). Source is copied after metadata so dependency installs
# can be cached across code-only changes.
COPY pyproject.toml README.md LICENSE ./
COPY run.py ./
COPY conduit ./conduit

# Install the package together with its runtime dependencies (no dev/test
# extras) into a self-contained prefix. We use --target rather than a venv:
# a venv's bin/python symlinks to this builder's interpreter, which does not
# exist in the distroless runtime; --target copies only site-packages, which
# the runtime's own interpreter loads via PYTHONPATH.
RUN python -m pip install --upgrade pip \
 && python -m pip install --target=/install .

# ---------------------------------------------------------------------------
# Stage 2: runtime (distroless, non-root)
# ---------------------------------------------------------------------------
# gcr.io/distroless/python3-debian13:nonroot (digest pinned)
FROM gcr.io/distroless/python3-debian13@sha256:886011e8c25a19ab38fa6d929e06b47d4fd3661d1dff61c6b4ebb4becd030c37

# OCI image metadata (populated at build time by the CD pipeline).
ARG VERSION=dev
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="conduit-mcp" \
      org.opencontainers.image.description="MCP server for Phabricator and Phorge" \
      org.opencontainers.image.source="https://github.com/skroutz/conduit" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"

ENV PYTHONPATH=/install \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Bring in the installed package + dependencies from the builder, plus the
# thin run.py launcher (invoked as a plain script to avoid the re-import
# RuntimeWarning that `python -m conduit.conduit` triggers).
COPY --from=builder /install /install
COPY --from=builder /src/run.py /app/run.py

# Drop to the unprivileged distroless user (UID 65532). Defence in depth:
# even though the container should be run with --read-only, --cap-drop ALL and
# --security-opt no-new-privileges, we never run as root inside the image.
USER nonroot

EXPOSE 8000

# Liveness probe without a shell or curl (neither exists in distroless): open a
# TCP connection to the listening port using the bundled Python interpreter.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python3", "-c", "import socket,sys; s=socket.create_connection(('127.0.0.1',8000),3); s.close()"]

# Default to the modern streamable-HTTP transport. Binding to 0.0.0.0 is
# correct inside a container — the container boundary is the trust boundary,
# and the port is only reachable via an explicit `-p`/published port. Override
# the CMD (e.g. `--transport stdio`) for single-user/embedded usage.
ENTRYPOINT ["python3", "/app/run.py"]
CMD ["--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
