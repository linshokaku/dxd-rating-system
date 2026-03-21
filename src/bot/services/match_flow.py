from __future__ import annotations

import logging
import random
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypedDict

import psycopg
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.orm import Session, selectinload, sessionmaker

from bot.constants import (
    MATCH_APPROVAL_WINDOW,
    MATCH_REPORT_DEADLINE_DELAY,
    MATCH_REPORT_OPEN_DELAY,
    OUTBOX_NOTIFY_CHANNEL,
    MatchFormatDefinition,
    get_match_format_definition,
)
from bot.db.session import session_scope
from bot.models import (
    ActiveMatchPlayerState,
    ActiveMatchState,
    FinalizedMatchPlayerResult,
    FinalizedMatchResult,
    Match,
    MatchAdminOverride,
    MatchApprovalStatus,
    MatchFormat,
    MatchParticipant,
    MatchParticipantTeam,
    MatchReport,
    MatchReportInputResult,
    MatchReportStatus,
    MatchResult,
    MatchState,
    OutboxEvent,
    OutboxEventType,
    PenaltyAdjustmentSource,
    PenaltyType,
    Player,
    PlayerFormatStats,
    PlayerPenalty,
    PlayerPenaltyAdjustment,
)
from bot.services.errors import (
    MatchAlreadyFinalizedError,
    MatchApprovalNotAvailableError,
    MatchApprovalNotRequiredError,
    MatchFlowError,
    MatchNotFinalizedError,
    MatchNotFoundError,
    MatchParentAlreadyAssignedError,
    MatchParticipantError,
    MatchReportingClosedError,
    MatchReportNotOpenError,
    RetryableTaskError,
)
from bot.services.matching_queue import MatchingQueueNotificationContext
from bot.services.rating import RatingParticipantSnapshot, calculate_rating_updates

MATCH_PARENT_ASSIGNED_NOTIFICATION_MESSAGE = "親が決定しました。"
MATCH_APPROVAL_STARTED_NOTIFICATION_MESSAGE = "承認フェーズに移行しました。"
MATCH_APPROVAL_REQUESTED_NOTIFICATION_MESSAGE = "仮決定結果の承認が必要です。"
MATCH_FINALIZED_NOTIFICATION_MESSAGE = "試合結果が確定しました。"
MATCH_AUTO_PENALTY_APPLIED_NOTIFICATION_MESSAGE = "自動ペナルティが付与されました。"
MATCH_ADMIN_REVIEW_REQUIRED_NOTIFICATION_MESSAGE = "admin による確認が必要です。"

_MATCH_ADVISORY_LOCK_NAMESPACE = 20_260_319
_PLAYER_ADVISORY_LOCK_NAMESPACE = 20_260_320


def _is_transient_task_db_error(exc: Exception) -> bool:
    if isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError)):
        return True

    if isinstance(exc, (OperationalError, InterfaceError)):
        return True

    if not isinstance(exc, DBAPIError):
        return False

    if exc.connection_invalidated:
        return True

    return isinstance(exc.orig, (psycopg.OperationalError, psycopg.InterfaceError))


@dataclass(frozen=True, slots=True)
class MatchParentAssignmentResult:
    match_id: int
    parent_player_id: int | None
    parent_decided_at: datetime | None
    report_open_at: datetime | None
    report_deadline_at: datetime | None
    assigned: bool


@dataclass(frozen=True, slots=True)
class MatchReportSubmissionResult:
    match_id: int
    report_id: int
    finalized: bool
    approval_started: bool
    approval_deadline_at: datetime | None


@dataclass(frozen=True, slots=True)
class MatchApprovalResult:
    match_id: int
    approval_status: MatchApprovalStatus


@dataclass(frozen=True, slots=True)
class MatchFinalizationResult:
    match_id: int
    final_result: MatchResult | None
    finalized: bool
    finalized_at: datetime | None
    approval_deadline_at: datetime | None
    admin_review_required: bool


@dataclass(frozen=True, slots=True)
class MatchAdminOverrideResult:
    match_id: int
    final_result: MatchResult
    finalized_at: datetime


@dataclass(frozen=True, slots=True)
class PlayerRatingState:
    rating: float
    games_played: int
    wins: int
    losses: int
    draws: int


@dataclass(frozen=True, slots=True)
class PlayerPenaltyAdjustmentResult:
    player_id: int
    penalty_type: PenaltyType
    count: int


@dataclass(frozen=True, slots=True)
class ActiveMatchTimerState:
    match_id: int
    state: MatchState
    parent_deadline_at: datetime
    report_open_at: datetime | None
    reporting_opened_at: datetime | None
    report_deadline_at: datetime | None
    approval_deadline_at: datetime | None


class NotificationDestinationPayload(TypedDict):
    channel_id: int
    guild_id: int | None


class TeamRatingEntryPayload(TypedDict):
    discord_user_id: int
    rating: float


