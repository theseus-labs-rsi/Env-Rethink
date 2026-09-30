# Local noise generation (multi-agent)

Build or rebuild the local-only noisy task set with the multi-agent pipeline:

```bash
python3 evaluation/scripts/noise/run_local_noise_pipeline.py \
  --task-dir evaluation/tasks/373 \
  --raw-workspace evaluation/filesys/houqin_raw \
  --run-dir evaluation/noise_generation/runs/373/demo \
  --provider-config evaluation/.generated/run_configs/runs/<config>.yaml \
  --seed 3732026 \
  --clean
```

Useful artifacts:

```text
workspace_subset/subset_manifest.json
workspace_subset/source_path_map.json
agent_run/task_plan.json
agent_run/workers/<job_id>/worker_result.json
agent_run/validation/round_*/validation_result.json
agent_run/integrated/task/metadata.json        # augmented manifest + relocated paths
agent_run/generation/path_relocation.json      # path-enhancement record
agent_run/pipeline_result.json
```

### Path enhancement (after noise)

After integration, a planner + auditor pair relocate every file to a more
realistic path (`path_relocation.py`). New CLI flags:

```text
--path-planner-provider-config   # defaults to --provider-config
--path-auditor-provider-config   # defaults to --validator-provider-config
--path-seed                      # defaults to --seed
```

Relocated paths and the auditor decision land in
`agent_run/generation/path_relocation.json`; the augmented manifest is written
to `agent_run/integrated/task/metadata.json`. The step records and never blocks.

Run the dataset through the normal benchmark entry:

```bash
evaluation/docker/run-benchmark.sh \
  --harness codex --model kimi-k2.5 --dataset tasks-hard
```

See `docs/local_noise_generation_flow.md` for the full pipeline description, and
`docs/local_noise_multi_agent_design.md` for the agent data contracts.
