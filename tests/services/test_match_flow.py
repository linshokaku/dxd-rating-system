from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from bot.constants import MATCH_PARENT_SELECTION_WINDOW
from bot.models import (
    ActiveMatchPlayerState,
    ActiveMatchState,
    FinalizedMatchPlayerResult,
    FinalizedMatchResult,
    MatchApprovalStatus,
    MatchParticipant,
    MatchParticipantTeam,
    MatchReportInputResult,
    MatchReportStatus,
    MatchResult,
    MatchState,
    OutboxEvent,
    OutboxEventType,
    PenaltyAdjustmentSource,
    PenaltyType,
    Player,
    PlayerPenalty,
    PlayerPenaltyAdjustment,
)
from bot.services import MatchFlowService, MatchingQueueNotificationContext, MatchingQueueService
from bot.services.registration import register_player


def get_database_now(session: Session) -> datetime:
    return session.execute(select(func.now())).scalar_one()


def create_player(session: Session, discord_user_id: int) -> Player:
    player = register_player(session=session, discord_user_id=discord_user_id)
    session.commit()
    return player


def create_players(
    session: Session,
    count: int,
    *,
    start_discord_user_id: int,
) -> list[Player]:
    return [create_player(session, start_discord_user_id + index) for index in range(count)]


def create_match(
    session: Session,
    session_factory: sessionmaker[Session],
    *,
    start_discord_user_id: int,
    channel_id: int,
    guild_id: int,
) -> tuple[int, list[Player], list[MatchParticipant]]:
    players = create_players(session, 6, start_discord_user_id=start_discord_user_id)
    queue_service = MatchingQueueService(session_factory)
    for player in players:
        queue_service.join_queue(
            player.id,
            notification_context=MatchingQueueNotificationContext(
                channel_id=channel_id,
                guild_id=guild_id,
                mention_discord_user_id=player.discord_user_id,
            ),
        )

    created_matches = queue_service.try_create_matches()

    session.expire_all()
    assert len(created_matches) == 1
    participants = session.scalars(
        select(MatchParticipant)
        .where(MatchParticipant.match_id == created_matches[0].match_id)
        .order_by(MatchParticipant.team, MatchParticipant.slot)
    ).all()
    return created_matches[0].match_id, players, participants


