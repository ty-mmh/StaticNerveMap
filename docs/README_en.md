# StaticNerveMap

Japanese: [README.md](README.md)

## Language Policy
StaticNerveMap uses this practical language split:

- Conversation and design discussion with the project owner may be Japanese.
- Generated YAML should prefer English/ASCII.
- Active agent-facing docs such as `OpenIssues.md`, `MVPDefinition.md`, and `docs/reference/ImplementationRoadmap.md` should prefer concise English.
- Human-facing docs may be Japanese when that makes collaboration easier.
- Dogfood / AI-agent handoff should usually start from this `README_en.md`.

The goal is not polished English. The goal is stable, machine-readable working context that survives Windows, PowerShell, and Codex output paths without mojibake.

## What It Is
StaticNerveMap is a static-analysis tool that produces YAML for **AI-agent code modification bootstrap**.

Its primary use case is:

> A user asks an AI agent to modify an existing repository. Before editing, the agent runs StaticNerveMap to build a static footing: what to read first, what to touch first, and what needs re-checking.

StaticNerveMap is therefore not a general-purpose code browser and not a replacement for agent judgment. It is a bootstrap map for the first few safe modification decisions.

Its main job is not full code comprehension. Its main job is to help an agent quickly decide:

- where to start reading
- what to touch first
- what may be impacted first
- what is certain vs unresolved

The current main target is **Python-first repositories**.

Typical agent-facing questions:

- Which files are likely runtime core?
- Which files are entry surfaces such as CLI, routes, scripts, UI, or framework hooks?
- Is there a visible entry-to-core modification path?
- Which unresolved calls are worth re-checking before editing?
- Which reading mode should the agent use for this task?

## What It Can Output
The current single-snapshot analysis can emit:

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

For layered usage, it can also emit:

- `snapshot` YAML
- `index.yaml`

## Installation
Editable install:

```powershell
pip install -e .
```

macOS / Linux:

```bash
pip install -e .
```

Without installation, you can still run it from this repo:

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
Current commands:

```powershell
staticnervemap analyze <repo> [--out <path>] [--project-name <name>] [--scan-mode full|default|focused]
staticnervemap snapshot create <repo> --snapshot-id <id> [--roadmap-ref <ref>] [--no-overwrite] [--scan-mode full|default|focused]
staticnervemap snapshot suggest-id <repo> [--roadmap-ref <ref>] [--stage pre|post|baseline]
staticnervemap index rebuild <repo-or-.staticnervemap-or-snapshot-dir> [--out <path>]
staticnervemap --version
```

Backward-compatible shorthand:

```powershell
staticnervemap <repo>
```

This behaves like `analyze`.

## Recommended Usage
StaticNerveMap can be used in two natural ways.

### Pattern 1. Run it from outside the target repo
This is useful when developing StaticNerveMap itself or testing many repositories from one tool workspace.

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

Default output locations:

- `analyze` -> `<repo>/.staticnervemap/work/out.yaml`
- `snapshot create` -> `<repo>/.staticnervemap/snapshots/<snapshot_id>.yaml`
- `index rebuild` -> `<repo>/.staticnervemap/index.yaml`

### Pattern 2. Run it inside the target repo
This is the preferred real-world workflow.

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

In this pattern, `.` is the repository root and outputs are created directly in that repo.

### Single Analysis

```powershell
staticnervemap analyze Voice-Design-Cloner --out out-vdc.yaml --scan-mode default
```

Use this when you want one current structural snapshot.

### Layered Snapshot

```powershell
staticnervemap snapshot suggest-id Voice-Design-Cloner --roadmap-ref docs/ImplementationRoadmap.md#task-9-1
staticnervemap snapshot create Voice-Design-Cloner --snapshot-id M09-post-001 --roadmap-ref docs/ImplementationRoadmap.md#task-9-1 --scan-mode default
staticnervemap index rebuild Voice-Design-Cloner
```

Use this when you want milestone-aware history.

`index rebuild` accepts any of these paths:

- the target repo root, if it contains `.staticnervemap/snapshots`
- the `.staticnervemap` directory
- the `.staticnervemap/snapshots` directory
- legacy `static-nervemap` / `static-nervemap/snapshots` paths are still accepted for compatibility

