# Local noise generation flow

End-to-end flow for producing a noisy Workspace-Bench task from a clean task
directory. The pipeline is deliberately staged so that each step fails soft and
records rather than blocking the others.

## Stages

1. **Workspace subset** (`prepare_workspace_subset.py` / `build_workspace_subset`)
   A private, size-bounded copy of the role's raw workspace is built so that the
   generating agents never see the full role filesystem.

2. **Planning** (`NoisePipeline.plan`)
   The planner Agent reads the task description, rubric, and every standard
   input, and emits one file-level job per input.

3. **Worker generation** (`NoisePipeline.run_workers`)
   Each file-level sub-Agent produces targeted noise (alternative versions,
   corrupt files, templates, distractors) under `artifacts/`, plus a
   `worker_result.json`.

4. **Integration** (`NoisePipeline.integrate` → `integration.py`)
   Deterministic checks place the standard inputs and the qualified noise into
   `agent_run/integrated/task` + `agent_run/integrated/workspace`, building the
   augmented `data_manifest` (noise items carry `generated_by`).

5. **Validation / rework** (`NoisePipeline.validate`)
   The validator Agent confirms the standard answer survives, the noise is
   plausibly distracting, and the task is still uniquely solvable. Failed jobs
   are sent back for rework up to `max_rework_rounds`.

6. **Path enhancement** (`PathRelocator`, `path_relocation.py`) — *new*
   After integration, a planner Agent proposes new `target_path` values for
   every manifest file (standard inputs **and** generated noise). An auditor
   Agent rejects moves that would break solvability (e.g. a path the task
   description names). Accepted moves are written back into
   `metadata.data_manifest`; each old `target_path` is added to
   `input_remove_paths` so the runtime deletes the stale location when the task
   is materialized. Unsafe planner paths (absolute / `..` / backslash) and
   auditor-rejected files keep their original location.

## Path enhancement in detail

```
gather_files  → read data_manifest, classify canonical vs generated
plan_paths    → planner Agent returns {key: targeted_path}
audit_paths   → auditor Agent returns {accepted, rejected}
apply         → accepted: target_path=new; old path → input_remove_paths
              → unsafe/rejected: keep original
write         → agent_run/generation/path_relocation.json
```

- **Determinism / rerun safety**: the step is driven by `--path-seed` (defaults
  to `--seed`). `base_state` in `path_relocation.json` snapshots the pre-move
  manifest; `--clean` restores it before re-running, so repeated runs are
  idempotent.
- **Never blocks**: any exception in planning, auditing, or applying is recorded
  in `failures` and the run still emits `path_relocation.json`. Tasks whose
  noise generation produced no noise (`noiseless_jobs`) are recorded in
  `failures` and left at their original paths.
- **Runtime compatibility**: `src/agent_runner.py` already consumes both
  `input_remove_paths` (deletes stale locations) and `data_manifest.target_path`
  (places files) when materializing the task workdir, so relocating via these
  two fields needs no downstream change.

## Artifacts

```text
workspace_subset/subset_manifest.json
workspace_subset/source_path_map.json
agent_run/task_plan.json
agent_run/workers/<job_id>/worker_result.json
agent_run/validation/round_*/validation_result.json
agent_run/integrated/task/metadata.json        # augmented manifest + relocated paths
agent_run/integrated/workspace/...             # validation workspace
agent_run/generation/path_relocation.json      # path-enhancement record
agent_run/pipeline_result.json
```

## Running

```bash
python3 evaluation/scripts/noise/run_local_noise_pipeline.py \
  --task-dir evaluation/tasks/373 \
  --raw-workspace evaluation/filesys/houqin_raw \
  --run-dir evaluation/noise_generation/runs/373/demo \
  --provider-config evaluation/.generated/run_configs/runs/<config>.yaml \
  --path-planner-provider-config evaluation/.generated/run_configs/runs/<planner>.yaml \
  --path-auditor-provider-config evaluation/.generated/run_configs/runs/<auditor>.yaml \
  --seed 3732026 \
  --clean
```

`run_noise_batch.py` calls `run_local_noise_pipeline.py`, so batch runs gain
the path-enhancement stage automatically.

Standalone invocation (e.g. re-relocate an already-integrated task):

```bash
python3 evaluation/scripts/noise/path_relocation.py \
  --task-dir evaluation/noise_generation/runs/373/demo/agent_run/integrated/task \
  --generation-dir evaluation/noise_generation/runs/373/demo/agent_run/generation \
  --path-planner-provider-config <planner>.yaml \
  --path-auditor-provider-config <auditor>.yaml \
  --seed 3732026 \
  --clean
```

See `local_noise_multi_agent_design.md` for the agent interactions and data
contracts.
