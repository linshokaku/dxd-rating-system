from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from bot.models import (
    FinalizedMatchResult,
    Match,
    MatchParticipant,
    MatchParticipantApprovalStatus,
    MatchParticipantReportStatus,
    MatchParticipantTeam,
    MatchQueueEntry,
    MatchQueueEntryStatus,
    MatchReportInput,
    MatchResultType,
    MatchState,
    OutboxEvent,
    OutboxEventType,
    Player,
    PlayerPenalty,
    PlayerPenaltyType,
)
from bot.services import (
    MATCH_QUEUE_TTL,
    REPORT_DEADLINE_DELAY,
    REPORT_OPEN_DELAY,
    MatchService,
    register_player,
)


@dataclass(frozen=True)
class FirstChoiceRandom:
    def choice(self, sequence: list[MatchParticipant]) -> MatchParticipant:
        return sequence[0]


def get_database_now(session: Session) -> datetime:
    return session.execute(select(func.now())).scalar_one()


def create_player(session: Session, discord_user_id: int) -> Player:
    player = register_player(session=session, discord_user_id=discord_user_id)
    session.commit()
    return player


def create_players(
    session: Session,
    *,
    count: int,
    start_discord_user_id: int,
) -> list[Player]:
    return [create_player(session, start_discord_user_id + index) for index in range(count)]


def create_match(
    session: Session,
    players: list[Player],
    *,
    created_at: datetime | None = None,
) -> Match:
    match_created_at = created_at or get_database_now(session)
    match = Match(
        created_at=match_created_at,
        state=MatchState.WAITING_FOR_PARENT,
        admin_review_required=False,
    )
    session.add(match)
    session.flush()

    for index, player in enumerate(players):
        queue_entry = MatchQueueEntry(
            player_id=player.id,
            status=MatchQueueEntryStatus.MATCHED,
            joined_at=match_created_at,
            last_present_at=match_created_at,
            expire_at=match_created_at + MATCH_QUEUE_TTL,
            revision=1,
            notification_channel_id=10_000 + index,
            notification_guild_id=20_000 + index,
            notification_mention_discord_user_id=player.discord_user_id,
            notification_recorded_at=match_created_at,
        )
        session.add(queue_entry)
        session.flush()

        participant = MatchParticipant(
            match_id=match.id,
            player_id=player.id,
            queue_entry_id=queue_entry.id,
            team=(
                MatchParticipantTeam.TEAM_A
                if index < 3
                else MatchParticipantTeam.TEAM_B
            ),
            slot=(index % 3) + 1,
            created_at=match_created_at,
        )
        session.add(participant)

    session.commit()
    return match


def open_report_window(session: Session, match_id: int) -> None:
    match = session.get(Match, match_id)
    assert match is not None
    current_time = get_database_now(session)
    match.report_open_at = current_time - timedelta(seconds=1)
    match.report_deadline_at = current_time + REPORT_DEADLINE_DELAY
    session.commit()


def get_penalty_count(
    session: Session,
    *,
    player_id: int,
    penalty_type: PlayerPenaltyType,
) -> int:
    session.expire_all()
    penalty = session.scalar(
        select(PlayerPenalty).where(
            PlayerPenalty.player_id == player_id,
            PlayerPenalty.penalty_type == penalty_type,
        )
    )
    return 0 if penalty is None else penalty.count