If the target snapshot file already exists, `snapshot create` warns and overwrites it by default.
Use `--no-overwrite` when you want append-only behavior, especially from wrappers or AgentCLI-style harnesses.

### Real-Operation / Dogfood Loop
When you modify code with StaticNerveMap in the loop, use this workflow.

1. Pick the current task from the roadmap / OpenIssues.
2. Use `snapshot suggest-id` to check the pre-work snapshot ID.
3. Create a `pre` snapshot.
4. Run `analyze` to create a lightweight check YAML.
5. Use `change_targets`, `modification_paths`, and `unresolved` to decide what to read and touch first.
6. Implement and test the change.
7. Update roadmap / OpenIssues with the result.
8. Create a `post` snapshot.
9. Rebuild `.staticnervemap/index.yaml`.

Example:

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

Current real files in this repository:

- work YAML: `.staticnervemap/work/phase14-8-check.yaml`
- medium-large profiling example: `.staticnervemap/work/phase14-7-erpnext.yaml`
- latest snapshot: `.staticnervemap/snapshots/M14-post-005.yaml`
- current index: `.staticnervemap/index.yaml`

Operational rules:

- Use the `pre` snapshot as the before-change layer.
- Use `.staticnervemap/work/*-check.yaml` as a temporary work analysis.
- Create the `post` snapshot after tests and docs are updated.
- Use `--no-overwrite` when snapshot history should stay append-only.
- Prefer English/ASCII in generated YAML, index files, and agent handoff artifacts.

Index field semantics:

- `latest_snapshot_id`: the most recently generated snapshot. This is fixed by automatic selection and does not include a quality gate.
- `latest_stable_snapshot_id`: the latest snapshot with `stable: true`. If none exists, it stays `null`.
- `baseline_snapshot_id`: the baseline snapshot for the history line. It is not a fallback replacement for `latest_stable_snapshot_id`.

How to use them:

- use `latest_snapshot_id` for current work tracking
- use `latest_stable_snapshot_id` for agent handoff or safer reuse
- while `latest_stable_snapshot_id = null`, treat the history as not yet handoff-ready

### Recommended Agent Workflow
When an AI agent uses StaticNerveMap before modifying code, use this workflow.

1. **Initialize / Update Map**: run `staticnervemap index rebuild <repo>` to refresh `.staticnervemap/index.yaml`.
2. **Find Reading Mode**: read `.staticnervemap/index.yaml` or the latest snapshot's `snapshot.summary.reading_modes`.
3. **Choose Context**: pick the mode whose `recommended_reading_order` matches the modification goal.
4. **Follow Path**: if `modification_paths` exists, follow the entry-to-core path.
5. **Act**: read the target files, modify code, and run tests.
6. **Capture Result**: run `snapshot create` and `index rebuild` after the change.

There is currently no dedicated `analyze --mode reading` command. `reading_modes` is a summary field in snapshot / index YAML.

Mode selection guide:

- `general`: use when the task is still broad, or when the agent needs a repo-wide first reading path.
- `library_core`: use for package runtime, domain core, model/core logic, or public library behavior changes.
- `entry_surface`: use for CLI, route, UI, script, framework entrypoint, or external-to-runtime wiring changes.

Practical rules:

- In pure library repos, `general` and `library_core` may be the same. That is expected.
- In app / CLI / web / ML-tool repos, `entry_surface` and `library_core` often diverge and provide real branching value.
- If `entry_surface` is absent, either StaticNerveMap did not find a meaningful entry surface, or the repo is library-first.
- If `modification_paths` exists, prefer the concrete entry-to-core path over a generic reading order.

### Scan Modes
- `full`: widest scan, least selective
- `default`: fixed excludes plus helper excludes
- `focused`: optimized for large repos; prioritizes root files, entry/config files, and primary packages

Recommended default:

- small to medium repo: `default`
- large repo: `focused`

## Output Tree
When you use `snapshot create` with the default location, it creates:

```text
.staticnervemap/
  index.yaml
  work/
    out.yaml
  snapshots/
    <snapshot_id>.yaml
  deltas/
```

