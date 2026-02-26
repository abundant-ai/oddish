FROM python:3.12-slim

WORKDIR /app

# System deps:
# - git: required for installing harbor from a git URL in pyproject.toml
# - docker.io: for running Harbor trials in containers
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    docker.io \
  && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

# Install package with LLM support (anthropic, openai, google)
RUN pip install --no-cache-dir -e ".[llm]"

ENV PYTHONPATH=/app/src

# Default command: run API server
CMD ["python", "-m", "oddish.api"]
