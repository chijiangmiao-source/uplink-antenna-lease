"""Dual-site coordinated uplink: ``POST /lease-bundles`` grants two antennas
atomically — all-or-nothing, one database instant, deadlock-free under
reversed-order concurrency.

Everything runs against the REAL API + REAL PostgreSQL: no mocks, no fixed
responses. The suite proves:

* both antennas free -> one atomic success; both member leases share a single
  sampled ``acquired_at``/``expires_at`` and stay ordinary leases for the
  token query interface;
* either antenna occupied -> the whole request persists nothing and the busy
  feedback lists every blocking antenna with its hand-over time;
* same idempotency key + same parameters -> the original token pair and
  original times with ``replay: true`` (only two lease records ever exist);
  changed parameters -> stable ``IDEMPOTENCY_CONFLICT``;
* two reversed-order concurrent bundle requests never deadlock and at most
  one group succeeds.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text

from conftest import (
    acquire,
    active_lease_count,
    bundle,
    count_rows,
    make_key,
    release,
    renew,
)

ANTENNA_A = "ANT-01"
ANTENNA_B = "ANT-02"


def _payload(key, antennas=(ANTENNA_A, ANTENNA_B), controller="dual-site-ctrl",
             duration=30):
    return {
        "antenna_ids": list(antennas),
        "controller": controller,
        "duration_seconds": duration,
        "idempotency_key": key,
    }


def _bundle_row(db_engine):
    with db_engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT id, controller, duration_seconds, acquired_at, "
                "expires_at FROM lease_bundles"
            )
        ).mappings().one()


def test_bundle_success_is_atomic_with_identical_times(http_client, db_engine):
    resp = bundle(http_client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["replay"] is False
    assert body["controller"] == "dual-site-ctrl"

    members = body["leases"]
    assert [m["antenna_id"] for m in members] == [ANTENNA_A, ANTENNA_B]
    tokens = {m["lease_token"] for m in members}
    assert len(tokens) == 2
    # First grant on each antenna: each carries its own antenna's generation.
    assert [m["control_generation"] for m in members] == [1, 1]

    # Each token is recognised by the original query interface as a perfectly
    # ordinary lease, with the SAME byte-identical timestamps.
    for member in members:
        status = http_client.get(f"/leases/{member['lease_token']}")
        assert status.status_code == 200
        sbody = status.json()
        assert sbody["antenna_id"] == member["antenna_id"]
        assert sbody["controller"] == "dual-site-ctrl"
        assert sbody["acquired_at"] == body["acquired_at"]
        assert sbody["expires_at"] == body["expires_at"]
        assert sbody["control_generation"] == member["control_generation"]
        assert sbody["active"] is True
        assert sbody["released_at"] is None
        assert sbody["last_command_sequence"] is None

    # One instant was sampled: both rows share identical stored timestamps,
    # and the bundle application record carries the same pair of instants.
    assert count_rows(
        db_engine, "SELECT count(DISTINCT acquired_at) FROM leases"
    ) == 1
    assert count_rows(
        db_engine, "SELECT count(DISTINCT expires_at) FROM leases"
    ) == 1
    bundle_row = _bundle_row(db_engine)
    assert bundle_row["acquired_at"].isoformat() == body["acquired_at"]
    assert bundle_row["expires_at"].isoformat() == body["expires_at"]
    assert bundle_row["duration_seconds"] == 30

    # The migration-established association links both leases to the bundle.
    assert count_rows(db_engine, "SELECT count(*) FROM leases") == 2
    assert count_rows(db_engine, "SELECT count(*) FROM lease_bundles") == 1
    assert count_rows(
        db_engine,
        "SELECT count(*) FROM leases WHERE bundle_id = :bid",
        bid=bundle_row["id"],
    ) == 2
    assert active_lease_count(db_engine, ANTENNA_A) == 1
    assert active_lease_count(db_engine, ANTENNA_B) == 1


def test_bundle_all_or_nothing_when_one_antenna_busy(http_client, db_engine):
    held = acquire(http_client, antenna_id=ANTENNA_A, duration_seconds=60)
    assert held.status_code == 200
    incumbent = held.json()

    resp = bundle(http_client)
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "ANTENNA_BUSY"
    blocked = body["error"]["details"]["blocked_antennas"]
    assert [b["antenna_id"] for b in blocked] == [ANTENNA_A]
    assert blocked[0]["held_by_lease"] == incumbent["lease_token"]
    assert blocked[0]["expires_at"] == incumbent["expires_at"]

    # Zero new rows of any kind: the free antenna is NOT leased on its own.
    assert count_rows(db_engine, "SELECT count(*) FROM leases") == 1
    assert count_rows(db_engine, "SELECT count(*) FROM lease_bundles") == 0
    assert active_lease_count(db_engine, ANTENNA_B) == 0


def test_bundle_busy_feedback_lists_every_blocking_antenna(
    http_client, db_engine
):
    held_a = acquire(http_client, antenna_id=ANTENNA_A, duration_seconds=60)
    held_b = acquire(http_client, antenna_id=ANTENNA_B, duration_seconds=60)
    assert held_a.status_code == 200 and held_b.status_code == 200

    resp = bundle(http_client)
    assert resp.status_code == 409
    blocked = resp.json()["error"]["details"]["blocked_antennas"]
    assert [b["antenna_id"] for b in blocked] == [ANTENNA_A, ANTENNA_B]
    assert {b["held_by_lease"] for b in blocked} == {
        held_a.json()["lease_token"],
        held_b.json()["lease_token"],
    }
    assert count_rows(db_engine, "SELECT count(*) FROM leases") == 2
    assert count_rows(db_engine, "SELECT count(*) FROM lease_bundles") == 0


def test_bundle_same_key_replay_keeps_only_two_records(
    http_client, db_engine
):
    key = make_key("bundle")
    first = bundle(http_client, idempotency_key=key)
    assert first.status_code == 200
    original = first.json()
    assert original["replay"] is False

    for _ in range(3):
        replay = bundle(http_client, idempotency_key=key)
        assert replay.status_code == 200
        body = replay.json()
        assert body["replay"] is True
        # Original token group, original times — byte-identical.
        assert body["leases"] == original["leases"]
        assert body["acquired_at"] == original["acquired_at"]
        assert body["expires_at"] == original["expires_at"]
        assert body["controller"] == original["controller"]

    # Antenna order is not significant: the reversed pair is the same
    # request and replays the original grant instead of conflicting.
    reversed_replay = http_client.post(
        "/lease-bundles", json=_payload(key, antennas=(ANTENNA_B, ANTENNA_A))
    )
    assert reversed_replay.status_code == 200
    assert reversed_replay.json()["replay"] is True
    assert reversed_replay.json()["leases"] == original["leases"]

    # Only two lease records and one bundle record were ever written.
    assert count_rows(db_engine, "SELECT count(*) FROM leases") == 2
    assert count_rows(db_engine, "SELECT count(*) FROM lease_bundles") == 1
    assert active_lease_count(db_engine, ANTENNA_A) == 1
    assert active_lease_count(db_engine, ANTENNA_B) == 1


def test_bundle_same_key_different_params_is_stable_conflict(
    http_client, db_engine
):
    key = make_key("bundle")
    first = bundle(http_client, idempotency_key=key)
    assert first.status_code == 200

    variants = [
        _payload(key, controller="other-controller"),
        _payload(key, duration=31),
        _payload(key, antennas=(ANTENNA_A, "ANT-03")),
    ]
    for payload in variants:
        conflict = http_client.post("/lease-bundles", json=payload)
        assert conflict.status_code == 409
        body = conflict.json()
        assert body["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert body["error"]["details"]["idempotency_key"] == key
        # Stable on repeat.
        again = http_client.post("/lease-bundles", json=payload)
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    # The original grant is untouched and still replays.
    replay = bundle(http_client, idempotency_key=key)
    assert replay.status_code == 200
    assert replay.json()["leases"] == first.json()["leases"]
    assert count_rows(db_engine, "SELECT count(*) FROM leases") == 2
    assert count_rows(db_engine, "SELECT count(*) FROM lease_bundles") == 1


def test_bundle_reversed_order_concurrent_requests_never_deadlock(
    http_client, db_engine
):
    """Two groups race for the same antenna pair in OPPOSITE order.

    Ascending-id lock ordering makes them serialise on the first antenna
    instead of deadlocking: every request completes with 200 or 409 (never a
    5xx / deadlock), and at most one group succeeds.
    """
    group_ab = [_payload(make_key("ab")) for _ in range(4)]
    group_ba = [
        _payload(make_key("ba"), antennas=(ANTENNA_B, ANTENNA_A))
        for _ in range(4)
    ]
    payloads = group_ab + group_ba
    barrier = threading.Barrier(len(payloads))

    def fire(payload):
        barrier.wait(timeout=10)
        return http_client.post("/lease-bundles", json=payload)

    with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
        responses = list(pool.map(fire, payloads))

    statuses = [r.status_code for r in responses]
    # No deadlock, no 5xx: every request reached a verdict.
    assert all(s in (200, 409) for s in statuses), statuses
    winners = [r for r in responses if r.status_code == 200]
    losers = [r for r in responses if r.status_code == 409]
    # At most one group succeeds — with both antennas initially free, exactly
    # one bundle is granted; everyone else is told the pair is busy.
    assert len(winners) == 1
    assert len(losers) == 7
    for loser in losers:
        assert loser.json()["error"]["code"] == "ANTENNA_BUSY"
        blocked = loser.json()["error"]["details"]["blocked_antennas"]
        assert {b["antenna_id"] for b in blocked} == {ANTENNA_A, ANTENNA_B}

    # The winner granted exactly two leases (one per antenna), one bundle.
    winner = winners[0].json()
    assert winner["replay"] is False
    assert len({m["lease_token"] for m in winner["leases"]}) == 2
    assert count_rows(db_engine, "SELECT count(*) FROM leases") == 2
    assert count_rows(db_engine, "SELECT count(*) FROM lease_bundles") == 1
    assert active_lease_count(db_engine, ANTENNA_A) == 1
    assert active_lease_count(db_engine, ANTENNA_B) == 1


def test_bundle_unknown_antenna_is_404_and_persists_nothing(
    http_client, db_engine
):
    resp = bundle(http_client, antenna_ids=[ANTENNA_A, "ANT-99"])
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "ANTENNA_NOT_FOUND"
    assert body["error"]["details"]["antenna_id"] == "ANT-99"

    assert count_rows(db_engine, "SELECT count(*) FROM leases") == 0
    assert count_rows(db_engine, "SELECT count(*) FROM lease_bundles") == 0
    assert active_lease_count(db_engine, ANTENNA_A) == 0


def test_bundle_duplicate_antenna_is_422_and_persists_nothing(
    http_client, db_engine
):
    resp = bundle(http_client, antenna_ids=[ANTENNA_A, ANTENNA_A])
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert count_rows(db_engine, "SELECT count(*) FROM leases") == 0
    assert count_rows(db_engine, "SELECT count(*) FROM lease_bundles") == 0


def test_bundle_input_validation(http_client, db_engine):
    # Out-of-range / non-integer durations are rejected like single leases.
    for bad_duration in (4, 121, "30"):
        payload = _payload(make_key("bundle"))
        payload["duration_seconds"] = bad_duration
        resp = http_client.post("/lease-bundles", json=payload)
        assert resp.status_code == 422, bad_duration
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    # Missing and extra fields are rejected.
    payload = _payload(make_key("bundle"))
    del payload["controller"]
    assert http_client.post("/lease-bundles", json=payload).status_code == 422
    payload = _payload(make_key("bundle"))
    payload["unexpected"] = True
    assert http_client.post("/lease-bundles", json=payload).status_code == 422
    # One or three antennas are not a dual-site bundle.
    for bad_count in ([ANTENNA_A], [ANTENNA_A, ANTENNA_B, "ANT-03"]):
        payload = _payload(make_key("bundle"))
        payload["antenna_ids"] = bad_count
        assert (
            http_client.post("/lease-bundles", json=payload).status_code
            == 422
        )

    assert count_rows(db_engine, "SELECT count(*) FROM leases") == 0
    assert count_rows(db_engine, "SELECT count(*) FROM lease_bundles") == 0

    # Boundary durations 5 and 120 are accepted (on two independent pairs).
    low = bundle(http_client, duration_seconds=5)
    assert low.status_code == 200
    high = bundle(
        http_client, antenna_ids=["ANT-03", "ANT-04"], duration_seconds=120
    )
    assert high.status_code == 200
    assert count_rows(db_engine, "SELECT count(*) FROM leases") == 4
    assert count_rows(db_engine, "SELECT count(*) FROM lease_bundles") == 2


def test_bundle_members_remain_ordinary_leases(http_client, db_engine):
    """Every interface built for single leases keeps working on members."""
    granted = bundle(http_client, duration_seconds=60)
    assert granted.status_code == 200
    members = granted.json()["leases"]
    token_a = members[0]["lease_token"]

    # A single-antenna contender on a bundle-held antenna gets the ordinary
    # ANTENNA_BUSY structure pointing at the member lease.
    contender = acquire(http_client, antenna_id=ANTENNA_A, duration_seconds=60)
    assert contender.status_code == 409
    details = contender.json()["error"]["details"]
    assert details["held_by_lease"] == token_a
    assert details["expires_at"] == granted.json()["expires_at"]

    # Progress reporting works on a member token.
    progress = http_client.post(
        f"/leases/{token_a}/progress", json={"sequence": 3}
    )
    assert progress.status_code == 200
    assert progress.json()["last_command_sequence"] == 3

    # Renewal works on a member token and only moves that member's expiry.
    renewed = renew(http_client, token_a, 20)
    assert renewed.status_code == 200
    assert renewed.json()["replay"] is False

    # Releasing one member frees only that antenna; the other stays held.
    released = release(http_client, token_a)
    assert released.status_code == 200
    assert released.json()["active"] is False
    successor = acquire(http_client, antenna_id=ANTENNA_A, duration_seconds=30)
    assert successor.status_code == 200
    # The released antenna's generation continued from the bundle grant.
    assert successor.json()["control_generation"] == 2
    still_busy = acquire(http_client, antenna_id=ANTENNA_B, duration_seconds=30)
    assert still_busy.status_code == 409
    assert active_lease_count(db_engine, ANTENNA_A) == 1
    assert active_lease_count(db_engine, ANTENNA_B) == 1


def test_bundle_expiry_boundary_hands_over_to_new_requests(
    http_client, db_engine
):
    granted = bundle(http_client, duration_seconds=30)
    assert granted.status_code == 200

    # Force the boundary to pass (database clock), exactly as the single-
    # lease expiry tests do via seeded rows. acquired_at is shifted back as
    # well so the stored row keeps satisfying expires_at > acquired_at.
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE leases "
                "SET acquired_at = acquired_at - make_interval(secs => 10), "
                "    expires_at = clock_timestamp() - make_interval(secs => 1)"
            )
        )
    assert active_lease_count(db_engine, ANTENNA_A) == 0
    assert active_lease_count(db_engine, ANTENNA_B) == 0

    # A fresh bundle takes over the pair with the next generation on each
    # antenna; single-antenna acquisition stays compatible afterwards.
    second = bundle(http_client, duration_seconds=30)
    assert second.status_code == 200
    assert second.json()["replay"] is False
    assert [m["control_generation"] for m in second.json()["leases"]] == [2, 2]
    assert count_rows(db_engine, "SELECT count(*) FROM leases") == 4
    assert count_rows(db_engine, "SELECT count(*) FROM lease_bundles") == 2
    assert active_lease_count(db_engine, ANTENNA_A) == 1
    assert active_lease_count(db_engine, ANTENNA_B) == 1


def test_bundle_generations_continue_per_antenna(http_client, db_engine):
    # ANT-01 already had a committed lease (generation 1) that was released.
    held = acquire(http_client, antenna_id=ANTENNA_A, duration_seconds=60)
    assert held.status_code == 200
    assert release(http_client, held.json()["lease_token"]).status_code == 200

    granted = bundle(http_client, duration_seconds=60)
    assert granted.status_code == 200
    members = {m["antenna_id"]: m for m in granted.json()["leases"]}
    # Each antenna's sequence is independent: ANT-01 continues at 2, the
    # never-leased ANT-02 starts at 1.
    assert members[ANTENNA_A]["control_generation"] == 2
    assert members[ANTENNA_B]["control_generation"] == 1