def test_try_create_matches_initializes_active_match_state_and_notification_context(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    match_id, players, participants = create_match(
        session,
        session_factory,
        start_discord_user_id=60_100,
        channel_id=91_001,
        guild_id=92_001,
    )

    active_state = session.get(ActiveMatchState, match_id)

    assert active_state is not None
    assert active_state.state == MatchState.WAITING_FOR_PARENT
    assert active_state.parent_player_id is None
    assert active_state.parent_decided_at is None
    assert active_state.report_open_at is None
    assert active_state.report_deadline_at is None
    assert active_state.approval_deadline_at is None
    assert active_state.admin_review_required is False
    assert active_state.admin_review_reasons == []
    assert active_state.parent_deadline_at == (
        active_state.created_at + MATCH_PARENT_SELECTION_WINDOW
    )
    assert (
        session.scalars(
            select(ActiveMatchPlayerState).where(ActiveMatchPlayerState.match_id == match_id)
        ).all()
        == []
    )

    participant_by_player_id = {participant.player_id: participant for participant in participants}
    for player in players:
        participant = participant_by_player_id[player.id]
        assert participant.notification_channel_id == 91_001
        assert participant.notification_guild_id == 92_001
        assert participant.notification_mention_discord_user_id == player.discord_user_id
        assert participant.notification_recorded_at is not None


def test_submit_reports_finalizes_immediately_when_no_approval_targets(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    match_id, _, participants = create_match(
        session,
        session_factory,
        start_discord_user_id=60_150,
        channel_id=91_001_5,
        guild_id=92_001_5,
    )
    match_service = MatchFlowService(session_factory)
    parent = participants[0]

    match_service.volunteer_parent(match_id, parent.player_id)

    session.expire_all()
    active_state = session.get(ActiveMatchState, match_id)
    assert active_state is not None
    now = get_database_now(session)
    active_state.report_open_at = now - timedelta(minutes=1)
    active_state.report_deadline_at = now + timedelta(minutes=10)
    session.commit()

    assert match_service.process_report_open(match_id) is True

    last_result = None
    for participant in participants:
        input_result = (
            MatchReportInputResult.WIN
            if participant.team == MatchParticipantTeam.TEAM_A
            else MatchReportInputResult.LOSE
        )
        last_result = match_service.submit_report(match_id, participant.player_id, input_result)

    session.expire_all()
    active_state = session.get(ActiveMatchState, match_id)
    finalized_result = session.get(FinalizedMatchResult, match_id)
    player_states = session.scalars(
        select(ActiveMatchPlayerState).where(ActiveMatchPlayerState.match_id == match_id)
    ).all()
    approval_events = session.scalars(
        select(OutboxEvent)
        .where(OutboxEvent.event_type == OutboxEventType.MATCH_APPROVAL_REQUESTED)
        .order_by(OutboxEvent.id)
    ).all()
    finalized_events = session.scalars(
        select(OutboxEvent).where(OutboxEvent.event_type == OutboxEventType.MATCH_FINALIZED)
    ).all()
    penalties = session.scalars(select(PlayerPenalty)).all()

    assert last_result is not None
    assert last_result.finalized is True
    assert last_result.approval_started is False
    assert last_result.approval_deadline_at is None
    assert active_state is not None
    assert active_state.state == MatchState.FINALIZED
    assert finalized_result is not None
    assert finalized_result.final_result == MatchResult.TEAM_A_WIN
    assert len(player_states) == 6
    assert all(
        player_state.report_status == MatchReportStatus.CORRECT for player_state in player_states
    )
    assert all(
        player_state.approval_status == MatchApprovalStatus.NOT_REQUIRED
        for player_state in player_states
    )
    assert approval_events == []
    assert len(finalized_events) == 1
    assert penalties == []


def test_submit_reports_from_all_players_starts_approval_and_accepts_approval(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    match_id, _, participants = create_match(
        session,
        session_factory,
        start_discord_user_id=60_200,
        channel_id=91_002,
        guild_id=92_002,
    )
    match_service = MatchFlowService(session_factory)
    parent = participants[0]

    assignment = match_service.volunteer_parent(match_id, parent.player_id)

    session.expire_all()
    active_state = session.get(ActiveMatchState, match_id)
    assert assignment.assigned is True
    assert active_state is not None
    assert active_state.parent_player_id == parent.player_id

    now = get_database_now(session)
    active_state.report_open_at = now - timedelta(minutes=1)
    active_state.report_deadline_at = now + timedelta(minutes=10)
    session.commit()

    assert match_service.process_report_open(match_id) is True

    dissenting_participant = next(
        participant
        for participant in reversed(participants)
        if participant.team == MatchParticipantTeam.TEAM_B
    )
    last_result = None
    for participant in participants:
        if participant.team == MatchParticipantTeam.TEAM_A:
            input_result = MatchReportInputResult.WIN
        elif participant.player_id == dissenting_participant.player_id:
            input_result = MatchReportInputResult.DRAW
        else:
            input_result = MatchReportInputResult.LOSE

        last_result = match_service.submit_report(
            match_id,
            participant.player_id,
            input_result,
        )

    session.expire_all()
    active_state = session.get(ActiveMatchState, match_id)
    player_states = session.scalars(
        select(ActiveMatchPlayerState).where(ActiveMatchPlayerState.match_id == match_id)
    ).all()
    approval_events = session.scalars(
        select(OutboxEvent).where(
            OutboxEvent.event_type == OutboxEventType.MATCH_APPROVAL_REQUESTED
        )
    ).all()

    assert last_result is not None
    assert last_result.approval_started is True
    assert last_result.approval_deadline_at is not None
    assert active_state is not None
    assert active_state.state == MatchState.AWAITING_RESULT_APPROVALS
    assert active_state.provisional_result == MatchResult.TEAM_A_WIN
    assert len(player_states) == 6
    assert {
        player_state.player_id
        for player_state in player_states
        if player_state.approval_status == MatchApprovalStatus.PENDING
    } == {dissenting_participant.player_id}
    assert len(approval_events) == 2
    assert approval_events[0].payload["phase_started"] is True
    assert approval_events[1].payload["phase_started"] is False
    assert approval_events[1].payload["mention_discord_user_id"] == (
        dissenting_participant.notification_mention_discord_user_id
    )

    approval_result = match_service.approve_provisional_result(
        match_id,
        dissenting_participant.player_id,
    )

    session.expire_all()
    dissenting_state = session.get(
        ActiveMatchPlayerState,
        {"match_id": match_id, "player_id": dissenting_participant.player_id},
    )

    assert last_result.finalized is False
    assert approval_result.approval_status == MatchApprovalStatus.APPROVED
    assert dissenting_state is not None
    assert dissenting_state.approval_status == MatchApprovalStatus.APPROVED
    assert dissenting_state.approved_at is not None


def test_process_deadlines_finalizes_match_and_applies_auto_penalties(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    match_id, _, participants = create_match(
        session,
        session_factory,
        start_discord_user_id=60_300,
        channel_id=91_003,
        guild_id=92_003,
    )
    match_service = MatchFlowService(session_factory)
    parent = participants[0]
    team_b_participants = [
        participant
        for participant in participants
        if participant.team == MatchParticipantTeam.TEAM_B
    ]
    dissenting_participant = team_b_participants[1]
    missing_participant = team_b_participants[2]

    match_service.volunteer_parent(match_id, parent.player_id)

    session.expire_all()
    active_state = session.get(ActiveMatchState, match_id)
    assert active_state is not None
    now = get_database_now(session)
    active_state.report_open_at = now - timedelta(minutes=1)
    active_state.report_deadline_at = now + timedelta(minutes=10)
    session.commit()

    assert match_service.process_report_open(match_id) is True

    for participant in participants:
        if participant.player_id == missing_participant.player_id:
            continue
        if participant.team == MatchParticipantTeam.TEAM_A:
            input_result = MatchReportInputResult.WIN
        elif participant.player_id == dissenting_participant.player_id:
            input_result = MatchReportInputResult.DRAW
        else:
            input_result = MatchReportInputResult.LOSE

        match_service.submit_report(match_id, participant.player_id, input_result)

    session.expire_all()
    active_state = session.get(ActiveMatchState, match_id)
    assert active_state is not None
    active_state.report_deadline_at = get_database_now(session) - timedelta(seconds=1)
    session.commit()

    report_deadline_result = match_service.process_report_deadline(match_id)

    session.expire_all()
    active_state = session.get(ActiveMatchState, match_id)
    assert report_deadline_result.finalized is False
    assert report_deadline_result.final_result == MatchResult.TEAM_A_WIN
    assert active_state is not None
    assert active_state.state == MatchState.AWAITING_RESULT_APPROVALS
    assert active_state.approval_deadline_at is not None

    active_state.approval_deadline_at = get_database_now(session) - timedelta(seconds=1)
    session.commit()

    approval_deadline_result = match_service.process_approval_deadline(match_id)

    session.expire_all()
    finalized_result = session.get(FinalizedMatchResult, match_id)
    finalized_player_results = session.scalars(
        select(FinalizedMatchPlayerResult).where(FinalizedMatchPlayerResult.match_id == match_id)
    ).all()
    penalties = session.scalars(select(PlayerPenalty)).all()
    penalty_adjustments = session.scalars(
        select(PlayerPenaltyAdjustment).where(PlayerPenaltyAdjustment.match_id == match_id)
    ).all()
    finalized_events = session.scalars(
        select(OutboxEvent)
        .where(OutboxEvent.event_type == OutboxEventType.MATCH_FINALIZED)
        .order_by(OutboxEvent.id)
    ).all()
    finalized_by_player_id = {
        player_result.player_id: player_result for player_result in finalized_player_results
    }
    penalties_by_key = {
        (penalty.player_id, penalty.penalty_type): penalty.count for penalty in penalties
    }

    assert approval_deadline_result.finalized is True
    assert approval_deadline_result.final_result == MatchResult.TEAM_A_WIN
    assert finalized_result is not None
    assert finalized_result.final_result == MatchResult.TEAM_A_WIN
    assert finalized_result.finalized_by_admin is False
    assert finalized_by_player_id[dissenting_participant.player_id].report_status == (
        MatchReportStatus.INCORRECT
    )
    assert finalized_by_player_id[dissenting_participant.player_id].auto_penalty_type == (
        PenaltyType.INCORRECT_REPORT
    )
    assert finalized_by_player_id[missing_participant.player_id].report_status == (
        MatchReportStatus.NOT_REPORTED
    )
    assert finalized_by_player_id[missing_participant.player_id].auto_penalty_type == (
        PenaltyType.NO_REPORT
    )
    assert penalties_by_key == {
        (dissenting_participant.player_id, PenaltyType.INCORRECT_REPORT): 1,
        (missing_participant.player_id, PenaltyType.NO_REPORT): 1,
    }
    assert len(finalized_events) == 3
    assert finalized_events[0].payload.get("auto_penalty_applied") is None
    assert {
        (
            event.payload["mention_discord_user_id"],
            event.payload["penalty_type"],
            event.payload["penalty_count"],
        )
        for event in finalized_events[1:]
    } == {
        (
            dissenting_participant.notification_mention_discord_user_id,
            PenaltyType.INCORRECT_REPORT.value,
            1,
        ),
        (
            missing_participant.notification_mention_discord_user_id,
            PenaltyType.NO_REPORT.value,
            1,
        ),
    }
    assert {
        (
            adjustment.player_id,
            adjustment.penalty_type,
            adjustment.source,
            adjustment.delta,
        )
        for adjustment in penalty_adjustments
    } == {
        (
            dissenting_participant.player_id,
            PenaltyType.INCORRECT_REPORT,
            PenaltyAdjustmentSource.AUTO_MATCH_FINALIZATION,
            1,
        ),
        (
            missing_participant.player_id,
            PenaltyType.NO_REPORT,
            PenaltyAdjustmentSource.AUTO_MATCH_FINALIZATION,
            1,
        ),
    }
