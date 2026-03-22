from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from dxd_rating.contexts.matches.domain.rating import (
    RatingParticipantSnapshot,
    calculate_rating_updates,
)
from dxd_rating.platform.db.models import MatchParticipantTeam, MatchResult


@dataclass(frozen=True, slots=True)
class RatingState:
    rating: float
    games_played: int
    wins: int
    losses: int
    draws: int


@dataclass(frozen=True, slots=True)
class HistoricalMatchPlayerSnapshot:
    player_id: int
    team: MatchParticipantTeam
    rating_before: float
    games_played_before: int
    wins_before: int
    losses_before: int
    draws_before: int


@dataclass(frozen=True, slots=True)
class HistoricalMatchRatingSnapshot:
    match_id: int
    final_result: MatchResult
    player_results: tuple[HistoricalMatchPlayerSnapshot, ...]


@dataclass(frozen=True, slots=True)
class ReplayedHistoricalPlayerResult:
    match_id: int
    player_id: int
    rating_before: float
    games_played_before: int
    wins_before: int
    losses_before: int
    draws_before: int
    rating_after: float
    games_played_after: int
    wins_after: int
    losses_after: int
    draws_after: int


@dataclass(frozen=True, slots=True)
class RatingReplayResult:
    player_states_by_player_id: dict[int, RatingState]
    player_results: tuple[ReplayedHistoricalPlayerResult, ...]


def replay_rating_history(
    *,
    finalized_matches: Sequence[HistoricalMatchRatingSnapshot],
    current_player_states: Mapping[int, RatingState],
) -> RatingReplayResult:
    working_states = dict(current_player_states)
    if not finalized_matches:
        return RatingReplayResult(
            player_states_by_player_id=working_states,
            player_results=tuple(),
        )

    for finalized_match in reversed(finalized_matches[1:]):
        for player_result in finalized_match.player_results:
            working_states[player_result.player_id] = RatingState(
                rating=player_result.rating_before,
                games_played=player_result.games_played_before,
                wins=player_result.wins_before,
                losses=player_result.losses_before,
                draws=player_result.draws_before,
            )

    for player_result in finalized_matches[0].player_results:
        working_states[player_result.player_id] = RatingState(
            rating=player_result.rating_before,
            games_played=player_result.games_played_before,
            wins=player_result.wins_before,
            losses=player_result.losses_before,
            draws=player_result.draws_before,
        )

    replayed_player_results: list[ReplayedHistoricalPlayerResult] = []
    for finalized_match in finalized_matches:
        ordered_player_results = sorted(
            finalized_match.player_results,
            key=lambda result: (result.team.value, result.player_id),
        )
        rating_snapshots = tuple(
            RatingParticipantSnapshot(
                player_id=player_result.player_id,
                team=player_result.team,
                rating=working_states[player_result.player_id].rating,
                games_played=working_states[player_result.player_id].games_played,
                wins=working_states[player_result.player_id].wins,
                losses=working_states[player_result.player_id].losses,
                draws=working_states[player_result.player_id].draws,
            )
            for player_result in ordered_player_results
        )
        rating_updates_by_player_id = calculate_rating_updates(
            rating_snapshots,
            finalized_match.final_result,
        )
        for player_result in ordered_player_results:
            rating_update = rating_updates_by_player_id[player_result.player_id]
            replayed_player_results.append(
                ReplayedHistoricalPlayerResult(
                    match_id=finalized_match.match_id,
                    player_id=player_result.player_id,
                    rating_before=rating_update.rating_before,
                    games_played_before=rating_update.games_played_before,
                    wins_before=rating_update.wins_before,
                    losses_before=rating_update.losses_before,
                    draws_before=rating_update.draws_before,
                    rating_after=rating_update.rating_after,
                    games_played_after=rating_update.games_played_after,
                    wins_after=rating_update.wins_after,
                    losses_after=rating_update.losses_after,
                    draws_after=rating_update.draws_after,
                )
            )
            working_states[player_result.player_id] = RatingState(
                rating=rating_update.rating_after,
                games_played=rating_update.games_played_after,
                wins=rating_update.wins_after,
                losses=rating_update.losses_after,
                draws=rating_update.draws_after,
            )

    return RatingReplayResult(
        player_states_by_player_id=working_states,
        player_results=tuple(replayed_player_results),
    )
