# StaticNerveMap

English: [README_en.md](README_en.md)

## 言語方針
StaticNerveMap の運用では、**対話は日本語、AI エージェントが継承する作業文脈は英語/ASCII** を基本方針にします。

人間同士、または Codex との設計相談では日本語を使います。違和感、判断、方針転換を素早く共有するためです。

一方で、生成 YAML や AI エージェントが次回以降に読む作業文脈は、英語/ASCII に寄せます。Windows + PowerShell + Codex の経路では UTF-8 日本語が表示上 mojibake することがあり、また後続エージェントに渡す構造化文脈としては簡潔な英語のほうが安定するためです。

実運用上の目安:

- チャットでの相談: 日本語
- 人間向け入口: `docs/README.md`
- dogfood / AI agent handoff の入口: `docs/README_en.md`
- active agent-facing docs: `OpenIssues.md`, `MVPDefinition.md`, `docs/reference/ImplementationRoadmap.md` は簡潔な英語寄せ
- generated YAML: `.staticnervemap/**/*.yaml` は英語/ASCII を優先
- dogfood notes: 人間レビュー用なら日本語でもよいが、再投入するなら英語版が望ましい

英語は凝った文章にせず、機械参照しやすい短い文を優先します。

## これは何か
StaticNerveMap は、**AI エージェントが既存コードを改造するときの初動判断を助ける YAML** を生成する静的解析ツールです。

主な利用場面は次です。

> ユーザが AI エージェントに既存リポジトリの改造を依頼する。エージェントは編集に入る前に StaticNerveMap を実行し、どこを読むか、どこを触るか、どこを再確認すべきかの静的な足場を作る。

StaticNerveMap は汎用コードブラウザでも、エージェント判断の代替でもありません。最初の数手を安全にするための bootstrap map です。

主目的は「コードを完全理解すること」ではありません。AI エージェントが次を素早く判断できるようにすることです。

- どこから読むべきか
- どこを最初に触るべきか
- どこに一次影響が出そうか
- どの relation が確実で、どこが unresolved か

現在の主対象は **Python-first なリポジトリ** です。

AI エージェント向けには、特に次の問いに答えることを狙います。

- runtime core らしいファイルはどれか
- CLI、route、script、UI、framework hook などの entry surface はどこか
- entry から core への modification path が見えるか
- 編集前に再確認すべき unresolved はどれか
- 今回の改造目的に合う reading mode はどれか

## 出力できるもの
単発解析では、現在次のような情報を YAML に出力できます。

- `files`
- `symbols`
- `relations`
- `entrypoints`
- `clusters`
- `change_targets`
- `modification_paths`
- `impact_rules`
- `api_contracts`
- `unresolved`
- `notes`

地層化した運用では、次も出力できます。

- `snapshot` YAML
- `index.yaml`

## インストール
開発中の editable install:

```powershell
pip install -e .
```

macOS / Linux でも同じです。

```bash
pip install -e .
```

インストールせず、このリポジトリから直接動かす場合:

Windows PowerShell:

```powershell
$env:PYTHONPATH="src"
python -m staticnervemap <repo>
```

macOS / Linux:

```bash
PYTHONPATH=src python -m staticnervemap <repo>
```

## CLI
現在の主なコマンド:

```powershell
staticnervemap analyze <repo> [--out <path>] [--project-name <name>] [--scan-mode full|default|focused]
staticnervemap snapshot create <repo> --snapshot-id <id> [--roadmap-ref <ref>] [--no-overwrite] [--scan-mode full|default|focused]
staticnervemap snapshot suggest-id <repo> [--roadmap-ref <ref>] [--stage pre|post|baseline]
staticnervemap index rebuild <repo-or-.staticnervemap-or-snapshot-dir> [--out <path>]
staticnervemap --version
```

後方互換の省略形:

```powershell
staticnervemap <repo>
```

これは `analyze` と同じ挙動です。

## 基本的な使い方
StaticNerveMap には自然な使い方が 2 パターンあります。

### パターン1. 対象リポジトリの外から実行する
StaticNerveMap 自身を開発しているときや、複数リポジトリをまとめて試すときに便利です。

```powershell
staticnervemap analyze C:\work\my-app --scan-mode default
staticnervemap snapshot create C:\work\my-app --snapshot-id M09-post-001 --scan-mode default
staticnervemap index rebuild C:\work\my-app
```

macOS / Linux:

