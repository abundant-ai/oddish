"""Merge experiment-model rename and trajectory-summary heads.

The retired ``analysis_log`` column stays through this deployment so old app
instances remain compatible; a later child migration can drop it.

Revision ID: analysislog02
Revises: expmodelrename01, trajsum_002
Create Date: 2026-08-20 15:20:00.000000
"""

from typing import Sequence, Union


revision: str = "analysislog02"
down_revision: Union[str, Sequence[str], None] = (
    "expmodelrename01",
    "trajsum_002",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
