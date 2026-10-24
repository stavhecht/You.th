"""Login rate limiting: the counter, and the endpoint that uses it.

No database and no sleeping — the clock is injected, so the 30-minute top of
the ladder is tested in microseconds. The route half uses a fake session, since
what is under test is the order of operations in the handler, not SQL.

Two properties are load-bearing and easy to break by accident:

**The check must precede the hash.** Argon2id is memory-hard on purpose, so an
unthrottled login endpoint is a CPU exhaustion vector before it is a password
oracle — and ``equalise_timing`` means junk input costs the same as a real
attempt. A throttle that runs after the user lookup protects the password and
leaves the process exposed.

**The 429 must not become an existence oracle.** ``api/console/auth.py`` equalises timing
so unknown-email and wrong-password are indistinguishable. Counting failures
only against accounts that exist would hand that back: whichever email produced
a 429 is the real one.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.deps import client_ip, get_login_throttle, get_session
from app.main import create_app
from app.security.auth import hash_password
from app.security.login_throttle import (
    ACCOUNT_BUDGET,
    BLOCK_LADDER,
    IP_BUDGET,
    MAX_TRACKED_KEYS,
    TIER_DECAY,
    Budget,
    InMemoryLoginThrottle,
)

LOGIN = "/api/console/auth/login"
PASSWORD = "correct-horse-battery-staple"
EMAIL = "reviewer@example.com"


class _Clock:
    """Advanced by hand. Blocks are minutes long; tests are not."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _throttle(clock: _Clock, **kwargs: Budget) -> InMemoryLoginThrottle:
    return InMemoryLoginThrottle(clock=clock, **kwargs)


def _fail(throttle: InMemoryLoginThrottle, times: int, *, ip: str, email: str) -> None:
    for _ in range(times):
        throttle.record_failure(ip=ip, email=email)


# ---------------------------------------------------------------------------
# The budget.
# ---------------------------------------------------------------------------


def test_attempts_under_the_budget_are_not_blocked() -> None:
    """A reviewer who mistypes must not be locked out. The budget only exists
    to bound an attack, and a limit that fires on ordinary use gets raised
    until it fires on nothing."""
    throttle = _throttle(_Clock())

    _fail(throttle, IP_BUDGET.max_failures - 1, ip="10.0.0.1", email=EMAIL)

    assert throttle.retry_after(ip="10.0.0.1", email=EMAIL) is None


def test_exhausting_the_ip_budget_blocks_at_the_first_rung() -> None:
    clock = _Clock()
    throttle = _throttle(clock)

    _fail(throttle, IP_BUDGET.max_failures, ip="10.0.0.1", email=EMAIL)

    wait = throttle.retry_after(ip="10.0.0.1", email=EMAIL)
    assert wait == pytest.approx(BLOCK_LADDER[0].total_seconds())


def test_the_block_expires_on_its_own() -> None:
    """Nothing here may become permanent — there is no unlock endpoint in this
    API, so a block that outlives its timer needs a shell on the server."""
    clock = _Clock()
    throttle = _throttle(clock)
    _fail(throttle, IP_BUDGET.max_failures, ip="10.0.0.1", email=EMAIL)

    clock.advance(BLOCK_LADDER[0].total_seconds() + 1)

    assert throttle.retry_after(ip="10.0.0.1", email=EMAIL) is None


def test_failures_outside_the_window_do_not_accumulate() -> None:
    """The window rolls. One wrong guess a day for a fortnight is a person who
    keeps forgetting, not an attack, and must never add up to a block."""
    clock = _Clock()
    throttle = _throttle(clock)

    for _ in range(IP_BUDGET.max_failures * 3):
        throttle.record_failure(ip="10.0.0.1", email=EMAIL)
        clock.advance(IP_BUDGET.window.total_seconds() + 1)

    assert throttle.retry_after(ip="10.0.0.1", email=EMAIL) is None


def test_separate_sources_have_separate_budgets() -> None:
    clock = _Clock()
    throttle = _throttle(clock)

    _fail(throttle, IP_BUDGET.max_failures, ip="10.0.0.1", email="a@example.com")

    assert throttle.retry_after(ip="10.0.0.1", email="a@example.com") is not None
    assert throttle.retry_after(ip="10.0.0.2", email="b@example.com") is None


# ---------------------------------------------------------------------------
# Escalation — the part a flat window does not give you.
# ---------------------------------------------------------------------------


def test_repeat_offences_climb_the_ladder() -> None:
    """A flat window is a rate, not a deterrent: ten guesses a minute forever
    is fourteen thousand a day at no cost. Each block has to hurt more."""
    clock = _Clock()
    throttle = _throttle(clock)
    seen = []

    for rung in BLOCK_LADDER:
        _fail(throttle, IP_BUDGET.max_failures, ip="10.0.0.1", email=EMAIL)
        seen.append(throttle.retry_after(ip="10.0.0.1", email=EMAIL))
        clock.advance(rung.total_seconds() + 1)

    assert seen == pytest.approx([rung.total_seconds() for rung in BLOCK_LADDER])


