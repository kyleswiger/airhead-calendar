"""Calendar source adapter interface (PRD §8).

One protocol, four eventual implementations (google, caldav, graph, ics).
This module is deliberately provider-agnostic and network-free: it mirrors the
repo layer's pattern where a Protocol plus shared dataclasses let concrete
backends swap behind one seam, held together by a shared contract test
(`airhead.adapters.contract`).

Normalization happens once, inside the adapter, into `ExternalEvent`.
Downstream code (sync, dedup, agent) never sees a provider-specific shape.
All-day events normalize to a floating date + `all_day=True`, never a
midnight-UTC instant.

v1 is pull-only. `push`/`remove` are declared now so the v2 work is additive;
concrete v1 adapters raise NotImplementedError from them.

Secrets discipline: `Credentials.secret_ref` is an SSM parameter *name*, never
the secret material itself. Nothing in this package may hold a raw token.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from airhead.domain import SourceKind

# Kinds an adapter may declare. NATIVE events are born in Airhead and never
# arrive through an adapter.
ADAPTER_KINDS: frozenset[SourceKind] = frozenset(
    {SourceKind.GOOGLE, SourceKind.CALDAV, SourceKind.GRAPH, SourceKind.ICS}
)


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """Static connection settings for one configured source.

    `settings` carries provider-specific non-secret knobs (calendar URL,
    tenant id, ...). Anything secret lives behind `secret_ref` in SSM.
    """

    source_id: str
    household_id: str
    kind: SourceKind
    secret_ref: str | None = None  # SSM parameter name, not a secret value.
    settings: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Credentials:
    """An authorized handle produced by `authorize`.

    Opaque to everything outside the adapter that minted it. `expires_at`
    lets the sync loop refresh proactively; None means non-expiring
    (e.g. an ICS secret URL).
    """

    kind: SourceKind
    token_ref: str  # SSM parameter name or adapter-opaque handle; never raw secret material.
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CalendarRef:
    """One calendar the authorized principal can see, for the picker UI."""

    calendar_id: str
    display_name: str
    read_only: bool = False


@dataclass(frozen=True, slots=True)
class SyncCursor:
    """Incremental-sync position: syncToken (google), ctag (caldav),
    delta link (graph), ETag/Last-Modified (ics). Opaque to everything but
    the adapter; persisted verbatim on `Source.cursor`."""

    value: str


@dataclass(frozen=True, slots=True)
class ExternalEvent:
    """A provider event normalized to one shape, pre-domain.

    Exactly one of (`start_utc`/`end_utc`) or (`start_date`/`end_date`,
    with `all_day=True`) is populated — all-day events stay floating dates.
    `content_fingerprint()`-style comparison happens later on the domain
    Event; this record is immutable source truth.
    """

    external_id: str
    title: str
    tz: str
    all_day: bool = False
    start_utc: datetime | None = None
    end_utc: datetime | None = None
    start_date: date | None = None
    end_date: date | None = None  # Exclusive, per iCalendar convention.
    rrule: str | None = None
    exdates: tuple[datetime, ...] = ()
    recurrence_id: str | None = None  # Original start of the overridden instance.
    location: str | None = None
    etag: str | None = None

    def __post_init__(self) -> None:
        if self.all_day:
            ok = self.start_date is not None and self.end_date is not None
            bad = self.start_utc is not None or self.end_utc is not None
        else:
            ok = self.start_utc is not None and self.end_utc is not None
            bad = self.start_date is not None or self.end_date is not None
        if not ok or bad:
            raise ValueError(
                "all-day events carry start_date/end_date only; "
                "timed events carry start_utc/end_utc only"
            )


@dataclass(frozen=True, slots=True)
class PullResult:
    """One incremental pull: upserts, deletions (external ids), next cursor."""

    upserts: tuple[ExternalEvent, ...] = ()
    deletions: tuple[str, ...] = ()
    cursor: SyncCursor | None = None


@dataclass(frozen=True, slots=True)
class ExternalRef:
    """Provider-side identity of an event Airhead pushed (v2)."""

    external_id: str
    etag: str | None = None


@runtime_checkable
class CalendarSource(Protocol):
    """The adapter seam. Concrete adapters are constructed with their transport
    dependencies injected so tests never touch the network."""

    kind: SourceKind

    def authorize(self, config: SourceConfig) -> Credentials:
        """Exchange stored configuration for a usable credential handle.
        Raises AuthError when the grant is missing, expired, or revoked."""
        ...

    def list_calendars(self, creds: Credentials) -> list[CalendarRef]:
        """Calendars the principal can read, for source setup UI."""
        ...

    def pull(self, creds: Credentials, cursor: SyncCursor | None) -> PullResult:
        """Changes since `cursor`; None means full initial sync. A stale or
        invalidated cursor raises CursorInvalid — the caller restarts with
        None rather than guessing."""
        ...

    # -- v2: declared now so the write path is additive, not a redesign. --

    def push(self, creds: Credentials, payload: ExternalEvent) -> ExternalRef:
        """Create/update the event upstream. v1 adapters raise NotImplementedError."""
        ...

    def remove(self, creds: Credentials, ref: ExternalRef) -> None:
        """Delete upstream. v1 adapters raise NotImplementedError."""
        ...


class AdapterError(RuntimeError):
    """Adapter-layer failure. Implementations wrap provider/transport
    exceptions in this so nothing above the adapter catches an HTTP or
    provider-SDK type (mirrors repo.base.RepoError)."""


class AuthError(AdapterError):
    """Credential missing, expired, or revoked. The 7-day Testing-status
    expiry (risk R1) surfaces here, visibly, never as silent staleness."""


class CursorInvalid(AdapterError):
    """The incremental cursor was rejected upstream (e.g. Google 410 GONE).
    Callers respond by re-pulling from scratch with cursor=None."""
