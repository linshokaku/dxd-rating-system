from __future__ import annotations

import logging
import random
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from bot.constants import OUTBOX_NOTIFY_CHANNEL
from bot.db.session import session_scope
from bot.models import (
    FinalizedMatchResult,
    Match,
    MatchParticipant,
    MatchParticipantApprovalStatus,
    MatchParticipantReportStatus,
    MatchParticipantTeam,
    MatchReport,
    MatchReportInput,
    MatchResultType,
    MatchState,
    OutboxEvent,
    OutboxEventType,
    Player,
    PlayerPenalty,
    PlayerPenaltyType,
)
from bot.services.errors import (
    MatchApprovalNotOpenError,
    MatchApprovalNotRequiredError,
    MatchNotFoundError,
    MatchParticipantError,
    MatchReportClosedError,
    MatchReportNotOpenError,
    ParentAlreadyDecidedError,
    ParentVolunteerClosedError,
)

PARENT_RECRUITMENT_WINDOW = timedelta(minutes=5)
REPORT_OPEN_DELAY = timedelta(minutes=7)
REPORT_DEADLINE_DELAY = timedelta(minutes=27)
APPROVAL_WINDOW = timedelta(minutes=5)
MATCH_PARTICIPANT_COUNT = 6
MATCH_NOTIFICATION_PARENT_DECIDED = "match_parent_decided"
MATCH_NOTIFICATION_APPROVAL_STARTED = "match_approval_started"
MATCH_NOTIFICATION_FINALIZED = "match_finalized"
MATCH_NOTIFICATION_ADMIN_REVIEW_REQUIRED = "match_admin_review_required"


@dataclass(frozen=True, slots=True)
class ParentVolunteerResult:
    match_id: int
    parent_player_id: int


@dataclass(frozen=True, slots=True)
class MatchReportSubmissionResult:
    match_id: int
    player_id: int
    input_result: MatchReportInput
    approval_started: bool


@dataclass(frozen=True, slots=True)
class MatchApprovalResult:
    match_id: int
    player_id: int


@dataclass(frozen=True, slots=True)
class MatchFinalizationResult:
    match_id: int
    final_result: MatchResultType
    admin_review_required: bool


@dataclass(frozen=True, slots=True)
class PenaltyAdjustmentResult:
    player_id: int
    penalty_type: PlayerPenaltyType
    count: int


@dataclass(frozen=True, slots=True)
class MatchReconcileResult:
    auto_parent_match_ids: tuple[int, ...]
    approval_started_match_ids: tuple[int, ...]
    finalized_match_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _DecisionSnapshot:
    result: MatchResultType
    report_statuses: dict[int, MatchParticipantReportStatus]
    approval_statuses: dict[int, MatchParticipantApprovalStatus]
    admin_review_required: bool
    admin_review_reasons: tuple[str, ...]