def test_the_ladder_caps_rather_than_growing_without_bound() -> None:
    """The difference between a long block and a lockout. Past the top rung the
    penalty stays put, so no amount of attacking produces an unbounded one."""
    clock = _Clock()
    throttle = _throttle(clock)

    for _ in range(len(BLOCK_LADDER) + 3):
        _fail(throttle, IP_BUDGET.max_failures, ip="10.0.0.1", email=EMAIL)
        clock.advance(BLOCK_LADDER[-1].total_seconds() + 1)

    _fail(throttle, IP_BUDGET.max_failures, ip="10.0.0.1", email=EMAIL)
    wait = throttle.retry_after(ip="10.0.0.1", email=EMAIL)
    assert wait == pytest.approx(BLOCK_LADDER[-1].total_seconds())


def test_waiting_out_a_block_does_not_reset_the_tier() -> None:
    """Otherwise the ladder never leaves its first rung: an attacker sits out
    sixty seconds and gets a fresh budget at the cheapest penalty forever."""
    clock = _Clock()
    throttle = _throttle(clock)

    _fail(throttle, IP_BUDGET.max_failures, ip="10.0.0.1", email=EMAIL)
    clock.advance(BLOCK_LADDER[0].total_seconds() + 1)
    _fail(throttle, IP_BUDGET.max_failures, ip="10.0.0.1", email=EMAIL)

    wait = throttle.retry_after(ip="10.0.0.1", email=EMAIL)
    assert wait == pytest.approx(BLOCK_LADDER[1].total_seconds())


def test_a_quiet_period_resets_the_tier() -> None:
    """The other side of it. Without decay a reviewer who fumbled twice last
    spring starts today at the top of the ladder, and the escalation designed
    for attackers lands on the only people who use the console."""
    clock = _Clock()
    throttle = _throttle(clock)
    _fail(throttle, IP_BUDGET.max_failures, ip="10.0.0.1", email=EMAIL)

    clock.advance(TIER_DECAY.total_seconds() + 1)
    _fail(throttle, IP_BUDGET.max_failures, ip="10.0.0.1", email=EMAIL)

    wait = throttle.retry_after(ip="10.0.0.1", email=EMAIL)
    assert wait == pytest.approx(BLOCK_LADDER[0].total_seconds())


def test_decay_is_measured_from_the_last_failure_not_the_last_attempt_in_a_burst() -> None:
    """The bug this method had while it was being written.

    Evaluating decay *after* recording compares a burst to itself: failures two
    through ten refresh the timestamp, the gap is always zero, and a key silent
    for a week still escalates from wherever it stopped. The check has to run
    against the previous failure, before this one is counted.
    """
    clock = _Clock()
    throttle = _throttle(clock)
    _fail(throttle, IP_BUDGET.max_failures, ip="10.0.0.1", email=EMAIL)  # tier 1

    clock.advance(TIER_DECAY.total_seconds() * 2)
    # A burst long enough that the naive check would compare its own members.
    _fail(throttle, IP_BUDGET.max_failures, ip="10.0.0.1", email=EMAIL)

    assert throttle.retry_after(ip="10.0.0.1", email=EMAIL) == pytest.approx(
        BLOCK_LADDER[0].total_seconds()
    )


# ---------------------------------------------------------------------------
# Two keys.
# ---------------------------------------------------------------------------


def test_a_distributed_attack_on_one_account_still_trips_the_account_budget() -> None:
    """What the per-IP budget cannot see. Each source stays well under its own
    limit; only the account counter notices that they are all guessing at the
    same reviewer."""
    clock = _Clock()
    throttle = _throttle(clock)

    for n in range(ACCOUNT_BUDGET.max_failures):
        throttle.record_failure(ip=f"10.0.0.{n}", email=EMAIL)

    assert throttle.retry_after(ip="10.0.9.9", email=EMAIL) is not None


def test_the_account_budget_is_looser_than_the_ip_budget() -> None:
    """Deliberate, and the reason the account key is not a lockout in practice:
    it is set to fire on a distributed attack rather than on a bad afternoon.
    Tightening it below the IP budget would make it the binding constraint and
    hand anyone who knows a reviewer's email an easy denial of service."""
    assert ACCOUNT_BUDGET.max_failures > IP_BUDGET.max_failures