```bash
staticnervemap analyze /work/my-app --scan-mode default
staticnervemap snapshot create /work/my-app --snapshot-id M09-post-001 --scan-mode default
staticnervemap index rebuild /work/my-app
```

この場合、解析対象は `<repo>` に渡したパスです。出力先は対象リポジトリ配下になります。

既定の出力先:

- `analyze` -> `<repo>/.staticnervemap/work/out.yaml`
- `snapshot create` -> `<repo>/.staticnervemap/snapshots/<snapshot_id>.yaml`
- `index rebuild` -> `<repo>/.staticnervemap/index.yaml`

### パターン2. 対象リポジトリの中で実行する
実運用ではこちらが自然です。

```powershell
cd C:\work\my-app
staticnervemap analyze . --scan-mode default
staticnervemap snapshot create . --snapshot-id M09-post-001 --scan-mode default
staticnervemap index rebuild .
```

macOS / Linux:

```bash
cd /work/my-app
staticnervemap analyze . --scan-mode default
staticnervemap snapshot create . --snapshot-id M09-post-001 --scan-mode default
staticnervemap index rebuild .
```

この場合、`.` がリポジトリ root として扱われ、出力もそのリポジトリ内に作られます。

対象リポジトリに次がある場合は、特にこの形が扱いやすくなります。

- `docs/ImplementationRoadmap.md`
- milestone 指向の docs
- 通常の project-local な `docs/` ディレクトリ

`--roadmap-ref` は、解析対象リポジトリ内の roadmap を参照できると最も素直に使えます。

### 単発解析

```powershell
staticnervemap analyze Voice-Design-Cloner --out out-vdc.yaml --scan-mode default
```

現在の構造を 1 枚の YAML として見たいときに使います。

### 地層化 snapshot

```powershell
staticnervemap snapshot suggest-id Voice-Design-Cloner --roadmap-ref docs/ImplementationRoadmap.md#task-9-1
staticnervemap snapshot create Voice-Design-Cloner --snapshot-id M09-post-001 --roadmap-ref docs/ImplementationRoadmap.md#task-9-1 --scan-mode default
staticnervemap index rebuild Voice-Design-Cloner
```

milestone と結びついた履歴を残したいときに使います。

`index rebuild` は次のどれも受け付けます。

- `.staticnervemap/snapshots` を持つ対象リポジトリ root
- `.staticnervemap` ディレクトリ
- `.staticnervemap/snapshots` ディレクトリ
- 旧 `static-nervemap` / `static-nervemap/snapshots` も互換入力として受け付ける

対象 snapshot file がすでに存在する場合、`snapshot create` は warning を出したうえで上書きします。
append-only にしたい場合、特に wrapper や AgentCLI 的な harness から使う場合は `--no-overwrite` を付けます。

### 実運用 / dogfood ループ
StaticNerveMap を併用しながら改修する場合は、次の流れを推奨します。

1. roadmap / OpenIssues から今回触る task を決める
2. `snapshot suggest-id` で作業前 snapshot ID を確認する
3. `snapshot create` で `pre` snapshot を残す
4. `analyze` で軽量な確認 YAML を出す
5. YAML の `change_targets` / `modification_paths` / `unresolved` を見て、読む順番と触る場所を決める
6. 実装とテストを行う
7. roadmap / OpenIssues に結果を反映する
8. `post` snapshot を作る
9. `index rebuild` で `.staticnervemap/index.yaml` を更新する

例:

```powershell
staticnervemap snapshot suggest-id . --roadmap-ref docs/reference/ImplementationRoadmap.md#task-14-8 --stage pre
staticnervemap snapshot create . --snapshot-id M14-pre-004 --roadmap-ref docs/reference/ImplementationRoadmap.md#task-14-8 --scan-mode focused --no-overwrite
staticnervemap analyze . --out .staticnervemap\work\phase14-8-check.yaml --scan-mode focused

# implement and test

staticnervemap snapshot create . --snapshot-id M14-post-005 --roadmap-ref docs/reference/ImplementationRoadmap.md#task-14-8 --scan-mode focused --no-overwrite
staticnervemap index rebuild .
```

macOS / Linux:

