# FROM ghcr.io/astral-sh/uv:trixie-slim
FROM python:3.13-slim-trixie

RUN apt-get update && apt-get install -y \
    bash \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install astral.sh/uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install stellvio globally using uv
# RUN uv python install 3.13
RUN uv pip install --system stelvio
# Verify and install pulumi
# RUN uv run stlv version
# Make sure pulumi is installed
# RUN stlv system

ENTRYPOINT ["stlv"]
CMD ["version"]