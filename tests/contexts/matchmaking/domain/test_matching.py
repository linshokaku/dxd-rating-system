from dataclasses import replace
from datetime import datetime, timezone

import pytest

from dxd_rating.contexts.matchmaking.domain import (
    QueueEntrySnapshot,
    is_queue_join_allowed,
    prepare_matches_for_batch,
    validate_queue_class_definitions,
)
from dxd_rating.platform.db.models import MatchFormat
from dxd_rating.shared.constants import (
    MatchFormatDefinition,
    MatchQueueClassDefinition,
    get_match_format_definition,
)


class FakeRandom:
    def __init__(self, outputs: list[int]) -> None:
        self._outputs = outputs

    def randrange(self, stop: int, /) -> int:
        value = self._outputs.pop(0)
        assert 0 <= value < stop
        return value


def _resolve_one_vs_one_format_definition(batch_size: int) -> MatchFormatDefinition:
    format_definition = get_match_format_definition(MatchFormat.ONE_VS_ONE)
    assert format_definition is not None
    if batch_size == 1:
        assert format_definition.batch_size == 1
        return format_definition
    return replace(format_definition, batch_size=batch_size)


def _build_one_vs_one_queue_entries(
    ratings: tuple[float, ...],
) -> tuple[QueueEntrySnapshot, ...]:
    return tuple(
        QueueEntrySnapshot(
            queue_entry_id=index,
            player_id=100 + index,
            match_format=MatchFormat.ONE_VS_ONE,
            rating=rating,
            joined_at=datetime(2026, 3, 22, 10, index - 1, tzinfo=timezone.utc),
        )
        for index, rating in enumerate(ratings, start=1)
    )


@pytest.mark.parametrize(
    ("batch_size", "ratings", "expected_match_entry_ids"),
    [
        pytest.param(
            1,
            (1700.0, 1650.0),
            (((1,), (2,)),),
            id="batch-size-1",
        ),
        pytest.param(
            2,
            (1700.0, 1650.0, 1400.0, 1300.0),
            (((1,), (2,)), ((3,), (4,))),
            id="batch-size-2",
        ),
    ],
)
def test_prepare_matches_for_batch_pairs_ranked_entries_for_one_vs_one(
    batch_size: int,
    ratings: tuple[float, ...],
    expected_match_entry_ids: tuple[tuple[tuple[int, ...], tuple[int, ...]], ...],
) -> None:
    format_definition = _resolve_one_vs_one_format_definition(batch_size)

    prepared_matches = prepare_matches_for_batch(
        _build_one_vs_one_queue_entries(ratings),
        format_definition,
        random_generator=FakeRandom([0] * (len(ratings) + batch_size)),
    )

    assert len(prepared_matches) == format_definition.batch_size
    assert (
        tuple(
            (prepared_match.team_a_entry_ids, prepared_match.team_b_entry_ids)
            for prepared_match in prepared_matches
        )
        == expected_match_entry_ids
    )


def test_is_queue_join_allowed_respects_minimum_and_maximum_ratings() -> None:
    definitions = (
        MatchQueueClassDefinition(
            match_format=MatchFormat.THREE_VS_THREE,
            queue_class_id="beginner",
            queue_name="beginner",
            description="beginner",
            maximum_rating=1600.0,
        ),
        MatchQueueClassDefinition(
            match_format=MatchFormat.THREE_VS_THREE,
            queue_class_id="regular",
            queue_name="regular",
            description="regular",
        ),
        MatchQueueClassDefinition(
            match_format=MatchFormat.THREE_VS_THREE,
            queue_class_id="master",
            queue_name="master",
            description="master",
            minimum_rating=1600.0,
        ),
    )

    assert (
        is_queue_join_allowed(
            rating=1599.0,
            queue_class_definition=definitions[0],
            definitions_for_format=definitions,
        )
        is True
    )
    assert (
        is_queue_join_allowed(
            rating=1600.0,
            queue_class_definition=definitions[0],
            definitions_for_format=definitions,
        )
        is False
    )
    assert (
        is_queue_join_allowed(
            rating=1200.0,
            queue_class_definition=definitions[1],
            definitions_for_format=definitions,
        )
        is True
    )
    assert (
        is_queue_join_allowed(
            rating=1850.0,
            queue_class_definition=definitions[1],
            definitions_for_format=definitions,
        )
        is True
    )
    assert (
        is_queue_join_allowed(
            rating=1599.0,
            queue_class_definition=definitions[2],
            definitions_for_format=definitions,
        )
        is False
    )
    assert (
        is_queue_join_allowed(
            rating=1600.0,
            queue_class_definition=definitions[2],
            definitions_for_format=definitions,
        )
        is True
    )


def test_validate_queue_class_definitions_rejects_duplicate_queue_names() -> None:
    with pytest.raises(ValueError, match="Duplicate queue_name"):
        validate_queue_class_definitions(
            (
                MatchQueueClassDefinition(
                    match_format=MatchFormat.THREE_VS_THREE,
                    queue_class_id="beginner_a",
                    queue_name="beginner",
                    description="beginner-a",
                ),
                MatchQueueClassDefinition(
                    match_format=MatchFormat.THREE_VS_THREE,
                    queue_class_id="beginner_b",
                    queue_name="begginer",
                    description="beginner-b",
                ),
            ),
            supported_match_formats={MatchFormat.THREE_VS_THREE},
        )


def test_validate_queue_class_definitions_rejects_inverted_rating_window() -> None:
    with pytest.raises(ValueError, match="maximum_rating must be greater than minimum_rating"):
        validate_queue_class_definitions(
            (
                MatchQueueClassDefinition(
                    match_format=MatchFormat.THREE_VS_THREE,
                    queue_class_id="invalid",
                    queue_name="regular",
                    description="invalid",
                    minimum_rating=1600.0,
                    maximum_rating=1600.0,
                ),
            ),
            supported_match_formats={MatchFormat.THREE_VS_THREE},
        )
