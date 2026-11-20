"""Searching *from* one place: the range it sweeps, what each value found, and what it costs.

Everywhere mode asks which pairs in the whole candidate set differ by exactly *n* characters, and
its cost is a pair count in the millions. From-a-place mode asks how near to identical one chosen
place's name is to the names it could be confused with, and its cost is one name-length group: the
places it can be compared with, once each (#9).

Everything here is the seam - `search.open_search` and `SearchSession.pairs` - because that is the
one place the rules can live (ADR 0002). The HTTP and browser adapters are checked in
`tests/test_http_search.py` and `tests/test_browser_app.py` for presenting this and adding nothing.

The distances a sweep reports are pinned to places whose positions are far apart on purpose: the
two Limas are 5,896 km and 6,128 km from Lima, Peru and only a few hundred km from each other, so
a search that measured from the wrong one of them would be caught by the numbers rather than by a
comment.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from support import (
    BOBA,
    LIMA_CA,
    LIMA_PE,
    LIMA_US,
    LIMAS,
    LINA,
    LINO,
    lima_family_database,
)

from distnsimilarity.core.distance import great_circle_distance_km
from distnsimilarity.core.place_lookup import PlaceNotFound
from distnsimilarity.core.sweep import DifferenceRange
from distnsimilarity.core.topk import Ranking
from distnsimilarity.search import (
    SearchControl,
    SearchRequest,
    SearchSession,
    open_search,
)

# The family and the ids live in `support.lima_family_rows`, because the seam, the HTTP adapter and
# the page all ask about the same three Limas, and about `Limas`, which is one character too long to
# have a character difference with any of them.

#: The partner set of Lima, Peru, in the order a candidate walk hands it over: population
#: ascending. The origin is not in it, and neither is `Limas`.
PARTNERS = [LIMA_CA, BOBA, LINO, LINA, LIMA_US]
#: What each of those partners is from Lima, Peru, in the same order.
DISTANCES = [6_128.289, 12_303.218, 11_186.930, 9_921.888, 5_896.192]
#: The partner set at a floor of 5,000 people: the 3,000-person Lima is not a candidate.
PARTNERS_AT_5000 = [BOBA, LINO, LINA, LIMA_US]


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return lima_family_database(tmp_path / "places.sqlite3")


def open_from(
    database: Path,
    geonameid: int,
    *,
    difference: DifferenceRange | None = None,
    poll_every_comparisons: int | None = None,
    **request,
) -> SearchSession:
    """A search from one place, on a control that can be told to stop after any one partner."""
    control = (
        None
        if poll_every_comparisons is None
        else SearchControl(poll_every_comparisons=poll_every_comparisons)
    )
    return open_search(
        SearchRequest(from_place=geonameid, database=database, difference=difference, **request),
        control=control,
    )


def finds(session: SearchSession) -> list:
    """Pull the whole sweep through the session, which is the only way it runs."""
    return list(session.pairs)


def partner_ids(session: SearchSession) -> list[int]:
    return [pair.second.geonameid for pair in finds(session)]


def counts(session: SearchSession) -> dict[int, int]:
    return {count.character_difference: count.pairs for count in session.stats.differences}


def excluded(session: SearchSession) -> list[int]:
    return [count.character_difference for count in session.stats.differences if count.excluded]


def distances(session: SearchSession) -> list[float]:
    return [ranked.distance_km for ranked in session.snapshot()]


class TestASearchFromAPlace:
    def test_it_finds_the_places_that_share_its_name(self, database: Path) -> None:
        """AC: "a name shared across countries produces results at differing-character count 0".
        Two places, one name, three countries' worth of distance between them: this is the whole
        reason the range starts at zero."""
        session = open_from(database, LIMA_PE, difference=DifferenceRange(limit=0))
        try:
            assert partner_ids(session) == [LIMA_CA, LIMA_US]
            assert counts(session) == {0: 2}
        finally:
            session.close()

    def test_it_walks_the_whole_range_in_one_pass(self, database: Path) -> None:
        """Zero through three in one walk: each partner is read once, not once per value of the
        range, which is what a threshold at a time would cost."""
        session = open_from(database, LIMA_PE, difference=DifferenceRange(limit=3))
        try:
            assert partner_ids(session) == PARTNERS
            assert counts(session) == {0: 2, 1: 1, 2: 1, 3: 1}
        finally:
            session.close()

    def test_it_compares_only_the_names_it_can_have_a_difference_with(
        self, database: Path
    ) -> None:
        """`Limas` is a place, is a candidate, and cannot have a character difference with `Lima`
        at all: reading it would be work that cannot produce an answer."""
        session = open_from(database, LIMA_PE, difference=DifferenceRange(limit=3))
        try:
            assert LIMAS not in partner_ids(session)
            assert session.stats.pairs_compared == 5, "five partners, five comparisons, once each"
        finally:
            session.close()

    def test_the_population_floor_applies_to_the_partners_not_to_the_origin(
        self, database: Path
    ) -> None:
        """The dataset and the floor decide what a selected place is compared with. The place it
        starts from is not filtered by them: a reader can search from a small place."""
        session = open_from(
            database, LIMA_PE, difference=DifferenceRange(limit=0), min_population=5_000
        )
        try:
            assert partner_ids(session) == [LIMA_US], "the 3,000-person Lima is not a candidate"
            assert session.stats.candidate_pairs == 4
        finally:
            session.close()

    def test_the_selected_place_is_never_one_of_its_own_results(self, database: Path) -> None:
        """AC: "the selected place is excluded from its own results". Everywhere mode reports a
        place against itself on purpose - that is how a threshold of zero reaches every name - so
        this rule belongs to the sweep rather than to the candidate rule."""
        session = open_from(database, LIMA_PE, difference=DifferenceRange(limit=0))
        try:
            pairs = finds(session)
            assert [pair.second.geonameid for pair in pairs] == [LIMA_CA, LIMA_US]
            assert all(pair.first.geonameid == LIMA_PE for pair in pairs), (
                "every pair is the selected place with something else, never the something else "
                "with itself"
            )
        finally:
            session.close()

    def test_it_is_refused_when_the_place_does_not_exist(self, database: Path) -> None:
        with pytest.raises(PlaceNotFound):
            open_from(database, 42)

    def test_the_finds_are_ranked_by_the_cores_ranking(self, database: Path) -> None:
        """The same ranking everywhere mode uses, so "best" cannot mean one thing over here and
        another thing over there."""
        nearest = open_from(
            database,
            LIMA_PE,
            direction=Ranking.LEAST,
            top_k=2,
            difference=DifferenceRange(limit=3),
        )
        furthest = open_from(
            database,
            LIMA_PE,
            direction=Ranking.GREATEST,
            top_k=2,
            difference=DifferenceRange(limit=3),
        )
        try:
            assert partner_ids(nearest) == PARTNERS, "the walk does not change for a ranking"
            assert distances(nearest) == pytest.approx([5_896.192, 6_128.289])
            finds(furthest)
            assert distances(furthest) == pytest.approx([12_303.218, 11_186.930])
        finally:
            nearest.close()
            furthest.close()

    def test_a_distance_is_between_the_selected_place_and_the_partner(self, database: Path) -> None:
        """AC: distances come from "the selected place's own coordinates, not the coordinates of
        another place with the same name". `Lima CA` is 434 km from `Lima US` and 6,128 km from
        Lima, Peru, so only the origin's own position can produce these numbers."""
        from_peru = open_from(database, LIMA_PE, difference=DifferenceRange(limit=0))
        try:
            found = {pair.second.geonameid: pair.distance_km for pair in finds(from_peru)}
            assert found[LIMA_US] == pytest.approx(
                great_circle_distance_km(-12.0464, -77.0428, 40.5736, -84.1084)
            )
            assert found[LIMA_CA] == pytest.approx(
                great_circle_distance_km(-12.0464, -77.0428, 43.0, -80.0)
            )
        finally:
            from_peru.close()

        from_ohio = open_from(database, LIMA_US, difference=DifferenceRange(limit=0))
        try:
            from_ohio_finds = {
                pair.second.geonameid: pair.distance_km for pair in finds(from_ohio)
            }
        finally:
            from_ohio.close()
        assert sorted(from_ohio_finds) == sorted([LIMA_CA, LIMA_PE])
        assert from_ohio_finds[LIMA_PE] == pytest.approx(found[LIMA_US]), (
            "the same two places are the same distance apart whichever of them the reader "
            "picked to start from"
        )
        assert from_ohio_finds[LIMA_CA] == pytest.approx(434.443), (
            "measuring from Ohio rather than Peru is a different answer, and a short one"
        )


