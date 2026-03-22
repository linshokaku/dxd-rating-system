from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from math import pow
from typing import Protocol

from dxd_rating.platform.db.models import MatchFormat
from dxd_rating.shared.constants import MatchFormatDefinition, MatchQueueClassDefinition

RATING_DIVISOR = 400.0


class RandomLike(Protocol):
    def randrange(self, stop: int, /) -> int: ...


@dataclass(frozen=True, slots=True)
class QueueEntrySnapshot:
    queue_entry_id: int
    player_id: int
    match_format: MatchFormat
    rating: float
    joined_at: datetime


@dataclass(frozen=True, slots=True)
class PreparedMatchPlan:
    match_format: MatchFormat
    team_a_entry_ids: tuple[int, ...]
    team_b_entry_ids: tuple[int, ...]


def prepare_matches_for_batch(
    queue_entries: Sequence[QueueEntrySnapshot],
    format_definition: MatchFormatDefinition,
    *,
    random_generator: RandomLike,
) -> tuple[PreparedMatchPlan, ...]:
    if format_definition.match_format == MatchFormat.ONE_VS_ONE:
        return _prepare_one_vs_one_matches(
            queue_entries,
            format_definition,
            random_generator=random_generator,
        )

    team_a_entries, team_b_entries = _assign_balanced_match_teams(
        queue_entries,
        team_player_count=format_definition.team_size,
        random_generator=random_generator,
    )
    return (
        PreparedMatchPlan(
            match_format=format_definition.match_format,
            team_a_entry_ids=tuple(entry.queue_entry_id for entry in team_a_entries),
            team_b_entry_ids=tuple(entry.queue_entry_id for entry in team_b_entries),
        ),
    )


def is_queue_join_allowed(
    *,
    rating: float,
    queue_class_definition: MatchQueueClassDefinition,
    definitions_for_format: Sequence[MatchQueueClassDefinition],
) -> bool:
    if not definitions_for_format:
        raise ValueError("definitions_for_format must contain at least one queue class definition")
    if not all(definition.target_rating is not None for definition in definitions_for_format):
        return True
    if len(definitions_for_format) == 1:
        return True

    queue_index = next(
        (
            index
            for index, definition in enumerate(definitions_for_format)
            if definition.queue_class_id == queue_class_definition.queue_class_id
        ),
        None,
    )
    if queue_index is None:
        raise ValueError(
            f"Unknown queue_class_id for format: {queue_class_definition.queue_class_id}"
        )

    if queue_index == 0:
        upper_definition = definitions_for_format[1]
        assert upper_definition.target_rating is not None
        return rating < upper_definition.target_rating

    if queue_index == len(definitions_for_format) - 1:
        lower_definition = definitions_for_format[-2]
        assert lower_definition.target_rating is not None
        return lower_definition.target_rating <= rating

    lower_definition = definitions_for_format[queue_index - 1]
    upper_definition = definitions_for_format[queue_index + 1]
    assert lower_definition.target_rating is not None
    assert upper_definition.target_rating is not None
    return lower_definition.target_rating <= rating < upper_definition.target_rating


def validate_queue_class_definitions(
    definitions: Sequence[MatchQueueClassDefinition],
    *,
    supported_match_formats: Collection[MatchFormat],
) -> tuple[MatchQueueClassDefinition, ...]:
    normalized_definitions = tuple(definitions)
    if not normalized_definitions:
        raise ValueError("At least one queue class definition is required")

    queue_class_ids: set[str] = set()
    normalized_queue_names_by_format: dict[MatchFormat, set[str]] = {}
    definitions_by_format: dict[MatchFormat, list[MatchQueueClassDefinition]] = {}

    for definition in normalized_definitions:
        if definition.match_format not in supported_match_formats:
            raise ValueError(f"Unsupported match_format: {definition.match_format.value}")
        normalized_queue_name = definition.queue_name.strip().casefold()
        if not normalized_queue_name:
            raise ValueError("queue_name must not be empty")
        if definition.queue_class_id in queue_class_ids:
            raise ValueError(f"Duplicate queue_class_id: {definition.queue_class_id}")
        names_for_format = normalized_queue_names_by_format.setdefault(
            definition.match_format,
            set(),
        )
        if normalized_queue_name in names_for_format:
            raise ValueError(f"Duplicate queue_name: {definition.queue_name}")

        queue_class_ids.add(definition.queue_class_id)
        names_for_format.add(normalized_queue_name)
        definitions_by_format.setdefault(definition.match_format, []).append(definition)

    for definitions_for_format in definitions_by_format.values():
        has_target_ratings = [
            definition.target_rating is not None for definition in definitions_for_format
        ]
        if any(has_target_ratings) and not all(has_target_ratings):
            raise ValueError(
                "queue_class_definitions for a match_format must either all define "
                "target_rating or all omit it"
            )

        previous_target_rating: float | None = None
        for definition in definitions_for_format:
            if definition.target_rating is None:
                continue
            if (
                previous_target_rating is not None
                and definition.target_rating <= previous_target_rating
            ):
                raise ValueError(
                    "target_rating values must be strictly increasing within a match_format"
                )
            previous_target_rating = definition.target_rating

    return normalized_definitions


