"""Control generation acceptance tests against REAL PostgreSQL + REAL API.

Every successful grant on an antenna carries a per-antenna, monotonically
increasing ``control_generation`` so that, after a network partition, a device
can reject commands issued by a stale controller. The suite proves:

* successive hand-overs produce strictly increasing generations, starting at 1;
* a same-key/same-params replay and the token query return the lease's fixed
  value byte-stably;
* concurrent contention commits exactly ONE new generation (the losers roll
  their counter increment back);
* busy / unknown-antenna / idempotency-conflict / input rejections consume no
  generation and change neither the antenna counter nor any lease data, so the
  first success after the failures is exactly one above the antenna's last
  committed lease;
* the historical backfill (migration 0004) orders generations deterministically
  by acquisition time with the lease record number as the tie-breaker.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from conftest import (
    KNOWN_ANTENNA,
    acquire,
    active_lease_count,
    count_rows,
    insert_expired_lease,
    make_key,
    parallel_acquire,
    release,
    renew,
)


def _antenna_generation(db_engine, antenna_id: str):
    with db_engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT last_control_generation FROM antennas WHERE id = :a"
            ),
            {"a": antenna_id},
        ).scalar_one()


def _lease_generation(db_engine, token: str) -> int:
    with db_engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT control_generation FROM leases WHERE token = :t"
            ),
            {"t": token},
        ).scalar_one()


def _all_generations(db_engine, antenna_id: str) -> list[int]:
    with db_engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    """
                    SELECT control_generation
                    FROM leases
                    WHERE antenna_id = :a
                    ORDER BY acquired_at, id
                    """
                ),
                {"a": antenna_id},
            ).scalars()
        )


# --------------------------------------------------------------------------
# Basic monotonicity
# --------------------------------------------------------------------------


def test_first_acquisition_starts_at_generation_one(http_client, db_engine):
    resp = acquire(http_client, antenna_id="ANT-01")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["control_generation"] == 1
    assert isinstance(body["control_generation"], int)
    assert _antenna_generation(db_engine, "ANT-01") == 1
    assert _lease_generation(db_engine, body["lease_token"]) == 1
    # An antenna that has never been leased keeps a NULL high-water mark.
    assert _antenna_generation(db_engine, "ANT-04") is None


def test_successive_handovers_are_strictly_increasing(http_client, db_engine):
    tokens = []
    for expected in range(1, 5):
        resp = acquire(
            http_client,
            antenna_id=KNOWN_ANTENNA,
            controller=f"controller-{expected}",
            duration_seconds=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["control_generation"] == expected, body
        tokens.append(body["lease_token"])
        # Release so the next grant is an immediate hand-over.
        if expected < 4:
            released = release(http_client, body["lease_token"])
            assert released.status_code == 200
            # The release response carries the same generation.
            assert (
                released.json()["control_generation"] == expected
            )

    assert len(set(tokens)) == 4
    assert _antenna_generation(db_engine, KNOWN_ANTENNA) == 4
    # Dense 1..N history, no gaps and no duplicates.
    gens = _all_generations(db_engine, KNOWN_ANTENNA)
    assert gens == [1, 2, 3, 4]


def test_generation_is_independent_per_antenna(http_client, db_engine):
    first_1 = acquire(http_client, antenna_id="ANT-01")
    first_2 = acquire(http_client, antenna_id="ANT-02")
    assert first_1.json()["control_generation"] == 1
    assert first_2.json()["control_generation"] == 1

    release(http_client, first_2.json()["lease_token"])
    second_2 = acquire(http_client, antenna_id="ANT-02")
    assert second_2.json()["control_generation"] == 2
    # The other antenna's high-water mark is untouched.
    assert _antenna_generation(db_engine, "ANT-01") == 1
    assert _lease_generation(
        db_engine, first_1.json()["lease_token"]
    ) == 1


def test_expired_handover_advances_by_exactly_one(http_client, db_engine):
    expired = insert_expired_lease(
        db_engine, antenna_id="ANT-03", age_seconds=2, ttl_seconds=10
    )
    assert expired["control_generation"] == 1
    assert _antenna_generation(db_engine, "ANT-03") == 1

    resp = acquire(http_client, antenna_id="ANT-03", controller="successor")
    assert resp.status_code == 200, resp.text
    assert resp.json()["control_generation"] == 2
    assert _antenna_generation(db_engine, "ANT-03") == 2
    # The expired row keeps its historical generation.
    assert _lease_generation(db_engine, expired["token"]) == 1


# --------------------------------------------------------------------------
# Replay / query stability
# --------------------------------------------------------------------------


def test_same_key_replay_and_token_query_return_fixed_generation(
    http_client, db_engine
):
    key = make_key()
    first = acquire(
        http_client,
        antenna_id="ANT-02",
        controller="replay-check",
        idempotency_key=key,
    )
    assert first.status_code == 200
    generation = first.json()["control_generation"]
    token = first.json()["lease_token"]

    for _ in range(3):
        replay = acquire(
            http_client,
            antenna_id="ANT-02",
            controller="replay-check",
            idempotency_key=key,
        )
        assert replay.status_code == 200
        rbody = replay.json()
        assert rbody["replay"] is True
        assert rbody["control_generation"] == generation
        assert rbody["lease_token"] == token

    status = http_client.get(f"/leases/{token}")
    assert status.status_code == 200
    assert status.json()["control_generation"] == generation

    # A later hand-over on the same antenna must not retroactively change the
    # replay or the historical query value.
    release(http_client, token)
    successor = acquire(
        http_client, antenna_id="ANT-02", controller="next"
    )
    assert successor.json()["control_generation"] == generation + 1
    replay = acquire(
        http_client,
        antenna_id="ANT-02",
        controller="replay-check",
        idempotency_key=key,
    )
    assert replay.json()["replay"] is True
    assert replay.json()["control_generation"] == generation
    old_status = http_client.get(f"/leases/{token}")
    assert old_status.json()["control_generation"] == generation


def test_concurrent_same_key_replay_allocates_one_generation(
    http_client, db_engine
):
    key = make_key()
    payload = {
        "antenna_id": KNOWN_ANTENNA,
        "controller": "partitioned-replay",
        "duration_seconds": 60,
        "idempotency_key": key,
    }
    responses = parallel_acquire(
        http_client, [dict(payload) for _ in range(8)]
    )
    assert all(r.status_code == 200 for r in responses)
    assert len({r.json()["lease_token"] for r in responses}) == 1
    assert {r.json()["control_generation"] for r in responses} == {1}
    assert _antenna_generation(db_engine, KNOWN_ANTENNA) == 1
    assert count_rows(db_engine, "SELECT count(*) FROM leases") == 1


# --------------------------------------------------------------------------
# Contention commits a single new generation
# --------------------------------------------------------------------------


def test_concurrent_handover_commits_exactly_one_new_generation(
    http_client, db_engine
):
    # Incumbent that has already expired, generation 1 committed.
    insert_expired_lease(
        db_engine, antenna_id=KNOWN_ANTENNA, age_seconds=1, ttl_seconds=10
    )
    assert _antenna_generation(db_engine, KNOWN_ANTENNA) == 1

    requests = [
        {
            "antenna_id": KNOWN_ANTENNA,
            "controller": f"racer-{make_key()}",
            "duration_seconds": 60,
            "idempotency_key": make_key(),
        }
        for _ in range(12)
    ]
    responses = parallel_acquire(http_client, requests)

    winners = [r for r in responses if r.status_code == 200]
    losers = [r for r in responses if r.status_code == 409]
    assert len(winners) == 1, [r.status_code for r in responses]
    assert len(losers) == 11
    for r in losers:
        assert r.json()["error"]["code"] == "ANTENNA_BUSY"

    winner = winners[0].json()
    assert winner["control_generation"] == 2
    assert winner["replay"] is False
    # Exactly one lease carries generation 2; the antenna high-water mark was
    # bumped once only.
    assert (
        count_rows(
            db_engine,
            "SELECT count(*) FROM leases WHERE antenna_id = :a "
            "AND control_generation = 2",
            a=KNOWN_ANTENNA,
        )
        == 1
    )
    assert _antenna_generation(db_engine, KNOWN_ANTENNA) == 2
    assert count_rows(db_engine, "SELECT count(*) FROM leases") == 2
    assert active_lease_count(db_engine, KNOWN_ANTENNA) == 1


# --------------------------------------------------------------------------
# Rejections never consume a generation
# --------------------------------------------------------------------------


def _lease_data_snapshot(db_engine):
    with db_engine.connect() as conn:
        leases = conn.execute(
            text(
                "SELECT id, antenna_id, control_generation, token "
                "FROM leases ORDER BY id"
            )
        ).all()
        antennas = dict(
            conn.execute(
                text("SELECT id, last_control_generation FROM antennas")
            ).all()
        )
    return leases, antennas


def test_every_rejection_leaves_counter_and_leases_untouched(
    http_client, db_engine
):
    # Two committed generations on the antenna under test.
    first = acquire(http_client, antenna_id=KNOWN_ANTENNA, duration_seconds=120)
    assert first.status_code == 200
    release(http_client, first.json()["lease_token"])
    incumbent = acquire(
        http_client,
        antenna_id=KNOWN_ANTENNA,
        controller="incumbent",
        duration_seconds=120,
    )
    assert incumbent.status_code == 200
    assert incumbent.json()["control_generation"] == 2

    # A key that will produce an idempotency conflict.
    conflict_key = make_key()
    seeded = acquire(
        http_client,
        antenna_id="ANT-02",
        controller="original",
        duration_seconds=30,
        idempotency_key=conflict_key,
    )
    assert seeded.status_code == 200

    before = _lease_data_snapshot(db_engine)
    assert _antenna_generation(db_engine, KNOWN_ANTENNA) == 2

    # 1. Busy antenna.
    busy = acquire(
        http_client, antenna_id=KNOWN_ANTENNA, controller="late",
        duration_seconds=30,
    )
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "ANTENNA_BUSY"

    # 2. Unknown antenna.
    unknown = acquire(
        http_client, antenna_id="ANT-99", controller="ghost",
        duration_seconds=30,
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "ANTENNA_NOT_FOUND"

    # 3. Same idempotency key with different parameters.
    conflict = http_client.post(
        "/leases",
        json={
            "antenna_id": "ANT-02",
            "controller": "original",
            "duration_seconds": 31,
            "idempotency_key": conflict_key,
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    # 4. Input rejections: out of range, textual number, missing field,
    #    blank controller.
    bad_bodies = [
        {
            "antenna_id": KNOWN_ANTENNA,
            "controller": "bad-range",
            "duration_seconds": 4,
            "idempotency_key": make_key(),
        },
        {
            "antenna_id": KNOWN_ANTENNA,
            "controller": "bad-range",
            "duration_seconds": 121,
            "idempotency_key": make_key(),
        },
        {
            "antenna_id": KNOWN_ANTENNA,
            "controller": "string-ttl",
            "duration_seconds": "30",
            "idempotency_key": make_key(),
        },
        {
            "antenna_id": KNOWN_ANTENNA,
            "duration_seconds": 30,
            "idempotency_key": make_key(),
        },
        {
            "antenna_id": KNOWN_ANTENNA,
            "controller": "   ",
            "duration_seconds": 30,
            "idempotency_key": make_key(),
        },
    ]
    for payload in bad_bodies:
        resp = http_client.post("/leases", json=payload)
        assert resp.status_code == 422, payload

    after = _lease_data_snapshot(db_engine)
    # Neither lease rows nor any antenna counter moved.
    assert after == before
    assert _antenna_generation(db_engine, KNOWN_ANTENNA) == 2
    with db_engine.connect() as conn:
        unknown_exists = conn.execute(
            text("SELECT count(*) FROM antennas WHERE id = 'ANT-99'")
        ).scalar_one()
    assert unknown_exists == 0

    # The first SUCCESS after all those failures follows directly on the
    # antenna's last committed lease: 2 -> 3, with no gap.
    release(http_client, incumbent.json()["lease_token"])
    recovered = acquire(
        http_client,
        antenna_id=KNOWN_ANTENNA,
        controller="after-failures",
        duration_seconds=30,
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["control_generation"] == 3
    assert _antenna_generation(db_engine, KNOWN_ANTENNA) == 3
    assert _all_generations(db_engine, KNOWN_ANTENNA) == [1, 2, 3]


def test_progress_and_renew_never_change_generation(http_client, db_engine):
    held = acquire(http_client, antenna_id="ANT-05", duration_seconds=30)
    token = held.json()["lease_token"]
    generation = held.json()["control_generation"]

    progress = http_client.post(
        f"/leases/{token}/progress", json={"sequence": 7}
    )
    assert progress.status_code == 200

    renewed = renew(http_client, token, 20)
    assert renewed.status_code == 200

    status = http_client.get(f"/leases/{token}")
    assert status.status_code == 200
    assert status.json()["control_generation"] == generation
    assert _antenna_generation(db_engine, "ANT-05") == generation


# --------------------------------------------------------------------------
# Migration backfill semantics
# --------------------------------------------------------------------------


def test_historical_backfill_orders_by_acquired_at_then_record_id(db_engine):
    # Validate the exact window-function the 0004 migration uses, including
    # the tie-break when two leases share one acquisition instant: run it
    # against an isolated scratch table on the real PostgreSQL.
    with db_engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TEMP TABLE hist_leases (
                    id BIGSERIAL PRIMARY KEY,
                    antenna_id TEXT NOT NULL,
                    acquired_at TIMESTAMPTZ NOT NULL,
                    control_generation BIGINT
                ) ON COMMIT DROP
                """
            )
        )
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # ANT-X: interleaved timestamps; ANT-Y: two grants at the SAME instant
        # so the record id tie-breaker decides.
        conn.execute(
            text(
                """
                INSERT INTO hist_leases (antenna_id, acquired_at) VALUES
                    ('ANT-X', :t2), ('ANT-X', :t1), ('ANT-X', :t3),
                    ('ANT-Y', :same), ('ANT-Y', :same)
                """
            ),
            {
                "t1": t0,
                "t2": t0 + timedelta(seconds=10),
                "t3": t0 + timedelta(seconds=20),
                "same": t0,
            },
        )
        conn.execute(
            text(
                """
                UPDATE hist_leases AS l
                SET control_generation = ranked.generation
                FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY antenna_id
                               ORDER BY acquired_at, id
                           ) AS generation
                    FROM hist_leases
                ) AS ranked
                WHERE l.id = ranked.id
                """
            )
        )
        rows = conn.execute(
            text(
                """
                SELECT antenna_id, control_generation
                FROM hist_leases
                ORDER BY antenna_id, id
                """
            )
        ).all()
    # ANT-X ordered by acquired_at regardless of insert order; ANT-Y ties
    # broken by record id; partitions start independently at 1.
    assert rows == [
        ("ANT-X", 2),
        ("ANT-X", 1),
        ("ANT-X", 3),
        ("ANT-Y", 1),
        ("ANT-Y", 2),
    ]


def test_live_schema_matches_backfill_invariant(http_client, db_engine):
    # After the upgrade every antenna's generations are dense 1..N in
    # acquisition order, and the antenna high-water mark equals the maximum.
    for i in range(1, 4):
        resp = acquire(
            http_client, antenna_id=f"ANT-0{i}", duration_seconds=60
        )
        assert resp.status_code == 200

    with db_engine.connect() as conn:
        gaps = conn.execute(
            text(
                """
                SELECT a.id
                FROM antennas a
                JOIN leases l ON l.antenna_id = a.id
                GROUP BY a.id, a.last_control_generation
                HAVING max(l.control_generation) IS DISTINCT FROM
                           a.last_control_generation
                    OR count(DISTINCT l.control_generation) <> count(*)
                    OR count(*) <> max(l.control_generation)
                    OR min(l.control_generation) <> 1
                """
            )
        ).all()
    assert gaps == []
