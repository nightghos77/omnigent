# Thin overlay on the official Omnigent server image.
#
# Why this exists: the Modal `sandbox:` block is read only from a YAML config
# file (no env-var equivalent). To keep the whole deploy source-controlled we
# bake deploy/render/server-config.yaml into the image and point the server at
# it. The web UI bundle, Python runtime, entrypoint, and CMD are all inherited
# from the base image — this is an overlay, NOT a from-source build, so nothing
# is lost.
#
# Base pinned to an immutable digest for reproducible builds. Bump = a tracked
# commit that updates this digest (never silent :latest drift).
#   :latest resolved 2026-07-02 -> sha256:dabbb00d...959daf
#   Refresh: ghcr.io token -> HEAD /v2/omnigent-ai/omnigent-server/manifests/latest,
#            copy the Docker-Content-Digest header.
FROM ghcr.io/omnigent-ai/omnigent-server@sha256:dabbb00deafa796c5ab75a31785285f78044df812db88d7c7c6acdee6c959daf

# The official server image is external-runner-only and does NOT bundle the
# Modal SDK. Provisioning managed Modal sandboxes needs it in the server env
# (the `omnigent[modal]` extra == modal>=1.0,<2). pip on PATH targets the
# baked venv at /opt/venv, so this lands in the same interpreter the server runs.
RUN pip install --no-cache-dir "modal>=1.0,<2"

# /app is baked into the image (the server's WORKDIR); /data is the runtime disk
# mount, so config MUST live outside /data or the mount would shadow it.
COPY deploy/render/server-config.yaml /app/config.yaml
ENV OMNIGENT_CONFIG=/app/config.yaml
