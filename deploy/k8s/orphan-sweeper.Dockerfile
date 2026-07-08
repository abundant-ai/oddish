# Minimal image for the orphaned trial-pod sweeper. Deliberately independent
# of the Oddish worker image and harbor[gke]: just Python plus a Kubernetes
# client and a Postgres driver, so the backstop keeps working even when the
# worker image or dispatch is broken. The single self-contained script talks
# to the in-cluster Kubernetes API and a read-only Postgres.
#
#   docker build -f deploy/k8s/orphan-sweeper.Dockerfile \
#     -t <REGISTRY>/oddish-orphan-sweeper:latest .
FROM python:3.13-slim

# Pinned, tiny dependency set. kubernetes: in-cluster API access; psycopg:
# read-only worker_jobs/trials reads.
RUN pip install --no-cache-dir "kubernetes==31.0.0" "psycopg[binary]==3.2.3"

WORKDIR /app
COPY deploy/k8s/gke_orphan_sweeper.py /app/gke_orphan_sweeper.py

# Run as a non-root user (matches the CronJob's runAsNonRoot).
RUN useradd --create-home --uid 10001 sweeper
USER sweeper

ENTRYPOINT ["python", "/app/gke_orphan_sweeper.py"]
