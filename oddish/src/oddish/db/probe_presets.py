"""ORM model for probe presets — named templates used by the probe submit form.

Replaces the previous flat-file storage at ``probe-presets.json`` so presets
survive deploys and can be scoped per-org. Seed presets stay hardcoded in
the frontend (``probe-submit-form.tsx``) and merge with user-created rows
returned from this table.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from oddish.db.models import Base, utcnow


class ProbePreset(Base):
    """A user-created probe preset, scoped by org."""

    __tablename__ = "probe_presets"
    __table_args__ = (
        Index("idx_probe_presets_org_id", "org_id"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    agent: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    operator_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    result_focus: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluation_metric: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ratio_unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ratio_verb: Mapped[str | None] = mapped_column(String(64), nullable=True)
    include_prior_attempts: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    is_seed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
