"""The equal-length prefilter may change how much work a scan does, never which pairs it reports.

Two paths through the same rules are compared here: the full comparison, which offers every
place to every later place as the original tool did, and the prefiltered one, which offers a
place only the later places whose ASCII name is the same length. The claim under test is
stronger than equal counts - the two must report the same pairs, in the same order, with the
same distances - and it is checked in both directions, because a prefilter can fail by
dropping a pair *or* by inventing one.

The fixtures are deliberately awkward: equal-length near-misses that a broken prefilter
would happily skip past, and unequal-length lookalikes (`Aba`/`Abba`) that a broken
prefilter would happily compare.
"""

from __future__ import annotations

import inspect
import random
from pathlib import Path
from typing import Iterable, Sequence

import pytest

from distnsimilarity import cli
from distnsimilarity.core import scan as scan_module
from distnsimilarity.core.places import Place, read_places
from distnsimilarity.core.prefilter import EqualLengthGroups, all_pair_count
from distnsimilarity.core.scan import scan
from support import dump_row, write_dump

REPO_ROOT = Path(__file__).resolve().parent.parent

NEAR_MISSES = ["Aba", "Abe", "Aec", "Aea", "Aaa"]
LOOKALIKES = ["Aba", "Abba", "Tara", "Taara", "Genoa", "Geneva", "Os", "Oss"]
EVERY_POSITION_DIFFERS = ["Aba", "Zed", "Xabc", "abcX"]
DUPLICATES_AND_CASE_VARIANTS = ["Lima", "Lima", "lima", "LIMA"]
ACCENTED_AND_FOLDED = ["Geneve", "Genève", "Zurich", "Zürich"]


def generated_names(seed: int = 20240517, count: int = 150) -> list[str]:
    """A fixed-seed crowd of short names in the two-to-five character range: the
    interesting collisions a hand-written fixture would be too tidy to produce."""
    maker = random.Random(seed)
    return [
        "".join(maker.choice("abcde") for _ in range(maker.randint(2, 5))) for _ in range(count)
    ]


FIXTURES: dict[str, list[str]] = {
    "equal-length near-misses": NEAR_MISSES,
    "unequal-length lookalikes": LOOKALIKES,
    "every position differing": EVERY_POSITION_DIFFERS,
    "duplicates and case variants": DUPLICATES_AND_CASE_VARIANTS,
    "accented and folded spellings": ACCENTED_AND_FOLDED,
    "all of them at once": NEAR_MISSES + LOOKALIKES + EVERY_POSITION_DIFFERS
    + DUPLICATES_AND_CASE_VARIANTS + ACCENTED_AND_FOLDED,
    "a fixed-seed crowd": generated_names(),
}

FIXTURE_CASES = [
    pytest.param(names, threshold, id=f"{label} at n={threshold}")
    for label, names in FIXTURES.items()
    for threshold in range(4)
]


def places_named(names: Sequence[str], *, population: int = 10_000) -> list[Place]:
    """Places with those ASCII names, each at its own coordinate and its own id."""
    return [
        Place(
            geonameid=index + 1,
            asciiname=name,
            display_name=name,
            # Spread far enough apart that no two pairs share a distance by accident.
            latitude=-60.0 + index * 0.5,
            longitude=-120.0 + index * 0.25,
            population=population,
            country_code="XX",
        )
        for index, name in enumerate(names)
    ]


def signature(matched) -> tuple[int, int, float]:
    """A reported pair: which two places, and how far apart. Exact, not rounded."""
    return (matched.first.geonameid, matched.second.geonameid, matched.distance_km)


def signatures(matcheds: Iterable[object]) -> list[tuple[int, int, float]]:
    return [signature(matched) for matched in matcheds]


def both_paths(
    places: Sequence[Place], threshold: int, population_floor: int | None = None
) -> tuple[list[tuple[int, int, float]], list[tuple[int, int, float]]]:
    """The same scan twice: once walking everything, once walking buckets."""
    full = signatures(
        scan(list(places), threshold, population_floor, equal_length_prefilter=False)
    )
    prefiltered = signatures(scan(list(places), threshold, population_floor))
    return full, prefiltered


