FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
CMD ["sleep", "infinity"]
