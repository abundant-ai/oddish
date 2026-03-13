"""
Oddish Cloud - Modal Worker

Scheduled functions that poll PGQueuer and execute Harbor trials.
Uses OSS oddish worker logic - wrapped with Modal scheduling.

Harbor Execution:
- Oddish Cloud runs on Modal, where Docker-in-Docker is not available.
- Only "modal" and "daytona" environments are allowed on cloud backend.

Pipeline stages:
- trial jobs: execute Harbor trial runs
- analysis jobs: classify trial outcomes + enqueue verdict when complete
- verdict jobs: synthesize trial analyses into a task verdict
"""

from .functions import poll_queue, process_single_job

__all__ = ["poll_queue", "process_single_job"]
