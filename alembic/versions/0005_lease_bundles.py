"""lease bundles: dual-site coordinated uplink (lease_bundles, leases.bundle_id)

Revision ID: 0005_lease_bundles
Revises: 0004_control_generation
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_lease_bundles"
down_revision: str | None = "0004_control_generation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # One row per accepted dual-site bundle application. The row doubles as
    # the idempotency record for POST /lease-bundles: the key is unique and
    # request_params stores the canonical parameter fingerprint, exactly like
    # idempotency_keys does for single-antenna acquisition. Bundle keys are
    # stored apart from the acquisition/renewal key tables, so the three
    # operations deduplicate independently.
    op.create_table(
        "lease_bundles",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("controller", sa.String(length=128), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        # Both member leases share these exact instants: the granting
        # transaction samples clock_timestamp() once and derives both rows
        # (and this record) from that single reading.
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_params", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.CheckConstraint(
            "expires_at > acquired_at",
            name="lease_bundles_expires_after_acquire",
        ),
        # Same inclusive [5, 120] bound as a single acquisition, enforced in
        # the database as well as at the HTTP/service boundary.
        sa.CheckConstraint(
            "duration_seconds >= 5 AND duration_seconds <= 120",
            name="lease_bundles_duration_range",
        ),
    )
    op.create_index(
        "uq_lease_bundles_idempotency_key",
        "lease_bundles",
        ["idempotency_key"],
        unique=True,
    )

    # Association between a bundle application and its leases: NULL for
    # ordinary single-antenna leases, the bundle id for the two member leases
    # granted together. Existing leases are untouched (NULL); RESTRICT keeps
    # the audit trail intact. Member rows stay ordinary leases in every other
    # respect, so the token query interface needs no change.
    op.add_column(
        "leases",
        sa.Column(
            "bundle_id",
            sa.BigInteger(),
            sa.ForeignKey("lease_bundles.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    # Replay of a bundle request looks the members up by bundle_id.
    op.create_index("ix_leases_bundle", "leases", ["bundle_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_leases_bundle", table_name="leases")
    op.drop_column("leases", "bundle_id")
    op.drop_index(
        "uq_lease_bundles_idempotency_key", table_name="lease_bundles"
    )
    op.drop_table("lease_bundles")
