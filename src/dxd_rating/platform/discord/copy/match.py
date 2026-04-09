from __future__ import annotations

from collections.abc import Sequence

from dxd_rating.platform.db.models import MatchResult, PenaltyType

# 試合告知チャンネルと試合スレッドの UI 本文
MATCHMAKING_NEWS_CHANNEL_MESSAGE = "\n".join(
    [
        "このチャンネルにはマッチ成立時のアナウンスが投稿されます。",
        "観戦ボタンもこのチャンネルのアナウンスメッセージに表示されます。",
    ]
)
MATCH_OPERATION_THREAD_VOID_GUIDE_MESSAGE = (
    "無効試合とする必要がある場合は下の「無効試合申請」ボタンを押してください。"
)
MATCH_OPERATION_THREAD_VOID_COMMAND_GUIDE_MESSAGE = (
    "無効試合とする必要がある場合は /match_void を使ってください。"
)
MATCHMAKING_NEWS_MATCH_ANNOUNCEMENT_SPECTATE_GUIDE_MESSAGE = (
    "観戦希望者は下の「観戦する」ボタンから応募してください。"
)

# 試合まわりのボタン文言
MATCH_OPERATION_THREAD_WIN_BUTTON_LABEL = "勝ち"
MATCH_OPERATION_THREAD_DRAW_BUTTON_LABEL = "引き分け"
MATCH_OPERATION_THREAD_LOSE_BUTTON_LABEL = "負け"
MATCH_OPERATION_THREAD_VOID_BUTTON_LABEL = "無効試合申請"
MATCH_OPERATION_THREAD_PARENT_BUTTON_LABEL = "親に立候補する"
MATCH_OPERATION_THREAD_APPROVE_BUTTON_LABEL = "承認"
MATCH_OPERATION_THREAD_FALLBACK_ERROR_MESSAGE = "試合操作に失敗しました。管理者に確認してください。"
MATCHMAKING_NEWS_MATCH_ANNOUNCEMENT_SPECTATE_BUTTON_LABEL = "観戦する"
MATCHMAKING_NEWS_MATCH_ANNOUNCEMENT_FALLBACK_ERROR_MESSAGE = (
    "観戦応募に失敗しました。管理者に確認してください。"
)

# slash command の説明文言
MATCH_COMMAND_MATCH_ID_DESCRIPTION = "対象の match_id"
MATCH_PARENT_COMMAND_DESCRIPTION = "試合の親に立候補します"
MATCH_SPECTATE_COMMAND_DESCRIPTION = "試合の観戦応募を行います"
MATCH_WIN_COMMAND_DESCRIPTION = "自分視点で勝ちを報告します"
MATCH_LOSE_COMMAND_DESCRIPTION = "自分視点で負けを報告します"
MATCH_DRAW_COMMAND_DESCRIPTION = "引き分けを報告します"
MATCH_VOID_COMMAND_DESCRIPTION = "無効試合を報告します"
MATCH_APPROVE_COMMAND_DESCRIPTION = "仮決定結果を承認します"

# 試合操作に対する応答文言
MATCH_PARENT_SUCCESS_MESSAGE = "親に立候補しました。"
MATCH_NOT_FOUND_MESSAGE = "指定した試合が見つかりません。"
MATCH_NOT_FINALIZED_MESSAGE = "この試合はまだ結果確定していません。"
MATCH_PARTICIPANT_REQUIRED_MESSAGE = "この試合の参加者ではありません。"
MATCH_PARENT_ALREADY_ASSIGNED_MESSAGE = "この試合の親はすでに決まっています。"
MATCH_PARENT_RECRUITMENT_CLOSED_MESSAGE = "親募集期間は終了しています。"
MATCH_SPECTATE_RESTRICTED_MESSAGE = "現在観戦を制限されています。"
MATCH_SPECTATING_CLOSED_MESSAGE = "この試合は観戦受付を終了しています。"
MATCH_PARTICIPANT_CANNOT_SPECTATE_MESSAGE = "この試合の参加者は観戦応募できません。"
MATCH_SPECTATOR_ALREADY_REGISTERED_MESSAGE = "すでにこの試合へ観戦応募済みです。"
MATCH_SPECTATOR_CAPACITY_MESSAGE = "この試合の観戦枠は埋まっています。"
MATCH_SPECTATE_FAILED_MESSAGE = "観戦応募に失敗しました。管理者に確認してください。"
MATCH_REPORT_SUCCESS_MESSAGE = "勝敗報告を受け付けました。"
MATCH_REPORT_APPROVAL_IN_PROGRESS_MESSAGE = "承認期間中は勝敗報告を変更できません。"
MATCH_REPORT_CLOSED_MESSAGE = "この試合の勝敗報告は締め切られています。"
MATCH_REPORT_NOT_OPEN_MESSAGE = "まだ勝敗報告を受け付けていません。"
MATCH_APPROVE_SUCCESS_MESSAGE = "仮決定結果を承認しました。"
MATCH_APPROVAL_NOT_AVAILABLE_MESSAGE = "この試合は承認期間中ではありません。"
MATCH_APPROVAL_NOT_REQUIRED_MESSAGE = "この試合では承認は不要です。"
MATCH_ALREADY_FINALIZED_MESSAGE = "この試合はすでに結果確定済みです。"
MATCH_ACTION_FAILED_MESSAGE = "試合操作に失敗しました。管理者に確認してください。"

