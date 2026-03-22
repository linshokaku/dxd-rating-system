from dxd_rating.contexts.matches.domain import (
    LatestMatchReportSnapshot,
    MatchParticipantIdentity,
    determine_admin_review_reasons,
    determine_auto_penalty_type,
    determine_match_result,
    determine_report_status,
    normalize_report_result,
)
from dxd_rating.platform.db.models import (
    MatchApprovalStatus,
    MatchParticipantTeam,
    MatchReportInputResult,
    MatchReportStatus,
    MatchResult,
    PenaltyType,
)


def test_determine_match_result_uses_parent_report_to_break_tie() -> None:
    result_decision = determine_match_result(
        latest_reports_by_player={
            1: LatestMatchReportSnapshot(
                player_id=1,
                normalized_result=MatchResult.TEAM_A_WIN,
            ),
            2: LatestMatchReportSnapshot(
                player_id=2,
                normalized_result=MatchResult.TEAM_B_WIN,
            ),
        },
        parent_player_id=1,
    )

    assert result_decision.provisional_result == MatchResult.TEAM_A_WIN
    assert result_decision.unresolved_tie is False


def test_determine_admin_review_reasons_flags_low_reports_single_team_and_tie() -> None:
    reasons = determine_admin_review_reasons(
        participants=(
            MatchParticipantIdentity(player_id=1, team=MatchParticipantTeam.TEAM_A),
            MatchParticipantIdentity(player_id=2, team=MatchParticipantTeam.TEAM_A),
            MatchParticipantIdentity(player_id=3, team=MatchParticipantTeam.TEAM_B),
            MatchParticipantIdentity(player_id=4, team=MatchParticipantTeam.TEAM_B),
        ),
        latest_reports_by_player={
            1: LatestMatchReportSnapshot(
                player_id=1,
                normalized_result=MatchResult.TEAM_A_WIN,
            ),
        },
        unresolved_tie=True,
        team_size=2,
    )

    assert reasons == ["low_report_count", "single_team_reports", "unresolved_tie"]


def test_determine_report_status_and_auto_penalty_type_follow_rule_set() -> None:
    report_status = determine_report_status(
        LatestMatchReportSnapshot(
            player_id=1,
            normalized_result=MatchResult.TEAM_B_WIN,
        ),
        MatchResult.TEAM_A_WIN,
    )
    penalty_type = determine_auto_penalty_type(
        report_status=report_status,
        approval_status=MatchApprovalStatus.PENDING,
        apply_auto_penalties=True,
    )

    assert report_status == MatchReportStatus.INCORRECT
    assert penalty_type == PenaltyType.INCORRECT_REPORT


def test_normalize_report_result_respects_participant_team() -> None:
    assert (
        normalize_report_result(
            MatchParticipantTeam.TEAM_A,
            MatchReportInputResult.WIN,
        )
        == MatchResult.TEAM_A_WIN
    )
    assert (
        normalize_report_result(
            MatchParticipantTeam.TEAM_B,
            MatchReportInputResult.LOSE,
        )
        == MatchResult.TEAM_A_WIN
    )