`deltas/` is currently a reserved location. Delta YAML itself is deferred.

## Direct CLI vs AgentCLI Harness
StaticNerveMap is intentionally a **neutral CLI**.

It answers structural questions and writes structural memory, but it does not decide every operational policy for the user. Direct StaticNerveMap usage allows an existing snapshot ID to be overwritten. When that happens, the CLI prints a warning, but it does not block the command.

AgentCLI-style usage is different. AgentCLI is an **operational constraint layer** above StaticNerveMap.

Recommended AgentCLI snapshot flow:

```powershell
staticnervemap snapshot suggest-id . --roadmap-ref docs/ImplementationRoadmap.md#task-13-3 --stage post
staticnervemap snapshot create . --snapshot-id M13-post-001 --roadmap-ref docs/ImplementationRoadmap.md#task-13-3 --no-overwrite
staticnervemap index rebuild .
```

The split is:

- StaticNerveMap: neutral structural map CLI
- AgentCLI: policy, workflow, and safety wrapper
- `.staticnervemap/`: structural memory layer shared by both

For direct use, overwrite is allowed with warning.
For AgentCLI use, pass `--no-overwrite` and treat snapshot history as append-only.

## Snapshot Naming
Recommended format:

```text
<prefix>-<stage>-<nnn>
```

Examples:

- `M07-post-001`
- `M09-pre-002`
- `GEN-post-001`

Rules:

- trailing number is counted from existing snapshot files
- prefix should come from roadmap milestone when possible
- if roadmap milestone cannot be inferred, fall back to `GEN`

## How To Write a Roadmap for StaticNerveMap
StaticNerveMap works best when roadmap documents are machine-readable.

Recommended minimum fields:

- phase header
- `milestone_id`
- `roadmap_ref`
- task header
- `task_id`
- `status`
- `priority`

Recommended pattern:

```md
## Phase 9: Snapshot Metadata and Index Meaning
milestone_id: M09
milestone_title_en: Snapshot metadata and index meaning
roadmap_ref: docs/ImplementationRoadmap.md#phase-9
status: in_progress

### 9-1. Strengthen snapshot metadata
task_id: 9-1
roadmap_ref: docs/ImplementationRoadmap.md#task-9-1
priority: high
status: in_progress
```

This makes it easier for StaticNerveMap to infer:

- `milestone_id`
- `milestone_title`
- `milestone_title_en`
- `snapshot_id` prefix candidates
- stable references to roadmap tasks

Detailed authoring rules live in [RoadmapAuthoringDictionary.md](reference/RoadmapAuthoringDictionary.md).
English version: [RoadmapAuthoringDictionary_en.md](reference/RoadmapAuthoringDictionary_en.md).

## What To Read First
If you are new to this repo, the best order is:

1. [CurrentStateSummary.md](reference/CurrentStateSummary.md)
2. [ImplementationRoadmap.md](reference/ImplementationRoadmap.md)
3. [OpenIssues.md](OpenIssues.md)
4. [ImplementationTests.md](reference/ImplementationTests.md)
5. [MVPDefinition.md](MVPDefinition.md)

For schema and layering:

- [YamlSchemaDraft.md](reference/YamlSchemaDraft.md)
- [SnapShotSchemaDraft.yaml](reference/SnapShotSchemaDraft.yaml)
- [indexSchemaDraft.yaml](reference/indexSchemaDraft.yaml)
- [SnapShotDraft.yaml](reference/SnapShotDraft.yaml)
- [indexDraft.yaml](reference/indexDraft.yaml)

For optimization and large-repo findings:

- [LargeRepoFindings.md](reference/LargeRepoFindings.md)
- [PostprocessOptimizationPlan.md](reference/PostprocessOptimizationPlan.md)

## Active Docs
The current top-level docs are:

- [README.md](README.md)
- [README_en.md](README_en.md)
- [OpenIssues.md](OpenIssues.md)
- [MVPDefinition.md](MVPDefinition.md)

Active reference docs live under `docs/reference/`.

Historical or completed notes are moved under `docs/old/`.