class MatchFlowService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        admin_discord_user_ids: frozenset[int] = frozenset(),
        logger: logging.Logger | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.admin_discord_user_ids = admin_discord_user_ids
        self.logger = logger or logging.getLogger(__name__)

    def volunteer_parent(
        self,
        match_id: int,
        player_id: int,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
    ) -> MatchParentAssignmentResult:
        with session_scope(self.session_factory) as session:
            self._acquire_match_lock(session, match_id)
            active_state = self._get_active_match_state_for_update(session, match_id)
            if active_state is None:
                self._raise_missing_match(match_id)
            assert active_state is not None

            current_time = self._get_database_now(session)
            self._ensure_not_finalized(active_state)
            participant = self._get_match_participant_for_update(session, match_id, player_id)
            if participant is None:
                raise MatchParticipantError("この試合の参加者ではありません。")

            if active_state.state != MatchState.WAITING_FOR_PARENT:
                raise MatchParentAlreadyAssignedError("この試合の親はすでに決まっています。")
            if current_time >= active_state.parent_deadline_at:
                raise MatchParentAlreadyAssignedError("親募集期間は終了しています。")

            self._apply_match_notification_context(
                participant,
                notification_context,
                mention_discord_user_id=participant.notification_mention_discord_user_id
                or participant.player.discord_user_id,
                recorded_at=current_time,
            )
            return self._assign_parent_locked(
                session,
                active_state=active_state,
                participants=self._get_match_participants(session, match_id),
                parent_player_id=player_id,
                decided_at=current_time,
                event_dedupe_suffix="manual",
            )

    def process_parent_deadline(self, match_id: int) -> MatchParentAssignmentResult:
        try:
            with session_scope(self.session_factory) as session:
                self._acquire_match_lock(session, match_id)
                active_state = self._get_active_match_state_for_update(session, match_id)
                if active_state is None:
                    return MatchParentAssignmentResult(
                        match_id=match_id,
                        parent_player_id=None,
                        parent_decided_at=None,
                        report_open_at=None,
                        report_deadline_at=None,
                        assigned=False,
                    )

                current_time = self._get_database_now(session)
                if (
                    active_state.state != MatchState.WAITING_FOR_PARENT
                    or active_state.parent_player_id is not None
                    or current_time < active_state.parent_deadline_at
                ):
                    return MatchParentAssignmentResult(
                        match_id=match_id,
                        parent_player_id=active_state.parent_player_id,
                        parent_decided_at=active_state.parent_decided_at,
                        report_open_at=active_state.report_open_at,
                        report_deadline_at=active_state.report_deadline_at,
                        assigned=False,
                    )

                participants = self._get_match_participants(session, match_id)
                chosen_participant = random.choice(participants)
                return self._assign_parent_locked(
                    session,
                    active_state=active_state,
                    participants=participants,
                    parent_player_id=chosen_participant.player_id,
                    decided_at=active_state.parent_deadline_at,
                    event_dedupe_suffix="timeout",
                )
        except Exception as exc:
            self._raise_retryable_task_error(exc, operation="processing match parent deadline")
            raise

    def submit_report(
        self,
        match_id: int,
        player_id: int,
        input_result: MatchReportInputResult,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
    ) -> MatchReportSubmissionResult:
        with session_scope(self.session_factory) as session:
            self._acquire_match_lock(session, match_id)
            active_state = self._get_active_match_state_for_update(session, match_id)
            if active_state is None:
                self._raise_missing_match(match_id)
            assert active_state is not None

            current_time = self._get_database_now(session)
            self._ensure_not_finalized(active_state)
            participant = self._get_match_participant_for_update(session, match_id, player_id)
            if participant is None:
                raise MatchParticipantError("この試合の参加者ではありません。")

            if active_state.state == MatchState.AWAITING_RESULT_APPROVALS:
                raise MatchReportingClosedError("承認期間中は勝敗報告を変更できません。")
            if active_state.state == MatchState.FINALIZED:
                raise MatchAlreadyFinalizedError("この試合はすでに結果確定済みです。")
            if (
                active_state.report_deadline_at is not None
                and current_time >= active_state.report_deadline_at
            ):
                raise MatchReportingClosedError("この試合の勝敗報告は締め切られています。")
            if input_result != MatchReportInputResult.VOID and (
                active_state.report_open_at is None or current_time < active_state.report_open_at
            ):
                raise MatchReportNotOpenError("まだ勝敗報告を受け付けていません。")

            self._apply_match_notification_context(
                participant,
                notification_context,
                mention_discord_user_id=participant.notification_mention_discord_user_id
                or participant.player.discord_user_id,
                recorded_at=current_time,
            )

            latest_report = self._get_latest_report_for_update(session, match_id, player_id)
            if latest_report is not None:
                latest_report.is_latest = False

            report = MatchReport(
                match_id=match_id,
                player_id=player_id,
                reported_input_result=input_result,
                normalized_result=self._normalize_report_result(participant.team, input_result),
                reported_at=current_time,
                is_latest=True,
            )
            session.add(report)
            session.flush()

            transition_result = self._maybe_start_approval_after_report_locked(
                session=session,
                active_state=active_state,
                current_time=current_time,
            )
            return MatchReportSubmissionResult(
                match_id=match_id,
                report_id=report.id,
                finalized=transition_result.finalized if transition_result is not None else False,
                approval_started=(
                    transition_result is not None and not transition_result.finalized
                ),
                approval_deadline_at=(
                    None if transition_result is None else transition_result.approval_deadline_at
                ),
            )

    def approve_provisional_result(
        self,
        match_id: int,
        player_id: int,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
    ) -> MatchApprovalResult:
        with session_scope(self.session_factory) as session:
            self._acquire_match_lock(session, match_id)
            active_state = self._get_active_match_state_for_update(session, match_id)
            if active_state is None:
                self._raise_missing_match(match_id)
            assert active_state is not None

            current_time = self._get_database_now(session)
            self._ensure_not_finalized(active_state)
            if (
                active_state.state != MatchState.AWAITING_RESULT_APPROVALS
                or active_state.approval_deadline_at is None
                or current_time >= active_state.approval_deadline_at
            ):
                raise MatchApprovalNotAvailableError("この試合は承認期間中ではありません。")

            participant = self._get_match_participant_for_update(session, match_id, player_id)
            if participant is None:
                raise MatchParticipantError("この試合の参加者ではありません。")
            self._apply_match_notification_context(
                participant,
                notification_context,
                mention_discord_user_id=participant.notification_mention_discord_user_id
                or participant.player.discord_user_id,
                recorded_at=current_time,
            )

            player_state = self._get_active_player_state_for_update(session, match_id, player_id)
            if (
                player_state is None
                or player_state.approval_status == MatchApprovalStatus.NOT_REQUIRED
            ):
                raise MatchApprovalNotRequiredError("この試合では承認は不要です。")
            if player_state.approval_status == MatchApprovalStatus.APPROVED:
                return MatchApprovalResult(
                    match_id=match_id,
                    approval_status=player_state.approval_status,
                )

            player_state.approval_status = MatchApprovalStatus.APPROVED
            player_state.approved_at = current_time
            return MatchApprovalResult(
                match_id=match_id,
                approval_status=player_state.approval_status,
            )

    def process_report_open(self, match_id: int) -> bool:
        try:
            with session_scope(self.session_factory) as session:
                self._acquire_match_lock(session, match_id)
                active_state = self._get_active_match_state_for_update(session, match_id)
                if active_state is None:
                    return False

                current_time = self._get_database_now(session)
                if (
                    active_state.state != MatchState.WAITING_FOR_RESULT_REPORTS
                    or active_state.report_open_at is None
                    or active_state.reporting_opened_at is not None
                    or current_time < active_state.report_open_at
                ):
                    return False

                active_state.reporting_opened_at = active_state.report_open_at
                return True
        except Exception as exc:
            self._raise_retryable_task_error(exc, operation="opening match reporting")
            raise

    def process_report_deadline(self, match_id: int) -> MatchFinalizationResult:
        try:
            with session_scope(self.session_factory) as session:
                self._acquire_match_lock(session, match_id)
                active_state = self._get_active_match_state_for_update(session, match_id)
                if active_state is None:
                    return MatchFinalizationResult(
                        match_id=match_id,
                        final_result=None,
                        finalized=False,
                        finalized_at=None,
                        approval_deadline_at=None,
                        admin_review_required=False,
                    )

                current_time = self._get_database_now(session)
                if (
                    active_state.state != MatchState.WAITING_FOR_RESULT_REPORTS
                    or active_state.report_deadline_at is None
                    or current_time < active_state.report_deadline_at
                ):
                    return MatchFinalizationResult(
                        match_id=match_id,
                        final_result=active_state.provisional_result,
                        finalized=False,
                        finalized_at=None,
                        approval_deadline_at=active_state.approval_deadline_at,
                        admin_review_required=active_state.admin_review_required,
                    )

                transition_result = self._start_approval_locked(
                    session,
                    active_state=active_state,
                    started_at=active_state.report_deadline_at,
                    finalization_dedupe_suffix="report_deadline",
                )
                return transition_result
        except Exception as exc:
            self._raise_retryable_task_error(exc, operation="processing match report deadline")
            raise

    def process_approval_deadline(self, match_id: int) -> MatchFinalizationResult:
        try:
            with session_scope(self.session_factory) as session:
                self._acquire_match_lock(session, match_id)
                active_state = self._get_active_match_state_for_update(session, match_id)
                if active_state is None:
                    return MatchFinalizationResult(
                        match_id=match_id,
                        final_result=None,
                        finalized=False,
                        finalized_at=None,
                        approval_deadline_at=None,
                        admin_review_required=False,
                    )

                current_time = self._get_database_now(session)
                if (
                    active_state.state != MatchState.AWAITING_RESULT_APPROVALS
                    or active_state.approval_deadline_at is None
                    or current_time < active_state.approval_deadline_at
                ):
                    return MatchFinalizationResult(
                        match_id=match_id,
                        final_result=active_state.provisional_result,
                        finalized=False,
                        finalized_at=None,
                        approval_deadline_at=active_state.approval_deadline_at,
                        admin_review_required=active_state.admin_review_required,
                    )

                return self._finalize_match_locked(
                    session,
                    active_state=active_state,
                    final_result=active_state.provisional_result or MatchResult.VOID,
                    finalized_at=current_time,
                    finalized_by_admin=False,
                    apply_auto_penalties=True,
                    finalization_dedupe_suffix="automatic",
                )
        except Exception as exc:
            self._raise_retryable_task_error(exc, operation="processing match approval deadline")
            raise

    def override_match_result(
        self,
        match_id: int,
        final_result: MatchResult,
        *,
        admin_discord_user_id: int,
    ) -> MatchAdminOverrideResult:
        with session_scope(self.session_factory) as session:
            self._acquire_match_lock(session, match_id)
            active_state = self._get_active_match_state_for_update(session, match_id)
            if active_state is None:
                self._raise_missing_match(match_id)
            assert active_state is not None

            current_time = self._get_database_now(session)
            finalized_result = session.get(FinalizedMatchResult, match_id)
            if (
                active_state.state != MatchState.FINALIZED
                or finalized_result is None
                or finalized_result.rated_at is None
            ):
                raise MatchNotFinalizedError("この試合はまだ結果確定していません。")

            previous_final_result = finalized_result.final_result
            session.add(
                MatchAdminOverride(
                    match_id=match_id,
                    admin_discord_user_id=admin_discord_user_id,
                    previous_final_result=previous_final_result,
                    new_final_result=final_result,
                    created_at=current_time,
                )
            )

            self._override_finalized_match_locked(
                session,
                active_state=active_state,
                finalized_result=finalized_result,
                final_result=final_result,
                finalized_at=current_time,
                finalization_dedupe_suffix=(
                    f"admin:{admin_discord_user_id}:{int(current_time.timestamp())}"
                ),
            )
            return MatchAdminOverrideResult(
                match_id=match_id,
                final_result=final_result,
                finalized_at=current_time,
            )

    def adjust_penalty(
        self,
        player_id: int,
        penalty_type: PenaltyType,
        delta: int,
        *,
        admin_discord_user_id: int,
    ) -> PlayerPenaltyAdjustmentResult:
        with session_scope(self.session_factory) as session:
            self._acquire_player_lock(session, player_id)
            self._ensure_player_exists(session, player_id)
            count = self._apply_penalty_adjustment(
                session,
                player_id=player_id,
                match_id=None,
                penalty_type=penalty_type,
                delta=delta,
                source=PenaltyAdjustmentSource.ADMIN_MANUAL,
                admin_discord_user_id=admin_discord_user_id,
            )
            return PlayerPenaltyAdjustmentResult(
                player_id=player_id,
                penalty_type=penalty_type,
                count=count,
            )

    def load_active_match_timer_states(
        self,
    ) -> tuple[datetime, tuple[ActiveMatchTimerState, ...]]:
        with session_scope(self.session_factory) as session:
            current_time = self._get_database_now(session)
            rows = session.scalars(
                select(ActiveMatchState)
                .where(ActiveMatchState.state != MatchState.FINALIZED)
                .order_by(ActiveMatchState.match_id)
            ).all()

        return current_time, tuple(
            ActiveMatchTimerState(
                match_id=row.match_id,
                state=row.state,
                parent_deadline_at=row.parent_deadline_at,
                report_open_at=row.report_open_at,
                reporting_opened_at=row.reporting_opened_at,
                report_deadline_at=row.report_deadline_at,
                approval_deadline_at=row.approval_deadline_at,
            )
            for row in rows
        )

    def _maybe_start_approval_after_report_locked(
        self,
        *,
        session: Session,
        active_state: ActiveMatchState,
        current_time: datetime,
    ) -> MatchFinalizationResult | None:
        if (
            active_state.state != MatchState.WAITING_FOR_RESULT_REPORTS
            or active_state.parent_decided_at is None
        ):
            return None

        latest_report_count = session.scalar(
            select(func.count(MatchReport.id)).where(
                MatchReport.match_id == active_state.match_id,
                MatchReport.is_latest.is_(True),
            )
        )
        participant_count = session.scalar(
            select(func.count(MatchParticipant.id)).where(
                MatchParticipant.match_id == active_state.match_id
            )
        )
        if latest_report_count != participant_count:
            return None

        return self._start_approval_locked(
            session,
            active_state=active_state,
            started_at=current_time,
            finalization_dedupe_suffix="all_reports",
        )

    def _start_approval_locked(
        self,
        session: Session,
        *,
        active_state: ActiveMatchState,
        started_at: datetime,
        finalization_dedupe_suffix: str,
    ) -> MatchFinalizationResult:
        participants = self._get_match_participants(session, active_state.match_id)
        match_format = active_state.match.match_format
        latest_reports_by_player = self._get_latest_reports_by_player(
            session,
            active_state.match_id,
        )
        provisional_result, unresolved_tie = self._determine_match_result(
            latest_reports_by_player=latest_reports_by_player,
            parent_player_id=active_state.parent_player_id,
        )
        format_definition = self._require_match_format_definition(match_format)
        admin_review_reasons = self._determine_admin_review_reasons(
            participants=participants,
            latest_reports_by_player=latest_reports_by_player,
            unresolved_tie=unresolved_tie,
            team_size=format_definition.team_size,
        )

        active_state.provisional_result = provisional_result
        active_state.admin_review_required = bool(admin_review_reasons)
        active_state.admin_review_reasons = admin_review_reasons

        player_states_by_player = {
            state.player_id: state
            for state in session.scalars(
                select(ActiveMatchPlayerState).where(
                    ActiveMatchPlayerState.match_id == active_state.match_id
                )
            ).all()
        }

        for participant in participants:
            latest_report = latest_reports_by_player.get(participant.player_id)
            report_status = self._determine_report_status(latest_report, provisional_result)
            approval_status = (
                MatchApprovalStatus.NOT_REQUIRED
                if report_status == MatchReportStatus.CORRECT
                else MatchApprovalStatus.PENDING
            )
            player_state = player_states_by_player.get(participant.player_id)
            if player_state is None:
                player_state = ActiveMatchPlayerState(
                    match_id=active_state.match_id,
                    player_id=participant.player_id,
                    report_status=report_status,
                    approval_status=approval_status,
                    locked_at=started_at,
                    approved_at=None,
                    locked_report_id=None if latest_report is None else latest_report.id,
                    last_reported_input_result=(
                        None if latest_report is None else latest_report.reported_input_result
                    ),
                    last_normalized_result=(
                        None if latest_report is None else latest_report.normalized_result
                    ),
                    last_reported_at=None if latest_report is None else latest_report.reported_at,
                )
                session.add(player_state)
                player_states_by_player[participant.player_id] = player_state
            else:
                player_state.report_status = report_status
                player_state.approval_status = approval_status
                player_state.locked_at = started_at
                player_state.approved_at = None
                player_state.locked_report_id = None if latest_report is None else latest_report.id
                player_state.last_reported_input_result = (
                    None if latest_report is None else latest_report.reported_input_result
                )
                player_state.last_normalized_result = (
                    None if latest_report is None else latest_report.normalized_result
                )
                player_state.last_reported_at = (
                    None if latest_report is None else latest_report.reported_at
                )

        session.flush()

        pending_player_ids = {
            player_id
            for player_id, player_state in player_states_by_player.items()
            if player_state.approval_status == MatchApprovalStatus.PENDING
        }
        if not pending_player_ids:
            active_state.approval_started_at = None
            active_state.approval_deadline_at = None
            return self._finalize_match_locked(
                session,
                active_state=active_state,
                final_result=provisional_result,
                finalized_at=started_at,
                finalized_by_admin=False,
                apply_auto_penalties=True,
                finalization_dedupe_suffix=finalization_dedupe_suffix,
            )

        active_state.approval_started_at = started_at
        active_state.approval_deadline_at = started_at + MATCH_APPROVAL_WINDOW
        active_state.state = MatchState.AWAITING_RESULT_APPROVALS

        for payload in self._build_match_channel_payloads(
            participants=participants,
            event_type=OutboxEventType.MATCH_APPROVAL_REQUESTED,
            extra_payload={
                "match_id": active_state.match_id,
                "provisional_result": active_state.provisional_result.value,
                "approval_deadline_at": active_state.approval_deadline_at.isoformat(),
                "phase_started": True,
            },
        ):
            destination = payload["destination"]
            self._enqueue_outbox_event(
                session,
                event_type=OutboxEventType.MATCH_APPROVAL_REQUESTED,
                dedupe_key=(
                    "match_approval_requested:phase_started:"
                    f"{active_state.match_id}:{destination['channel_id']}"
                ),
                payload=payload,
            )

        for participant in participants:
            if participant.player_id not in pending_player_ids:
                continue
            self._enqueue_outbox_event(
                session,
                event_type=OutboxEventType.MATCH_APPROVAL_REQUESTED,
                dedupe_key=f"match_approval_requested:{active_state.match_id}:{participant.player_id}",
                payload=self._build_match_approval_requested_payload(
                    participant=participant,
                    active_state=active_state,
                ),
            )

        return MatchFinalizationResult(
            match_id=active_state.match_id,
            final_result=provisional_result,
            finalized=False,
            finalized_at=None,
            approval_deadline_at=active_state.approval_deadline_at,
            admin_review_required=active_state.admin_review_required,
        )

    def _finalize_match_locked(
        self,
        session: Session,
        *,
        active_state: ActiveMatchState,
        final_result: MatchResult,
        finalized_at: datetime,
        finalized_by_admin: bool,
        apply_auto_penalties: bool,
        finalization_dedupe_suffix: str,
    ) -> MatchFinalizationResult:
        participants = self._get_match_participants(session, active_state.match_id)
        latest_reports_by_player = self._get_latest_reports_by_player(
            session,
            active_state.match_id,
        )
        player_states_by_player = self._ensure_active_player_states_for_finalization(
            session=session,
            active_state=active_state,
            participants=participants,
            latest_reports_by_player=latest_reports_by_player,
            final_result=final_result,
            finalized_at=finalized_at,
            finalized_by_admin=finalized_by_admin,
        )

        finalized_result = session.get(FinalizedMatchResult, active_state.match_id)
        if finalized_result is None:
            finalized_result = FinalizedMatchResult(match_id=active_state.match_id)
            session.add(finalized_result)

        finalized_result.created_at = active_state.match.created_at
        if finalized_result.rated_at is None:
            finalized_result.rated_at = finalized_at
        finalized_result.team_a_player_ids = [
            participant.player_id
            for participant in participants
            if participant.team == MatchParticipantTeam.TEAM_A
        ]
        finalized_result.team_b_player_ids = [
            participant.player_id
            for participant in participants
            if participant.team == MatchParticipantTeam.TEAM_B
        ]
        finalized_result.parent_player_id = active_state.parent_player_id
        finalized_result.parent_decided_at = active_state.parent_decided_at
        finalized_result.provisional_result = active_state.provisional_result
        finalized_result.final_result = final_result
        finalized_result.admin_review_required = active_state.admin_review_required
        finalized_result.admin_review_reasons = active_state.admin_review_reasons
        finalized_result.finalized_at = finalized_at
        finalized_result.finalized_by_admin = finalized_by_admin

        existing_finalized_by_player = {
            result.player_id: result
            for result in session.scalars(
                select(FinalizedMatchPlayerResult).where(
                    FinalizedMatchPlayerResult.match_id == active_state.match_id
                )
            ).all()
        }
        previous_auto_penalties_by_player = {
            result.player_id: (result.auto_penalty_type if result.auto_penalty_applied else None)
            for result in existing_finalized_by_player.values()
        }
        participants_by_player_id = {
            participant.player_id: participant for participant in participants
        }
        player_format_stats_by_player_id = self._get_player_format_stats_by_player_id(
            session,
            player_ids=[participant.player_id for participant in participants],
            match_format=active_state.match.match_format,
        )
        rating_snapshots_by_player_id = {
            participant.player_id: RatingParticipantSnapshot(
                player_id=participant.player_id,
                team=participant.team,
                rating=player_format_stats_by_player_id[participant.player_id].rating,
                games_played=player_format_stats_by_player_id[participant.player_id].games_played,
                wins=player_format_stats_by_player_id[participant.player_id].wins,
                losses=player_format_stats_by_player_id[participant.player_id].losses,
                draws=player_format_stats_by_player_id[participant.player_id].draws,
            )
            for participant in participants
        }
        auto_penalty_notifications: list[tuple[MatchParticipant, PenaltyType, int]] = []

        desired_auto_penalties_by_player: dict[int, PenaltyType | None] = {}
        for participant in participants:
            player_state = player_states_by_player[participant.player_id]
            desired_auto_penalty = self._determine_auto_penalty_type(
                report_status=player_state.report_status,
                approval_status=player_state.approval_status,
                apply_auto_penalties=apply_auto_penalties,
            )
            desired_auto_penalties_by_player[participant.player_id] = desired_auto_penalty

            finalized_player_result = existing_finalized_by_player.get(participant.player_id)
            latest_report = latest_reports_by_player.get(participant.player_id)
            if finalized_player_result is None:
                finalized_player_result = FinalizedMatchPlayerResult(
                    match_id=active_state.match_id,
                    player_id=participant.player_id,
                    team=participant.team,
                    rating_before=None,
                    games_played_before=None,
                    wins_before=None,
                    losses_before=None,
                    draws_before=None,
                    latest_report_id=None,
                    last_reported_input_result=None,
                    last_normalized_result=None,
                    last_reported_at=None,
                    report_status=player_state.report_status,
                    approval_status=player_state.approval_status,
                    approved_at=player_state.approved_at,
                    auto_penalty_type=desired_auto_penalty,
                    auto_penalty_applied=desired_auto_penalty is not None,
                )
                session.add(finalized_player_result)
            finalized_player_result.team = participant.team
            if finalized_player_result.rating_before is None:
                finalized_player_result.rating_before = rating_snapshots_by_player_id[
                    participant.player_id
                ].rating
            if finalized_player_result.games_played_before is None:
                finalized_player_result.games_played_before = rating_snapshots_by_player_id[
                    participant.player_id
                ].games_played
            if finalized_player_result.wins_before is None:
                finalized_player_result.wins_before = rating_snapshots_by_player_id[
                    participant.player_id
                ].wins
            if finalized_player_result.losses_before is None:
                finalized_player_result.losses_before = rating_snapshots_by_player_id[
                    participant.player_id
                ].losses
            if finalized_player_result.draws_before is None:
                finalized_player_result.draws_before = rating_snapshots_by_player_id[
                    participant.player_id
                ].draws
            finalized_player_result.latest_report_id = (
                None if latest_report is None else latest_report.id
            )
            finalized_player_result.last_reported_input_result = (
                None if latest_report is None else latest_report.reported_input_result
            )
            finalized_player_result.last_normalized_result = (
                None if latest_report is None else latest_report.normalized_result
            )
            finalized_player_result.last_reported_at = (
                None if latest_report is None else latest_report.reported_at
            )
            finalized_player_result.report_status = player_state.report_status
            finalized_player_result.approval_status = player_state.approval_status
            finalized_player_result.approved_at = player_state.approved_at
            finalized_player_result.auto_penalty_type = desired_auto_penalty
            finalized_player_result.auto_penalty_applied = desired_auto_penalty is not None

        for player_id, previous_penalty_type in previous_auto_penalties_by_player.items():
            desired_penalty_type = desired_auto_penalties_by_player.get(player_id)
            if previous_penalty_type == desired_penalty_type:
                continue
            if previous_penalty_type is not None:
                self._apply_penalty_adjustment(
                    session,
                    player_id=player_id,
                    match_id=active_state.match_id,
                    penalty_type=previous_penalty_type,
                    delta=-1,
                    source=PenaltyAdjustmentSource.ADMIN_RESULT_OVERRIDE,
                    admin_discord_user_id=None,
                )
            if desired_penalty_type is not None:
                penalty_count = self._apply_penalty_adjustment(
                    session,
                    player_id=player_id,
                    match_id=active_state.match_id,
                    penalty_type=desired_penalty_type,
                    delta=1,
                    source=PenaltyAdjustmentSource.AUTO_MATCH_FINALIZATION,
                    admin_discord_user_id=None,
                )
                auto_penalty_notifications.append(
                    (
                        participants_by_player_id[player_id],
                        desired_penalty_type,
                        penalty_count,
                    )
                )

        if finalized_by_admin:
            # TODO: Recalculate rating-related player state when match result correction support
            # is implemented. For now, admin overrides update only the stored match result.
            pass
        else:
            self._apply_rating_updates(
                participants=participants,
                final_result=final_result,
                rating_snapshots=tuple(rating_snapshots_by_player_id.values()),
                match_format=active_state.match.match_format,
                player_format_stats_by_player_id=player_format_stats_by_player_id,
            )

        for player_id, desired_penalty_type in desired_auto_penalties_by_player.items():
            if player_id in previous_auto_penalties_by_player:
                continue
            if desired_penalty_type is not None:
                penalty_count = self._apply_penalty_adjustment(
                    session,
                    player_id=player_id,
                    match_id=active_state.match_id,
                    penalty_type=desired_penalty_type,
                    delta=1,
                    source=PenaltyAdjustmentSource.AUTO_MATCH_FINALIZATION,
                    admin_discord_user_id=None,
                )
                auto_penalty_notifications.append(
                    (
                        participants_by_player_id[player_id],
                        desired_penalty_type,
                        penalty_count,
                    )
                )

        active_state.state = MatchState.FINALIZED
        active_state.finalized_at = finalized_at
        active_state.finalized_by_admin = finalized_by_admin

        self._enqueue_finalization_notifications_locked(
            session,
            active_state=active_state,
            participants=participants,
            final_result=final_result,
            finalized_at=finalized_at,
            finalized_by_admin=finalized_by_admin,
            finalization_dedupe_suffix=finalization_dedupe_suffix,
            auto_penalty_notifications=auto_penalty_notifications,
        )

        return MatchFinalizationResult(
            match_id=active_state.match_id,
            final_result=final_result,
            finalized=True,
            finalized_at=finalized_at,
            approval_deadline_at=active_state.approval_deadline_at,
            admin_review_required=active_state.admin_review_required,
        )

    def _override_finalized_match_locked(
        self,
        session: Session,
        *,
        active_state: ActiveMatchState,
        finalized_result: FinalizedMatchResult,
        final_result: MatchResult,
        finalized_at: datetime,
        finalization_dedupe_suffix: str,
    ) -> None:
        participants = self._get_match_participants(session, active_state.match_id)
        latest_reports_by_player = self._get_latest_reports_by_player(
            session,
            active_state.match_id,
        )
        player_states_by_player = self._ensure_active_player_states_for_finalization(
            session=session,
            active_state=active_state,
            participants=participants,
            latest_reports_by_player=latest_reports_by_player,
            final_result=final_result,
            finalized_at=finalized_at,
            finalized_by_admin=True,
        )

        finalized_result.created_at = active_state.match.created_at
        finalized_result.team_a_player_ids = [
            participant.player_id
            for participant in participants
            if participant.team == MatchParticipantTeam.TEAM_A
        ]
        finalized_result.team_b_player_ids = [
            participant.player_id
            for participant in participants
            if participant.team == MatchParticipantTeam.TEAM_B
        ]
        finalized_result.parent_player_id = active_state.parent_player_id
        finalized_result.parent_decided_at = active_state.parent_decided_at
        finalized_result.provisional_result = active_state.provisional_result
        finalized_result.final_result = final_result
        finalized_result.admin_review_required = active_state.admin_review_required
        finalized_result.admin_review_reasons = active_state.admin_review_reasons
        finalized_result.finalized_at = finalized_at
        finalized_result.finalized_by_admin = True

        existing_finalized_by_player = {
            result.player_id: result
            for result in session.scalars(
                select(FinalizedMatchPlayerResult).where(
                    FinalizedMatchPlayerResult.match_id == active_state.match_id
                )
            ).all()
        }
        previous_auto_penalties_by_player = {
            result.player_id: (result.auto_penalty_type if result.auto_penalty_applied else None)
            for result in existing_finalized_by_player.values()
        }
        player_format_stats_by_player_id = self._get_player_format_stats_by_player_id(
            session,
            player_ids=[participant.player_id for participant in participants],
            match_format=active_state.match.match_format,
        )
        rating_snapshots_by_player_id = {
            participant.player_id: RatingParticipantSnapshot(
                player_id=participant.player_id,
                team=participant.team,
                rating=player_format_stats_by_player_id[participant.player_id].rating,
                games_played=player_format_stats_by_player_id[participant.player_id].games_played,
                wins=player_format_stats_by_player_id[participant.player_id].wins,
                losses=player_format_stats_by_player_id[participant.player_id].losses,
                draws=player_format_stats_by_player_id[participant.player_id].draws,
            )
            for participant in participants
        }

        for participant in participants:
            player_state = player_states_by_player[participant.player_id]
            latest_report = latest_reports_by_player.get(participant.player_id)
            finalized_player_result = existing_finalized_by_player.get(participant.player_id)
            if finalized_player_result is None:
                finalized_player_result = FinalizedMatchPlayerResult(
                    match_id=active_state.match_id,
                    player_id=participant.player_id,
                    team=participant.team,
                    rating_before=rating_snapshots_by_player_id[participant.player_id].rating,
                    games_played_before=rating_snapshots_by_player_id[
                        participant.player_id
                    ].games_played,
                    wins_before=rating_snapshots_by_player_id[participant.player_id].wins,
                    losses_before=rating_snapshots_by_player_id[participant.player_id].losses,
                    draws_before=rating_snapshots_by_player_id[participant.player_id].draws,
                    latest_report_id=None,
                    last_reported_input_result=None,
                    last_normalized_result=None,
                    last_reported_at=None,
                    report_status=player_state.report_status,
                    approval_status=player_state.approval_status,
                    approved_at=player_state.approved_at,
                    auto_penalty_type=None,
                    auto_penalty_applied=False,
                )
                session.add(finalized_player_result)

            finalized_player_result.team = participant.team
            finalized_player_result.latest_report_id = (
                None if latest_report is None else latest_report.id
            )
            finalized_player_result.last_reported_input_result = (
                None if latest_report is None else latest_report.reported_input_result
            )
            finalized_player_result.last_normalized_result = (
                None if latest_report is None else latest_report.normalized_result
            )
            finalized_player_result.last_reported_at = (
                None if latest_report is None else latest_report.reported_at
            )
            finalized_player_result.report_status = player_state.report_status
            finalized_player_result.approval_status = player_state.approval_status
            finalized_player_result.approved_at = player_state.approved_at
            finalized_player_result.auto_penalty_type = None
            finalized_player_result.auto_penalty_applied = False

        for player_id, previous_penalty_type in previous_auto_penalties_by_player.items():
            if previous_penalty_type is None:
                continue
            self._apply_penalty_adjustment(
                session,
                player_id=player_id,
                match_id=active_state.match_id,
                penalty_type=previous_penalty_type,
                delta=-1,
                source=PenaltyAdjustmentSource.ADMIN_RESULT_OVERRIDE,
                admin_discord_user_id=None,
            )

        active_state.state = MatchState.FINALIZED
        active_state.finalized_at = finalized_at
        active_state.finalized_by_admin = True

        self._recalculate_ratings_after_match_correction_locked(
            session,
            target_finalized_result=finalized_result,
        )
        self._enqueue_finalization_notifications_locked(
            session,
            active_state=active_state,
            participants=participants,
            final_result=final_result,
            finalized_at=finalized_at,
            finalized_by_admin=True,
            finalization_dedupe_suffix=finalization_dedupe_suffix,
            auto_penalty_notifications=tuple(),
        )

    def _recalculate_ratings_after_match_correction_locked(
        self,
        session: Session,
        *,
        target_finalized_result: FinalizedMatchResult,
    ) -> None:
        if target_finalized_result.rated_at is None:
            raise MatchFlowError("試合結果の補正に必要な rated_at が見つかりません。")

        affected_match_ids = tuple(
            session.scalars(
                select(FinalizedMatchResult.match_id)
                .join(Match, Match.id == FinalizedMatchResult.match_id)
                .where(
                    Match.match_format == target_finalized_result.match.match_format,
                    or_(
                        FinalizedMatchResult.rated_at > target_finalized_result.rated_at,
                        and_(
                            FinalizedMatchResult.rated_at == target_finalized_result.rated_at,
                            FinalizedMatchResult.match_id >= target_finalized_result.match_id,
                        ),
                    ),
                )
                .order_by(FinalizedMatchResult.rated_at, FinalizedMatchResult.match_id)
            ).all()
        )
        for match_id in sorted(affected_match_ids):
            self._acquire_match_lock(session, match_id)

        affected_matches = list(
            session.scalars(
                select(FinalizedMatchResult)
                .options(selectinload(FinalizedMatchResult.player_results))
                .where(FinalizedMatchResult.match_id.in_(affected_match_ids))
                .order_by(FinalizedMatchResult.rated_at, FinalizedMatchResult.match_id)
            ).all()
        )
        affected_player_ids = sorted(
            {
                player_result.player_id
                for finalized_match in affected_matches
                for player_result in finalized_match.player_results
            }
        )
        for player_id in affected_player_ids:
            self._acquire_player_lock(session, player_id)

        locked_player_format_stats = session.scalars(
            select(PlayerFormatStats).where(
                PlayerFormatStats.player_id.in_(affected_player_ids),
                PlayerFormatStats.match_format == target_finalized_result.match.match_format,
            )
        ).all()
        player_format_stats_by_player_id = {
            player_format_stats.player_id: player_format_stats
            for player_format_stats in locked_player_format_stats
        }
        missing_player_ids = sorted(
            set(affected_player_ids) - set(player_format_stats_by_player_id)
        )
        if missing_player_ids:
            raise MatchFlowError(
                "試合結果の補正に必要なプレイヤー統計が不足しています。"
                f" player_ids={missing_player_ids}"
            )
        working_states = {
            player_id: PlayerRatingState(
                rating=player_format_stats.rating,
                games_played=player_format_stats.games_played,
                wins=player_format_stats.wins,
                losses=player_format_stats.losses,
                draws=player_format_stats.draws,
            )
            for player_id, player_format_stats in player_format_stats_by_player_id.items()
        }

        for finalized_match in reversed(affected_matches[1:]):
            for player_result in finalized_match.player_results:
                if (
                    player_result.rating_before is None
                    or player_result.games_played_before is None
                    or player_result.wins_before is None
                    or player_result.losses_before is None
                    or player_result.draws_before is None
                ):
                    raise MatchFlowError("試合結果の補正に必要な開始時点状態が不足しています。")
                working_states[player_result.player_id] = PlayerRatingState(
                    rating=player_result.rating_before,
                    games_played=player_result.games_played_before,
                    wins=player_result.wins_before,
                    losses=player_result.losses_before,
                    draws=player_result.draws_before,
                )

        for player_result in affected_matches[0].player_results:
            if (
                player_result.rating_before is None
                or player_result.games_played_before is None
                or player_result.wins_before is None
                or player_result.losses_before is None
                or player_result.draws_before is None
            ):
                raise MatchFlowError("試合結果の補正に必要な開始時点状態が不足しています。")
            working_states[player_result.player_id] = PlayerRatingState(
                rating=player_result.rating_before,
                games_played=player_result.games_played_before,
                wins=player_result.wins_before,
                losses=player_result.losses_before,
                draws=player_result.draws_before,
            )

        for finalized_match in affected_matches:
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

            for player_result in ordered_player_results:
                player_state = working_states[player_result.player_id]
                player_result.rating_before = player_state.rating
                player_result.games_played_before = player_state.games_played
                player_result.wins_before = player_state.wins
                player_result.losses_before = player_state.losses
                player_result.draws_before = player_state.draws

            rating_updates_by_player_id = calculate_rating_updates(
                rating_snapshots,
                finalized_match.final_result,
            )
            for player_id, rating_update in rating_updates_by_player_id.items():
                working_states[player_id] = PlayerRatingState(
                    rating=rating_update.rating_after,
                    games_played=rating_update.games_played_after,
                    wins=rating_update.wins_after,
                    losses=rating_update.losses_after,
                    draws=rating_update.draws_after,
                )

        for player_id, player_state in working_states.items():
            player_format_stats = player_format_stats_by_player_id[player_id]
            player_format_stats.rating = player_state.rating
            player_format_stats.games_played = player_state.games_played
            player_format_stats.wins = player_state.wins
            player_format_stats.losses = player_state.losses
            player_format_stats.draws = player_state.draws

    def _enqueue_finalization_notifications_locked(
        self,
        session: Session,
        *,
        active_state: ActiveMatchState,
        participants: Sequence[MatchParticipant],
        final_result: MatchResult,
        finalized_at: datetime,
        finalized_by_admin: bool,
        finalization_dedupe_suffix: str,
        auto_penalty_notifications: Sequence[tuple[MatchParticipant, PenaltyType, int]],
    ) -> None:
        finalized_notification_payload: dict[str, Any] = {
            "match_id": active_state.match_id,
            "final_result": final_result.value,
            "finalized_at": finalized_at.isoformat(),
            "finalized_by_admin": finalized_by_admin,
        }
        if not finalized_by_admin:
            finalized_notification_payload["team_a_rating_entries"] = (
                self._build_team_rating_entries(
                    participants=participants,
                    team=MatchParticipantTeam.TEAM_A,
                    match_format=active_state.match.match_format,
                )
            )
            finalized_notification_payload["team_b_rating_entries"] = (
                self._build_team_rating_entries(
                    participants=participants,
                    team=MatchParticipantTeam.TEAM_B,
                    match_format=active_state.match.match_format,
                )
            )

        for payload in self._build_match_channel_payloads(
            participants=participants,
            event_type=OutboxEventType.MATCH_FINALIZED,
            extra_payload=finalized_notification_payload,
        ):
            destination = payload["destination"]
            self._enqueue_outbox_event(
                session,
                event_type=OutboxEventType.MATCH_FINALIZED,
                dedupe_key=(
                    "match_finalized:"
                    f"{active_state.match_id}:{destination['channel_id']}:{finalization_dedupe_suffix}"
                ),
                payload=payload,
            )

        for participant, penalty_type, penalty_count in auto_penalty_notifications:
            self._enqueue_outbox_event(
                session,
                event_type=OutboxEventType.MATCH_FINALIZED,
                dedupe_key=(
                    "match_finalized:auto_penalty:"
                    f"{active_state.match_id}:{participant.player_id}:{finalization_dedupe_suffix}"
                ),
                payload={
                    "match_id": active_state.match_id,
                    "final_result": final_result.value,
                    "finalized_at": finalized_at.isoformat(),
                    "finalized_by_admin": finalized_by_admin,
                    "auto_penalty_applied": True,
                    "mention_discord_user_id": (
                        participant.notification_mention_discord_user_id
                        or participant.player.discord_user_id
                    ),
                    "penalty_type": penalty_type.value,
                    "penalty_count": penalty_count,
                    "destination": self._build_notification_destination_payload(
                        participant,
                        event_context="match_finalized_auto_penalty",
                    ),
                },
            )

        if active_state.admin_review_required:
            for payload in self._build_match_channel_payloads(
                participants=participants,
                event_type=OutboxEventType.MATCH_ADMIN_REVIEW_REQUIRED,
                extra_payload={
                    "match_id": active_state.match_id,
                    "final_result": final_result.value,
                    "admin_review_reasons": active_state.admin_review_reasons,
                    "admin_discord_user_ids": sorted(self.admin_discord_user_ids),
                },
            ):
                destination = payload["destination"]
                self._enqueue_outbox_event(
                    session,
                    event_type=OutboxEventType.MATCH_ADMIN_REVIEW_REQUIRED,
                    dedupe_key=(
                        "match_admin_review_required:"
                        f"{active_state.match_id}:{destination['channel_id']}"
                    ),
                    payload=payload,
                )

    def _apply_rating_updates(
        self,
        *,
        participants: Sequence[MatchParticipant],
        final_result: MatchResult,
        rating_snapshots: Sequence[RatingParticipantSnapshot],
        match_format: MatchFormat,
        player_format_stats_by_player_id: dict[int, PlayerFormatStats],
    ) -> None:
        rating_updates_by_player_id = calculate_rating_updates(rating_snapshots, final_result)
        for participant in participants:
            rating_update = rating_updates_by_player_id[participant.player_id]
            format_stats = player_format_stats_by_player_id[participant.player_id]
            if format_stats.match_format != match_format:
                raise MatchFlowError(
                    "試合フォーマットと更新対象のプレイヤー統計が一致していません。"
                )
            format_stats.rating = rating_update.rating_after
            format_stats.games_played = rating_update.games_played_after
            format_stats.wins = rating_update.wins_after
            format_stats.losses = rating_update.losses_after
            format_stats.draws = rating_update.draws_after

    def _ensure_active_player_states_for_finalization(
        self,
        *,
        session: Session,
        active_state: ActiveMatchState,
        participants: Sequence[MatchParticipant],
        latest_reports_by_player: dict[int, MatchReport],
        final_result: MatchResult,
        finalized_at: datetime,
        finalized_by_admin: bool,
    ) -> dict[int, ActiveMatchPlayerState]:
        player_states_by_player = {
            state.player_id: state
            for state in session.scalars(
                select(ActiveMatchPlayerState).where(
                    ActiveMatchPlayerState.match_id == active_state.match_id
                )
            ).all()
        }

        for participant in participants:
            latest_report = latest_reports_by_player.get(participant.player_id)
            player_state = player_states_by_player.get(participant.player_id)
            if player_state is None:
                player_state = ActiveMatchPlayerState(
                    match_id=active_state.match_id,
                    player_id=participant.player_id,
                    report_status=self._determine_report_status(latest_report, final_result),
                    approval_status=MatchApprovalStatus.NOT_REQUIRED,
                    locked_at=finalized_at,
                    approved_at=None,
                    locked_report_id=None if latest_report is None else latest_report.id,
                    last_reported_input_result=(
                        None if latest_report is None else latest_report.reported_input_result
                    ),
                    last_normalized_result=(
                        None if latest_report is None else latest_report.normalized_result
                    ),
                    last_reported_at=None if latest_report is None else latest_report.reported_at,
                )
                session.add(player_state)
                player_states_by_player[participant.player_id] = player_state
            else:
                player_state.report_status = self._determine_report_status(
                    latest_report,
                    final_result,
                )
                if player_state.approval_status == MatchApprovalStatus.PENDING:
                    player_state.approval_status = MatchApprovalStatus.NOT_APPROVED
                if finalized_by_admin and active_state.state in {
                    MatchState.WAITING_FOR_PARENT,
                    MatchState.WAITING_FOR_RESULT_REPORTS,
                }:
                    player_state.approval_status = MatchApprovalStatus.NOT_REQUIRED
                    player_state.approved_at = None
                player_state.locked_at = (
                    finalized_at if player_state.locked_at is None else player_state.locked_at
                )
                player_state.locked_report_id = None if latest_report is None else latest_report.id
                player_state.last_reported_input_result = (
                    None if latest_report is None else latest_report.reported_input_result
                )
                player_state.last_normalized_result = (
                    None if latest_report is None else latest_report.normalized_result
                )
                player_state.last_reported_at = (
                    None if latest_report is None else latest_report.reported_at
                )

        session.flush()
        return player_states_by_player

    def _assign_parent_locked(
        self,
        session: Session,
        *,
        active_state: ActiveMatchState,
        participants: Sequence[MatchParticipant],
        parent_player_id: int,
        decided_at: datetime,
        event_dedupe_suffix: str,
    ) -> MatchParentAssignmentResult:
        active_state.parent_player_id = parent_player_id
        active_state.parent_decided_at = decided_at
        active_state.report_open_at = decided_at + MATCH_REPORT_OPEN_DELAY
        active_state.reporting_opened_at = None
        active_state.report_deadline_at = decided_at + MATCH_REPORT_DEADLINE_DELAY
        active_state.state = MatchState.WAITING_FOR_RESULT_REPORTS
        parent_participant = next(
            participant for participant in participants if participant.player_id == parent_player_id
        )

        for payload in self._build_match_channel_payloads(
            participants=participants,
            event_type=OutboxEventType.MATCH_PARENT_ASSIGNED,
            extra_payload={
                "match_id": active_state.match_id,
                "parent_discord_user_id": (
                    parent_participant.notification_mention_discord_user_id
                    or parent_participant.player.discord_user_id
                ),
                "report_open_at": active_state.report_open_at.isoformat(),
                "report_deadline_at": active_state.report_deadline_at.isoformat(),
            },
        ):
            destination = payload["destination"]
            self._enqueue_outbox_event(
                session,
                event_type=OutboxEventType.MATCH_PARENT_ASSIGNED,
                dedupe_key=(
                    "match_parent_assigned:"
                    f"{active_state.match_id}:{destination['channel_id']}:{event_dedupe_suffix}"
                ),
                payload=payload,
            )

        return MatchParentAssignmentResult(
            match_id=active_state.match_id,
            parent_player_id=parent_player_id,
            parent_decided_at=active_state.parent_decided_at,
            report_open_at=active_state.report_open_at,
            report_deadline_at=active_state.report_deadline_at,
            assigned=True,
        )

    def _determine_match_result(
        self,
        *,
        latest_reports_by_player: dict[int, MatchReport],
        parent_player_id: int | None,
    ) -> tuple[MatchResult, bool]:
        if not latest_reports_by_player:
            return MatchResult.VOID, False

        counts = Counter(report.normalized_result for report in latest_reports_by_player.values())
        max_count = max(counts.values())
        candidates = [result for result, count in counts.items() if count == max_count]
        if len(candidates) == 1:
            return candidates[0], False

        if parent_player_id is not None:
            parent_report = latest_reports_by_player.get(parent_player_id)
            if parent_report is not None and parent_report.normalized_result in candidates:
                return parent_report.normalized_result, False

        return MatchResult.VOID, True

    def _determine_admin_review_reasons(
        self,
        *,
        participants: Sequence[MatchParticipant],
        latest_reports_by_player: dict[int, MatchReport],
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

    def _determine_report_status(
        self,
        latest_report: MatchReport | None,
        final_result: MatchResult,
    ) -> MatchReportStatus:
        if latest_report is None:
            return MatchReportStatus.NOT_REPORTED
        if latest_report.normalized_result == final_result:
            return MatchReportStatus.CORRECT
        return MatchReportStatus.INCORRECT

    def _determine_auto_penalty_type(
        self,
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

    def _normalize_report_result(
        self,
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

    def _apply_penalty_adjustment(
        self,
        session: Session,
        *,
        player_id: int,
        match_id: int | None,
        penalty_type: PenaltyType,
        delta: int,
        source: PenaltyAdjustmentSource,
        admin_discord_user_id: int | None,
    ) -> int:
        penalty = session.get(
            PlayerPenalty,
            {"player_id": player_id, "penalty_type": penalty_type},
        )
        current_time = self._get_database_now(session)
        if penalty is None:
            penalty = PlayerPenalty(
                player_id=player_id,
                penalty_type=penalty_type,
                count=0,
                updated_at=current_time,
            )
            session.add(penalty)

        penalty.count += delta
        penalty.updated_at = current_time
        session.add(
            PlayerPenaltyAdjustment(
                player_id=player_id,
                match_id=match_id,
                penalty_type=penalty_type,
                delta=delta,
                source=source,
                admin_discord_user_id=admin_discord_user_id,
                created_at=current_time,
            )
        )
        return penalty.count

    def _build_match_channel_payloads(
        self,
        *,
        participants: Sequence[MatchParticipant],
        event_type: OutboxEventType,
        extra_payload: dict[str, Any],
    ) -> tuple[dict[str, Any], ...]:
        destinations_by_channel_id: dict[int, NotificationDestinationPayload] = {}
        for participant in participants:
            destination = self._build_notification_destination_payload(
                participant,
                event_context=event_type.value,
            )
            destinations_by_channel_id.setdefault(destination["channel_id"], destination)

        team_a_discord_user_ids = [
            participant.notification_mention_discord_user_id or participant.player.discord_user_id
            for participant in participants
            if participant.team == MatchParticipantTeam.TEAM_A
        ]
        team_b_discord_user_ids = [
            participant.notification_mention_discord_user_id or participant.player.discord_user_id
            for participant in participants
            if participant.team == MatchParticipantTeam.TEAM_B
        ]
        return tuple(
            {
                **extra_payload,
                "destination": destination,
                "team_a_discord_user_ids": team_a_discord_user_ids,
                "team_b_discord_user_ids": team_b_discord_user_ids,
            }
            for destination in destinations_by_channel_id.values()
        )

    def _build_team_rating_entries(
        self,
        *,
        participants: Sequence[MatchParticipant],
        team: MatchParticipantTeam,
        match_format: MatchFormat,
    ) -> list[TeamRatingEntryPayload]:
        return [
            {
                "discord_user_id": (
                    participant.notification_mention_discord_user_id
                    or participant.player.discord_user_id
                ),
                "rating": self._get_player_format_rating(
                    participant.player,
                    match_format=match_format,
                ),
            }
            for participant in participants
            if participant.team == team
        ]

    def _get_player_format_stats_by_player_id(
        self,
        session: Session,
        *,
        player_ids: Sequence[int],
        match_format: MatchFormat,
    ) -> dict[int, PlayerFormatStats]:
        player_format_stats = session.scalars(
            select(PlayerFormatStats).where(
                PlayerFormatStats.player_id.in_(player_ids),
                PlayerFormatStats.match_format == match_format,
            )
        ).all()
        player_format_stats_by_player_id = {
            format_stats.player_id: format_stats for format_stats in player_format_stats
        }
        missing_player_ids = sorted(set(player_ids) - set(player_format_stats_by_player_id))
        if missing_player_ids:
            raise MatchFlowError(
                "プレイヤー統計が見つかりません。"
                f" match_format={match_format.value} player_ids={missing_player_ids}"
            )
        return player_format_stats_by_player_id

    def _get_player_format_rating(
        self,
        player: Player,
        *,
        match_format: MatchFormat,
    ) -> float:
        for format_stats in player.format_stats:
            if format_stats.match_format == match_format:
                return format_stats.rating
        raise MatchFlowError(
            "プレイヤー統計が見つかりません。"
            f" player_id={player.id} match_format={match_format.value}"
        )

    def _require_match_format_definition(
        self,
        match_format: MatchFormat,
    ) -> MatchFormatDefinition:
        format_definition = get_match_format_definition(match_format)
        if format_definition is None:
            raise MatchFlowError(f"未対応の対戦フォーマットです: {match_format.value}")
        return format_definition

    def _build_match_approval_requested_payload(
        self,
        *,
        participant: MatchParticipant,
        active_state: ActiveMatchState,
    ) -> dict[str, Any]:
        return {
            "match_id": active_state.match_id,
            "provisional_result": (
                None
                if active_state.provisional_result is None
                else active_state.provisional_result.value
            ),
            "approval_deadline_at": (
                None
                if active_state.approval_deadline_at is None
                else active_state.approval_deadline_at.isoformat()
            ),
            "destination": self._build_notification_destination_payload(
                participant,
                event_context="match_approval_requested",
            ),
            "phase_started": False,
            "mention_discord_user_id": (
                participant.notification_mention_discord_user_id
                or participant.player.discord_user_id
            ),
        }

    def _build_notification_destination_payload(
        self,
        participant: MatchParticipant,
        *,
        event_context: str,
    ) -> NotificationDestinationPayload:
        if participant.notification_channel_id is None:
            raise ValueError(
                f"notification_channel_id is missing for {event_context} "
                f"match_id={participant.match_id} player_id={participant.player_id}"
            )
        return {
            "channel_id": participant.notification_channel_id,
            "guild_id": participant.notification_guild_id,
        }

    def _apply_match_notification_context(
        self,
        participant: MatchParticipant,
        notification_context: MatchingQueueNotificationContext | None,
        *,
        mention_discord_user_id: int,
        recorded_at: datetime,
    ) -> None:
        participant.notification_mention_discord_user_id = mention_discord_user_id
        if notification_context is None:
            return

        participant.notification_channel_id = notification_context.channel_id
        participant.notification_guild_id = notification_context.guild_id
        participant.notification_mention_discord_user_id = (
            notification_context.mention_discord_user_id
        )
        participant.notification_recorded_at = recorded_at

    def _get_match_participants(
        self,
        session: Session,
        match_id: int,
    ) -> list[MatchParticipant]:
        return list(
            session.scalars(
                select(MatchParticipant)
                .where(MatchParticipant.match_id == match_id)
                .order_by(MatchParticipant.team, MatchParticipant.slot)
            ).all()
        )

    def _get_match_participant_for_update(
        self,
        session: Session,
        match_id: int,
        player_id: int,
    ) -> MatchParticipant | None:
        return session.scalar(
            select(MatchParticipant)
            .where(
                MatchParticipant.match_id == match_id,
                MatchParticipant.player_id == player_id,
            )
            .with_for_update()
        )

    def _get_latest_report_for_update(
        self,
        session: Session,
        match_id: int,
        player_id: int,
    ) -> MatchReport | None:
        return session.scalar(
            select(MatchReport)
            .where(
                MatchReport.match_id == match_id,
                MatchReport.player_id == player_id,
                MatchReport.is_latest.is_(True),
            )
            .with_for_update()
        )

    def _get_latest_reports_by_player(
        self,
        session: Session,
        match_id: int,
    ) -> dict[int, MatchReport]:
        latest_reports = session.scalars(
            select(MatchReport).where(
                MatchReport.match_id == match_id,
                MatchReport.is_latest.is_(True),
            )
        ).all()
        return {report.player_id: report for report in latest_reports}

    def _get_active_match_state_for_update(
        self,
        session: Session,
        match_id: int,
    ) -> ActiveMatchState | None:
        return session.scalar(
            select(ActiveMatchState).where(ActiveMatchState.match_id == match_id).with_for_update()
        )

    def _get_active_player_state_for_update(
        self,
        session: Session,
        match_id: int,
        player_id: int,
    ) -> ActiveMatchPlayerState | None:
        return session.scalar(
            select(ActiveMatchPlayerState)
            .where(
                ActiveMatchPlayerState.match_id == match_id,
                ActiveMatchPlayerState.player_id == player_id,
            )
            .with_for_update()
        )

    def _get_database_now(self, session: Session) -> datetime:
        return session.execute(select(func.now())).scalar_one()

    def _ensure_not_finalized(self, active_state: ActiveMatchState) -> None:
        if active_state.state == MatchState.FINALIZED:
            raise MatchAlreadyFinalizedError("この試合はすでに結果確定済みです。")

    def _ensure_player_exists(self, session: Session, player_id: int) -> Player:
        player = session.get(Player, player_id)
        if player is None:
            raise MatchParticipantError("指定したプレイヤーが見つかりません。")
        return player

    def _raise_missing_match(self, match_id: int) -> None:
        del match_id
        raise MatchNotFoundError("指定した試合が見つかりません。")

    def _raise_retryable_task_error(self, exc: Exception, *, operation: str) -> None:
        if isinstance(exc, RetryableTaskError):
            return
        if _is_transient_task_db_error(exc):
            raise RetryableTaskError(f"Temporary database failure while {operation}") from exc

    def _acquire_match_lock(self, session: Session, match_id: int) -> None:
        session.execute(
            select(func.pg_advisory_xact_lock(_MATCH_ADVISORY_LOCK_NAMESPACE, match_id))
        )

    def _acquire_player_lock(self, session: Session, player_id: int) -> None:
        session.execute(
            select(func.pg_advisory_xact_lock(_PLAYER_ADVISORY_LOCK_NAMESPACE, player_id))
        )

    def _enqueue_outbox_event(
        self,
        session: Session,
        *,
        event_type: OutboxEventType,
        dedupe_key: str,
        payload: dict[str, Any],
    ) -> None:
        inserted_event_id = session.execute(
            pg_insert(OutboxEvent)
            .values(
                event_type=event_type,
                dedupe_key=dedupe_key,
                payload=payload,
            )
            .on_conflict_do_nothing(index_elements=[OutboxEvent.dedupe_key])
            .returning(OutboxEvent.id)
        ).scalar_one_or_none()

        if inserted_event_id is None:
            return

        session.execute(
            select(
                func.pg_notify(
                    OUTBOX_NOTIFY_CHANNEL,
                    str(inserted_event_id),
                )
            )
        )