class TestWhatEachValueOfTheRangeFound:
    """AC: "the number of candidates produced by each character-difference value is shown
    alongside the results as the search runs", "a switch excludes identical names"."""

    def test_the_tally_is_there_before_anything_has_been_compared(self, database: Path) -> None:
        """A reader can see which values a sweep will report, and each starts at nothing because
        nothing has been walked yet."""
        session = open_from(database, LIMA_PE, difference=DifferenceRange(limit=2))
        try:
            assert [count.character_difference for count in session.stats.differences] == [0, 1, 2]
            assert [count.pairs for count in session.stats.differences] == [0, 0, 0]
        finally:
            session.close()

    def test_the_tally_moves_with_the_walk(self, database: Path) -> None:
        """The counts arrive as the sweep goes, on the same poll boundaries as the comparison
        count, so a watcher sees "2 differing characters: nothing yet" rather than a silent gap."""
        session = open_from(
            database,
            LIMA_PE,
            difference=DifferenceRange(limit=3),
            min_population=5_000,
            poll_every_comparisons=1,
        )
        seen: list[list[int]] = []
        try:
            for _ in session.pairs:
                seen.append([count.pairs for count in session.stats.differences])
            assert [len(row) for row in seen] == [4, 4, 4, 4]
            assert seen[0] == [0, 0, 0, 0], (
                "the tally is a poll window behind the walk at worst, and never ahead of it: a "
                "count of a pair that has not been compared would be a promise"
            )
            assert seen[-1] == [0, 1, 1, 1], (
                "the fourth partner's own pair is not in the tally a reader sees next to it - the "
                "walk was still on it when the counts were last taken"
            )
        finally:
            session.close()
        assert counts(session) == {0: 1, 1: 1, 2: 1, 3: 1}, (
            "and once the walk is over, the tally has caught up with it"
        )

    def test_excluding_identical_names_hides_the_pairs_and_not_the_count(
        self, database: Path
    ) -> None:
        """AC: "a switch excludes identical names". The pairs go; the number of them stays,
        because the sweep walked past them either way and hiding the count would report a search
        this search did not run."""
        session = open_from(
            database,
            LIMA_PE,
            difference=DifferenceRange(limit=3, exclude_identical_names=True),
        )
        try:
            assert partner_ids(session) == [BOBA, LINO, LINA]
            assert counts(session) == {0: 2, 1: 1, 2: 1, 3: 1}
            assert excluded(session) == [0]
        finally:
            session.close()

    def test_a_range_of_zero_sweeps_the_duplicate_names_only(self, database: Path) -> None:
        session = open_from(database, LIMA_PE, difference=DifferenceRange(limit=0))
        try:
            assert len(finds(session)) == 2
            assert counts(session) == {0: 2}
            assert excluded(session) == []
        finally:
            session.close()

    def test_the_tally_is_reported_even_where_it_found_nothing(self, database: Path) -> None:
        """`Lima` has no partner two, three or four characters away once the floor is 25,000, and
        the sweep says so instead of shortening its own range."""
        session = open_from(
            database, LIMA_PE, difference=DifferenceRange(limit=4), min_population=25_000
        )
        try:
            assert partner_ids(session) == [LINA, LIMA_US]
            assert counts(session) == {0: 1, 1: 1, 2: 0, 3: 0, 4: 0}
        finally:
            session.close()


