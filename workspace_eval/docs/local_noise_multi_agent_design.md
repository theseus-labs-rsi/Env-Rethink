# Local noise multi-agent design

Design notes for the multi-agent local-noise pipeline and the path-enhancement
stage. Companion to `local_noise_generation_flow.md`.

## Agents and roles

| Role | Backend arg | Responsibility |
|---|---|---|
| planner (main) | `--planner-provider-config` | Split the task into one file-level job per standard input. |
| worker | `--worker-provider-config` | Generate targeted noise per job into `artifacts/`. |
| validator | `--validator-provider-config` | Confirm solvability and noise quality; request rework. |
| worker fallback | `--worker-fallback-provider-config` | Retry a failed worker with a different model. |
| **path planner** | `--path-planner-provider-config` | Propose new `target_path` for every manifest file. |
| **path auditor** | `--path-auditor-provider-config` | Reject moves that break solvability. |

The two path-enhancement roles reuse the same `AgentBackend` contract as the
noise agents (`run(*, role, prompt, work_dir, sandbox_dir, resume_session_id)`)
and the same `CodexBackend` implementation.

## Manifest classification

`data_manifest` items are classified by `gather_files`:

- **canonical** — standard inputs, identified by the *absence* of
  `generated_by`.
- **generated** — noise, identified by `generated_by` (set by
  `integration.py` when the augmented manifest is written).

Both participate in relocation; the auditor is the only gate that decides
whether a proposed move is safe.

## §9.3 Path-enhancement data contract

### Planner output (`path_planner_prompt`)

```json
{
  "path_map": {
    "<key>": "<new relative posix path>",
    "...": "..."
  }
}
```

Constraints enforced by the prompt (and re-checked at apply time):

- POSIX relative path, `'/'` separators, no leading `/`, no `..`, no backslash.
- New directories are allowed and normalized (created at runtime).
- Do not move paths the task description names.
- `path_map` must cover every file key; drop-in-place if a move is unsafe.

`<key>` is the stable file identifier produced by `gather_files`
(`filename`, de-duplicated with a numeric suffix when names collide).

### Auditor output (`path_auditor_prompt`)

```json
{
  "accepted": ["<key>", "..."],
  "rejected": [{"key": "<key>", "reason": "<why it breaks solvability>"}]
}
```

The auditor only checks the task description's named paths and generic
solvability sanity; it does **not** perform cross-file reference-graph
analysis. A planned key the auditor neither accepts nor rejects is treated as
accepted (least-blocking).

### Apply behavior

For each accepted, safe key:

- `data_manifest[i].target_path = new_path`
- old `target_path` appended to `input_remove_paths` (sorted, de-duplicated)
- collisions resolved by in-place rename `{stem}_{n}{suffix}`, reusing the
  `integration.py` collision logic.

Rejected or unsafe keys keep their original `target_path`.

### `path_relocation.json` record

```json
{
  "schema_version": 1,
  "status": "applied | no_change | failed",
  "seed": 3732026,
  "base_state": {
    "files": [{"index": 0, "key": "input.csv", "original_target_path": "...", "kind": "canonical"}],
    "input_remove_paths": ["..."]
  },
  "path_map": {"<key>": "<final target_path>"},
  "audit": {"accepted": ["..."], "rejected": [{"key": "...", "reason": "..."}]},
  "rejected": [{"key": "...", "reason": "..."}],
  "failures": [{"type": "unsafe_path|noiseless_job|plan|audit|apply|...", "...": "..."}]
}
```

`base_state` lets `--clean` restore the manifest before a re-run, keeping the
step deterministic. `failures` is the only place exceptions surface — the step
never raises into the enclosing add-noise run.

## Failure modes (recorded, not raised)

| Failure | Recorded as | Effect |
|---|---|---|
| Planner Agent throws / returns no `path_map` | `failures[].type == "plan"` | status `no_change`, nothing moved |
| Auditor Agent throws | `failures[].type == "audit"` | planner plan accepted wholesale |
| Planner returns absolute / `..` / backslash path | `failures[].type == "unsafe_path"` | that file kept at original path |
| Auditor rejects a move | `rejected[]` + `audit.rejected` | file kept at original path |
| Noise generation left an input with no noise | `failures[].type == "noiseless_job"` | input left at original path |
| `data_manifest` empty / missing | `failures[].type == "no_files"` | status `no_change` |