def test_the_longer_of_the_two_blocks_wins() -> None:
    """Reporting the shorter one would invite a client to retry into a block it
    was just told had expired."""
    clock = _Clock()
    throttle = _throttle(
        clock,
        ip_budget=Budget(2, IP_BUDGET.window),
        account_budget=Budget(4, ACCOUNT_BUDGET.window),
    )

    _fail(throttle, 2, ip="10.0.0.1", email=EMAIL)  # ip blocked, tier 1
    clock.advance(BLOCK_LADDER[0].total_seconds() + 1)
    _fail(throttle, 2, ip="10.0.0.1", email=EMAIL)  # ip tier 2, account trips at 4

    wait = throttle.retry_after(ip="10.0.0.1", email=EMAIL)
    assert wait == pytest.approx(BLOCK_LADDER[1].total_seconds())


def test_a_successful_login_clears_both_keys() -> None:
    """A correct password proves the caller is not whoever the counter was
    accumulating against."""
    clock = _Clock()
    throttle = _throttle(clock)
    _fail(throttle, IP_BUDGET.max_failures - 1, ip="10.0.0.1", email=EMAIL)

    throttle.record_success(ip="10.0.0.1", email=EMAIL)
    _fail(throttle, IP_BUDGET.max_failures - 1, ip="10.0.0.1", email=EMAIL)

    assert throttle.retry_after(ip="10.0.0.1", email=EMAIL) is None


# ---------------------------------------------------------------------------
# The table itself is attacker-influenced.
# ---------------------------------------------------------------------------


def test_spent_entries_are_evicted_so_the_table_cannot_grow_forever() -> None:
    """Half the key space is chosen by the caller, so an unbounded dict here is
    the same class of bug this module exists to fix — a slow memory exhaustion
    behind a rate limiter."""
    clock = _Clock()
    throttle = _throttle(clock)

    for n in range(MAX_TRACKED_KEYS + 10):
        throttle.record_failure(ip=f"10.{n // 65536}.{n // 256 % 256}.{n % 256}", email=EMAIL)
        clock.advance(TIER_DECAY.total_seconds() + 1)

    assert len(throttle._state) <= MAX_TRACKED_KEYS


def test_flooding_the_table_cannot_release_an_existing_block() -> None:
    """The eviction has to refuse blocked keys, or it becomes the bypass: fill
    the table from fresh addresses and your own block is forgotten. It holds
    because reaching a block costs ten Argon2 verifications, so the memory
    attack is bounded by the CPU attack it must pay for first."""
    clock = _Clock()
    throttle = _throttle(clock)
    _fail(throttle, IP_BUDGET.max_failures, ip="10.0.0.1", email=EMAIL)

    for n in range(MAX_TRACKED_KEYS + 100):
        throttle.record_failure(ip=f"172.{n // 65536}.{n // 256 % 256}.{n % 256}", email=EMAIL)

    assert throttle.retry_after(ip="10.0.0.1", email=EMAIL) is not None


# ---------------------------------------------------------------------------
# The endpoint.
# ---------------------------------------------------------------------------


class _FakeUser:
    def __init__(self, password: str) -> None:
        self.id = "00000000-0000-0000-0000-000000000001"
        self.email = EMAIL
        self.display_name = "Reviewer"
        self.role = "reviewer"
        self.password_hash = hash_password(password)
        self.is_active = True


class _FakeResult:
    def __init__(self, user: Any) -> None:
        self._user = user

    def scalar_one_or_none(self) -> Any:
        return self._user


class _FakeSession:
    """Serves the one query login makes, and counts it.

    The count is the point: a blocked request must not reach the database, let
    alone the password hash.
    """

    def __init__(self, user: Any) -> None:
        self._user = user
        self.queries = 0

    async def execute(self, *_: Any, **__: Any) -> _FakeResult:
        self.queries += 1
        return _FakeResult(self._user)

    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...


@pytest.fixture
def client() -> Any:
    """App with a fake session, a fresh throttle, and a controllable IP."""
    user = _FakeUser(PASSWORD)
    session = _FakeSession(user)
    clock = _Clock()
    throttle = _throttle(clock)
    caller = {"ip": "10.0.0.1"}

    app = create_app()

    async def _session() -> Any:
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_login_throttle] = lambda: throttle
    app.dependency_overrides[client_ip] = lambda: caller["ip"]

    with TestClient(app) as http:
        yield http, session, throttle, clock, caller, user


def _post(http: Any, password: str, email: str = EMAIL) -> Any:
    return http.post(LOGIN, json={"email": email, "password": password})


def test_correct_credentials_still_work(client: Any) -> None:
    http, *_ = client

    response = _post(http, PASSWORD)

    assert response.status_code == 200
    assert response.json()["access_token"]


def test_wrong_passwords_become_a_429_with_a_retry_after(client: Any) -> None:
    http, *_ = client

    for _ in range(IP_BUDGET.max_failures):
        assert _post(http, "wrong").status_code == 401

    blocked = _post(http, "wrong")
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) == pytest.approx(
        BLOCK_LADDER[0].total_seconds(), abs=2
    )


