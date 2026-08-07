"""One contract, every adapter.

Mirror of the repo layer's shared contract test: every concrete
`CalendarSource` (google, caldav, graph, ics — and test fakes) must pass this
suite against its own fixture, so provider quirks can never leak divergent
behavior past the seam. Import `CalendarSourceContract` into a test module,
subclass it, and provide the two fixtures.

Runs entirely offline: fixtures inject fake transports; nothing here may
touch the network.
"""

from __future__ import annotations

import dataclasses

import pytest

from airhead.adapters.base import (
    ADAPTER_KINDS,
    CalendarRef,
    CalendarSource,
    Credentials,
    ExternalRef,
    PullResult,
    SourceConfig,
)


class CalendarSourceContract:
    """Behavioral contract for any CalendarSource implementation.

    Subclass in a test module and define fixtures:

        @pytest.fixture
        def adapter(self) -> CalendarSource: ...
        @pytest.fixture
        def config(self) -> SourceConfig: ...  # valid config for that adapter

    `config` must be authorizable and `pull(creds, None)` must yield at least
    one upsert, so round-trip assertions have something to bite on.
    """

    def test_satisfies_protocol(self, adapter: CalendarSource) -> None:
        assert isinstance(adapter, CalendarSource)

    def test_kind_is_an_adapter_kind(self, adapter: CalendarSource) -> None:
        assert adapter.kind in ADAPTER_KINDS

    def test_kind_matches_config(self, adapter: CalendarSource, config: SourceConfig) -> None:
        assert config.kind == adapter.kind

    def test_authorize_returns_credentials(
        self, adapter: CalendarSource, config: SourceConfig
    ) -> None:
        creds = adapter.authorize(config)
        assert isinstance(creds, Credentials)
        assert creds.kind is adapter.kind
        # token_ref is a reference (SSM name / opaque handle), never empty.
        assert creds.token_ref

    def test_list_calendars_shape(self, adapter: CalendarSource, config: SourceConfig) -> None:
        creds = adapter.authorize(config)
        calendars = adapter.list_calendars(creds)
        assert isinstance(calendars, list)
        assert all(isinstance(c, CalendarRef) for c in calendars)

    def test_initial_pull_normalizes(self, adapter: CalendarSource, config: SourceConfig) -> None:
        creds = adapter.authorize(config)
        result = adapter.pull(creds, None)
        assert isinstance(result, PullResult)
        assert result.upserts, "contract fixtures must supply at least one event"
        for event in result.upserts:
            assert event.external_id
            # The all-day/timed field split is enforced by ExternalEvent
            # itself; re-run it to catch adapters bypassing __post_init__.
            dataclasses.replace(event)

    def test_pull_yields_reusable_cursor(
        self, adapter: CalendarSource, config: SourceConfig
    ) -> None:
        creds = adapter.authorize(config)
        first = adapter.pull(creds, None)
        assert first.cursor is not None, "pull must return a cursor for incremental sync"
        second = adapter.pull(creds, first.cursor)
        assert isinstance(second, PullResult)
        # An immediate re-pull at the same cursor reports no changes.
        assert second.upserts == ()
        assert second.deletions == ()

    def test_push_declared_but_not_required_in_v1(
        self, adapter: CalendarSource, config: SourceConfig
    ) -> None:
        creds = adapter.authorize(config)
        events = adapter.pull(creds, None).upserts
        try:
            ref = adapter.push(creds, events[0])
        except NotImplementedError:
            pytest.xfail("push is v2; NotImplementedError is the expected v1 answer")
        else:  # pragma: no cover - exercised once v2 adapters exist.
            assert isinstance(ref, ExternalRef)

    def test_remove_declared_but_not_required_in_v1(
        self, adapter: CalendarSource, config: SourceConfig
    ) -> None:
        creds = adapter.authorize(config)
        try:
            adapter.remove(creds, ExternalRef(external_id="ext_contract"))
        except NotImplementedError:
            pytest.xfail("remove is v2; NotImplementedError is the expected v1 answer")
