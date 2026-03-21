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
- `target_rating`

### `queue_class_id`

DB に保存するための安定した内部識別子である。

方針:

- `queue_class_id` は全フォーマットを通して一意とする
- 既存値は後から変更しない

推奨命名例:

- `1v1_open_low`
- `1v1_open_high`
- `2v2_open_low`
- `2v2_open_high`
- `3v3_open_low`
- `3v3_open_high`

### `queue_name`

ユーザーが `/join` で指定する階級名である。

同じ `queue_name` を複数フォーマットで再利用してよい。  
ただし `/join` では `match_format` と組み合わせて解決する。

## 初期構成

### 初期状態の階級構成

初期状態では、中間階級を設けない。  
各フォーマットについて、以下の 2 階級のみを持つ。

| `match_format` | 論理順序 | `queue_class_id` | `queue_name` | 説明 |
| --- | --- | --- | --- | --- |
| `1v1` | low 側 | `1v1_open_low` | `low` | 1v1 レート下限無制限キュー |
| `1v1` | high 側 | `1v1_open_high` | `high` | 1v1 レート上限無制限キュー |
| `2v2` | low 側 | `2v2_open_low` | `low` | 2v2 レート下限無制限キュー |
| `2v2` | high 側 | `2v2_open_high` | `high` | 2v2 レート上限無制限キュー |
| `3v3` | low 側 | `3v3_open_low` | `low` | 3v3 レート下限無制限キュー |
| `3v3` | high 側 | `3v3_open_high` | `high` | 3v3 レート上限無制限キュー |

### 初期状態の参加可能条件

初期状態では中間階級が存在しないため、各フォーマットの登録済みプレイヤーは、そのフォーマットの `low` と `high` のどちらにも参加できる。

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
                queue_class_id="1v1_open_low",
                queue_name="low",
                description="1v1 レート下限無制限キュー",
                target_rating=None,
            ),
            MatchQueueClassDefinition(
                queue_class_id="1v1_open_high",
                queue_name="high",
                description="1v1 レート上限無制限キュー",
                target_rating=None,
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

### `/join` の入力

`/join` は以下を引数に取る。

- `match_format`
- `queue_name`

Bot はこの 2 つを組み合わせて `queue_class_id` に解決し、`match_queue_entries` に保存する。

### `present` と `leave`

`present` と `leave` は現在参加中の `waiting` 行へ暗黙適用する。  
フォーマットや階級名の入力は不要とする。

## 将来の中間階級追加

### 追加方針

各フォーマットについて、人口増加に応じて中間階級を独立に追加できる。

例:

- `1v1` だけ 3 階級化する
- `2v2` は 2 階級のまま据え置く
- `3v3` は 4 階級化する

### `target_rating` の順序

あるフォーマットの階級定義が low 側から high 側へ `C_1, C_2, ..., C_n` と並ぶとき、対応する `target_rating` は厳密増加とする。

```text
R_1 < R_2 < ... < R_n
```

### 参加可能条件

参加条件は、対象フォーマットのレート `r` を用いて、そのフォーマット内でのみ判定する。

半開区間ルール:

- low 側端の階級: `r < R_2`
- high 側端の階級: `R_(n-1) <= r`
- 中間階級 `i` (`1 < i < n`): `R_(i-1) <= r < R_(i+1)`

補足:

- 下側境界は含み、上側境界は含まない
- 一度 `join` に成功した後は、その後にレートが変化しても待機中は再判定しない