def test_a_blocked_request_never_reaches_the_database_or_the_hash(client: Any) -> None:
    """Where the check sits, asserted rather than assumed.

    Argon2id is memory-hard, so an unthrottled login is a CPU amplifier before
    it is a password oracle. A throttle placed after the user lookup would pass
    every test above and protect nothing that matters under a flood.
    """
    http, session, *_ = client
    for _ in range(IP_BUDGET.max_failures):
        _post(http, "wrong")
    queries_before = session.queries

    assert _post(http, "wrong").status_code == 429
    assert session.queries == queries_before


def test_the_correct_password_is_refused_while_blocked(client: Any) -> None:
    """Unavoidable and worth stating: the block cannot know the password is
    right without doing the work it is refusing to do. Bounded by the ladder
    expiring on its own, which is why nothing here waits for an admin."""
    http, *_ = client
    for _ in range(IP_BUDGET.max_failures):
        _post(http, "wrong")

    assert _post(http, PASSWORD).status_code == 429


def test_the_block_lifts_and_the_reviewer_gets_back_in(client: Any) -> None:
    http, _session, _unused, clock, *_ = client
    for _ in range(IP_BUDGET.max_failures):
        _post(http, "wrong")

    clock.advance(BLOCK_LADDER[0].total_seconds() + 1)

    assert _post(http, PASSWORD).status_code == 200


def test_a_successful_login_resets_the_count(client: Any) -> None:
    """A reviewer who mistyped nine times, got in, then mistyped nine more must
    not be blocked — the first nine were demonstrably not an attack."""
    http, *_ = client
    for _ in range(IP_BUDGET.max_failures - 1):
        _post(http, "wrong")

    assert _post(http, PASSWORD).status_code == 200
    for _ in range(IP_BUDGET.max_failures - 1):
        assert _post(http, "wrong").status_code == 401


def test_an_unknown_email_is_counted_exactly_like_a_wrong_password(client: Any) -> None:
    """The enumeration guard, from the throttling side.

    Counting only real accounts would make the 429 an existence oracle: spray
    ten guesses at each candidate address and whichever one starts refusing is
    the reviewer. Both branches must reach ``record_failure``.
    """
    http, *_ = client

    for _ in range(IP_BUDGET.max_failures):
        assert _post(http, "wrong", email="nobody@example.com").status_code == 401

    refused = _post(http, "wrong", email="nobody@example.com")
    assert refused.status_code == 429


def test_a_known_and_an_unknown_email_are_indistinguishable_at_the_limit(
    client: Any,
) -> None:
    """Same status and same body, so the response says nothing about which
    address exists."""
    http, _session, _unused, _clock, caller, _user = client

    caller["ip"] = "10.0.0.7"
    for _ in range(IP_BUDGET.max_failures):
        _post(http, "wrong", email=EMAIL)
    known = _post(http, "wrong", email=EMAIL)

    caller["ip"] = "10.0.0.8"
    for _ in range(IP_BUDGET.max_failures):
        _post(http, "wrong", email="nobody@example.com")
    unknown = _post(http, "wrong", email="nobody@example.com")

    assert (known.status_code, known.json()) == (unknown.status_code, unknown.json())


def test_the_email_is_normalised_before_it_is_counted(client: Any) -> None:
    """Otherwise ``Reviewer@Example.com`` and ``reviewer@example.com`` are two
    budgets for one account, and case alone multiplies the allowance."""
    http, _session, throttle, *_ = client

    for _ in range(IP_BUDGET.max_failures - 1):
        _post(http, "wrong", email="  REVIEWER@Example.com  ")

    assert throttle.retry_after(ip="10.0.0.1", email=EMAIL) is None
    _post(http, "wrong", email=EMAIL)
    assert throttle.retry_after(ip="10.0.0.1", email=EMAIL) is not None


def test_a_forwarded_for_header_does_not_buy_a_fresh_budget() -> None:
    """``X-Forwarded-For`` is attacker-controlled with nothing in front of
    uvicorn. Honouring it here would let one client mint an identity per
    request and bypass the IP budget entirely — worse than no limit, because
    the endpoint would look protected.

    This test uses the real ``client_ip``; behind a load balancer the fix is
    ``--proxy-headers --forwarded-allow-ips``, not reading the header here.
    """
    session = _FakeSession(_FakeUser(PASSWORD))
    throttle = _throttle(_Clock())
    app = create_app()

    async def _session() -> Any:
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_login_throttle] = lambda: throttle

    with TestClient(app) as http:
        for _ in range(IP_BUDGET.max_failures):
            _post(http, "wrong")

        spoofed = http.post(
            LOGIN,
            json={"email": EMAIL, "password": "wrong"},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )

    assert spoofed.status_code == 429