# 試合進行の通知文言と表示ラベル
MATCH_CREATED_NOTIFICATION_MESSAGE = "マッチ成立です。"
MATCH_PARENT_ASSIGNED_NOTIFICATION_MESSAGE = "親が決定しました。"
MATCH_REPORT_OPENED_NOTIFICATION_MESSAGE = "試合が終わったら参加者全員試合結果を報告してください。"
MATCH_APPROVAL_STARTED_NOTIFICATION_MESSAGE = "承認フェーズに移行しました。"
MATCH_APPROVAL_REQUESTED_NOTIFICATION_MESSAGE = "仮決定結果の承認が必要です。"
MATCH_FINALIZED_NOTIFICATION_MESSAGE = "試合結果が確定しました。"
MATCH_AUTO_PENALTY_APPLIED_NOTIFICATION_MESSAGE = "自動ペナルティが付与されました。"
MATCH_ADMIN_REVIEW_REQUIRED_NOTIFICATION_MESSAGE = "admin による確認が必要です。"
MATCH_RESULT_LABELS = {
    MatchResult.TEAM_A_WIN: "チーム A の勝ち",
    MatchResult.TEAM_B_WIN: "チーム B の勝ち",
    MatchResult.DRAW: "引き分け",
    MatchResult.VOID: "無効試合",
}
PENALTY_TYPE_LABELS = {
    PenaltyType.INCORRECT_REPORT: "誤報告",
    PenaltyType.NO_REPORT: "未報告",
    PenaltyType.ROOM_SETUP_DELAY: "部屋立て遅延",
    PenaltyType.MATCH_MISTAKE: "試合進行ミス",
    PenaltyType.LATE: "遅刻",
    PenaltyType.DISCONNECT: "切断",
}
_MATCH_RESULT_VALUE_LABELS = {result.value: label for result, label in MATCH_RESULT_LABELS.items()}
_PENALTY_TYPE_VALUE_LABELS = {penalty_type.value: label for penalty_type, label in PENALTY_TYPE_LABELS.items()}
_ADMIN_REVIEW_REASON_LABELS = {
    "low_report_count": "勝敗報告を行ったプレイヤーが 2 人以下です",
    "single_team_reports": "勝敗報告が片方のチームに偏っています",
    "unresolved_tie": "同票が解消できませんでした",
}

# 試合文言の組み立て関数
def get_match_result_label(value: MatchResult | str) -> str:
    if isinstance(value, MatchResult):
        return MATCH_RESULT_LABELS.get(value, value.value)
    return _MATCH_RESULT_VALUE_LABELS.get(value, value)


def get_penalty_type_label(value: PenaltyType | str) -> str:
    if isinstance(value, PenaltyType):
        return PENALTY_TYPE_LABELS.get(value, value.value)
    return _PENALTY_TYPE_VALUE_LABELS.get(value, value)


def get_admin_review_reason_label(value: str) -> str:
    return _ADMIN_REVIEW_REASON_LABELS.get(value, value)


def build_match_operation_thread_name(match_id: int) -> str:
    return f"試合-{match_id}"


def build_match_spectate_success_message(
    active_spectator_count: int,
    max_spectators: int,
) -> str:
    return (
        "観戦応募を受け付けました。"
        f"現在 {active_spectator_count} / {max_spectators} 人です。"
    )


def build_match_operation_thread_initial_content(
    *,
    match_format: str,
    queue_name: str,
    team_a_labels: Sequence[str],
    team_b_labels: Sequence[str],
    with_void_button: bool,
) -> str:
    lines = [
        MATCH_CREATED_NOTIFICATION_MESSAGE,
        f"試合形式: {match_format}",
        f"試合階級: {queue_name}",
        "Team A",
        *[f"    {label}" for label in team_a_labels],
        "Team B",
        *[f"    {label}" for label in team_b_labels],
    ]
    if with_void_button:
        lines.append(MATCH_OPERATION_THREAD_VOID_GUIDE_MESSAGE)
    else:
        lines.append(MATCH_OPERATION_THREAD_VOID_COMMAND_GUIDE_MESSAGE)
    return "\n".join(lines)


