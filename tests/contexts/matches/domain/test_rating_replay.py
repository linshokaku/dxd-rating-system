from dxd_rating.contexts.matches.domain import (
    HistoricalMatchPlayerSnapshot,
    HistoricalMatchRatingSnapshot,
    RatingParticipantSnapshot,
    RatingState,
    calculate_rating_updates,
    replay_rating_history,
)
from dxd_rating.platform.db.models import MatchParticipantTeam, MatchResult


def test_replay_rating_history_rebuilds_before_state_chain() -> None:
    first_match = HistoricalMatchRatingSnapshot(
        match_id=10,
        final_result=MatchResult.TEAM_A_WIN,
        player_results=(
            HistoricalMatchPlayerSnapshot(
                player_id=1,
                team=MatchParticipantTeam.TEAM_A,
                rating_before=1500.0,
                games_played_before=0,
                wins_before=0,
                losses_before=0,
                draws_before=0,
            ),
            HistoricalMatchPlayerSnapshot(
                player_id=2,
                team=MatchParticipantTeam.TEAM_B,
                rating_before=1500.0,
                games_played_before=0,
                wins_before=0,
                losses_before=0,
                draws_before=0,
            ),
        ),
    )
    first_match_updates = calculate_rating_updates(
        (
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
                team=MatchParticipantTeam.TEAM_B,
                rating=1500.0,
                games_played=0,
                wins=0,
                losses=0,
                draws=0,
            ),
        ),
        MatchResult.TEAM_A_WIN,
    )
    second_match = HistoricalMatchRatingSnapshot(
        match_id=11,
        final_result=MatchResult.TEAM_B_WIN,
        player_results=(
            HistoricalMatchPlayerSnapshot(
                player_id=1,
                team=MatchParticipantTeam.TEAM_A,
                rating_before=first_match_updates[1].rating_after,
                games_played_before=first_match_updates[1].games_played_after,
                wins_before=first_match_updates[1].wins_after,
                losses_before=first_match_updates[1].losses_after,
                draws_before=first_match_updates[1].draws_after,
            ),
            HistoricalMatchPlayerSnapshot(
                player_id=2,
                team=MatchParticipantTeam.TEAM_B,
                rating_before=first_match_updates[2].rating_after,
                games_played_before=first_match_updates[2].games_played_after,
                wins_before=first_match_updates[2].wins_after,
                losses_before=first_match_updates[2].losses_after,
                draws_before=first_match_updates[2].draws_after,
            ),
        ),
    )
    second_match_updates = calculate_rating_updates(
        (
            RatingParticipantSnapshot(
                player_id=1,
                team=MatchParticipantTeam.TEAM_A,
                rating=first_match_updates[1].rating_after,
                games_played=first_match_updates[1].games_played_after,
                wins=first_match_updates[1].wins_after,
                losses=first_match_updates[1].losses_after,
                draws=first_match_updates[1].draws_after,
            ),
            RatingParticipantSnapshot(
                player_id=2,
                team=MatchParticipantTeam.TEAM_B,
                rating=first_match_updates[2].rating_after,
                games_played=first_match_updates[2].games_played_after,
                wins=first_match_updates[2].wins_after,
                losses=first_match_updates[2].losses_after,
                draws=first_match_updates[2].draws_after,
            ),
        ),
        MatchResult.TEAM_B_WIN,
    )

    replay_result = replay_rating_history(
        finalized_matches=(first_match, second_match),
        current_player_states={
            1: RatingState(
                rating=second_match_updates[1].rating_after,
                games_played=second_match_updates[1].games_played_after,
                wins=second_match_updates[1].wins_after,
                losses=second_match_updates[1].losses_after,
                draws=second_match_updates[1].draws_after,
            ),
            2: RatingState(
                rating=second_match_updates[2].rating_after,
                games_played=second_match_updates[2].games_played_after,
                wins=second_match_updates[2].wins_after,
                losses=second_match_updates[2].losses_after,
                draws=second_match_updates[2].draws_after,
            ),
        },
    )

    replayed_results_by_key = {
        (player_result.match_id, player_result.player_id): player_result
        for player_result in replay_result.player_results
    }
    assert replayed_results_by_key[(10, 1)].rating_before == 1500.0
    assert replayed_results_by_key[(10, 2)].rating_before == 1500.0
    assert replayed_results_by_key[(11, 1)].rating_before == first_match_updates[1].rating_after
    assert replayed_results_by_key[(11, 2)].rating_before == first_match_updates[2].rating_after
    assert replay_result.player_states_by_player_id[1].rating == second_match_updates[1].rating_after
    assert replay_result.player_states_by_player_id[2].rating == second_match_updates[2].rating_after
