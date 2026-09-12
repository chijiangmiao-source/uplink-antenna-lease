"""lease control generation: per-antenna monotonically increasing generation

Revision ID: 0004_control_generation
Revises: 0003_lease_renew
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_control_generation"
down_revision: str | None = "0003_lease_renew"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Per-lease control generation. It is added nullable ONLY so existing rows
    # can be backfilled in place; the column is made NOT NULL in the same
    # migration afterwards (a NOT NULL column without a default cannot be
    # added to a non-empty table on older PostgreSQL).
    op.add_column(
        "leases",
        sa.Column("control_generation", sa.BigInteger(), nullable=True),
    )

    # Backfill generations from historical acquisition order: dense 1..N per
    # antenna, ordered by acquisition time with the lease record number as the
    # deterministic tie-breaker for same-instant grants. This makes historical
    # token queries return stable generations after the upgrade and leaves the
    # first new grant on every antenna exactly one above its last committed
    # lease.
    op.execute(
        """
        UPDATE leases AS l
        SET control_generation = ranked.generation
        FROM (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY antenna_id
                       ORDER BY acquired_at, id
                   ) AS generation
            FROM leases
        ) AS ranked
        WHERE l.id = ranked.id
        """
    )
    op.alter_column(
        "leases",
        "control_generation",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.create_check_constraint(
        "leases_control_generation_positive",
        "leases",
        "control_generation >= 1",
    )

    # The antenna's last allocated generation: the high-water mark the
    # acquisition transaction increments under the antenna's row lock. NULL
    # only for antennas that have never had a lease; backfill it from the
    # lease history so generations continue seamlessly after the upgrade.
    op.add_column(
        "antennas",
        sa.Column(
            "last_control_generation", sa.BigInteger(), nullable=True
        ),
    )
    op.execute(
        """
        UPDATE antennas AS a
        SET last_control_generation = history.last_generation
        FROM (
            SELECT antenna_id, max(control_generation) AS last_generation
            FROM leases
            GROUP BY antenna_id
        ) AS history
        WHERE a.id = history.antenna_id
        """
    )
    op.create_check_constraint(
        "antennas_last_control_generation_positive",
        "antennas",
        "last_control_generation IS NULL "
        "OR last_control_generation >= 1",
    )


def downgrade() -> None:
    op.drop_constraint(
        "antennas_last_control_generation_positive", "antennas"
    )
    op.drop_column("antennas", "last_control_generation")
    op.drop_constraint("leases_control_generation_positive", "leases")
    op.drop_column("leases", "control_generation")
