from __future__ import annotations

import pytest

from dxd_rating.contexts.matches.domain import RatingParticipantSnapshot, calculate_rating_updates
from dxd_rating.platform.db.models import MatchParticipantTeam, MatchResult


def build_equal_rating_snapshots(
    team_size: int,
) -> tuple[RatingParticipantSnapshot, ...]:
    participants: list[RatingParticipantSnapshot] = []
    player_id = 1
    for team in (MatchParticipantTeam.TEAM_A, MatchParticipantTeam.TEAM_B):
        for _ in range(team_size):
            participants.append(
                RatingParticipantSnapshot(
                    player_id=player_id,
                    team=team,
                    rating=1500.0,
                    games_played=0,
                    wins=0,
                    losses=0,
                    draws=0,
                )
            )
            player_id += 1
    return tuple(participants)


@pytest.mark.parametrize(
    ("team_size", "expected_delta"),
    [
        pytest.param(1, 20.0, id="1v1"),
        pytest.param(2, 20.0, id="2v2"),
        pytest.param(3, 20.0, id="3v3"),
    ],
)
def test_calculate_rating_updates_team_a_win_by_format(
    team_size: int,
    expected_delta: float,
) -> None:
    participants = build_equal_rating_snapshots(team_size)

    updates = calculate_rating_updates(participants, MatchResult.TEAM_A_WIN)

    for participant in participants:
        update = updates[participant.player_id]
        assert update.games_played_after == 1
        if participant.team == MatchParticipantTeam.TEAM_A:
            assert update.rating_after == pytest.approx(1500.0 + expected_delta)
            assert update.wins_after == 1
            assert update.losses_after == 0
            assert update.draws_after == 0
        else:
            assert update.rating_after == pytest.approx(1500.0 - expected_delta)
            assert update.wins_after == 0
            assert update.losses_after == 1
            assert update.draws_after == 0


@pytest.mark.parametrize(
    "team_size",
    [
        pytest.param(1, id="1v1"),
        pytest.param(2, id="2v2"),
        pytest.param(3, id="3v3"),
    ],
)
def test_calculate_rating_updates_draw_by_format(team_size: int) -> None:
    participants = build_equal_rating_snapshots(team_size)

    updates = calculate_rating_updates(participants, MatchResult.DRAW)

    for participant in participants:
        update = updates[participant.player_id]
        assert update.rating_after == pytest.approx(1500.0)
        assert update.games_played_after == 1
        assert update.wins_after == 0
        assert update.losses_after == 0
        assert update.draws_after == 1


@pytest.mark.parametrize(
    "team_size",
    [
        pytest.param(1, id="1v1"),
        pytest.param(2, id="2v2"),
        pytest.param(3, id="3v3"),
    ],
)
def test_calculate_rating_updates_void_by_format(team_size: int) -> None:
    participants = build_equal_rating_snapshots(team_size)

    updates = calculate_rating_updates(participants, MatchResult.VOID)

    for participant in participants:
        update = updates[participant.player_id]
        assert update.rating_after == pytest.approx(1500.0)
        assert update.games_played_after == 0
        assert update.wins_after == 0
        assert update.losses_after == 0
        assert update.draws_after == 0


def test_calculate_rating_updates_rejects_mismatched_team_sizes() -> None:
    participants = (
        RatingParticipantSnapshot(
            player_id=1,
            team=MatchParticipantTeam.TEAM_A,
            rating=1500.0,
            games_played=0,
            wins=0,
            losses=0,
            draws=0,
        ),
        RatingParticipantSnapshot(
            player_id=2,
            team=MatchParticipantTeam.TEAM_A,
            rating=1500.0,
            games_played=0,
            wins=0,
            losses=0,
            draws=0,
        ),
        RatingParticipantSnapshot(
            player_id=3,
            team=MatchParticipantTeam.TEAM_B,
            rating=1500.0,
            games_played=0,
            wins=0,
            losses=0,
            draws=0,
        ),
    )

    with pytest.raises(ValueError, match="same number of participants"):
        calculate_rating_updates(participants, MatchResult.TEAM_A_WIN)
