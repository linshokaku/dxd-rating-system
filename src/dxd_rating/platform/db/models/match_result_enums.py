from __future__ import annotations

from enum import StrEnum


class MatchState(StrEnum):
    WAITING_FOR_PARENT = "waiting_for_parent"
    WAITING_FOR_RESULT_REPORTS = "waiting_for_result_reports"
    AWAITING_RESULT_APPROVALS = "awaiting_result_approvals"
    FINALIZED = "finalized"


class MatchResult(StrEnum):
    TEAM_A_WIN = "team_a_win"
    TEAM_B_WIN = "team_b_win"
    DRAW = "draw"
    VOID = "void"


class MatchReportInputResult(StrEnum):
    WIN = "win"
    LOSE = "lose"
    DRAW = "draw"
    VOID = "void"


class MatchReportStatus(StrEnum):
    CORRECT = "correct"
    INCORRECT = "incorrect"
    NOT_REPORTED = "not_reported"


class MatchApprovalStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    NOT_APPROVED = "not_approved"


class PenaltyType(StrEnum):
    INCORRECT_REPORT = "incorrect_report"
    NO_REPORT = "no_report"
    ROOM_SETUP_DELAY = "room_setup_delay"
    MATCH_MISTAKE = "match_mistake"
    LATE = "late"
    DISCONNECT = "disconnect"


class PenaltyAdjustmentSource(StrEnum):
    AUTO_MATCH_FINALIZATION = "auto_match_finalization"
    ADMIN_MANUAL = "admin_manual"
    ADMIN_RESULT_OVERRIDE = "admin_result_override"