class MatchService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        logger: logging.Logger | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.logger = logger or logging.getLogger(__name__)
        self.rng = rng or random.Random()

    def volunteer_parent(self, match_id: int, player_id: int) -> ParentVolunteerResult:
        result: ParentVolunteerResult | None = None

        with session_scope(self.session_factory) as session:
            match = self._get_match_for_update(session, match_id)
            current_time = self._get_database_now(session)
            participants = self._get_participants_for_match(session, match.id)
            self._require_participant(participants, player_id)

            if match.state != MatchState.WAITING_FOR_PARENT:
                if match.parent_player_id is not None:
                    raise ParentAlreadyDecidedError(
                        f"Parent is already decided for match_id={match_id}"
                    )
                raise ParentVolunteerClosedError(
                    f"Parent volunteer window is closed for match_id={match_id}"
                )
            if match.parent_player_id is not None:
                raise ParentAlreadyDecidedError(
                    f"Parent is already decided for match_id={match_id}"
                )

            parent_deadline = match.created_at + PARENT_RECRUITMENT_WINDOW
            if current_time >= parent_deadline:
                raise ParentVolunteerClosedError(
                    f"Parent volunteer window is closed for match_id={match_id}"
                )

            self._decide_parent(session, match, participants, player_id, decided_at=current_time)
            result = ParentVolunteerResult(match_id=match.id, parent_player_id=player_id)

        if result is None:
            raise RuntimeError("volunteer_parent result was not created")
        return result

    def submit_report(
        self,
        match_id: int,
        player_id: int,
        input_result: MatchReportInput,
    ) -> MatchReportSubmissionResult:
        result: MatchReportSubmissionResult | None = None

        with session_scope(self.session_factory) as session:
            match = self._get_match_for_update(session, match_id)
            current_time = self._get_database_now(session)
            participants = self._get_participants_for_match(session, match.id)
            participant = self._require_participant(participants, player_id)
            latest_reports = self._get_latest_reports_for_match(session, match.id)

            self._validate_report_submission(
                match=match,
                current_time=current_time,
                input_result=input_result,
            )

            existing_latest_report = latest_reports.get(player_id)
            if existing_latest_report is not None:
                existing_latest_report.is_latest = False

            report = MatchReport(
                match_id=match.id,
                player_id=player_id,
                reported_input_result=input_result,
                normalized_result=self._normalize_input_result(input_result, participant.team),
                reported_at=current_time,
                is_latest=True,
            )
            session.add(report)
            latest_reports[player_id] = report

            approval_started = self._maybe_start_approval(
                session,
                match,
                participants,
                latest_reports,
                current_time=current_time,
                force_by_deadline=False,
            )

            result = MatchReportSubmissionResult(
                match_id=match.id,
                player_id=player_id,
                input_result=input_result,
                approval_started=approval_started,
            )

        if result is None:
            raise RuntimeError("submit_report result was not created")
        return result

    def approve_provisional_result(self, match_id: int, player_id: int) -> MatchApprovalResult:
        result: MatchApprovalResult | None = None

        with session_scope(self.session_factory) as session:
            match = self._get_match_for_update(session, match_id)
            current_time = self._get_database_now(session)
            participants = self._get_participants_for_match(session, match.id)
            participant = self._require_participant(participants, player_id)

            if match.state != MatchState.AWAITING_RESULT_APPROVALS:
                raise MatchApprovalNotOpenError(
                    f"Approval period is not open for match_id={match_id}"
                )
            if (
                match.approval_deadline_at is not None
                and current_time >= match.approval_deadline_at
            ):
                raise MatchApprovalNotOpenError(
                    f"Approval period is already closed for match_id={match_id}"
                )
            if participant.approval_status != MatchParticipantApprovalStatus.PENDING:
                raise MatchApprovalNotRequiredError(
                    f"Approval is not required for player_id={player_id} match_id={match_id}"
                )

            participant.approval_status = MatchParticipantApprovalStatus.APPROVED
            participant.approved_at = current_time
            result = MatchApprovalResult(match_id=match.id, player_id=player_id)

        if result is None:
            raise RuntimeError("approve_provisional_result result was not created")
        return result

    def override_final_result(
        self,
        match_id: int,
        final_result: MatchResultType,
    ) -> MatchFinalizationResult:
        result: MatchFinalizationResult | None = None

        with session_scope(self.session_factory) as session:
            match = self._get_match_for_update(session, match_id)
            current_time = self._get_database_now(session)
            participants = self._get_participants_for_match(session, match.id)
            latest_reports = self._get_latest_reports_for_match(session, match.id)

            decision = self._build_status_snapshot(
                participants=participants,
                latest_reports=latest_reports,
                final_result=final_result,
                preserve_approved=True,
            )
            self._apply_decision_snapshot(
                participants=participants,
                decision=decision,
                locked_at=current_time,
                preserve_existing_approvals=True,
            )
            for participant in participants:
                if participant.approval_status == MatchParticipantApprovalStatus.PENDING:
                    participant.approval_status = MatchParticipantApprovalStatus.NOT_APPROVED

            match.state = MatchState.FINALIZED
            match.provisional_result = final_result
            match.admin_review_required = False

            self._upsert_finalized_result(
                session,
                match=match,
                participants=participants,
                final_result=final_result,
                finalized_at=current_time,
            )
            self._sync_automatic_penalties(session, participants, current_time=current_time)
            self._enqueue_outbox_event(
                session,
                event_type=OutboxEventType.MATCH_CREATED,
                dedupe_key=f"match_finalized:{match.id}:{current_time.isoformat()}",
                payload=self._build_match_finalized_payload(
                    match_id=match.id,
                    final_result=final_result,
                    finalized_at=current_time,
                ),
            )

            result = MatchFinalizationResult(
                match_id=match.id,
                final_result=final_result,
                admin_review_required=False,
            )

        if result is None:
            raise RuntimeError("override_final_result result was not created")
        return result

    def adjust_penalty(
        self,
        player_id: int,
        penalty_type: PlayerPenaltyType,
        delta: int,
    ) -> PenaltyAdjustmentResult:
        if delta == 0:
            raise ValueError("delta must not be zero")

        result: PenaltyAdjustmentResult | None = None
        with session_scope(self.session_factory) as session:
            current_time = self._get_database_now(session)
            self._ensure_player_exists(session, player_id)
            count = self._adjust_penalty_counter(
                session,
                player_id=player_id,
                penalty_type=penalty_type,
                delta=delta,
                updated_at=current_time,
            )
            result = PenaltyAdjustmentResult(
                player_id=player_id,
                penalty_type=penalty_type,
                count=count,
            )

        if result is None:
            raise RuntimeError("adjust_penalty result was not created")
        return result

    def run_startup_sync(self) -> MatchReconcileResult:
        return self.run_reconcile_cycle()

    def run_reconcile_cycle(self) -> MatchReconcileResult:
        auto_parent_match_ids: list[int] = []
        approval_started_match_ids: list[int] = []
        finalized_match_ids: list[int] = []

        due_parent_match_ids = self._load_due_match_ids(
            state=MatchState.WAITING_FOR_PARENT,
            time_column=Match.created_at,
            delay=PARENT_RECRUITMENT_WINDOW,
        )
        for match_id in due_parent_match_ids:
            if self._process_parent_deadline(match_id):
                auto_parent_match_ids.append(match_id)

        due_approval_match_ids = self._load_due_match_ids(
            state=MatchState.WAITING_FOR_RESULT_REPORTS,
            time_column=Match.report_deadline_at,
        )
        for match_id in due_approval_match_ids:
            if self._process_report_deadline(match_id):
                approval_started_match_ids.append(match_id)

        due_finalize_match_ids = self._load_due_match_ids(
            state=MatchState.AWAITING_RESULT_APPROVALS,
            time_column=Match.approval_deadline_at,
        )
        for match_id in due_finalize_match_ids:
            if self._process_approval_deadline(match_id):
                finalized_match_ids.append(match_id)

        result = MatchReconcileResult(
            auto_parent_match_ids=tuple(auto_parent_match_ids),
            approval_started_match_ids=tuple(approval_started_match_ids),
            finalized_match_ids=tuple(finalized_match_ids),
        )
        if auto_parent_match_ids or approval_started_match_ids or finalized_match_ids:
            self.logger.info(
                "Processed match reconcile auto_parent=%s approval_started=%s finalized=%s",
                len(auto_parent_match_ids),
                len(approval_started_match_ids),
                len(finalized_match_ids),
            )
        return result

    def _process_parent_deadline(self, match_id: int) -> bool:
        with session_scope(self.session_factory) as session:
            match = self._get_match_for_update(session, match_id)
            current_time = self._get_database_now(session)
            if match.state != MatchState.WAITING_FOR_PARENT:
                return False
            if match.parent_player_id is not None:
                return False
            if current_time < match.created_at + PARENT_RECRUITMENT_WINDOW:
                return False

            participants = self._get_participants_for_match(session, match.id)
            chosen_parent = self.rng.choice(participants).player_id
            self._decide_parent(
                session,
                match,
                participants,
                chosen_parent,
                decided_at=current_time,
            )
            return True

    def _process_report_deadline(self, match_id: int) -> bool:
        with session_scope(self.session_factory) as session:
            match = self._get_match_for_update(session, match_id)
            current_time = self._get_database_now(session)
            if match.state != MatchState.WAITING_FOR_RESULT_REPORTS:
                return False
            if match.report_deadline_at is None or current_time < match.report_deadline_at:
                return False

            participants = self._get_participants_for_match(session, match.id)
            latest_reports = self._get_latest_reports_for_match(session, match.id)
            return self._maybe_start_approval(
                session,
                match,
                participants,
                latest_reports,
                current_time=current_time,
                force_by_deadline=True,
            )

    def _process_approval_deadline(self, match_id: int) -> bool:
        with session_scope(self.session_factory) as session:
            match = self._get_match_for_update(session, match_id)
            current_time = self._get_database_now(session)
            if match.state != MatchState.AWAITING_RESULT_APPROVALS:
                return False
            if match.approval_deadline_at is None or current_time < match.approval_deadline_at:
                return False

            participants = self._get_participants_for_match(session, match.id)
            finalization = self._finalize_match(
                session,
                match,
                participants,
                finalized_at=current_time,
            )
            return finalization

    def _decide_parent(
        self,
        session: Session,
        match: Match,
        participants: list[MatchParticipant],
        parent_player_id: int,
        *,
        decided_at: datetime,
    ) -> None:
        match.parent_player_id = parent_player_id
        match.parent_decided_at = decided_at
        match.report_open_at = decided_at + REPORT_OPEN_DELAY
        match.report_deadline_at = decided_at + REPORT_DEADLINE_DELAY
        match.state = MatchState.WAITING_FOR_RESULT_REPORTS
        self._enqueue_outbox_event(
            session,
            event_type=OutboxEventType.MATCH_CREATED,
            dedupe_key=f"match_parent_decided:{match.id}",
            payload=self._build_match_parent_decided_payload(match),
        )

        latest_reports = self._get_latest_reports_for_match(session, match.id)
        self._maybe_start_approval(
            session,
            match,
            participants,
            latest_reports,
            current_time=decided_at,
            force_by_deadline=False,
        )

    def _maybe_start_approval(
        self,
        session: Session,
        match: Match,
        participants: list[MatchParticipant],
        latest_reports: dict[int, MatchReport],
        *,
        current_time: datetime,
        force_by_deadline: bool,
    ) -> bool:
        if match.state != MatchState.WAITING_FOR_RESULT_REPORTS:
            return False

        all_reports_submitted = len(latest_reports) == len(participants) == MATCH_PARTICIPANT_COUNT
        if not all_reports_submitted:
            if not force_by_deadline:
                return False
            if match.report_deadline_at is None or current_time < match.report_deadline_at:
                return False

        decision = self._build_provisional_decision(
            match=match,
            participants=participants,
            latest_reports=latest_reports,
        )
        match.state = MatchState.AWAITING_RESULT_APPROVALS
        match.provisional_result = decision.result
        match.approval_started_at = current_time
        match.approval_deadline_at = current_time + APPROVAL_WINDOW
        match.admin_review_required = decision.admin_review_required
        self._apply_decision_snapshot(
            participants=participants,
            decision=decision,
            locked_at=current_time,
            preserve_existing_approvals=False,
        )

        self._enqueue_outbox_event(
            session,
            event_type=OutboxEventType.MATCH_CREATED,
            dedupe_key=f"match_approval_started:{match.id}:{current_time.isoformat()}",
            payload=self._build_match_approval_started_payload(
                match_id=match.id,
                provisional_result=decision.result,
                approval_deadline_at=match.approval_deadline_at,
                approval_target_player_ids=[
                    participant.player_id
                    for participant in participants
                    if participant.approval_status == MatchParticipantApprovalStatus.PENDING
                ],
            ),
        )
        return True

    def _finalize_match(
        self,
        session: Session,
        match: Match,
        participants: list[MatchParticipant],
        *,
        finalized_at: datetime,
    ) -> bool:
        if match.provisional_result is None:
            raise RuntimeError(f"provisional_result is missing for match_id={match.id}")

        for participant in participants:
            if participant.approval_status == MatchParticipantApprovalStatus.PENDING:
                participant.approval_status = MatchParticipantApprovalStatus.NOT_APPROVED

        match.state = MatchState.FINALIZED
        self._upsert_finalized_result(
            session,
            match=match,
            participants=participants,
            final_result=match.provisional_result,
            finalized_at=finalized_at,
        )
        self._sync_automatic_penalties(session, participants, current_time=finalized_at)
        self._enqueue_outbox_event(
            session,
            event_type=OutboxEventType.MATCH_CREATED,
            dedupe_key=f"match_finalized:{match.id}:{finalized_at.isoformat()}",
            payload=self._build_match_finalized_payload(
                match_id=match.id,
                final_result=match.provisional_result,
                finalized_at=finalized_at,
            ),
        )

        decision = self._build_provisional_decision(
            match=match,
            participants=participants,
            latest_reports=self._get_latest_reports_for_match(session, match.id),
        )
        if match.admin_review_required:
            self._enqueue_outbox_event(
                session,
                event_type=OutboxEventType.MATCH_CREATED,
                dedupe_key=f"match_admin_review_required:{match.id}:{finalized_at.isoformat()}",
                payload=self._build_match_admin_review_payload(
                    match_id=match.id,
                    final_result=match.provisional_result,
                    reasons=decision.admin_review_reasons,
                ),
            )
        return True

    def _build_provisional_decision(
        self,
        *,
        match: Match,
        participants: list[MatchParticipant],
        latest_reports: dict[int, MatchReport],
    ) -> _DecisionSnapshot:
        if not latest_reports:
            final_result = MatchResultType.VOID
            unresolved_tie = False
        else:
            result_counter = Counter(report.normalized_result for report in latest_reports.values())
            highest_vote_count = max(result_counter.values())
            top_results = {
                result for result, count in result_counter.items() if count == highest_vote_count
            }

            if len(top_results) == 1:
                final_result = next(iter(top_results))
                unresolved_tie = False
            else:
                parent_report = (
                    latest_reports.get(match.parent_player_id)
                    if match.parent_player_id is not None
                    else None
                )
                if parent_report is not None and parent_report.normalized_result in top_results:
                    final_result = parent_report.normalized_result
                    unresolved_tie = False
                else:
                    final_result = MatchResultType.VOID
                    unresolved_tie = True

        decision = self._build_status_snapshot(
            participants=participants,
            latest_reports=latest_reports,
            final_result=final_result,
            preserve_approved=False,
        )
        admin_review_reasons = list(decision.admin_review_reasons)
        if unresolved_tie and "unresolved_tie" not in admin_review_reasons:
            admin_review_reasons.append("unresolved_tie")

        return _DecisionSnapshot(
            result=decision.result,
            report_statuses=decision.report_statuses,
            approval_statuses=decision.approval_statuses,
            admin_review_required=bool(admin_review_reasons),
            admin_review_reasons=tuple(admin_review_reasons),
        )

    def _build_status_snapshot(
        self,
        *,
        participants: list[MatchParticipant],
        latest_reports: dict[int, MatchReport],
        final_result: MatchResultType,
        preserve_approved: bool,
    ) -> _DecisionSnapshot:
        report_statuses: dict[int, MatchParticipantReportStatus] = {}
        approval_statuses: dict[int, MatchParticipantApprovalStatus] = {}

        for participant in participants:
            latest_report = latest_reports.get(participant.player_id)
            if latest_report is None:
                report_status = MatchParticipantReportStatus.NOT_REPORTED
            elif latest_report.normalized_result == final_result:
                report_status = MatchParticipantReportStatus.CORRECT
            else:
                report_status = MatchParticipantReportStatus.INCORRECT

            report_statuses[participant.player_id] = report_status
            if preserve_approved and (
                participant.approval_status == MatchParticipantApprovalStatus.APPROVED
            ):
                approval_statuses[participant.player_id] = MatchParticipantApprovalStatus.APPROVED
            elif report_status == MatchParticipantReportStatus.CORRECT:
                approval_statuses[participant.player_id] = (
                    MatchParticipantApprovalStatus.NOT_REQUIRED
                )
            else:
                approval_statuses[participant.player_id] = MatchParticipantApprovalStatus.PENDING

        review_reasons = self._build_admin_review_reasons(
            participants=participants,
            latest_reports=latest_reports,
        )
        return _DecisionSnapshot(
            result=final_result,
            report_statuses=report_statuses,
            approval_statuses=approval_statuses,
            admin_review_required=bool(review_reasons),
            admin_review_reasons=review_reasons,
        )

    def _build_admin_review_reasons(
        self,
        *,
        participants: list[MatchParticipant],
        latest_reports: dict[int, MatchReport],
    ) -> tuple[str, ...]:
        if not latest_reports:
            return ("reported_player_count_lte_2",)

        participants_by_player_id = {
            participant.player_id: participant for participant in participants
        }
        reported_participants = [
            participants_by_player_id[player_id]
            for player_id in latest_reports
            if player_id in participants_by_player_id
        ]

        reasons: list[str] = []
        if len(reported_participants) <= 2:
            reasons.append("reported_player_count_lte_2")

        reported_teams = {participant.team for participant in reported_participants}
        if len(reported_teams) == 1:
            reasons.append("reports_from_single_team_only")

        return tuple(reasons)

    def _apply_decision_snapshot(
        self,
        *,
        participants: list[MatchParticipant],
        decision: _DecisionSnapshot,
        locked_at: datetime,
        preserve_existing_approvals: bool,
    ) -> None:
        for participant in participants:
            participant.report_status = decision.report_statuses[participant.player_id]
            participant.report_status_locked_at = locked_at

            if preserve_existing_approvals and (
                participant.approval_status == MatchParticipantApprovalStatus.APPROVED
            ):
                continue

            participant.approval_status = decision.approval_statuses[participant.player_id]
            if participant.approval_status != MatchParticipantApprovalStatus.APPROVED:
                participant.approved_at = None

    def _upsert_finalized_result(
        self,
        session: Session,
        *,
        match: Match,
        participants: list[MatchParticipant],
        final_result: MatchResultType,
        finalized_at: datetime,
    ) -> None:
        team_a_player_ids = [
            participant.player_id
            for participant in participants
            if participant.team == MatchParticipantTeam.TEAM_A
        ]
        team_b_player_ids = [
            participant.player_id
            for participant in participants
            if participant.team == MatchParticipantTeam.TEAM_B
        ]

        finalized_result = session.get(FinalizedMatchResult, match.id)
        if finalized_result is None:
            finalized_result = FinalizedMatchResult(
                match_id=match.id,
                created_at=match.created_at,
                team_a_player_ids=team_a_player_ids,
                team_b_player_ids=team_b_player_ids,
                parent_player_id=match.parent_player_id,
                parent_decided_at=match.parent_decided_at,
                final_result=final_result,
                finalized_at=finalized_at,
            )
            session.add(finalized_result)
            return

        finalized_result.created_at = match.created_at
        finalized_result.team_a_player_ids = team_a_player_ids
        finalized_result.team_b_player_ids = team_b_player_ids
        finalized_result.parent_player_id = match.parent_player_id
        finalized_result.parent_decided_at = match.parent_decided_at
        finalized_result.final_result = final_result
        finalized_result.finalized_at = finalized_at

    def _sync_automatic_penalties(
        self,
        session: Session,
        participants: list[MatchParticipant],
        *,
        current_time: datetime,
    ) -> None:
        for participant in participants:
            should_apply_incorrect = (
                participant.report_status == MatchParticipantReportStatus.INCORRECT
                and participant.approval_status != MatchParticipantApprovalStatus.APPROVED
            )
            should_apply_not_reported = (
                participant.report_status == MatchParticipantReportStatus.NOT_REPORTED
                and participant.approval_status != MatchParticipantApprovalStatus.APPROVED
            )

            if should_apply_incorrect and not participant.auto_incorrect_penalty_applied:
                self._adjust_penalty_counter(
                    session,
                    player_id=participant.player_id,
                    penalty_type=PlayerPenaltyType.INCORRECT_REPORT,
                    delta=1,
                    updated_at=current_time,
                )
                participant.auto_incorrect_penalty_applied = True
            elif not should_apply_incorrect and participant.auto_incorrect_penalty_applied:
                self._adjust_penalty_counter(
                    session,
                    player_id=participant.player_id,
                    penalty_type=PlayerPenaltyType.INCORRECT_REPORT,
                    delta=-1,
                    updated_at=current_time,
                )
                participant.auto_incorrect_penalty_applied = False

            if should_apply_not_reported and not participant.auto_not_reported_penalty_applied:
                self._adjust_penalty_counter(
                    session,
                    player_id=participant.player_id,
                    penalty_type=PlayerPenaltyType.NOT_REPORTED,
                    delta=1,
                    updated_at=current_time,
                )
                participant.auto_not_reported_penalty_applied = True
            elif not should_apply_not_reported and participant.auto_not_reported_penalty_applied:
                self._adjust_penalty_counter(
                    session,
                    player_id=participant.player_id,
                    penalty_type=PlayerPenaltyType.NOT_REPORTED,
                    delta=-1,
                    updated_at=current_time,
                )
                participant.auto_not_reported_penalty_applied = False

    def _adjust_penalty_counter(
        self,
        session: Session,
        *,
        player_id: int,
        penalty_type: PlayerPenaltyType,
        delta: int,
        updated_at: datetime,
    ) -> int:
        penalty = session.scalar(
            select(PlayerPenalty).where(
                PlayerPenalty.player_id == player_id,
                PlayerPenalty.penalty_type == penalty_type,
            )
        )
        if penalty is None:
            penalty = PlayerPenalty(
                player_id=player_id,
                penalty_type=penalty_type,
                count=0,
                updated_at=updated_at,
            )
            session.add(penalty)
            session.flush()

        penalty.count = max(penalty.count + delta, 0)
        penalty.updated_at = updated_at
        return penalty.count

    def _validate_report_submission(
        self,
        *,
        match: Match,
        current_time: datetime,
        input_result: MatchReportInput,
    ) -> None:
        if match.state == MatchState.FINALIZED:
            raise MatchReportClosedError(f"Match is already finalized: match_id={match.id}")
        if match.state == MatchState.AWAITING_RESULT_APPROVALS:
            raise MatchReportClosedError(f"Approval period is active: match_id={match.id}")

        if match.state == MatchState.WAITING_FOR_PARENT:
            if input_result == MatchReportInput.VOID:
                return
            raise MatchReportNotOpenError(f"Report is not open yet: match_id={match.id}")

        if match.report_deadline_at is not None and current_time >= match.report_deadline_at:
            raise MatchReportClosedError(f"Report deadline has passed: match_id={match.id}")
        if input_result == MatchReportInput.VOID:
            return
        if match.report_open_at is None or current_time < match.report_open_at:
            raise MatchReportNotOpenError(f"Report is not open yet: match_id={match.id}")

    def _normalize_input_result(
        self,
        input_result: MatchReportInput,
        team: MatchParticipantTeam,
    ) -> MatchResultType:
        if input_result == MatchReportInput.DRAW:
            return MatchResultType.DRAW
        if input_result == MatchReportInput.VOID:
            return MatchResultType.VOID
        if team == MatchParticipantTeam.TEAM_A:
            return (
                MatchResultType.TEAM_A_WIN
                if input_result == MatchReportInput.WIN
                else MatchResultType.TEAM_B_WIN
            )
        return (
            MatchResultType.TEAM_B_WIN
            if input_result == MatchReportInput.WIN
            else MatchResultType.TEAM_A_WIN
        )

    def _load_due_match_ids(
        self,
        *,
        state: MatchState,
        time_column: Any,
        delay: timedelta | None = None,
    ) -> tuple[int, ...]:
        with session_scope(self.session_factory) as session:
            current_time = self._get_database_now(session)
            comparison_value = current_time - delay if delay is not None else current_time
            match_ids = session.scalars(
                select(Match.id)
                .where(
                    Match.state == state,
                    time_column.is_not(None),
                    time_column <= comparison_value,
                )
                .order_by(Match.id)
            ).all()
        return tuple(match_ids)

    def _get_match_for_update(self, session: Session, match_id: int) -> Match:
        match = session.scalar(select(Match).where(Match.id == match_id).with_for_update())
        if match is None:
            raise MatchNotFoundError(f"Match not found: {match_id}")
        return match

    def _get_participants_for_match(
        self,
        session: Session,
        match_id: int,
    ) -> list[MatchParticipant]:
        return session.scalars(
            select(MatchParticipant)
            .where(MatchParticipant.match_id == match_id)
            .order_by(MatchParticipant.team, MatchParticipant.slot, MatchParticipant.id)
        ).all()

    def _get_latest_reports_for_match(
        self,
        session: Session,
        match_id: int,
    ) -> dict[int, MatchReport]:
        latest_reports = session.scalars(
            select(MatchReport)
            .where(
                MatchReport.match_id == match_id,
                MatchReport.is_latest.is_(True),
            )
            .order_by(MatchReport.player_id, MatchReport.id)
        ).all()
        return {report.player_id: report for report in latest_reports}

    def _require_participant(
        self,
        participants: list[MatchParticipant],
        player_id: int,
    ) -> MatchParticipant:
        for participant in participants:
            if participant.player_id == player_id:
                return participant
        raise MatchParticipantError(f"Player is not a participant: player_id={player_id}")

    def _ensure_player_exists(self, session: Session, player_id: int) -> None:
        player = session.get(Player, player_id)
        if player is None:
            raise MatchParticipantError(f"Player is not registered: {player_id}")

    def _get_database_now(self, session: Session) -> datetime:
        return session.execute(select(func.now())).scalar_one()

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

        session.execute(select(func.pg_notify(OUTBOX_NOTIFY_CHANNEL, str(inserted_event_id))))

    def _build_match_parent_decided_payload(self, match: Match) -> dict[str, Any]:
        if match.parent_player_id is None:
            raise RuntimeError(f"parent_player_id is missing for match_id={match.id}")
        if match.parent_decided_at is None:
            raise RuntimeError(f"parent_decided_at is missing for match_id={match.id}")
        if match.report_open_at is None:
            raise RuntimeError(f"report_open_at is missing for match_id={match.id}")
        if match.report_deadline_at is None:
            raise RuntimeError(f"report_deadline_at is missing for match_id={match.id}")
        return {
            "notification_kind": MATCH_NOTIFICATION_PARENT_DECIDED,
            "match_id": match.id,
            "parent_player_id": match.parent_player_id,
            "parent_decided_at": match.parent_decided_at.isoformat(),
            "report_open_at": match.report_open_at.isoformat(),
            "report_deadline_at": match.report_deadline_at.isoformat(),
        }

    def _build_match_approval_started_payload(
        self,
        *,
        match_id: int,
        provisional_result: MatchResultType,
        approval_deadline_at: datetime | None,
        approval_target_player_ids: list[int],
    ) -> dict[str, Any]:
        if approval_deadline_at is None:
            raise RuntimeError(f"approval_deadline_at is missing for match_id={match_id}")
        return {
            "notification_kind": MATCH_NOTIFICATION_APPROVAL_STARTED,
            "match_id": match_id,
            "provisional_result": provisional_result.value,
            "approval_deadline_at": approval_deadline_at.isoformat(),
            "approval_target_player_ids": approval_target_player_ids,
        }

    def _build_match_finalized_payload(
        self,
        *,
        match_id: int,
        final_result: MatchResultType,
        finalized_at: datetime,
    ) -> dict[str, Any]:
        return {
            "notification_kind": MATCH_NOTIFICATION_FINALIZED,
            "match_id": match_id,
            "final_result": final_result.value,
            "finalized_at": finalized_at.isoformat(),
        }

    def _build_match_admin_review_payload(
        self,
        *,
        match_id: int,
        final_result: MatchResultType,
        reasons: tuple[str, ...],
    ) -> dict[str, Any]:
        return {
            "notification_kind": MATCH_NOTIFICATION_ADMIN_REVIEW_REQUIRED,
            "match_id": match_id,
            "final_result": final_result.value,
            "reasons": list(reasons),
        }
