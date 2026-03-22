from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from dxd_rating.platform.db.models import (
    MatchApprovalStatus,
    MatchParticipantTeam,
    MatchReportInputResult,
    MatchReportStatus,
    MatchResult,
    PenaltyType,
)


@dataclass(frozen=True, slots=True)
class MatchParticipantIdentity:
    player_id: int
    team: MatchParticipantTeam


@dataclass(frozen=True, slots=True)
class LatestMatchReportSnapshot:
    player_id: int
    normalized_result: MatchResult


@dataclass(frozen=True, slots=True)
class MatchResultDecision:
    provisional_result: MatchResult
    unresolved_tie: bool


def determine_match_result(
    *,
    latest_reports_by_player: Mapping[int, LatestMatchReportSnapshot],
    parent_player_id: int | None,
) -> MatchResultDecision:
    if not latest_reports_by_player:
        return MatchResultDecision(
            provisional_result=MatchResult.VOID,
            unresolved_tie=False,
        )

    counts = Counter(report.normalized_result for report in latest_reports_by_player.values())
    max_count = max(counts.values())
    candidates = [result for result, count in counts.items() if count == max_count]
    if len(candidates) == 1:
        return MatchResultDecision(
            provisional_result=candidates[0],
            unresolved_tie=False,
        )

    if parent_player_id is not None:
        parent_report = latest_reports_by_player.get(parent_player_id)
        if parent_report is not None and parent_report.normalized_result in candidates:
            return MatchResultDecision(
                provisional_result=parent_report.normalized_result,
                unresolved_tie=False,
            )

    return MatchResultDecision(
        provisional_result=MatchResult.VOID,
        unresolved_tie=True,
    )


def determine_admin_review_reasons(
    *,
    participants: Sequence[MatchParticipantIdentity],
    latest_reports_by_player: Mapping[int, LatestMatchReportSnapshot],
    unresolved_tie: bool,
    team_size: int,
) -> list[str]:
    reasons: list[str] = []
    if len(latest_reports_by_player) < team_size:
        reasons.append("low_report_count")

    teams_with_reports = {
        participant.team
        for participant in participants
        if participant.player_id in latest_reports_by_player
    }
    if latest_reports_by_player and len(teams_with_reports) == 1:
        reasons.append("single_team_reports")

    if unresolved_tie:
        reasons.append("unresolved_tie")

    return reasons


def determine_report_status(
    latest_report: LatestMatchReportSnapshot | None,
    final_result: MatchResult,
) -> MatchReportStatus:
    if latest_report is None:
        return MatchReportStatus.NOT_REPORTED
    if latest_report.normalized_result == final_result:
        return MatchReportStatus.CORRECT
    return MatchReportStatus.INCORRECT


def determine_auto_penalty_type(
    *,
    report_status: MatchReportStatus,
    approval_status: MatchApprovalStatus,
    apply_auto_penalties: bool,
) -> PenaltyType | None:
    if not apply_auto_penalties:
        return None
    if approval_status == MatchApprovalStatus.APPROVED:
        return None
    if report_status == MatchReportStatus.INCORRECT:
        return PenaltyType.INCORRECT_REPORT
    if report_status == MatchReportStatus.NOT_REPORTED:
        return PenaltyType.NO_REPORT
    return None


def normalize_report_result(
    team: MatchParticipantTeam,
    input_result: MatchReportInputResult,
) -> MatchResult:
    if input_result == MatchReportInputResult.DRAW:
        return MatchResult.DRAW
    if input_result == MatchReportInputResult.VOID:
        return MatchResult.VOID
    if team == MatchParticipantTeam.TEAM_A:
        return (
            MatchResult.TEAM_A_WIN
            if input_result == MatchReportInputResult.WIN
            else MatchResult.TEAM_B_WIN
        )
    return (
        MatchResult.TEAM_B_WIN
        if input_result == MatchReportInputResult.WIN
        else MatchResult.TEAM_A_WIN
    )