class TestWhatASearchFromAPlaceCosts:
    """AC: "the cost shown before the search runs reflects the reduced candidate set"."""

    def test_the_cost_is_the_partner_set_once_rather_than_every_pair(self, database: Path) -> None:
        from_place = open_from(database, LIMA_PE)
        everywhere = open_search(
            SearchRequest(n=0, database=database, equal_length_prefilter=False)
        )
        try:
            assert from_place.stats.candidate_pairs == 5
            assert everywhere.stats.candidate_pairs == 28, (
                "seven places, every one against itself and the later ones: the same question "
                "asked of everybody rather than of one place"
            )
        finally:
            from_place.close()
            everywhere.close()

    def test_the_cost_leaves_out_the_place_it_starts_from(self, database: Path) -> None:
        """A comparison the search will never make is not a cost, and a progress bar that counted
        it would never reach its end."""
        session = open_from(database, LIMA_CA, difference=DifferenceRange(limit=0))
        try:
            # `Lima CA` is in its own name-length group and is not a partner of itself, so the
            # count is the other four places of four letters plus the two other Limas less itself.
            assert session.stats.candidate_pairs == 5
        finally:
            session.close()

    def test_the_cost_is_said_before_the_first_partner_is_read(self, database: Path) -> None:
        session = open_from(database, LIMA_PE)
        try:
            assert session.stats.candidate_pairs == 5
            assert session.stats.pairs_compared == 0
        finally:
            session.close()

    def test_the_dataset_behind_the_sweep_is_still_reported(self, database: Path) -> None:
        """A sweep compares one name-length group and nothing else, but the candidate set behind
        it is still the dataset's, and an interface says both numbers rather than one."""
        session = open_from(database, LIMA_PE, dataset="5000")
        try:
            assert session.stats.places == 6
            assert session.stats.population_floor == 5_000
            assert session.stats.candidate_pairs == 4
        finally:
            session.close()


class TestStoppingASearchFromAPlace:
    def test_a_sweep_stops_when_it_is_told_to(self, database: Path) -> None:
        session = open_from(
            database, LIMA_PE, difference=DifferenceRange(limit=3), poll_every_comparisons=1
        )
        try:
            first = next(iter(session.pairs))
            session.control.cancel("the reader hung up")
            remaining = list(session.pairs)
            assert first.second.geonameid == LIMA_CA
            assert remaining == [], "the walk ends between two partners, where the stop landed"
            assert session.stats.cancelled is True
            assert session.stats.completed is False
        finally:
            session.close()

    def test_a_stopped_sweep_says_how_far_it_got(self, database: Path) -> None:
        """The tally a sweep keeps is the one for the partners it really compared, so a stopped
        search reports less rather than nothing."""
        session = open_from(
            database, LIMA_PE, difference=DifferenceRange(limit=3), poll_every_comparisons=1
        )
        try:
            next(iter(session.pairs))
            session.control.cancel("hung up")
            list(session.pairs)
            assert session.stats.pairs_compared < session.stats.candidate_pairs
            assert sum(counts(session).values()) == session.stats.pairs_emitted == 1
            assert session.stats.retryable is True
        finally:
            session.close()