```bash
staticnervemap snapshot suggest-id . --roadmap-ref docs/reference/ImplementationRoadmap.md#task-14-8 --stage pre
staticnervemap snapshot create . --snapshot-id M14-pre-004 --roadmap-ref docs/reference/ImplementationRoadmap.md#task-14-8 --scan-mode focused --no-overwrite
staticnervemap analyze . --out .staticnervemap/work/phase14-8-check.yaml --scan-mode focused

# implement and test

staticnervemap snapshot create . --snapshot-id M14-post-005 --roadmap-ref docs/reference/ImplementationRoadmap.md#task-14-8 --scan-mode focused --no-overwrite
staticnervemap index rebuild .
```

このリポジトリの現在の実ファイル例:

- work YAML: `.staticnervemap/work/mvp-closeout-check.yaml`
- medium-large profiling example: `.staticnervemap/work/phase14-7-erpnext.yaml`
- latest snapshot: `.staticnervemap/snapshots/M15-post-003.yaml`
- current index: `.staticnervemap/index.yaml`

運用上の目安:

- `pre` snapshot は「改修前の地層」として残す
- `.staticnervemap/work/*-check.yaml` は作業中に見る一時解析として使う
- `post` snapshot はテストと docs 更新後に残す
- `--no-overwrite` は、snapshot 履歴を append-only に保ちたいときに使う
- generated YAML / index / agent handoff に入る文言は英語/ASCII を優先する

index の参照ルール:

- `latest_snapshot_id`: 最新に生成された snapshot を指す。これは自動判定で固定し、品質判定を挟まない
- `latest_stable_snapshot_id`: `stable: true` の最新 snapshot を指す。存在しなければ `null`
- `baseline_snapshot_id`: その履歴列の基底 snapshot を指す。`latest_stable_snapshot_id` の代用にはしない

運用上の使い分け:

- 直近の作業追跡には `latest_snapshot_id` を使う
- agent handoff や再利用しやすい参照先には `latest_stable_snapshot_id` を使う
- `latest_stable_snapshot_id = null` の間は、まだ handoff-ready ではないとみなす

### Recommended Agent Workflow
AI エージェントが StaticNerveMap を使って改造に入る場合は、次の流れを推奨します。

1. **Initialize / Update Map**: `staticnervemap index rebuild <repo>` で `.staticnervemap/index.yaml` を最新化する
2. **Find Reading Mode**: `.staticnervemap/index.yaml` または最新 snapshot の `snapshot.summary.reading_modes` を見る
3. **Choose Context**: 改造目的に合う mode の `recommended_reading_order` を読む
4. **Follow Path**: `modification_paths` がある場合は entry -> core の導線をたどる
5. **Act**: 対象ファイルを読み、変更し、テストする
6. **Capture Result**: `snapshot create` と `index rebuild` で変更後の地層を残す

現時点では `analyze --mode reading` のような専用CLIはありません。`reading_modes` は snapshot / index YAML の summary に含まれる補助情報として使います。

mode の選び方:

- `general`: 目的がまだ曖昧なとき、または repo-wide に最初の読み筋を決めたいとき
- `library_core`: ライブラリ本体、runtime、domain core、model/core logic を改造したいとき
- `entry_surface`: CLI、route、UI、script、framework entrypoint、外部から入る配線を改造したいとき

判断の目安:

- pure library repo では `general` と `library_core` が同じになることがある。これは自然な結果であり、無理に別 mode を探さない
- app / CLI / web / ML tool repo では `entry_surface` と `library_core` が分かれることが多い
- `entry_surface` が出ていない repo は、StaticNerveMap が意味のある入口を見つけていないか、そもそも library-first な repo と考える
- `modification_paths` がある場合は、mode の読み順よりも具体的な entry -> core 導線を優先してよい

### scan mode
- `full`: 最も広く走査する
- `default`: 固定除外と補助除外を使う
- `focused`: 大きめのリポジトリ向け。root file、entry/config file、primary package を優先する

推奨:

- 小〜中規模 repo: `default`
- 大きめの repo: `focused`

## 出力ディレクトリ
`snapshot create` を既定設定で使うと、対象リポジトリに次を作ります。

```text
.staticnervemap/
  index.yaml
  work/
    out.yaml
  snapshots/
    <snapshot_id>.yaml
  deltas/
```

`deltas/` は予約領域です。delta YAML の実装はまだ後続フェーズです。

## Direct CLI と AgentCLI Harness の分担
StaticNerveMap は意図的に **中立 CLI** として設計しています。

StaticNerveMap は構造に関する問いに答え、構造記憶を書き出します。ただし、すべての運用ポリシーを StaticNerveMap 自身が決めるわけではありません。たとえば直接利用では、既存 snapshot ID の上書きを許容します。その場合は warning を出しますが、コマンド自体は止めません。

