FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app

# Install oddish library first (backend depends on it via local path)
COPY oddish/ /oddish/

# Copy backend source and lockfile
COPY backend/pyproject.toml backend/uv.lock ./

# Rewrite oddish source path for container layout
RUN sed -i 's|path = "../oddish"|path = "/oddish"|' pyproject.toml

RUN uv sync --frozen --no-dev

# Copy backend application code
COPY backend/ .

# Backend modules (api, auth, models, etc.) resolve from /app
ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "serve:api", "--host", "0.0.0.0", "--port", "8000"]