def test_volunteer_parent_decides_parent_and_sets_report_timers(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    players = create_players(session, count=6, start_discord_user_id=70_100)
    match = create_match(session, players)
    service = MatchService(session_factory)

    result = service.volunteer_parent(match.id, players[0].id)

    session.expire_all()
    refreshed_match = session.get(Match, match.id)
    outbox_events = session.scalars(select(OutboxEvent).order_by(OutboxEvent.id)).all()

    assert refreshed_match is not None
    assert result.parent_player_id == players[0].id
    assert refreshed_match.parent_player_id == players[0].id
    assert refreshed_match.state == MatchState.WAITING_FOR_RESULT_REPORTS
    assert refreshed_match.parent_decided_at is not None
    assert refreshed_match.report_open_at == refreshed_match.parent_decided_at + REPORT_OPEN_DELAY
    assert refreshed_match.report_deadline_at == (
        refreshed_match.parent_decided_at + REPORT_DEADLINE_DELAY
    )
    assert [event.event_type for event in outbox_events] == [OutboxEventType.MATCH_CREATED]
    assert [event.payload.get("notification_kind") for event in outbox_events] == [
        "match_parent_decided"
    ]


def test_six_reports_start_approval_and_parent_breaks_tie(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    players = create_players(session, count=6, start_discord_user_id=70_200)
    match = create_match(session, players)
    service = MatchService(session_factory)
    service.volunteer_parent(match.id, players[0].id)
    open_report_window(session, match.id)

    service.submit_report(match.id, players[0].id, MatchReportInput.WIN)
    service.submit_report(match.id, players[1].id, MatchReportInput.WIN)
    service.submit_report(match.id, players[2].id, MatchReportInput.LOSS)
    service.submit_report(match.id, players[3].id, MatchReportInput.WIN)
    service.submit_report(match.id, players[4].id, MatchReportInput.WIN)
    final_submission = service.submit_report(match.id, players[5].id, MatchReportInput.LOSS)

    session.expire_all()
    refreshed_match = session.get(Match, match.id)
    participants = session.scalars(
        select(MatchParticipant)
        .where(MatchParticipant.match_id == match.id)
        .order_by(MatchParticipant.id)
    ).all()
    participants_by_player_id = {participant.player_id: participant for participant in participants}
    outbox_events = session.scalars(select(OutboxEvent).order_by(OutboxEvent.id)).all()

    assert final_submission.approval_started is True
    assert refreshed_match is not None
    assert refreshed_match.state == MatchState.AWAITING_RESULT_APPROVALS
    assert refreshed_match.provisional_result == MatchResultType.TEAM_A_WIN
    assert refreshed_match.approval_started_at is not None
    assert (
        participants_by_player_id[players[0].id].approval_status
        == MatchParticipantApprovalStatus.NOT_REQUIRED
    )
    assert (
        participants_by_player_id[players[2].id].approval_status
        == MatchParticipantApprovalStatus.PENDING
    )
    assert (
        participants_by_player_id[players[2].id].report_status
        == MatchParticipantReportStatus.INCORRECT
    )
    assert [event.event_type for event in outbox_events] == [
        OutboxEventType.MATCH_CREATED,
        OutboxEventType.MATCH_CREATED,
    ]
    assert [event.payload.get("notification_kind") for event in outbox_events] == [
        "match_parent_decided",
        "match_approval_started",
    ]


def test_finalize_applies_auto_penalties_and_excludes_approved_players(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    players = create_players(session, count=6, start_discord_user_id=70_300)
    match = create_match(session, players)
    service = MatchService(session_factory)
    service.volunteer_parent(match.id, players[0].id)
    open_report_window(session, match.id)

    submissions = [
        (players[0].id, MatchReportInput.WIN),
        (players[1].id, MatchReportInput.WIN),
        (players[2].id, MatchReportInput.LOSS),
        (players[3].id, MatchReportInput.WIN),
        (players[4].id, MatchReportInput.WIN),
        (players[5].id, MatchReportInput.LOSS),
    ]
    for player_id, input_result in submissions:
        service.submit_report(match.id, player_id, input_result)

    service.approve_provisional_result(match.id, players[2].id)

    match_row = session.get(Match, match.id)
    assert match_row is not None
    match_row.approval_deadline_at = get_database_now(session) - timedelta(seconds=1)
    session.commit()

    reconcile_result = service.run_reconcile_cycle()

    session.expire_all()
    refreshed_match = session.get(Match, match.id)
    finalized_result = session.get(FinalizedMatchResult, match.id)
    participants = session.scalars(
        select(MatchParticipant)
        .where(MatchParticipant.match_id == match.id)
        .order_by(MatchParticipant.id)
    ).all()
    participants_by_player_id = {participant.player_id: participant for participant in participants}

    assert reconcile_result.finalized_match_ids == (match.id,)
    assert refreshed_match is not None
    assert refreshed_match.state == MatchState.FINALIZED
    assert finalized_result is not None
    assert finalized_result.final_result == MatchResultType.TEAM_A_WIN
    assert (
        participants_by_player_id[players[2].id].approval_status
        == MatchParticipantApprovalStatus.APPROVED
    )
    assert (
        participants_by_player_id[players[3].id].approval_status
        == MatchParticipantApprovalStatus.NOT_APPROVED
    )
    assert get_penalty_count(
        session,
        player_id=players[2].id,
        penalty_type=PlayerPenaltyType.INCORRECT_REPORT,
    ) == 0
    assert get_penalty_count(
        session,
        player_id=players[3].id,
        penalty_type=PlayerPenaltyType.INCORRECT_REPORT,
    ) == 1
    assert get_penalty_count(
        session,
        player_id=players[4].id,
        penalty_type=PlayerPenaltyType.INCORRECT_REPORT,
    ) == 1


def test_reconcile_auto_assigns_parent_after_deadline(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    players = create_players(session, count=6, start_discord_user_id=70_400)
    created_at = get_database_now(session) - timedelta(minutes=6)
    match = create_match(session, players, created_at=created_at)
    service = MatchService(session_factory, rng=FirstChoiceRandom())  # type: ignore[arg-type]

    result = service.run_reconcile_cycle()

    session.expire_all()
    refreshed_match = session.get(Match, match.id)

    assert result.auto_parent_match_ids == (match.id,)
    assert refreshed_match is not None
    assert refreshed_match.parent_player_id == players[0].id
    assert refreshed_match.state == MatchState.WAITING_FOR_RESULT_REPORTS


def test_override_final_result_recalculates_automatic_penalties(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    players = create_players(session, count=6, start_discord_user_id=70_500)
    match = create_match(session, players)
    service = MatchService(session_factory)
    service.volunteer_parent(match.id, players[0].id)
    open_report_window(session, match.id)

    submissions = [
        (players[0].id, MatchReportInput.WIN),
        (players[1].id, MatchReportInput.WIN),
        (players[2].id, MatchReportInput.LOSS),
        (players[3].id, MatchReportInput.WIN),
        (players[4].id, MatchReportInput.WIN),
        (players[5].id, MatchReportInput.LOSS),
    ]
    for player_id, input_result in submissions:
        service.submit_report(match.id, player_id, input_result)

    match_row = session.get(Match, match.id)
    assert match_row is not None
    match_row.approval_deadline_at = get_database_now(session) - timedelta(seconds=1)
    session.commit()
    service.run_reconcile_cycle()

    service.override_final_result(match.id, MatchResultType.TEAM_B_WIN)

    assert get_penalty_count(
        session,
        player_id=players[3].id,
        penalty_type=PlayerPenaltyType.INCORRECT_REPORT,
    ) == 0
    assert get_penalty_count(
        session,
        player_id=players[4].id,
        penalty_type=PlayerPenaltyType.INCORRECT_REPORT,
    ) == 0
    assert get_penalty_count(
        session,
        player_id=players[0].id,
        penalty_type=PlayerPenaltyType.INCORRECT_REPORT,
    ) == 1
    assert get_penalty_count(
        session,
        player_id=players[1].id,
        penalty_type=PlayerPenaltyType.INCORRECT_REPORT,
    ) == 1
    assert get_penalty_count(
        session,
        player_id=players[5].id,
        penalty_type=PlayerPenaltyType.INCORRECT_REPORT,
    ) == 1