これは単体ツールとしての探索性を保つためです。

- scan mode を調整しながら同じ snapshot を作り直す
- 壊れたローカル snapshot を置き換える
- milestone 履歴として固定する前に試す
- より大きな harness に入れず StaticNerveMap 単体で使う

一方で AgentCLI 的な使い方では、AgentCLI は StaticNerveMap の上に乗る **運用制約レイヤ** です。

AgentCLI は append-only snapshot、milestone discipline、validation order などを強制し、リポジトリ運用をより安全で再現可能にします。StaticNerveMap は primitive command を提供し、AgentCLI がより厳しい手順を選びます。

AgentCLI 向けの推奨 snapshot flow:

```powershell
staticnervemap snapshot suggest-id . --roadmap-ref docs/ImplementationRoadmap.md#task-13-3 --stage post
staticnervemap snapshot create . --snapshot-id M13-post-001 --roadmap-ref docs/ImplementationRoadmap.md#task-13-3 --no-overwrite
staticnervemap index rebuild .
```

分担は次のとおりです。

- StaticNerveMap: 中立な structural map CLI
- AgentCLI: policy、workflow、safety wrapper
- `.staticnervemap/`: 両者が共有する structural memory layer

直接利用では warning 付き overwrite を許容します。
AgentCLI 利用では `--no-overwrite` を付け、snapshot history を append-only として扱います。

## snapshot 命名
推奨形式:

```text
<prefix>-<stage>-<nnn>
```

例:

- `M07-post-001`
- `M09-pre-002`
- `GEN-post-001`

ルール:

- 末尾番号は既存 snapshot file から数える
- prefix は可能なら roadmap milestone から取る
- milestone を推定できない場合は `GEN` に落とす

## StaticNerveMap に向いた roadmap の書き方
StaticNerveMap は、roadmap が機械参照しやすいほど扱いやすくなります。

推奨する最小項目:

- phase header
- `milestone_id`
- `roadmap_ref`
- task header
- `task_id`
- `status`
- `priority`

推奨パターン:

```md
## フェーズ9: Snapshot Metadata と Index Meaning の強化
milestone_id: M09
milestone_title_en: Snapshot metadata and index meaning
roadmap_ref: docs/ImplementationRoadmap.md#phase-9
status: in_progress

### 9-1. snapshot metadata の強化
task_id: 9-1
roadmap_ref: docs/ImplementationRoadmap.md#task-9-1
priority: high
status: in_progress
```

この形にしておくと、StaticNerveMap が次を推定しやすくなります。

- `milestone_id`
- `milestone_title`
- `milestone_title_en`
- `snapshot_id` の prefix 候補
- roadmap task への安定参照

`status` の正規値は `planned / in_progress / done / paused / dropped` です。
`priority` の正規値は `critical / high / medium / low` です。

詳細な記法は [RoadmapAuthoringDictionary.md](reference/RoadmapAuthoringDictionary.md) にあります。

## 最初に読むもの
このリポジトリに初めて入る場合は、次の順がおすすめです。

1. [CurrentStateSummary.md](reference/CurrentStateSummary.md)
2. [ImplementationRoadmap.md](reference/ImplementationRoadmap.md)
3. [OpenIssues.md](OpenIssues.md)
4. [ImplementationTests.md](reference/ImplementationTests.md)
5. [MVPDefinition.md](MVPDefinition.md)

schema と地層化:

- [YamlSchemaDraft.md](reference/YamlSchemaDraft.md)
- [SnapShotSchemaDraft.yaml](reference/SnapShotSchemaDraft.yaml)
- [indexSchemaDraft.yaml](reference/indexSchemaDraft.yaml)
- [SnapShotDraft.yaml](reference/SnapShotDraft.yaml)
- [indexDraft.yaml](reference/indexDraft.yaml)

最適化と大規模 repo での知見:

- [LargeRepoFindings.md](reference/LargeRepoFindings.md)
- [PostprocessOptimizationPlan.md](reference/PostprocessOptimizationPlan.md)

## Active Docs
現在の top-level docs は次の 4 つです。

- [README.md](README.md)
- [README_en.md](README_en.md)
- [OpenIssues.md](OpenIssues.md)
- [MVPDefinition.md](MVPDefinition.md)

Active reference docs は `docs/reference/` にあります。

履歴化済み・完了済みの notes は `docs/old/` に移しています。
