from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import pow

from bot.models import MatchParticipantTeam, MatchResult

RATING_K_PROVISIONAL = 40
RATING_K_ESTABLISHED = 32
RATING_K_VETERAN = 24
RATING_PROVISIONAL_GAMES = 20
RATING_ESTABLISHED_GAMES = 100
RATING_DIVISOR = 400.0


@dataclass(frozen=True, slots=True)
class RatingParticipantSnapshot:
    player_id: int
    team: MatchParticipantTeam
    rating: float
    games_played: int
    wins: int
    losses: int
    draws: int


@dataclass(frozen=True, slots=True)
class RatingUpdate:
    player_id: int
    rating_before: float
    rating_after: float
    games_played_before: int
    games_played_after: int
    wins_before: int
    wins_after: int
    losses_before: int
    losses_after: int
    draws_before: int
    draws_after: int


def calculate_rating_updates(
    participants: Sequence[RatingParticipantSnapshot],
    final_result: MatchResult,
) -> dict[int, RatingUpdate]:
    if final_result == MatchResult.VOID:
        return {
            participant.player_id: RatingUpdate(
                player_id=participant.player_id,
                rating_before=participant.rating,
                rating_after=participant.rating,
                games_played_before=participant.games_played,
                games_played_after=participant.games_played,
                wins_before=participant.wins,
                wins_after=participant.wins,
                losses_before=participant.losses,
                losses_after=participant.losses,
                draws_before=participant.draws,
                draws_after=participant.draws,
            )
            for participant in participants
        }

    team_a = [
        participant
        for participant in participants
        if participant.team == MatchParticipantTeam.TEAM_A
    ]
    team_b = [
        participant
        for participant in participants
        if participant.team == MatchParticipantTeam.TEAM_B
    ]
    if not team_a or not team_b:
        raise ValueError("Both teams must have at least one participant to calculate ratings")
    if len(team_a) != len(team_b):
        raise ValueError(
            "Both teams must have the same number of participants to calculate ratings"
        )

    q_by_player_id = {
        participant.player_id: pow(10.0, participant.rating / RATING_DIVISOR)
        for participant in participants
    }
    team_a_strength = sum(q_by_player_id[participant.player_id] for participant in team_a)
    team_b_strength = sum(q_by_player_id[participant.player_id] for participant in team_b)
    team_a_expected_score = team_a_strength / (team_a_strength + team_b_strength)
    team_a_actual_score = _get_team_a_score(final_result)
    team_size_multiplier = len(team_a)

    updates_by_player_id: dict[int, RatingUpdate] = {}
    for participant in participants:
        team_strength = (
            team_a_strength if participant.team == MatchParticipantTeam.TEAM_A else team_b_strength
        )
        q_value = q_by_player_id[participant.player_id]
        k_value = _get_k_value(participant.games_played)
        rating_delta = (
            k_value
            * team_size_multiplier
            * (q_value / team_strength)
            * (team_a_actual_score - team_a_expected_score)
        )
        if participant.team == MatchParticipantTeam.TEAM_B:
            rating_delta *= -1

        wins_after, losses_after, draws_after = _get_updated_record(
            participant=participant,
            final_result=final_result,
        )
        updates_by_player_id[participant.player_id] = RatingUpdate(
            player_id=participant.player_id,
            rating_before=participant.rating,
            rating_after=participant.rating + rating_delta,
            games_played_before=participant.games_played,
            games_played_after=participant.games_played + 1,
            wins_before=participant.wins,
            wins_after=wins_after,
            losses_before=participant.losses,
            losses_after=losses_after,
            draws_before=participant.draws,
            draws_after=draws_after,
        )

    return updates_by_player_id


def _get_team_a_score(final_result: MatchResult) -> float:
    if final_result == MatchResult.TEAM_A_WIN:
        return 1.0
    if final_result == MatchResult.DRAW:
        return 0.5
    if final_result == MatchResult.TEAM_B_WIN:
        return 0.0
    raise ValueError(f"Unsupported result for rating calculation: {final_result}")


def _get_k_value(games_played: int) -> int:
    if games_played < RATING_PROVISIONAL_GAMES:
        return RATING_K_PROVISIONAL
    if games_played < RATING_ESTABLISHED_GAMES:
        return RATING_K_ESTABLISHED
    return RATING_K_VETERAN


def _get_updated_record(
    *,
    participant: RatingParticipantSnapshot,
    final_result: MatchResult,
) -> tuple[int, int, int]:
    if final_result == MatchResult.DRAW:
        return participant.wins, participant.losses, participant.draws + 1

    team_a_won = final_result == MatchResult.TEAM_A_WIN
    participant_won = (
        team_a_won if participant.team == MatchParticipantTeam.TEAM_A else not team_a_won
    )
    if participant_won:
        return participant.wins + 1, participant.losses, participant.draws
    return participant.wins, participant.losses + 1, participant.draws
