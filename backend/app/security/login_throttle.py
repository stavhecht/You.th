"""Rate limiting for the login endpoint.

Without this, ``/auth/login`` is two separate free gifts. It is an unlimited
password oracle against a two-account console whose emails are guessable, and —
less obvious and more immediately dangerous — it is a CPU amplifier: Argon2id is
memory-hard on purpose, so every request costs the server ~100ms and megabytes
while costing the attacker one HTTP call. The dummy-hash call that equalises
timing for unknown emails means even *nonsense* input is expensive. A few
hundred concurrent POSTs is a resource exhaustion attack, not just a guessing
attack, **so the check has to happen before any hashing** or it protects the
password and not the process.

Three deliberate shapes:

**It rejects, it never waits.** The token bucket in ``retrieval/throttle.py``
sleeps to pace outbound calls, which is right for a client and backwards for a
server. A delay only slows a client that chooses to wait for the response — an
attacker firing 500 concurrent requests waits four seconds *in total*, while the
reviewer who mistyped waits four seconds for real. It puts the friction on the
honest user. Rejecting immediately costs us less per request than it costs the
attacker, which is the asymmetry worth having.

**Blocks escalate but always expire.** A flat window is a rate, not a
deterrent — ten guesses a minute forever is fourteen thousand a day. The ladder
below makes sustained attempts progressively unprofitable. It caps, and nothing
here can be extended by an attacker into a permanent block: there is no
user-management endpoint in this API, so an account disabled until an admin
clears it would need a shell on the server to undo, and would hand anyone who
knows a reviewer's email a kill switch on publishing (``approve()`` is the only
path to ``published``).

**Both keys are counted, whether or not the account exists.** ``api/console/auth.py``
goes to some trouble to make unknown-email and wrong-password indistinguishable.
Counting failures only against real accounts would undo that immediately: a 429
on an email would confirm the email.

The limit worth stating plainly: with a password alone, blocking distributed
guessing against a known account and guaranteeing that account's owner is never
blocked are not simultaneously achievable. MFA or a trusted-device cookie is
what actually resolves it. Until then the account budget is deliberately loose,
so it fires on a distributed attack rather than on a bad afternoon.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Budget:
    """Failures tolerated inside a rolling window before a key is blocked."""

    max_failures: int
    window: timedelta


#: Does the real work: stops spraying and the Argon2 flood, and trips fast
#: because a single source making ten wrong guesses in five minutes is not a
#: person who forgot their password.
IP_BUDGET = Budget(max_failures=10, window=timedelta(minutes=5))

#: Deliberately looser. This is the only defence against a distributed attack
#: on a known email, and also the only way an attacker can inconvenience a real
#: reviewer, so it is set to fire on the former rather than the latter.
ACCOUNT_BUDGET = Budget(max_failures=20, window=timedelta(minutes=15))

#: How long a key stays blocked, indexed by how many times it has already been
#: blocked. Capped at the last entry — a block that grows without bound is a
#: permanent lockout wearing a timer.
BLOCK_LADDER = (timedelta(seconds=60), timedelta(minutes=5), timedelta(minutes=30))

#: Quiet time after which a key's escalation tier resets. Without it, a
#: reviewer who fumbled their password twice last spring starts at the top of
#: the ladder today.
TIER_DECAY = timedelta(hours=1)

#: Ceiling on tracked keys, because the key is partly attacker-controlled and
#: an unbounded dict is the same class of bug this module exists to fix.
#: Blocked keys are never evicted (see ``_evict``), so an attacker cannot free
#: their own block by flooding new ones.
MAX_TRACKED_KEYS = 10_000


@dataclass
class _KeyState:
    failures: list[float] = field(default_factory=list)
    tier: int = 0
    blocked_until: float = 0.0
    #: Timestamp of the most recent failure, or None for a key that has never
    #: had one. Not a 0.0 sentinel: the clock is monotonic and may legitimately
    #: read near zero shortly after boot.
    last_failure: float | None = None

    def prune(self, now: float, window: float) -> None:
        cutoff = now - window
        self.failures = [stamp for stamp in self.failures if stamp > cutoff]

    def gone_quiet(self, now: float) -> bool:
        return (
            self.last_failure is not None
            and now - self.last_failure > TIER_DECAY.total_seconds()
        )

    def is_spent(self, now: float) -> bool:
        """Nothing left worth remembering: unblocked, no live failures, idle."""
        return not self.failures and now >= self.blocked_until and self.gone_quiet(now)


class LoginThrottle(Protocol):
    """The seam. ``InMemoryLoginThrottle`` is what runs.

    A Redis or Postgres implementation is a drop-in when the console runs on
    more than one process — that is the honest limitation of the one below,
    not a reason to pull the infrastructure forward now.
    """

    def retry_after(self, *, ip: str, email: str) -> float | None:
        """Seconds until this caller may try again, or None if allowed."""
        ...

    def record_failure(self, *, ip: str, email: str) -> None: ...

    def record_success(self, *, ip: str, email: str) -> None: ...


class InMemoryLoginThrottle:
    """Per-process counters in a dict.

    Every method is synchronous and never awaits, so under a single event loop
    each one is atomic and no lock is needed. Keep it that way: adding an
    ``await`` inside would introduce a race that only shows up under load.

    Per-process is the real caveat. N uvicorn workers means N times the budget
    and a restart clears the state — neither matters with one local process,
    and both are what ``LoginThrottle`` exists to let you swap out.
    """

    def __init__(
        self,
        ip_budget: Budget = IP_BUDGET,
        account_budget: Budget = ACCOUNT_BUDGET,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._budgets = {"ip": ip_budget, "account": account_budget}
        self._state: dict[tuple[str, str], _KeyState] = {}
        #: Injected so tests can advance time instead of sleeping through a
        #: 30-minute ladder. Monotonic, so a clock adjustment cannot shorten a
        #: block or extend one to eternity.
        self._clock = clock

    def _now(self) -> float:
        return self._clock()

    def _keys(self, ip: str, email: str) -> tuple[tuple[str, str], tuple[str, str]]:
        return ("ip", ip), ("account", email)

    def retry_after(self, *, ip: str, email: str) -> float | None:
        """The longest block across both keys, or None.

        Checked before the user lookup and before any hashing, so a blocked
        caller costs a dict read.
        """
        now = self._now()
        waits = [
            state.blocked_until - now
            for key in self._keys(ip, email)
            if (state := self._state.get(key)) is not None and state.blocked_until > now
        ]
        return max(waits) if waits else None

    def record_failure(self, *, ip: str, email: str) -> None:
        """Count one failed attempt against both keys.

        Called for an unknown email as well as a wrong password. Counting only
        real accounts would make a 429 an existence oracle and undo the timing
        equalisation in ``api/console/auth.py``.
        """
        now = self._now()
        self._evict(now)
        for key in self._keys(ip, email):
            self._register(key, now)

    def record_success(self, *, ip: str, email: str) -> None:
        """Clear both keys.

        A correct password is proof the caller is not the attacker the counter
        was accumulating against, so the reviewer's next mistake starts from
        zero rather than from wherever a spraying attempt left them.
        """
        for key in self._keys(ip, email):
            self._state.pop(key, None)

    def _register(self, key: tuple[str, str], now: float) -> None:
        budget = self._budgets[key[0]]
        state = self._state.get(key)
        if state is None:
            if len(self._state) >= MAX_TRACKED_KEYS:
                return  # table full of live entries; see _evict
            state = self._state[key] = _KeyState()

        # Decay is evaluated *before* this failure is recorded, against the
        # previous one. Checking afterwards compares the burst to itself —
        # failures two through ten refresh the timestamp, so the gap is always
        # zero and a key quiet for a week still escalates from where it left
        # off. Waiting out a block is not quiet time either: only the absence
        # of failures counts, or the ladder resets every time it fires.
        if state.gone_quiet(now):
            state.tier = 0
            state.failures.clear()

        state.prune(now, budget.window.total_seconds())
        state.failures.append(now)
        state.last_failure = now

        if len(state.failures) < budget.max_failures:
            return

        state.tier = min(state.tier + 1, len(BLOCK_LADDER))
        block = BLOCK_LADDER[state.tier - 1]
        state.blocked_until = now + block.total_seconds()
        state.failures.clear()

        logger.warning(
            "login blocked for %s %r after %d failures: %.0fs (tier %d/%d)",
            key[0],
            key[1],
            budget.max_failures,
            block.total_seconds(),
            state.tier,
            len(BLOCK_LADDER),
        )

    def _evict(self, now: float) -> None:
        """Drop spent entries, and only ever spent ones.

        Evicting a *blocked* key would let an attacker release their own block
        by flooding the table with fresh ones. They cannot: reaching a block
        costs ten Argon2 verifications, so the memory attack is bounded by the
        CPU attack it has to pay for first. If the table is somehow full of
        live entries we stop tracking new ones rather than forget existing
        blocks — degraded, but never self-defeating.
        """
        if len(self._state) < MAX_TRACKED_KEYS:
            return
        for key, state in list(self._state.items()):
            if state.is_spent(now):
                del self._state[key]
        if len(self._state) >= MAX_TRACKED_KEYS:
            logger.error(
                "login throttle table full (%d live keys); new sources are not "
                "being tracked until entries expire",
                len(self._state),
            )