def _prepare_one_vs_one_matches(
    queue_entries: Sequence[QueueEntrySnapshot],
    format_definition: MatchFormatDefinition,
    *,
    random_generator: RandomLike,
) -> tuple[PreparedMatchPlan, ...]:
    if len(queue_entries) != format_definition.players_per_batch:
        raise ValueError(
            f"1v1 batch must contain exactly {format_definition.players_per_batch} queue entries"
        )

    ranked_entries = _sort_entries_by_rating_desc_with_random_ties(
        queue_entries,
        random_generator=random_generator,
    )
    prepared_matches: list[PreparedMatchPlan] = []
    for index in range(0, len(ranked_entries), 2):
        first_entry = ranked_entries[index]
        second_entry = ranked_entries[index + 1]
        if random_generator.randrange(2) == 0:
            team_a_entries = (first_entry,)
            team_b_entries = (second_entry,)
        else:
            team_a_entries = (second_entry,)
            team_b_entries = (first_entry,)
        prepared_matches.append(
            PreparedMatchPlan(
                match_format=MatchFormat.ONE_VS_ONE,
                team_a_entry_ids=tuple(entry.queue_entry_id for entry in team_a_entries),
                team_b_entry_ids=tuple(entry.queue_entry_id for entry in team_b_entries),
            )
        )

    return tuple(prepared_matches)


def _assign_balanced_match_teams(
    queue_entries: Sequence[QueueEntrySnapshot],
    *,
    team_player_count: int,
    random_generator: RandomLike,
) -> tuple[tuple[QueueEntrySnapshot, ...], tuple[QueueEntrySnapshot, ...]]:
    first_group, second_group = _find_best_team_split(
        queue_entries,
        team_player_count=team_player_count,
    )
    if random_generator.randrange(2) == 0:
        team_a_entries, team_b_entries = first_group, second_group
    else:
        team_a_entries, team_b_entries = second_group, first_group

    return (
        _sort_team_entries(team_a_entries),
        _sort_team_entries(team_b_entries),
    )


def _find_best_team_split(
    queue_entries: Sequence[QueueEntrySnapshot],
    *,
    team_player_count: int,
) -> tuple[tuple[QueueEntrySnapshot, ...], tuple[QueueEntrySnapshot, ...]]:
    expected_player_count = team_player_count * 2
    if len(queue_entries) != expected_player_count:
        raise ValueError(
            f"Expected {expected_player_count} queue entries, got {len(queue_entries)}"
        )

    best_split: tuple[tuple[QueueEntrySnapshot, ...], tuple[QueueEntrySnapshot, ...]] | None = None
    best_distance_from_even: float | None = None
    all_indices = tuple(range(len(queue_entries)))

    for remaining_indices in combinations(all_indices[1:], team_player_count - 1):
        team_one_indices = (all_indices[0], *remaining_indices)
        team_one_index_set = set(team_one_indices)
        team_one_entries = tuple(queue_entries[index] for index in team_one_indices)
        team_two_entries = tuple(
            queue_entries[index] for index in all_indices if index not in team_one_index_set
        )
        team_one_expected_score = _calculate_expected_score(
            team_one_entries,
            team_two_entries,
        )
        distance_from_even = abs(team_one_expected_score - 0.5)

        if best_distance_from_even is None or distance_from_even < best_distance_from_even:
            best_distance_from_even = distance_from_even
            best_split = (team_one_entries, team_two_entries)

    if best_split is None:
        raise RuntimeError("Failed to find a team split for queue entries")
    return best_split


def _calculate_expected_score(
    team_a_entries: Sequence[QueueEntrySnapshot],
    team_b_entries: Sequence[QueueEntrySnapshot],
) -> float:
    team_a_strength = sum(pow(10.0, entry.rating / RATING_DIVISOR) for entry in team_a_entries)
    team_b_strength = sum(pow(10.0, entry.rating / RATING_DIVISOR) for entry in team_b_entries)
    return team_a_strength / (team_a_strength + team_b_strength)


def _sort_team_entries(
    team_entries: Sequence[QueueEntrySnapshot],
) -> tuple[QueueEntrySnapshot, ...]:
    return tuple(
        sorted(
            team_entries,
            key=lambda entry: (-entry.rating, entry.joined_at, entry.queue_entry_id),
        )
    )


def _sort_entries_by_rating_desc_with_random_ties(
    queue_entries: Sequence[QueueEntrySnapshot],
    *,
    random_generator: RandomLike,
) -> tuple[QueueEntrySnapshot, ...]:
    entries = list(
        sorted(
            queue_entries,
            key=lambda entry: (-entry.rating, entry.joined_at, entry.queue_entry_id),
        )
    )
    ranked_entries: list[QueueEntrySnapshot] = []
    index = 0
    while index < len(entries):
        tie_group = [entries[index]]
        tie_rating = entries[index].rating
        index += 1
        while index < len(entries) and entries[index].rating == tie_rating:
            tie_group.append(entries[index])
            index += 1
        ranked_entries.extend(
            _shuffle_entries(
                tie_group,
                random_generator=random_generator,
            )
        )
    return tuple(ranked_entries)


def _shuffle_entries(
    queue_entries: Sequence[QueueEntrySnapshot],
    *,
    random_generator: RandomLike,
) -> tuple[QueueEntrySnapshot, ...]:
    remaining_entries = list(queue_entries)
    shuffled_entries: list[QueueEntrySnapshot] = []
    while remaining_entries:
        random_index = random_generator.randrange(len(remaining_entries))
        shuffled_entries.append(remaining_entries.pop(random_index))
    return tuple(shuffled_entries)