class ComparisonRecorder:
    """Wraps the comparison the scan performs, so a test can see what it was asked."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.asked: list[tuple[str, str]] = []
        real = scan_module.matches_character_difference

        def recording(first: str, second: str, character_difference: int) -> bool:
            self.asked.append((first, second))
            return real(first, second, character_difference)

        monkeypatch.setattr(scan_module, "matches_character_difference", recording)

    def unequal_comparisons(self) -> list[tuple[str, str]]:
        return [pair for pair in self.asked if len(pair[0]) != len(pair[1])]


class TestCandidateGenerationBucketsByNameLength:
    def test_places_are_bucketed_by_the_length_of_their_ascii_name(self) -> None:
        groups = EqualLengthGroups.of(places_named(["Os", "Aba", "Abc", "Geneva"]))
        assert groups.positions_by_length == {2: [0], 3: [1, 2], 6: [3]}
        assert len(groups) == 4

    def test_the_bucket_length_is_the_ascii_names_not_the_display_names(self) -> None:
        places = [
            Place(1, "Aba", "A very long display name indeed", 1.0, 2.0, 10, "XX"),
            Place(2, "Abe", "Ab", 3.0, 4.0, 10, "XX"),
            Place(3, "Abba", "Aba", 5.0, 6.0, 10, "XX"),
        ]
        groups = EqualLengthGroups.of(places)
        assert groups.positions_by_length == {3: [0, 1], 4: [2]}, (
            "places belong in the same bucket when their ASCII names are the same length: "
            "display names are shown to a person and are never compared"
        )

    def test_a_places_partners_are_itself_and_the_later_places_of_its_length(self) -> None:
        # Aba(0) Os(1) Abe(2) Aec(3) Osso(4) - the 3-character bucket is 0, 2 and 3.
        groups = EqualLengthGroups.of(places_named(["Aba", "Os", "Abe", "Aec", "Osso"]))
        assert list(groups.partners_from(0)) == [0, 2, 3]
        assert list(groups.partners_from(1)) == [1], "the only 2-character name"
        assert list(groups.partners_from(2)) == [2, 3]
        assert list(groups.partners_from(3)) == [3], "a later place has no later partners"
        assert list(groups.partners_from(4)) == [4], "the only 4-character name"

    def test_an_empty_candidate_set_has_no_buckets(self) -> None:
        groups = EqualLengthGroups.of([])
        assert groups.positions_by_length == {}
        assert groups.pair_count() == 0

    def test_the_buckets_are_fewer_pairs_than_walking_everything(self) -> None:
        names = ["Aba", "Abe", "Os", "Oss", "Geneva"]
        groups = EqualLengthGroups.of(places_named(names))
        assert all_pair_count(len(names)) == 15, "5 places, each against itself and later ones"
        assert groups.pair_count() == 8, (
            "three 3-character names (3 pairs each way: 6), one 2-character name (1) and "
            "one 6-character name (1); Os and Oss are the same length, Osso is not Os"
        )


class TestOnlyWithinGroupComparisons:
    def test_the_prefiltered_scan_never_compares_unequal_lengths(self, monkeypatch) -> None:
        recorder = ComparisonRecorder(monkeypatch)
        list(scan(places_named(FIXTURES["all of them at once"]), 1))
        assert recorder.unequal_comparisons() == [], (
            "the prefilter offered a pair of unequal-length names to the comparison, which "
            "the rule would refuse anyway - the point of the bucketing is never to ask"
        )

    def test_the_full_comparison_does_ask_about_unequal_lookalikes(self, monkeypatch) -> None:
        """The recorder is live: without the prefilter the awkward pairs are asked about,
        so the test above is not passing because nothing is ever compared."""
        recorder = ComparisonRecorder(monkeypatch)
        list(scan(places_named(LOOKALIKES), 1, equal_length_prefilter=False))
        assert ("Aba", "Abba") in recorder.asked
        assert len(recorder.unequal_comparisons()) > 0

    def test_the_prefiltered_scan_asks_about_strictly_fewer_pairs(self) -> None:
        names = FIXTURES["all of them at once"]
        places = places_named(names)
        groups = EqualLengthGroups.of(places)
        assert groups.pair_count() < all_pair_count(len(places))
        assert sum(groups.partner_count_from(index) for index in range(len(places))) == (
            groups.pair_count()
        ), "the count a scan would do is the count the buckets hold"


class TestBothPathsAgree:
    @pytest.mark.parametrize("names,threshold", FIXTURE_CASES)
    def test_no_pair_is_lost_by_the_prefilter(
        self, names: list[str], threshold: int
    ) -> None:
        full, prefiltered = both_paths(places_named(names), threshold)
        lost = set(full) - set(prefiltered)
        assert not lost, (
            f"at n={threshold} the prefiltered scan lost {len(lost)} pair(s) the full "
            f"comparison reported: {sorted(lost)[:5]} (place ids, distance km)"
        )

    @pytest.mark.parametrize("names,threshold", FIXTURE_CASES)
    def test_no_pair_is_gained_by_the_prefilter(
        self, names: list[str], threshold: int
    ) -> None:
        full, prefiltered = both_paths(places_named(names), threshold)
        gained = set(prefiltered) - set(full)
        assert not gained, (
            f"at n={threshold} the prefiltered scan invented {len(gained)} pair(s) the full "
            f"comparison did not report: {sorted(gained)[:5]} (place ids, distance km)"
        )

    @pytest.mark.parametrize("names,threshold", FIXTURE_CASES)
    def test_the_two_paths_report_the_same_pairs_in_the_same_order(
        self, names: list[str], threshold: int
    ) -> None:
        full, prefiltered = both_paths(places_named(names), threshold)
        assert prefiltered == full, (
            f"at n={threshold} the two paths agree on the set of pairs but not the order: "
            f"first difference at position "
            f"{next(i for i, (a, b) in enumerate(zip(full, prefiltered)) if a != b)} of "
            f"{len(full)}"
        )

    def test_the_fixtures_really_do_produce_pairs(self) -> None:
        """Guards the tests above against passing on two empty answers."""
        for label, names in FIXTURES.items():
            counts = [
                sum(1 for _ in scan(places_named(names), threshold)) for threshold in range(4)
            ]
            assert sum(counts) > 0, f"fixture {label!r} produced no pairs at n=0..3: {counts}"

    def test_the_fixed_seed_crowd_is_big_enough_to_be_worth_bothering(self) -> None:
        names = FIXTURES["a fixed-seed crowd"]
        places = places_named(names)
        assert len(names) == 150
        assert {len(name) for name in names} == {2, 3, 4, 5}
        groups = EqualLengthGroups.of(places)
        assert groups.pair_count() < all_pair_count(len(places)) / 2, (
            "with four name lengths the prefilter should be shedding most of the work; "
            f"buckets hold {groups.pair_count()} of {all_pair_count(len(places))} pairs"
        )


class TestTheFloorAndTheOddCasesSurviveTheChange:
    def test_a_place_below_the_floor_is_in_no_bucket_at_all(self) -> None:
        places = [
            Place(1, "Aba", "Aba", 0.0, 0.0, 500, "XX"),
            Place(2, "Abe", "Abe", 1.0, 1.0, 5_000, "XX"),
            Place(3, "Aec", "Aec", 2.0, 2.0, 50_000, "XX"),
        ]
        # "Aba" is below the floor; "Abe"/"Aec" differ in 2 of 3 positions.
        full, prefiltered = both_paths(places, 2, population_floor=1_000)
        assert full == prefiltered, "the floor must not bite differently on the two paths"
        assert full == prefiltered == [(2, 3, full[0][2])], (
            "only the two places at or above the floor can be a pair, and the one pair they "
            "form must survive both paths"
        )

    def test_a_place_exactly_on_the_floor_is_still_compared(self) -> None:
        places = [
            Place(1, "Aba", "Aba", 0.0, 0.0, 1_000, "XX"),
            Place(2, "Abe", "Abe", 1.0, 1.0, 1_000, "XX"),
        ]
        full, prefiltered = both_paths(places, 1, population_floor=1_000)
        assert full == prefiltered
        assert [(first, second) for first, second, _ in full] == [(1, 2)], (
            "a place sitting exactly on the floor takes part in the comparison"
        )

    def test_every_place_still_pairs_with_itself(self) -> None:
        names = FIXTURES["all of them at once"]
        places = places_named(names)
        full = signatures(scan(places, 0, equal_length_prefilter=False))
        prefiltered = signatures(scan(places, 0))
        assert full == prefiltered
        self_pairs = [pair for pair in prefiltered if pair[0] == pair[1]]
        assert len(self_pairs) == len(places), (
            "a place is a pair with itself, so a threshold of 0 reports one self-pair per "
            f"candidate: expected {len(places)}, got {len(self_pairs)}"
        )
        assert {distance for _, _, distance in self_pairs} == {0.0}

    def test_unequal_lookalikes_are_not_reported_by_either_path(self) -> None:
        places = places_named(LOOKALIKES)
        by_id = {place.geonameid: place.asciiname for place in places}
        for threshold in range(4):
            full, prefiltered = both_paths(places, threshold)
            assert full == prefiltered
            for first, second, _ in full:
                assert len(by_id[first]) == len(by_id[second]), (
                    f"a pair of unequal-length names was reported at n={threshold}: "
                    f"{by_id[first]}/{by_id[second]}"
                )


class TestTheOptimisationIsReversibleOnItsOwn:
    def test_the_prefilter_is_on_unless_asked_otherwise(self) -> None:
        parameter = inspect.signature(scan).parameters["equal_length_prefilter"]
        assert parameter.default is True
        assert scan_module.USE_EQUAL_LENGTH_PREFILTER_BY_DEFAULT is True

    def test_switching_it_off_changes_nothing_a_person_can_see(
        self, tmp_path: Path, capsys
    ) -> None:
        rows = [
            dump_row(index + 1, name, -30.0 + index * 0.4, 20.0 - index * 0.2, population=10_000)
            for index, name in enumerate(FIXTURES["all of them at once"])
        ]
        dump = write_dump(tmp_path / "cities3.txt", rows)
        outputs = {}
        for flag in ([], ["--full-comparison"]):
            output = tmp_path / f"out{len(flag)}.csv"
            exit_code = cli.main(
                ["--dataset", str(dump), "--n", "1", "--output", str(output), "--quiet", *flag]
            )
            assert exit_code == 0, capsys.readouterr().err
            outputs[flag and "full" or "prefiltered"] = output.read_bytes()

        assert outputs["prefiltered"] == outputs["full"], (
            "the same query must produce the same bytes whichever comparison path runs"
        )
        assert outputs["prefiltered"].splitlines()[0] == (
            b"place1,lat1,lon1,place2,lat2,lon2,haversinedistance"
        )

    def test_the_switch_is_documented_where_it_is_used(self) -> None:
        assert "--full-comparison" in cli.build_parser().format_help()
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        assert "--full-comparison" in readme, "a reversible switch has to be written down"


class TestRealDataAgreement:
    @pytest.mark.slow
    def test_a_slice_of_a_real_dump_agrees_pair_for_pair(self) -> None:
        """Real GeoNames names, with the duplicates and accents the fixtures only imitate.

        A full comparison is quadratic, so the slice is kept small enough to walk twice;
        the arithmetic of what the prefilter saves over the whole dump is in the ticket.
        """
        places = read_places(REPO_ROOT / "cities15000.txt")[:1_000]
        for threshold in (0, 1, 2):
            full, prefiltered = both_paths(places, threshold)
            assert full, f"no pairs at all at n={threshold}: the fixture is wrong"
            assert prefiltered == full, (
                f"real data disagrees at n={threshold}: {len(full)} pair(s) from the full "
                f"comparison, {len(prefiltered)} from the prefiltered one"
            )
