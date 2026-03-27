# マッチング階級仕様

## 目的

フォーマットごとのキュー階級定義と参加条件を定める。

## 用語

### 階級

特定フォーマットの実力帯を表す定義単位である。

各階級は少なくとも以下を持つ。

- `match_format`
- `queue_class_id`
- `queue_name`
- `description`
- `minimum_rating`
- `maximum_rating`

### `queue_class_id`

DB に保存するための安定した内部識別子である。

方針:

- `queue_class_id` は全フォーマットを通して一意とする
- 既存値は後から変更しない

推奨命名例:

- `1v1_open_beginner`
- `1v1_open_regular`
- `1v1_open_master`
- `2v2_open_beginner`
- `2v2_open_regular`
- `2v2_open_master`
- `3v3_open_beginner`
- `3v3_open_regular`
- `3v3_open_master`

### `queue_name`

ユーザーが `/join` またはマッチングチャンネル UI で指定する階級名である。

同じ `queue_name` を複数フォーマットで再利用してよい。  
ただし参加時は `match_format` と組み合わせて解決する。

### レート境界

- `minimum_rating` は下限であり、境界値を含む
- `maximum_rating` は上限であり、境界値を含まない
- `None` はその方向に無制限であることを表す
- 階級同士のレート範囲は重複してよい

## 初期構成

### 初期状態の階級構成

初期状態では、各フォーマットについて以下の 3 階級を持つ。

| `match_format` | `queue_class_id` | `queue_name` | 説明 | 参加可能レート |
| --- | --- | --- | --- | --- |
| `1v1` | `1v1_open_beginner` | `beginner` | 1v1 レート 1600 未満向けキュー | `r < 1600` |
| `1v1` | `1v1_open_regular` | `regular` | 1v1 全レート参加可能キュー | 無制限 |
| `1v1` | `1v1_open_master` | `master` | 1v1 レート 1600 以上向けキュー | `1600 <= r` |
| `2v2` | `2v2_open_beginner` | `beginner` | 2v2 レート 1600 未満向けキュー | `r < 1600` |
| `2v2` | `2v2_open_regular` | `regular` | 2v2 全レート参加可能キュー | 無制限 |
| `2v2` | `2v2_open_master` | `master` | 2v2 レート 1600 以上向けキュー | `1600 <= r` |
| `3v3` | `3v3_open_beginner` | `beginner` | 3v3 レート 1600 未満向けキュー | `r < 1600` |
| `3v3` | `3v3_open_regular` | `regular` | 3v3 全レート参加可能キュー | 無制限 |
| `3v3` | `3v3_open_master` | `master` | 3v3 レート 1600 以上向けキュー | `1600 <= r` |

### 初期状態の参加可能条件

各フォーマットの登録済みプレイヤーは、そのフォーマットの現在レート `r` に応じて以下へ参加できる。

- `r < 1600` のとき
  - `beginner`
  - `regular`
- `r >= 1600` のとき
  - `regular`
  - `master`

ただし、同時参加できるキューは全体で 1 つのみとする。

## 実装上の設定例

```python
MATCH_FORMAT_DEFINITIONS = (
    MatchFormatDefinition(
        match_format="1v1",
        team_size=1,
        batch_size=2,
        queue_classes=(
            MatchQueueClassDefinition(
                queue_class_id="1v1_open_beginner",
                queue_name="beginner",
                description="1v1 レート 1600 未満向けキュー",
                maximum_rating=1600,
            ),
            MatchQueueClassDefinition(
                queue_class_id="1v1_open_regular",
                queue_name="regular",
                description="1v1 全レート参加可能キュー",
            ),
            MatchQueueClassDefinition(
                queue_class_id="1v1_open_master",
                queue_name="master",
                description="1v1 レート 1600 以上向けキュー",
                minimum_rating=1600,
            ),
        ),
    ),
    MatchFormatDefinition(
        match_format="2v2",
        team_size=2,
        batch_size=1,
        queue_classes=(...),
    ),
    MatchFormatDefinition(
        match_format="3v3",
        team_size=3,
        batch_size=1,
        queue_classes=(...),
    ),
)
```

## キュー参加ルール

### キュー参加時の入力

`/join` と [../ui/matchmaking_channel.md](../ui/matchmaking_channel.md) の参加 UI は以下を入力として扱う。

- `match_format`
- `queue_name`

Bot はこの 2 つを組み合わせて `queue_class_id` に解決し、`match_queue_entries` に保存する。

### `present` と `leave`

`present` と `leave` は現在参加中の `waiting` 行へ暗黙適用する。  
フォーマットや階級名の入力は不要とする。

## 将来の階級追加・変更

### 追加方針

各フォーマットについて、人口増加に応じて階級構成を独立に変更できる。

例:

- `1v1` だけ 4 階級化する
- `2v2` は 3 階級のまま据え置く
- `3v3` は `regular` を廃止して上下 2 階級に戻す

### 参加可能条件

参加条件は、対象フォーマットのレート `r` を用いて、そのフォーマット内でのみ判定する。

ここで使う `r` は、`join` 時点で稼働中のシーズンに属する `player_format_stats.rating` とする。

判定ルール:

- `minimum_rating` が設定されている場合は `minimum_rating <= r` を満たす必要がある
- `maximum_rating` が設定されている場合は `r < maximum_rating` を満たす必要がある
- 両方が未設定なら、その階級は全レート参加可能である

補足:

- 一度 `join` に成功した後は、その後にレートが変化しても待機中は再判定しない
- 階級同士の範囲は重複してよい
