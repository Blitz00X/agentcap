"""SQLite-backed reservation ledger with rolling spend caps.

Every reservation is checked and inserted inside ``BEGIN IMMEDIATE`` so that
concurrent workers sharing one ledger file cannot jointly overshoot a cap.
Amounts are stored as decimal strings and summed as :class:`~decimal.Decimal`
in Python, never as floats.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterator

ZERO = Decimal("0")
_STATUSES = ("reserved", "settled", "released")


def default_ledger_path() -> Path:
    env_path = os.environ.get("AGENTCAP_DB") or os.environ.get("AGENTCAP_LEDGER_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".local" / "state" / "agentcap" / "ledger.sqlite3"


def _money(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool):
        raise TypeError(f"{field} must be a Decimal, not bool")
    if isinstance(value, float):
        raise TypeError(
            f"{field} must be a Decimal/str/int, not float (money math must be exact)"
        )
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, (str, int)):
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{field} is not a valid decimal: {value!r}") from exc
    else:
        raise TypeError(f"{field} must be a Decimal, str or int")
    if not result.is_finite() or result < 0:
        raise ValueError(f"{field} must be a finite non-negative decimal")
    return result


@dataclass(frozen=True)
class Caps:
    """Active spend caps.  ``None`` disables a scope."""

    run_usd: Decimal | None = None
    hour_usd: Decimal | None = None
    day_usd: Decimal | None = None

    def __post_init__(self) -> None:
        for field in ("run_usd", "hour_usd", "day_usd"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _money(value, field=field))


class LedgerError(Exception):
    """Base ledger error."""


class ReservationNotFound(LedgerError):
    pass


class InvalidReservationState(LedgerError):
    pass


class CapExceeded(LedgerError):
    """A reservation would push one of the active caps over its limit."""

    def __init__(
        self,
        *,
        scope: str,
        cap: Decimal,
        committed: Decimal,
        requested: Decimal,
    ) -> None:
        self.scope = scope
        self.cap = cap
        self.committed = committed
        self.requested = requested
        super().__init__(
            f"{scope} cap exceeded: committed ${committed} + requested "
            f"${requested} > cap ${cap}"
        )


class Ledger:
    """Thread-safe ledger.  One SQLite connection is opened per operation."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.path = Path(path).expanduser() if path is not None else default_ledger_path()
        self._clock = clock or time.time
        self._initialise()

    # ------------------------------------------------------------------ setup
    def _initialise(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS reservations (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    reserved_usd TEXT NOT NULL,
                    actual_usd TEXT,
                    status TEXT NOT NULL
                        CHECK (status IN ('reserved', 'settled', 'released')),
                    created_at REAL NOT NULL,
                    settled_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_reservations_created
                    ON reservations(created_at);
                CREATE INDEX IF NOT EXISTS idx_reservations_run
                    ON reservations(run_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_reservations_status
                    ON reservations(status, created_at);
                """
            )
        finally:
            conn.close()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.path), timeout=30.0, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            else:
                conn.execute("COMMIT")
        finally:
            conn.close()

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    # --------------------------------------------------------------- internals
    @staticmethod
    def _committed(
        conn: sqlite3.Connection,
        *,
        run_id: str | None = None,
        since: float | None = None,
    ) -> Decimal:
        clauses = ["status IN ('reserved', 'settled')"]
        params: list[object] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        sql = (
            "SELECT status, reserved_usd, actual_usd FROM reservations WHERE "
            + " AND ".join(clauses)
        )
        total = ZERO
        for row in conn.execute(sql, params):
            if row["status"] == "settled" and row["actual_usd"] is not None:
                total += Decimal(row["actual_usd"])
            else:
                total += Decimal(row["reserved_usd"])
        return total

    def _check_cap(
        self,
        *,
        scope: str,
        cap: Decimal | None,
        committed: Decimal,
        requested: Decimal,
    ) -> None:
        if cap is None:
            return
        if committed + requested > cap:
            raise CapExceeded(
                scope=scope, cap=cap, committed=committed, requested=requested
            )

    # ----------------------------------------------------------------- writing
    def reserve(
        self,
        run_id: str,
        provider: str,
        model: str,
        amount: Decimal | str | int,
        caps: Caps | None = None,
    ) -> str:
        """Atomically reserve worst-case spend and return the reservation id."""

        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a non-empty string")
        requested = _money(amount, field="amount")
        active = caps or Caps()
        now = self._clock()

        with self._transaction() as conn:
            run_committed = self._committed(conn, run_id=run_id)
            self._check_cap(
                scope="run",
                cap=active.run_usd,
                committed=run_committed,
                requested=requested,
            )
            hour_committed = self._committed(conn, since=now - 3600.0)
            self._check_cap(
                scope="hour",
                cap=active.hour_usd,
                committed=hour_committed,
                requested=requested,
            )
            day_committed = self._committed(conn, since=now - 86400.0)
            self._check_cap(
                scope="day",
                cap=active.day_usd,
                committed=day_committed,
                requested=requested,
            )

            reservation_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO reservations
                    (id, run_id, provider, model, reserved_usd, actual_usd,
                     status, created_at, settled_at)
                VALUES (?, ?, ?, ?, ?, NULL, 'reserved', ?, NULL)
                """,
                (
                    reservation_id,
                    run_id,
                    provider,
                    model,
                    str(requested),
                    now,
                ),
            )
        return reservation_id

    def settle(self, reservation_id: str, actual_usd: Decimal | str | int) -> bool:
        """Mark a reservation settled.  Returns True when actual > reserved."""

        actual = _money(actual_usd, field="actual_usd")
        now = self._clock()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT status, reserved_usd FROM reservations WHERE id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise ReservationNotFound(f"reservation {reservation_id!r} not found")
            if row["status"] != "reserved":
                raise InvalidReservationState(
                    f"reservation {reservation_id!r} is {row['status']}, not reserved"
                )
            overshoot = actual > Decimal(row["reserved_usd"])
            conn.execute(
                """
                UPDATE reservations
                   SET status = 'settled', actual_usd = ?, settled_at = ?
                 WHERE id = ? AND status = 'reserved'
                """,
                (str(actual), now, reservation_id),
            )
        return overshoot

    def release(self, reservation_id: str) -> None:
        """Drop a reservation (e.g. upstream failed before producing tokens)."""

        now = self._clock()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT status FROM reservations WHERE id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise ReservationNotFound(f"reservation {reservation_id!r} not found")
            if row["status"] != "reserved":
                raise InvalidReservationState(
                    f"reservation {reservation_id!r} is {row['status']}, not reserved"
                )
            conn.execute(
                """
                UPDATE reservations
                   SET status = 'released', actual_usd = NULL, settled_at = ?
                 WHERE id = ? AND status = 'reserved'
                """,
                (now, reservation_id),
            )

    # ---------------------------------------------------------------- reading
    def spent(self, run_id: str) -> Decimal:
        """Committed spend for a run: settled actuals + open reservations."""

        with self._reader() as conn:
            return self._committed(conn, run_id=run_id)

    def window_spent(self, seconds: float) -> Decimal:
        """Committed spend across all runs in the last ``seconds``."""

        if seconds <= 0:
            raise ValueError("seconds must be positive")
        cutoff = self._clock() - float(seconds)
        with self._reader() as conn:
            return self._committed(conn, since=cutoff)

    def summary(self, run_id: str) -> dict[str, object]:
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT status, reserved_usd, actual_usd, created_at "
                "FROM reservations WHERE run_id = ?",
                (run_id,),
            ).fetchall()

        reserved = ZERO
        actual = ZERO
        open_count = 0
        settled_count = 0
        released_count = 0
        for row in rows:
            if row["status"] == "reserved":
                open_count += 1
                reserved += Decimal(row["reserved_usd"])
            elif row["status"] == "settled":
                settled_count += 1
                actual += Decimal(row["actual_usd"] or row["reserved_usd"])
            elif row["status"] == "released":
                released_count += 1

        return {
            "run_id": run_id,
            "reservations": len(rows),
            "open": open_count,
            "settled": settled_count,
            "released": released_count,
            "reserved_usd": reserved,
            "actual_usd": actual,
            "committed_usd": reserved + actual,
        }
