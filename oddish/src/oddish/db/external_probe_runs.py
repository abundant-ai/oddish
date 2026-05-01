"""ORM model for probe runs forwarded to the agent-sandbox-service.

Each row records one probe submission that was routed externally via
``AgentSandboxClient.submit_probe`` instead of creating a local ``TrialModel``.
The row is written at submission time; live status/results are fetched on demand
from the service via ``AgentSandboxClient.get_probe``.
"""

from __future__ import annotations

from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column

from oddish.db.models import Base, generate_id, utcnow
from datetime import datetime
from sqlalchemy import DateTime


class ExternalProbeRun(Base):
    """Tracks a probe run that was forwarded to the agent-sandbox-service.

    ``id`` is the ``probe_run_id`` returned by the service (not locally
    generated) so it can be used directly for ``get_probe`` lookups.
    ``created_at`` and ``updated_at`` are inherited from ``Base``.
    """

    __tablename__ = "external_probe_runs"
    __table_args__ = (
        Index("idx_external_probe_runs_org_id", "org_id"),
        Index("idx_external_probe_runs_task_id", "task_id"),
    )

    # Override Base.id: value comes from the service, not locally generated.
    id: Mapped[str] = mapped_column(String(128), primary_key=True)

    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