def build_match_operation_thread_parent_recruitment_content() -> str:
    return "\n".join(
        [
            "まず初めに、部屋立てと試合の進行を行う親を募集します。",
            "親募集期間は5分です。",
            "5分以内に立候補がない場合は Bot が参加メンバーからランダムに決定します。",
        ]
    )


def build_match_operation_thread_self_introduction_content() -> str:
    return "試合参加者はゲーム内のプレイヤー名を報告してください。"


def build_match_created_content(
    *,
    team_a_labels: Sequence[str],
    team_b_labels: Sequence[str],
    match_format: str | None = None,
    queue_name: str | None = None,
    include_spectate_guide: bool = False,
) -> str:
    lines = [MATCH_CREATED_NOTIFICATION_MESSAGE]
    if match_format is not None:
        lines.append(f"試合形式: {match_format}")
    if queue_name is not None:
        lines.append(f"試合階級: {queue_name}")
    lines.extend(
        [
            "Team A",
            *[f"    {label}" for label in team_a_labels],
            "Team B",
            *[f"    {label}" for label in team_b_labels],
        ]
    )
    if include_spectate_guide:
        lines.append(MATCHMAKING_NEWS_MATCH_ANNOUNCEMENT_SPECTATE_GUIDE_MESSAGE)
    return "\n".join(lines)


def build_match_operation_thread_routing_message(thread_mention: str) -> str:
    return f"試合運営は {thread_mention} で行ってください。"


def build_match_parent_assigned_content(
    parent_mention: str,
    report_open_at: str,
    report_deadline_at: str,
) -> str:
    return "\n".join(
        [
            MATCH_PARENT_ASSIGNED_NOTIFICATION_MESSAGE,
            f"親: {parent_mention}",
            f"勝敗報告開始: {report_open_at}",
            f"勝敗報告締切: {report_deadline_at}",
        ]
    )


def build_match_report_opened_content(report_deadline_at: str) -> str:
    return "\n".join(
        [
            MATCH_REPORT_OPENED_NOTIFICATION_MESSAGE,
            "自分視点で「勝ち」「引き分け」「負け」を選んでください。",
            "無効試合にすべき場合は「無効試合申請」を押してください。",
            f"勝敗報告締切: {report_deadline_at}",
        ]
    )


def build_match_approval_started_content(
    provisional_result_label: str,
    approval_deadline_at: str,
) -> str:
    return "\n".join(
        [
            MATCH_APPROVAL_STARTED_NOTIFICATION_MESSAGE,
            f"仮決定結果: {provisional_result_label}",
            f"承認締切: {approval_deadline_at}",
        ]
    )


def build_match_approval_requested_content(
    mention_text: str,
    provisional_result_label: str,
    approval_deadline_at: str,
) -> str:
    return "\n".join(
        [
            f"{mention_text} {MATCH_APPROVAL_REQUESTED_NOTIFICATION_MESSAGE}",
            f"仮決定結果: {provisional_result_label}",
            f"承認締切: {approval_deadline_at}",
            "承認できない場合は証拠を提示したうえで admin へ連絡してください。",
        ]
    )


def build_match_finalized_auto_penalty_content(
    mention_text: str,
    final_result_label: str,
    penalty_label: str,
    penalty_count: int,
) -> str:
    return "\n".join(
        [
            f"{mention_text} {MATCH_AUTO_PENALTY_APPLIED_NOTIFICATION_MESSAGE}",
            f"結果: {final_result_label}",
            f"ペナルティ: {penalty_label}",
            f"現在の累積: {penalty_count}",
        ]
    )


def build_match_finalized_content(
    final_result_label: str,
    *,
    finalized_by_admin: bool,
    rating_lines: Sequence[str],
) -> str:
    lines = [
        MATCH_FINALIZED_NOTIFICATION_MESSAGE,
        f"結果: {final_result_label}",
    ]
    if finalized_by_admin:
        lines.append("admin により結果が確定または更新されました。")
    elif rating_lines:
        lines.extend(["更新後レート", *rating_lines])
    return "\n".join(lines)


def build_match_admin_review_required_content(
    mention_prefix: str,
    final_result_label: str,
    reason_labels: Sequence[str],
) -> str:
    body = [
        MATCH_ADMIN_REVIEW_REQUIRED_NOTIFICATION_MESSAGE,
        f"結果: {final_result_label}",
    ]
    if reason_labels:
        body.append("理由: " + ", ".join(reason_labels))
    if mention_prefix:
        return "\n".join([mention_prefix, *body])
    return "\n".join(body)

