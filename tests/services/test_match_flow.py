from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

import bot.services.match_flow as match_flow_module
from bot.constants import MATCH_PARENT_SELECTION_WINDOW, get_match_format_definition
from bot.models import (
    INITIAL_RATING,
    ActiveMatchPlayerState,
    ActiveMatchState,
    FinalizedMatchPlayerResult,
    FinalizedMatchResult,
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
from bot.services import MatchFlowService, MatchingQueueNotificationContext, MatchingQueueService
from bot.services.errors import MatchNotFinalizedError, MatchReportNotOpenError
from bot.services.rating import RatingParticipantSnapshot, calculate_rating_updates
from bot.services.registration import register_player

DEFAULT_MATCH_FORMAT = MatchFormat.THREE_VS_THREE
DEFAULT_QUEUE_NAME = "low"


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


def get_player_format_stats_by_player_id(
    session: Session,
    player_ids: list[int],
    *,
    match_format: MatchFormat = DEFAULT_MATCH_FORMAT,
) -> dict[int, PlayerFormatStats]:
    rows = session.scalars(
        select(PlayerFormatStats).where(
            PlayerFormatStats.player_id.in_(player_ids),
            PlayerFormatStats.match_format == match_format,
        )
    ).all()
    return {row.player_id: row for row in rows}


def create_matches_for_format(
    session: Session,
    session_factory: sessionmaker[Session],
    *,
    match_format: MatchFormat,
    start_discord_user_id: int,
    channel_id: int,
    guild_id: int,
) -> tuple[list[Player], dict[int, list[MatchParticipant]]]:
    format_definition = get_match_format_definition(match_format)
    assert format_definition is not None
    players = create_players(
        session,
        format_definition.players_per_batch,
        start_discord_user_id=start_discord_user_id,
    )
    queue_service = MatchingQueueService(session_factory)
    for player in players:
        queue_service.join_queue(
            player.id,
            match_format,
            DEFAULT_QUEUE_NAME,
            notification_context=MatchingQueueNotificationContext(
                channel_id=channel_id,
                guild_id=guild_id,
                mention_discord_user_id=player.discord_user_id,
            ),
        )

    created_matches = queue_service.try_create_matches()

    session.expire_all()
    assert len(created_matches) == format_definition.batch_size
    participants_by_match_id: dict[int, list[MatchParticipant]] = {}
    for created_match in created_matches:
        participants_by_match_id[created_match.match_id] = session.scalars(
            select(MatchParticipant)
            .where(MatchParticipant.match_id == created_match.match_id)
            .order_by(MatchParticipant.team, MatchParticipant.slot)
        ).all()
    return players, participants_by_match_id


def build_initial_rating_snapshots(
    participants: list[MatchParticipant],
) -> tuple[RatingParticipantSnapshot, ...]:
    return tuple(
        RatingParticipantSnapshot(
            player_id=participant.player_id,
            team=participant.team,
            rating=INITIAL_RATING,
            games_played=0,
            wins=0,
            losses=0,
            draws=0,
        )
        for participant in participants
    )


def create_first_match_for_format(
    session: Session,
    session_factory: sessionmaker[Session],
    *,
    match_format: MatchFormat,
    start_discord_user_id: int,
    channel_id: int,
    guild_id: int,
) -> tuple[int, list[MatchParticipant]]:
    _, participants_by_match_id = create_matches_for_format(
        session,
        session_factory,
        match_format=match_format,
        start_discord_user_id=start_discord_user_id,
        channel_id=channel_id,
        guild_id=guild_id,
    )
    match_id = sorted(participants_by_match_id)[0]
    return match_id, participants_by_match_id[match_id]


def set_report_window_open(session: Session, match_id: int) -> ActiveMatchState:
    session.expire_all()
    active_state = session.get(ActiveMatchState, match_id)
    assert active_state is not None
    now = get_database_now(session)
    active_state.report_open_at = now - timedelta(minutes=1)
    active_state.report_deadline_at = now + timedelta(minutes=10)
    session.commit()
    return active_state


def set_parent_deadline_passed(session: Session, match_id: int) -> ActiveMatchState:
    session.expire_all()
    active_state = session.get(ActiveMatchState, match_id)
    assert active_state is not None
    active_state.parent_deadline_at = get_database_now(session) - timedelta(seconds=1)
    session.commit()
    return active_state


def set_report_deadline_passed(session: Session, match_id: int) -> ActiveMatchState:
    session.expire_all()
    active_state = session.get(ActiveMatchState, match_id)
    assert active_state is not None
    active_state.report_deadline_at = get_database_now(session) - timedelta(seconds=1)
    session.commit()
    return active_state


def set_approval_deadline_passed(session: Session, match_id: int) -> ActiveMatchState:
    session.expire_all()
    active_state = session.get(ActiveMatchState, match_id)
    assert active_state is not None
    assert active_state.approval_deadline_at is not None
    active_state.approval_deadline_at = get_database_now(session) - timedelta(seconds=1)
    session.commit()
    return active_state


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
            DEFAULT_MATCH_FORMAT,
            DEFAULT_QUEUE_NAME,
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


def create_match_for_players(
    session: Session,
    session_factory: sessionmaker[Session],
    *,
    players: list[Player],
    channel_id: int,
    guild_id: int,
) -> tuple[int, list[MatchParticipant]]:
    queue_service = MatchingQueueService(session_factory)
    for player in players:
        queue_service.join_queue(
            player.id,
            DEFAULT_MATCH_FORMAT,
            DEFAULT_QUEUE_NAME,
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
    return created_matches[0].match_id, participants


def input_result_for_match_result(
    team: MatchParticipantTeam,
    final_result: MatchResult,
) -> MatchReportInputResult:
    if final_result == MatchResult.DRAW:
        return MatchReportInputResult.DRAW
    if final_result == MatchResult.VOID:
        return MatchReportInputResult.VOID
    if final_result == MatchResult.TEAM_A_WIN:
        return (
            MatchReportInputResult.WIN
            if team == MatchParticipantTeam.TEAM_A
            else MatchReportInputResult.LOSE
        )
    return (
        MatchReportInputResult.WIN
        if team == MatchParticipantTeam.TEAM_B
        else MatchReportInputResult.LOSE
    )


def finalize_match_with_result(
    session: Session,
    match_service: MatchFlowService,
    *,
    match_id: int,
    participants: list[MatchParticipant],
    final_result: MatchResult,
) -> None:
    match_service.volunteer_parent(match_id, participants[0].player_id)

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
        last_result = match_service.submit_report(
            match_id,
            participant.player_id,
            input_result_for_match_result(participant.team, final_result),
        )

    assert last_result is not None
    assert last_result.finalized is True


def get_match_events(
    session: Session,
    *,
    event_type: OutboxEventType,
    match_id: int,
) -> list[OutboxEvent]:
    return [
        event
        for event in session.scalars(
            select(OutboxEvent).where(OutboxEvent.event_type == event_type).order_by(OutboxEvent.id)
        ).all()
        if event.payload.get("match_id") == match_id
    ]


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
    match_id, players, participants = create_match(
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
    finalized_player_results = session.scalars(
        select(FinalizedMatchPlayerResult).where(FinalizedMatchPlayerResult.match_id == match_id)
    ).all()
    approval_events = get_match_events(
        session,
        event_type=OutboxEventType.MATCH_APPROVAL_REQUESTED,
        match_id=match_id,
    )
    finalized_events = get_match_events(
        session,
        event_type=OutboxEventType.MATCH_FINALIZED,
        match_id=match_id,
    )
    penalties = session.scalars(select(PlayerPenalty)).all()
    format_stats_by_player_id = get_player_format_stats_by_player_id(
        session, [player.id for player in players]
    )
    finalized_player_results_by_id = {
        player_result.player_id: player_result for player_result in finalized_player_results
    }
    expected_delta = 20.0 / 3.0

    assert last_result is not None
    assert last_result.finalized is True
    assert last_result.approval_started is False
    assert last_result.approval_deadline_at is None
    assert active_state is not None
    assert active_state.state == MatchState.FINALIZED
    assert finalized_result is not None
    assert finalized_result.final_result == MatchResult.TEAM_A_WIN
    assert finalized_result.rated_at == finalized_result.finalized_at
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
    finalized_event_payload = finalized_events[0].payload
    team_a_rating_entries = finalized_event_payload["team_a_rating_entries"]
    team_b_rating_entries = finalized_event_payload["team_b_rating_entries"]
    assert [entry["discord_user_id"] for entry in team_a_rating_entries] == [
        participant.notification_mention_discord_user_id
        for participant in participants
        if participant.team == MatchParticipantTeam.TEAM_A
    ]
    assert [entry["discord_user_id"] for entry in team_b_rating_entries] == [
        participant.notification_mention_discord_user_id
        for participant in participants
        if participant.team == MatchParticipantTeam.TEAM_B
    ]
    assert [entry["rating"] for entry in team_a_rating_entries] == pytest.approx(
        [INITIAL_RATING + expected_delta] * 3
    )
    assert [entry["rating"] for entry in team_b_rating_entries] == pytest.approx(
        [INITIAL_RATING - expected_delta] * 3
    )
    assert penalties == []
    for player in players:
        persisted_player = format_stats_by_player_id[player.id]
        finalized_player_result = finalized_player_results_by_id[player.id]
        assert persisted_player.games_played == 1
        assert finalized_player_result.rating_before == INITIAL_RATING
        assert finalized_player_result.games_played_before == 0
        assert finalized_player_result.wins_before == 0
        assert finalized_player_result.losses_before == 0
        assert finalized_player_result.draws_before == 0
        participant = next(
            participant for participant in participants if participant.player_id == player.id
        )
        if participant.team == MatchParticipantTeam.TEAM_A:
            assert persisted_player.rating == pytest.approx(INITIAL_RATING + expected_delta)
            assert persisted_player.wins == 1
            assert persisted_player.losses == 0
            assert persisted_player.draws == 0
        else:
            assert persisted_player.rating == pytest.approx(INITIAL_RATING - expected_delta)
            assert persisted_player.wins == 0
            assert persisted_player.losses == 1
            assert persisted_player.draws == 0


@pytest.mark.parametrize(
    ("match_format", "start_discord_user_id", "channel_id", "guild_id"),
    [
        (MatchFormat.ONE_VS_ONE, 60_175, 91_001_7, 92_001_7),
        (MatchFormat.TWO_VS_TWO, 60_176, 91_001_8, 92_001_8),
        (MatchFormat.THREE_VS_THREE, 60_177, 91_001_9, 92_001_9),
    ],
)
def test_submit_reports_updates_only_target_format_rating_state(
    session: Session,
    session_factory: sessionmaker[Session],
    match_format: MatchFormat,
    start_discord_user_id: int,
    channel_id: int,
    guild_id: int,
) -> None:
    players, participants_by_match_id = create_matches_for_format(
        session,
        session_factory,
        match_format=match_format,
        start_discord_user_id=start_discord_user_id,
        channel_id=channel_id,
        guild_id=guild_id,
    )
    match_service = MatchFlowService(session_factory)
    expected_updates_by_player_id = {}

    for match_id, participants in participants_by_match_id.items():
        expected_updates_by_player_id.update(
            calculate_rating_updates(
                build_initial_rating_snapshots(participants),
                MatchResult.TEAM_A_WIN,
            )
        )
        finalize_match_with_result(
            session,
            match_service,
            match_id=match_id,
            participants=participants,
            final_result=MatchResult.TEAM_A_WIN,
        )

    session.expire_all()
    player_ids = [player.id for player in players]
    for stats_format in MatchFormat:
        format_stats_by_player_id = get_player_format_stats_by_player_id(
            session,
            player_ids,
            match_format=stats_format,
        )
        for player in players:
            persisted_player = format_stats_by_player_id[player.id]
            if stats_format == match_format:
                expected_update = expected_updates_by_player_id[player.id]
                assert persisted_player.rating == pytest.approx(expected_update.rating_after)
                assert persisted_player.games_played == expected_update.games_played_after
                assert persisted_player.wins == expected_update.wins_after
                assert persisted_player.losses == expected_update.losses_after
                assert persisted_player.draws == expected_update.draws_after
            else:
                assert persisted_player.rating == pytest.approx(INITIAL_RATING)
                assert persisted_player.games_played == 0
                assert persisted_player.wins == 0
                assert persisted_player.losses == 0
                assert persisted_player.draws == 0


@pytest.mark.parametrize(
    ("match_format", "start_discord_user_id", "channel_id", "guild_id"),
    [
        (MatchFormat.ONE_VS_ONE, 60_180, 91_001_10, 92_001_10),
        (MatchFormat.TWO_VS_TWO, 60_181, 91_001_11, 92_001_11),
        (MatchFormat.THREE_VS_THREE, 60_182, 91_001_12, 92_001_12),
    ],
)
def test_volunteer_parent_assigns_parent_and_notifies_for_all_formats(
    session: Session,
    session_factory: sessionmaker[Session],
    match_format: MatchFormat,
    start_discord_user_id: int,
    channel_id: int,
    guild_id: int,
) -> None:
    match_id, participants = create_first_match_for_format(
        session,
        session_factory,
        match_format=match_format,
        start_discord_user_id=start_discord_user_id,
        channel_id=channel_id,
        guild_id=guild_id,
    )
    match_service = MatchFlowService(session_factory)
    parent = participants[0]

    assignment = match_service.volunteer_parent(match_id, parent.player_id)

    session.expire_all()
    active_state = session.get(ActiveMatchState, match_id)
    parent_events = get_match_events(
        session,
        event_type=OutboxEventType.MATCH_PARENT_ASSIGNED,
        match_id=match_id,
    )

    assert assignment.assigned is True
    assert assignment.parent_player_id == parent.player_id
    assert assignment.parent_decided_at is not None
    assert assignment.report_open_at is not None
    assert assignment.report_deadline_at is not None
    assert active_state is not None
    assert active_state.state == MatchState.WAITING_FOR_RESULT_REPORTS
    assert active_state.parent_player_id == parent.player_id
    assert active_state.parent_decided_at == assignment.parent_decided_at
    assert active_state.report_open_at == assignment.report_open_at
    assert active_state.report_deadline_at == assignment.report_deadline_at
    assert len(parent_events) == 1
    assert parent_events[0].payload["parent_discord_user_id"] == (
        parent.notification_mention_discord_user_id
    )


@pytest.mark.parametrize(
    ("match_format", "start_discord_user_id", "channel_id", "guild_id"),
    [
        (MatchFormat.ONE_VS_ONE, 60_190, 91_001_20, 92_001_20),
        (MatchFormat.TWO_VS_TWO, 60_191, 91_001_21, 92_001_21),
        (MatchFormat.THREE_VS_THREE, 60_192, 91_001_22, 92_001_22),
    ],
)
def test_process_parent_deadline_auto_assigns_parent_for_all_formats(
    session: Session,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    match_format: MatchFormat,
    start_discord_user_id: int,
    channel_id: int,
    guild_id: int,
) -> None:
    match_id, participants = create_first_match_for_format(
        session,
        session_factory,
        match_format=match_format,
        start_discord_user_id=start_discord_user_id,
        channel_id=channel_id,
        guild_id=guild_id,
    )
    match_service = MatchFlowService(session_factory)
    chosen_parent = participants[-1]
    monkeypatch.setattr(match_flow_module.random, "choice", lambda options: options[-1])
    set_parent_deadline_passed(session, match_id)

    assignment = match_service.process_parent_deadline(match_id)

    session.expire_all()
    active_state = session.get(ActiveMatchState, match_id)
    parent_events = get_match_events(
        session,
        event_type=OutboxEventType.MATCH_PARENT_ASSIGNED,
        match_id=match_id,
    )

    assert assignment.assigned is True
    assert assignment.parent_player_id == chosen_parent.player_id
    assert active_state is not None
    assert active_state.parent_player_id == chosen_parent.player_id
    assert active_state.parent_decided_at == assignment.parent_decided_at
    assert len(parent_events) == 1
    assert parent_events[0].payload["parent_discord_user_id"] == (
        chosen_parent.notification_mention_discord_user_id
    )


@pytest.mark.parametrize(
    ("match_format", "start_discord_user_id", "channel_id", "guild_id"),
    [
        (MatchFormat.ONE_VS_ONE, 60_200_1, 91_002_1, 92_002_1),
        (MatchFormat.TWO_VS_TWO, 60_200_2, 91_002_2, 92_002_2),
        (MatchFormat.THREE_VS_THREE, 60_200_3, 91_002_3, 92_002_3),
    ],
)
def test_submit_report_allows_void_before_report_open_and_overwrites_latest_report_for_all_formats(
    session: Session,
    session_factory: sessionmaker[Session],
    match_format: MatchFormat,
    start_discord_user_id: int,
    channel_id: int,
    guild_id: int,
) -> None:
    match_id, participants = create_first_match_for_format(
        session,
        session_factory,
        match_format=match_format,
        start_discord_user_id=start_discord_user_id,
        channel_id=channel_id,
        guild_id=guild_id,
    )
    match_service = MatchFlowService(session_factory)
    reporter = participants[0]

    match_service.volunteer_parent(match_id, reporter.player_id)
    session.expire_all()
    active_state = session.get(ActiveMatchState, match_id)
    assert active_state is not None
    now = get_database_now(session)
    active_state.report_open_at = now + timedelta(minutes=1)
    active_state.report_deadline_at = now + timedelta(minutes=10)
    session.commit()

    with pytest.raises(MatchReportNotOpenError):
        match_service.submit_report(
            match_id,
            reporter.player_id,
            MatchReportInputResult.WIN,
        )

    void_result = match_service.submit_report(
        match_id,
        reporter.player_id,
        MatchReportInputResult.VOID,
    )

    assert void_result.finalized is False
    assert void_result.approval_started is False

    set_report_window_open(session, match_id)

    first_report_result = match_service.submit_report(
        match_id,
        reporter.player_id,
        MatchReportInputResult.WIN,
    )
    overwritten_report_result = match_service.submit_report(
        match_id,
        reporter.player_id,
        MatchReportInputResult.DRAW,
    )

    session.expire_all()
    reports = session.scalars(
        select(MatchReport)
        .where(MatchReport.match_id == match_id, MatchReport.player_id == reporter.player_id)
        .order_by(MatchReport.id)
    ).all()

    assert first_report_result.finalized is False
    assert overwritten_report_result.finalized is False
    assert len(reports) == 3
    assert [report.is_latest for report in reports] == [False, False, True]
    assert reports[-1].reported_input_result == MatchReportInputResult.DRAW
    assert reports[-1].normalized_result == MatchResult.DRAW


@pytest.mark.parametrize(
    ("match_format", "start_discord_user_id", "channel_id", "guild_id"),
    [
        (MatchFormat.ONE_VS_ONE, 60_210, 91_002_10, 92_002_10),
        (MatchFormat.TWO_VS_TWO, 60_211, 91_002_11, 92_002_11),
        (MatchFormat.THREE_VS_THREE, 60_212, 91_002_12, 92_002_12),
    ],
)
def test_submit_reports_starts_approval_and_accepts_approval_for_all_formats(
    session: Session,
    session_factory: sessionmaker[Session],
    match_format: MatchFormat,
    start_discord_user_id: int,
    channel_id: int,
    guild_id: int,
) -> None:
    match_id, participants = create_first_match_for_format(
        session,
        session_factory,
        match_format=match_format,
        start_discord_user_id=start_discord_user_id,
        channel_id=channel_id,
        guild_id=guild_id,
    )
    match_service = MatchFlowService(session_factory)
    parent = participants[0]

    assignment = match_service.volunteer_parent(match_id, parent.player_id)
    set_report_window_open(session, match_id)

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
        last_result = match_service.submit_report(match_id, participant.player_id, input_result)

    session.expire_all()
    active_state = session.get(ActiveMatchState, match_id)
    player_states = session.scalars(
        select(ActiveMatchPlayerState).where(ActiveMatchPlayerState.match_id == match_id)
    ).all()
    approval_events = get_match_events(
        session,
        event_type=OutboxEventType.MATCH_APPROVAL_REQUESTED,
        match_id=match_id,
    )

    assert assignment.assigned is True
    assert last_result is not None
    assert last_result.approval_started is True
    assert last_result.approval_deadline_at is not None
    assert active_state is not None
    assert active_state.state == MatchState.AWAITING_RESULT_APPROVALS
    assert active_state.provisional_result == MatchResult.TEAM_A_WIN
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

    assert approval_result.approval_status == MatchApprovalStatus.APPROVED
    assert dissenting_state is not None
    assert dissenting_state.approval_status == MatchApprovalStatus.APPROVED
    assert dissenting_state.approved_at is not None


@pytest.mark.parametrize(
    ("match_format", "start_discord_user_id", "channel_id", "guild_id"),
    [
        (MatchFormat.ONE_VS_ONE, 60_220, 91_002_20, 92_002_20),
        (MatchFormat.TWO_VS_TWO, 60_221, 91_002_21, 92_002_21),
        (MatchFormat.THREE_VS_THREE, 60_222, 91_002_22, 92_002_22),
    ],
)
def test_no_reports_trigger_admin_review_and_no_report_penalties_for_all_formats(
    session: Session,
    session_factory: sessionmaker[Session],
    match_format: MatchFormat,
    start_discord_user_id: int,
    channel_id: int,
    guild_id: int,
) -> None:
    match_id, participants = create_first_match_for_format(
        session,
        session_factory,
        match_format=match_format,
        start_discord_user_id=start_discord_user_id,
        channel_id=channel_id,
        guild_id=guild_id,
    )
    admin_discord_user_ids = frozenset({9_001, 9_002})
    match_service = MatchFlowService(
        session_factory,
        admin_discord_user_ids=admin_discord_user_ids,
    )
    parent = participants[0]

    match_service.volunteer_parent(match_id, parent.player_id)
    set_report_deadline_passed(session, match_id)

    report_deadline_result = match_service.process_report_deadline(match_id)

    session.expire_all()
    active_state = session.get(ActiveMatchState, match_id)
    assert report_deadline_result.finalized is False
    assert report_deadline_result.final_result == MatchResult.VOID
    assert active_state is not None
    assert active_state.state == MatchState.AWAITING_RESULT_APPROVALS
    assert active_state.admin_review_required is True
    assert active_state.admin_review_reasons == ["low_report_count"]

    set_approval_deadline_passed(session, match_id)
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
    finalized_events = get_match_events(
        session,
        event_type=OutboxEventType.MATCH_FINALIZED,
        match_id=match_id,
    )
    admin_review_events = get_match_events(
        session,
        event_type=OutboxEventType.MATCH_ADMIN_REVIEW_REQUIRED,
        match_id=match_id,
    )
    penalties_by_key = {
        (penalty.player_id, penalty.penalty_type): penalty.count for penalty in penalties
    }

    assert approval_deadline_result.finalized is True
    assert approval_deadline_result.final_result == MatchResult.VOID
    assert finalized_result is not None
    assert finalized_result.final_result == MatchResult.VOID
    assert finalized_result.admin_review_required is True
    assert finalized_result.admin_review_reasons == ["low_report_count"]
    assert len(finalized_player_results) == len(participants)
    assert all(
        player_result.report_status == MatchReportStatus.NOT_REPORTED
        for player_result in finalized_player_results
    )
    assert all(
        player_result.auto_penalty_type == PenaltyType.NO_REPORT
        for player_result in finalized_player_results
    )
    assert penalties_by_key == {
        (participant.player_id, PenaltyType.NO_REPORT): 1 for participant in participants
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
            participant.player_id,
            PenaltyType.NO_REPORT,
            PenaltyAdjustmentSource.AUTO_MATCH_FINALIZATION,
            1,
        )
        for participant in participants
    }
    assert len(finalized_events) == 1 + len(participants)
    assert len(admin_review_events) == 1
    assert admin_review_events[0].payload["admin_review_reasons"] == ["low_report_count"]
    assert admin_review_events[0].payload["admin_discord_user_ids"] == sorted(
        admin_discord_user_ids
    )


@pytest.mark.parametrize(
    ("match_format", "start_discord_user_id", "channel_id", "guild_id"),
    [
        (MatchFormat.ONE_VS_ONE, 60_230, 91_002_30, 92_002_30),
        (MatchFormat.TWO_VS_TWO, 60_231, 91_002_31, 92_002_31),
        (MatchFormat.THREE_VS_THREE, 60_232, 91_002_32, 92_002_32),
    ],
)
def test_submit_reports_uses_parent_to_break_tie_for_all_formats(
    session: Session,
    session_factory: sessionmaker[Session],
    match_format: MatchFormat,
    start_discord_user_id: int,
    channel_id: int,
    guild_id: int,
) -> None:
    match_id, participants = create_first_match_for_format(
        session,
        session_factory,
        match_format=match_format,
        start_discord_user_id=start_discord_user_id,
        channel_id=channel_id,
        guild_id=guild_id,
    )
    match_service = MatchFlowService(session_factory)
    parent = participants[0]

    match_service.volunteer_parent(match_id, parent.player_id)
    set_report_window_open(session, match_id)

    last_result = None
    for participant in participants:
        input_result = (
            MatchReportInputResult.WIN
            if participant.team == MatchParticipantTeam.TEAM_A
            else MatchReportInputResult.DRAW
        )
        last_result = match_service.submit_report(match_id, participant.player_id, input_result)

    session.expire_all()
    active_state = session.get(ActiveMatchState, match_id)

    assert last_result is not None
    assert last_result.finalized is False
    assert last_result.approval_started is True
    assert active_state is not None
    assert active_state.provisional_result == MatchResult.TEAM_A_WIN
    assert active_state.admin_review_required is False
    assert active_state.admin_review_reasons == []


@pytest.mark.parametrize(
    ("match_format", "start_discord_user_id", "channel_id", "guild_id"),
    [
        (MatchFormat.TWO_VS_TWO, 60_240, 91_002_40, 92_002_40),
        (MatchFormat.THREE_VS_THREE, 60_241, 91_002_41, 92_002_41),
    ],
)
def test_process_deadlines_unresolved_tie_becomes_void_and_notifies_admin_for_team_formats(
    session: Session,
    session_factory: sessionmaker[Session],
    match_format: MatchFormat,
    start_discord_user_id: int,
    channel_id: int,
    guild_id: int,
) -> None:
    match_id, participants = create_first_match_for_format(
        session,
        session_factory,
        match_format=match_format,
        start_discord_user_id=start_discord_user_id,
        channel_id=channel_id,
        guild_id=guild_id,
    )
    match_service = MatchFlowService(
        session_factory,
        admin_discord_user_ids=frozenset({8_001}),
    )
    parent = participants[0]

    match_service.volunteer_parent(match_id, parent.player_id)
    set_report_window_open(session, match_id)

    team_a_others = [
        participant
        for participant in participants
        if participant.team == MatchParticipantTeam.TEAM_A
        and participant.player_id != parent.player_id
    ]
    team_b_players = [
        participant
        for participant in participants
        if participant.team == MatchParticipantTeam.TEAM_B
    ]

    if match_format == MatchFormat.TWO_VS_TWO:
        match_service.submit_report(
            match_id,
            team_a_others[0].player_id,
            MatchReportInputResult.WIN,
        )
        match_service.submit_report(
            match_id,
            team_b_players[0].player_id,
            MatchReportInputResult.WIN,
        )
    else:
        match_service.submit_report(
            match_id,
            team_a_others[0].player_id,
            MatchReportInputResult.WIN,
        )
        match_service.submit_report(
            match_id,
            team_b_players[0].player_id,
            MatchReportInputResult.WIN,
        )
        match_service.submit_report(
            match_id,
            team_a_others[1].player_id,
            MatchReportInputResult.DRAW,
        )

    set_report_deadline_passed(session, match_id)
    report_deadline_result = match_service.process_report_deadline(match_id)

    session.expire_all()
    active_state = session.get(ActiveMatchState, match_id)
    assert report_deadline_result.finalized is False
    assert report_deadline_result.final_result == MatchResult.VOID
    assert active_state is not None
    assert active_state.provisional_result == MatchResult.VOID
    assert active_state.admin_review_required is True
    assert active_state.admin_review_reasons == ["unresolved_tie"]

    set_approval_deadline_passed(session, match_id)
    approval_deadline_result = match_service.process_approval_deadline(match_id)

    session.expire_all()
    finalized_result = session.get(FinalizedMatchResult, match_id)
    admin_review_events = get_match_events(
        session,
        event_type=OutboxEventType.MATCH_ADMIN_REVIEW_REQUIRED,
        match_id=match_id,
    )

    assert approval_deadline_result.finalized is True
    assert approval_deadline_result.final_result == MatchResult.VOID
    assert finalized_result is not None
    assert finalized_result.final_result == MatchResult.VOID
    assert finalized_result.admin_review_reasons == ["unresolved_tie"]
    assert len(admin_review_events) == 1
    assert admin_review_events[0].payload["admin_review_reasons"] == ["unresolved_tie"]


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
    approval_events = get_match_events(
        session,
        event_type=OutboxEventType.MATCH_APPROVAL_REQUESTED,
        match_id=match_id,
    )

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
    finalized_events = get_match_events(
        session,
        event_type=OutboxEventType.MATCH_FINALIZED,
        match_id=match_id,
    )
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


def test_submit_draw_reports_updates_record_without_changing_equal_ratings(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    match_id, players, participants = create_match(
        session,
        session_factory,
        start_discord_user_id=60_350,
        channel_id=91_003_5,
        guild_id=92_003_5,
    )
    match_service = MatchFlowService(session_factory)

    match_service.volunteer_parent(match_id, participants[0].player_id)

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
        last_result = match_service.submit_report(
            match_id,
            participant.player_id,
            MatchReportInputResult.DRAW,
        )

    session.expire_all()
    finalized_result = session.get(FinalizedMatchResult, match_id)
    format_stats_by_player_id = get_player_format_stats_by_player_id(
        session, [player.id for player in players]
    )
    finalized_player_results = {
        player_result.player_id: player_result
        for player_result in session.scalars(
            select(FinalizedMatchPlayerResult).where(
                FinalizedMatchPlayerResult.match_id == match_id
            )
        ).all()
    }

    assert last_result is not None
    assert last_result.finalized is True
    assert finalized_result is not None
    assert finalized_result.final_result == MatchResult.DRAW
    assert finalized_result.rated_at == finalized_result.finalized_at
    for player in players:
        persisted_player = format_stats_by_player_id[player.id]
        finalized_player_result = finalized_player_results[player.id]
        assert persisted_player.rating == pytest.approx(INITIAL_RATING)
        assert persisted_player.games_played == 1
        assert persisted_player.wins == 0
        assert persisted_player.losses == 0
        assert persisted_player.draws == 1
        assert finalized_player_result.rating_before == INITIAL_RATING
        assert finalized_player_result.games_played_before == 0
        assert finalized_player_result.wins_before == 0
        assert finalized_player_result.losses_before == 0
        assert finalized_player_result.draws_before == 0


def test_submit_void_reports_does_not_change_player_rating_state(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    match_id, players, participants = create_match(
        session,
        session_factory,
        start_discord_user_id=60_360,
        channel_id=91_003_6,
        guild_id=92_003_6,
    )
    match_service = MatchFlowService(session_factory)

    match_service.volunteer_parent(match_id, participants[0].player_id)

    last_result = None
    for participant in participants:
        last_result = match_service.submit_report(
            match_id,
            participant.player_id,
            MatchReportInputResult.VOID,
        )

    session.expire_all()
    finalized_result = session.get(FinalizedMatchResult, match_id)
    format_stats_by_player_id = get_player_format_stats_by_player_id(
        session, [player.id for player in players]
    )
    finalized_player_results = {
        player_result.player_id: player_result
        for player_result in session.scalars(
            select(FinalizedMatchPlayerResult).where(
                FinalizedMatchPlayerResult.match_id == match_id
            )
        ).all()
    }

    assert last_result is not None
    assert last_result.finalized is True
    assert finalized_result is not None
    assert finalized_result.final_result == MatchResult.VOID
    assert finalized_result.rated_at == finalized_result.finalized_at
    for player in players:
        persisted_player = format_stats_by_player_id[player.id]
        finalized_player_result = finalized_player_results[player.id]
        assert persisted_player.rating == pytest.approx(INITIAL_RATING)
        assert persisted_player.games_played == 0
        assert persisted_player.wins == 0
        assert persisted_player.losses == 0
        assert persisted_player.draws == 0
        assert finalized_player_result.rating_before == INITIAL_RATING
        assert finalized_player_result.games_played_before == 0
        assert finalized_player_result.wins_before == 0
        assert finalized_player_result.losses_before == 0
        assert finalized_player_result.draws_before == 0


def test_override_match_result_rejects_non_finalized_match(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    match_id, _, _ = create_match(
        session,
        session_factory,
        start_discord_user_id=60_365,
        channel_id=91_003_7,
        guild_id=92_003_7,
    )
    match_service = MatchFlowService(session_factory)

    with pytest.raises(MatchNotFinalizedError):
        match_service.override_match_result(
            match_id,
            MatchResult.TEAM_A_WIN,
            admin_discord_user_id=99_001,
        )


def test_override_match_result_recalculates_rating_state_for_corrected_and_following_matches(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    players = create_players(session, 6, start_discord_user_id=60_370)
    match_service = MatchFlowService(session_factory)

    first_match_id, first_participants = create_match_for_players(
        session,
        session_factory,
        players=players,
        channel_id=91_003_8,
        guild_id=92_003_8,
    )
    finalize_match_with_result(
        session,
        match_service,
        match_id=first_match_id,
        participants=first_participants,
        final_result=MatchResult.TEAM_A_WIN,
    )

    second_match_id, second_participants = create_match_for_players(
        session,
        session_factory,
        players=players,
        channel_id=91_003_9,
        guild_id=92_003_9,
    )
    finalize_match_with_result(
        session,
        match_service,
        match_id=second_match_id,
        participants=second_participants,
        final_result=MatchResult.TEAM_A_WIN,
    )

    session.expire_all()
    original_first_finalized = session.get(FinalizedMatchResult, first_match_id)
    assert original_first_finalized is not None
    original_first_finalized_at = original_first_finalized.finalized_at

    override_result = match_service.override_match_result(
        first_match_id,
        MatchResult.TEAM_B_WIN,
        admin_discord_user_id=99_002,
    )

    session.expire_all()
    first_finalized_result = session.get(FinalizedMatchResult, first_match_id)
    second_finalized_result = session.get(FinalizedMatchResult, second_match_id)
    format_stats_by_player_id = get_player_format_stats_by_player_id(
        session, [player.id for player in players]
    )
    first_player_results = {
        result.player_id: result
        for result in session.scalars(
            select(FinalizedMatchPlayerResult).where(
                FinalizedMatchPlayerResult.match_id == first_match_id
            )
        ).all()
    }
    second_player_results = {
        result.player_id: result
        for result in session.scalars(
            select(FinalizedMatchPlayerResult).where(
                FinalizedMatchPlayerResult.match_id == second_match_id
            )
        ).all()
    }

    initial_snapshots = tuple(
        RatingParticipantSnapshot(
            player_id=participant.player_id,
            team=participant.team,
            rating=INITIAL_RATING,
            games_played=0,
            wins=0,
            losses=0,
            draws=0,
        )
        for participant in first_participants
    )
    first_match_updates = calculate_rating_updates(initial_snapshots, MatchResult.TEAM_B_WIN)
    second_match_snapshots = tuple(
        RatingParticipantSnapshot(
            player_id=participant.player_id,
            team=participant.team,
            rating=first_match_updates[participant.player_id].rating_after,
            games_played=first_match_updates[participant.player_id].games_played_after,
            wins=first_match_updates[participant.player_id].wins_after,
            losses=first_match_updates[participant.player_id].losses_after,
            draws=first_match_updates[participant.player_id].draws_after,
        )
        for participant in second_participants
    )
    second_match_snapshots_by_player_id = {
        snapshot.player_id: snapshot for snapshot in second_match_snapshots
    }
    second_match_updates = calculate_rating_updates(
        second_match_snapshots,
        MatchResult.TEAM_A_WIN,
    )

    assert override_result.match_id == first_match_id
    assert override_result.final_result == MatchResult.TEAM_B_WIN
    assert first_finalized_result is not None
    assert first_finalized_result.final_result == MatchResult.TEAM_B_WIN
    assert first_finalized_result.finalized_by_admin is True
    assert first_finalized_result.finalized_at >= original_first_finalized_at
    assert second_finalized_result is not None
    assert second_finalized_result.final_result == MatchResult.TEAM_A_WIN
    assert second_finalized_result.finalized_by_admin is False

    for player in players:
        first_player_result = first_player_results[player.id]
        second_player_result = second_player_results[player.id]
        second_snapshot = second_match_snapshots_by_player_id[player.id]
        second_update = second_match_updates[player.id]
        persisted_player = format_stats_by_player_id[player.id]

        assert first_player_result.rating_before == INITIAL_RATING
        assert first_player_result.games_played_before == 0
        assert first_player_result.wins_before == 0
        assert first_player_result.losses_before == 0
        assert first_player_result.draws_before == 0

        assert second_player_result.rating_before == pytest.approx(second_snapshot.rating)
        assert second_player_result.games_played_before == second_snapshot.games_played
        assert second_player_result.wins_before == second_snapshot.wins
        assert second_player_result.losses_before == second_snapshot.losses
        assert second_player_result.draws_before == second_snapshot.draws

        assert persisted_player.rating == pytest.approx(second_update.rating_after)
        assert persisted_player.games_played == second_update.games_played_after
        assert persisted_player.wins == second_update.wins_after
        assert persisted_player.losses == second_update.losses_after
        assert persisted_player.draws == second_update.draws_after
