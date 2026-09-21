"""The curated interval table: deterministic lookups and data hygiene.

A catalog hit is what keeps a routine off the model path, so the matching
rules here are behaviour the household sees ("I said haircut, it knew").
"""

from __future__ import annotations

import pytest

from airhead.domain import Anchor
from airhead.routines import catalog


class TestLookup:
    @pytest.mark.parametrize(
        ("name", "key"),
        [
            ("Cabin air filter 2023 Kia EV6", "ev6_cabin_air_filter"),
            ("Haircut", "haircut_mens"),
            ("Put up the Christmas lights", "christmas_decorations_up"),
            ("clean the gutters", "gutter_cleaning"),
        ],
    )
    def test_matches_common_phrasings(self, name, key):
        entry = catalog.lookup(name)
        assert entry is not None and entry.key == key

    def test_miss_returns_none(self):
        assert catalog.lookup("buy milk") is None

    def test_blank_returns_none(self):
        assert catalog.lookup("") is None
        assert catalog.lookup("the my a") is None

    def test_stop_words_and_plurals_fold(self):
        """ "Change my cabin filters" is the alias "cabin filter" once noise is gone."""
        assert catalog.lookup("Change my cabin filters").key == "ev6_cabin_air_filter"
        assert catalog.lookup("the haircuts").key == "haircut_mens"

    def test_case_and_punctuation_are_ignored(self):
        assert catalog.lookup("HAIRCUT!").key == "haircut_mens"
        assert catalog.lookup("Cabin-air-filter").key == "ev6_cabin_air_filter"

    def test_longest_alias_wins(self):
        """ "haircut trim" is a two-word alias of the longer-styles entry; the
        one-word "haircut" alias of the men's entry must not shadow it."""
        assert catalog.lookup("Haircut trim").key == "haircut_longer_styles"
        assert catalog.lookup("Haircut").key == "haircut_mens"

    def test_get_by_key(self):
        assert catalog.get("haircut_mens").name == "Haircut"
        assert catalog.get("nope") is None


class TestNormalize:
    def test_drops_stop_words(self):
        assert catalog.normalize("get the haircut for my car") == ("haircut", "car")

    def test_folds_simple_plurals_but_not_ss(self):
        assert catalog.normalize("filters gutters glass") == ("filter", "gutter", "glass")

    def test_keeps_short_words_ending_in_s(self):
        # "gas" would become "ga" under a naive rule; the length guard stops that.
        assert catalog.normalize("gas") == ("gas",)


class TestData:
    def test_keys_are_unique(self):
        keys = [e.key for e in catalog.entries()]
        assert len(keys) == len(set(keys))

    def test_every_entry_is_well_formed(self):
        for entry in catalog.entries():
            assert entry.interval_days > 0, entry.key
            assert isinstance(entry.anchor, Anchor), entry.key
            assert entry.name.strip(), entry.key
            assert entry.category.strip(), entry.key
            if entry.interval_min_days is not None and entry.interval_max_days is not None:
                assert entry.interval_min_days <= entry.interval_max_days, entry.key

    def test_every_entry_is_findable_by_its_own_name(self):
        """A name that resolves to a *different* entry means an alias is shadowing it."""
        for entry in catalog.entries():
            hit = catalog.lookup(entry.name)
            assert hit is not None, entry.key
            assert hit.key == entry.key, f"{entry.key} shadowed by {hit.key}"
