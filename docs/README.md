# Docs Overview

このディレクトリでは、仕様を以下のレイヤに分けて管理する。

- [formats.md](formats.md)
  - 対応フォーマット、独立管理の範囲、基本データモデル方針
- [seasons.md](seasons.md)
  - シーズン期間、事前作成、carryover、シーズン別レート管理
- [players/identity.md](players/identity.md)
  - プレイヤー識別子と Bot が保持する表示名キャッシュ
- [players/access_restrictions.md](players/access_restrictions.md)
  - admin によるプレイヤー利用制限
- [matching/common.md](matching/common.md)
  - キュー参加、在席更新、退出、期限切れ、共通排他制御
- [matching/queue_classes.md](matching/queue_classes.md)
  - フォーマットごとの階級定義と参加条件
- [matching/1v1.md](matching/1v1.md)
  - 1v1 のマッチングバッチ構築
- [matching/2v2.md](matching/2v2.md)
  - 2v2 のチーム分けとマッチ構築
- [matching/3v3.md](matching/3v3.md)
  - 3v3 のチーム分けとマッチ構築
- [matches/common.md](matches/common.md)
  - マッチ進行、勝敗報告、承認、ペナルティの共通仕様
- [matches/1v1.md](matches/1v1.md)
  - 1v1 マッチ進行時の読み替え
- [matches/2v2.md](matches/2v2.md)
  - 2v2 マッチ進行時の読み替え
- [matches/3v3.md](matches/3v3.md)
  - 3v3 マッチ進行時の読み替え
- [matches/spectating.md](matches/spectating.md)
  - 観戦応募と観戦枠管理
- [matches/result_correction.md](matches/result_correction.md)
  - 確定済みマッチ結果の修正とレーティング再計算
- [rating/common.md](rating/common.md)
  - レーティング共通方針、保持値、K 設計
- [rating/1v1.md](rating/1v1.md)
  - 1v1 レート計算
- [rating/2v2.md](rating/2v2.md)
  - 2v2 レート計算
- [rating/3v3.md](rating/3v3.md)
  - 3v3 レート計算
- [leaderboard/ranking.md](leaderboard/ranking.md)
  - ランキング表示と順位変化量の仕様
- [leaderboard/snapshots.md](leaderboard/snapshots.md)
  - 日次ランキング snapshot の生成、保持、運用方針
- [outbox.md](outbox.md)
  - 非同期通知配送
- [commands/user-commands.md](commands/user-commands.md)
  - ユーザー向け slash command。プレイヤー操作コマンドとマッチ操作コマンドを含む
- [commands/dev-commands.md](commands/dev-commands.md)
  - 開発者向け slash command
- [discord_permissions.md](discord_permissions.md)
  - Discord Bot の OAuth scope、Gateway Intents、必要権限
- [ui/common.md](ui/common.md)
  - Discord UI の共通仕様
- [ui/registered_channels.md](ui/registered_channels.md)
  - 登録済みユーザーが利用できるチャンネル構成と権限
- [ui/admin_operations_channel.md](ui/admin_operations_channel.md)
  - super admin 専用の運用チャンネルと worker 通知
- [ui/register.md](ui/register.md)
  - 公開チャンネルに設置する登録 UI
- [ui/matchmaking_channel.md](ui/matchmaking_channel.md)
  - `レート戦マッチング` チャンネルに設置するキュー参加 UI
- [ui/matchmaking_presence_thread.md](ui/matchmaking_presence_thread.md)
  - キュー参加後に作成する在席確認 private thread UI
- [ui/match_operation_thread.md](ui/match_operation_thread.md)
  - マッチ成立後に作成するマッチ運営 private thread UI
- [ui/info_channel.md](ui/info_channel.md)
  - `レート戦情報` チャンネルに設置する公開 button UI
- [ui/info_thread.md](ui/info_thread.md)
  - 情報確認用 private thread UI
- [ui/matchmaking_news_match_announcement.md](ui/matchmaking_news_match_announcement.md)
  - `レート戦マッチ速報` に投稿するマッチ成立アナウンス UI
- [ui/setup_channel.md](ui/setup_channel.md)
  - UI 設置チャンネルの作成・撤収コマンド

読み方の推奨順は以下とする。

1. [formats.md](formats.md)
2. [seasons.md](seasons.md)
3. [players/identity.md](players/identity.md)
4. [players/access_restrictions.md](players/access_restrictions.md)
5. [matching/common.md](matching/common.md)
6. [matching/queue_classes.md](matching/queue_classes.md)
7. 必要なフォーマット別仕様
8. [matches/common.md](matches/common.md)
9. [rating/common.md](rating/common.md)
10. [leaderboard/ranking.md](leaderboard/ranking.md)
11. [leaderboard/snapshots.md](leaderboard/snapshots.md)
12. [commands/user-commands.md](commands/user-commands.md)
13. [discord_permissions.md](discord_permissions.md)
14. [ui/common.md](ui/common.md)
15. [ui/registered_channels.md](ui/registered_channels.md)
16. [ui/admin_operations_channel.md](ui/admin_operations_channel.md)
17. [ui/setup_channel.md](ui/setup_channel.md)
18. [ui/register.md](ui/register.md)
19. [ui/matchmaking_channel.md](ui/matchmaking_channel.md)
20. [ui/matchmaking_presence_thread.md](ui/matchmaking_presence_thread.md)
21. [ui/match_operation_thread.md](ui/match_operation_thread.md)
22. [ui/info_channel.md](ui/info_channel.md)
23. [ui/info_thread.md](ui/info_thread.md)
24. [ui/matchmaking_news_match_announcement.md](ui/matchmaking_news_match_announcement.md)
