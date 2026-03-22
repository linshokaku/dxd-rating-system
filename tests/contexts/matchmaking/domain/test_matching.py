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


def test_prepare_matches_for_batch_pairs_ranked_entries_for_one_vs_one() -> None:
    format_definition = get_match_format_definition(MatchFormat.ONE_VS_ONE)
    assert format_definition is not None

    prepared_matches = prepare_matches_for_batch(
        (
            QueueEntrySnapshot(
                queue_entry_id=1,
                player_id=101,
                match_format=MatchFormat.ONE_VS_ONE,
                rating=1700.0,
                joined_at=datetime(2026, 3, 22, 10, 0, tzinfo=timezone.utc),
            ),
            QueueEntrySnapshot(
                queue_entry_id=2,
                player_id=102,
                match_format=MatchFormat.ONE_VS_ONE,
                rating=1650.0,
                joined_at=datetime(2026, 3, 22, 10, 1, tzinfo=timezone.utc),
            ),
            QueueEntrySnapshot(
                queue_entry_id=3,
                player_id=103,
                match_format=MatchFormat.ONE_VS_ONE,
                rating=1400.0,
                joined_at=datetime(2026, 3, 22, 10, 2, tzinfo=timezone.utc),
            ),
            QueueEntrySnapshot(
                queue_entry_id=4,
                player_id=104,
                match_format=MatchFormat.ONE_VS_ONE,
                rating=1300.0,
                joined_at=datetime(2026, 3, 22, 10, 3, tzinfo=timezone.utc),
            ),
        ),
        format_definition,
        random_generator=FakeRandom([0, 0, 0, 0, 0, 0]),
    )

    assert prepared_matches[0].team_a_entry_ids == (1,)
    assert prepared_matches[0].team_b_entry_ids == (2,)
    assert prepared_matches[1].team_a_entry_ids == (3,)
    assert prepared_matches[1].team_b_entry_ids == (4,)


def test_is_queue_join_allowed_uses_neighbor_target_ratings() -> None:
    definitions = (
        MatchQueueClassDefinition(
            match_format=MatchFormat.THREE_VS_THREE,
            queue_class_id="low",
            queue_name="low",
            description="low",
            target_rating=1200.0,
        ),
        MatchQueueClassDefinition(
            match_format=MatchFormat.THREE_VS_THREE,
            queue_class_id="mid",
            queue_name="mid",
            description="mid",
            target_rating=1500.0,
        ),
        MatchQueueClassDefinition(
            match_format=MatchFormat.THREE_VS_THREE,
            queue_class_id="high",
            queue_name="high",
            description="high",
            target_rating=1800.0,
        ),
    )

    assert (
        is_queue_join_allowed(
            rating=1600.0,
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
        is False
    )


def test_validate_queue_class_definitions_rejects_duplicate_queue_names() -> None:
    with pytest.raises(ValueError, match="Duplicate queue_name"):
        validate_queue_class_definitions(
            (
                MatchQueueClassDefinition(
                    match_format=MatchFormat.THREE_VS_THREE,
                    queue_class_id="low_a",
                    queue_name="low",
                    description="low-a",
                ),
                MatchQueueClassDefinition(
                    match_format=MatchFormat.THREE_VS_THREE,
                    queue_class_id="low_b",
                    queue_name="LOW",
                    description="low-b",
                ),
            ),
            supported_match_formats={MatchFormat.THREE_VS_THREE},
        )
