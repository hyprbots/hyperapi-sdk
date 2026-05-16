---
description: Spawn the 6-teammate hyperapi-sdk end-to-end testing team. Lead synthesises findings to tests/reports/team-summary.md. Preproduction cluster only — production is hard-blocked.
argument-hint: "[--soak] [--skip <name1,name2>] [--only <name>]"
---

# /sdk-team — end-to-end hyperapi-sdk testing

Spawn a 6-teammate Agent Team to test `hyperapi-sdk` end-to-end against the
**preproduction** cluster. The lead (you) coordinates, never edits SDK code.

## Pre-flight (do this FIRST, before anything else)

```bash
# 1. cwd must be inside services/hyperapi-sdk
test -f services/hyperapi-sdk/pyproject.toml || \
  test -f ./pyproject.toml && grep -q '^name = "hyperapi-sdk"' ./pyproject.toml || \
  { echo "FAIL: not in services/hyperapi-sdk/ — cd there first"; exit 1; }

# 2. preprod key must be set
test -n "$HYPERAPI_PREPROD_KEY" || \
  { echo "FAIL: export HYPERAPI_PREPROD_KEY=hk_test_... before running"; exit 1; }

# 3. Agent Teams must be enabled
grep -q 'CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS' ~/.claude/settings.json || \
  { echo "FAIL: set CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 in ~/.claude/settings.json"; exit 1; }

# 4. Reports dir
mkdir -p services/hyperapi-sdk/tests/reports
```

If any check fails, stop and tell the user how to fix it. Do NOT proceed.

## Parse arguments

- `--soak` → pass `SDK_TEAM_SOAK=1` to teammate #2 (adds ~10 min)
- `--skip name1,name2` → don't spawn those teammates (comma-separated)
- `--only name` → spawn just that one teammate. Use Task() directly instead
  of TeamCreate — Agent Teams overhead isn't worth it for a single role.
- No args → spawn all 6 teammates, soak OFF

## Spawn the team

For the default (all-six) case:

> Create a 6-teammate agent team named `hyperapi-sdk-e2e`. Use the
> following subagent definitions (project scope, all under
> `.claude/agents/hyperapi-sdk-team/`):
>
> 1. `hyperapi-sdk-l1-contract-guardian` (start immediately)
> 2. `hyperapi-sdk-l2-sim-operator` (start immediately, long pole)
> 3. `hyperapi-sdk-drift-and-type-sentinel` (depends on #2)
> 4. `hyperapi-sdk-release-gate-auditor` (start immediately)
> 5. `hyperapi-sdk-tutorial-validator` (start immediately)
> 6. `hyperapi-sdk-billing-usage-reconciler` (depends on #2)
>
> Hard rules every teammate inherits:
> - PREPROD ONLY: target the preprod ALB. NEVER `apis.hyperbots.com`. NEVER
>   set `ALLOW_PROD_SIM=1`.
> - Exclusive file ownership: write only to your designated report file.
> - Findings only on SDK source — do not auto-fix.
> - Report must start with the YAML frontmatter contract in your role file.
>
> When all six teammates report idle, synthesise `tests/reports/team-summary.md`
> with this shape:
>
> ```yaml
> ---
> team: hyperapi-sdk-e2e
> version: 1.0
> run_id: <unified run id>
> target: preprod
> verdict: GO | NO-GO | PARTIAL
> teammates_total: 6
> teammates_pass: <int>
> teammates_fail: <int>
> teammates_partial: <int>
> total_findings: <int>
> blocking_findings: <int>
> started_at: <iso8601>
> finished_at: <iso8601>
> ---
> ```
>
> Plus sections: per-teammate status table, blocking findings (severity = high),
> non-blocking findings, recommended actions (open `sdk-drift` issue / file bug /
> propose baseline update / no-op).
>
> After synthesis, call TeamDelete to clean up.

## Post-team actions (lead only, after synthesis)

For each blocking finding the lead may:

- **`baseline.json` update proposed by #2**: `git diff
  tests/customer_sim/baseline.json.proposed tests/customer_sim/baseline.json`
  → ask the user whether to merge it (PR-only by default).
- **`sdk-drift` issue opened by #2/#3/#5/#6**: `gh issue list -L sdk-drift`
  → ensure idempotency; comment vs create.
- **Source bug filed by any teammate**: create a GitHub issue with the
  teammate's finding text; do NOT auto-PR a fix.

## Constraints on the lead

- The lead synthesises only. Do NOT edit SDK source. Do NOT run tests
  yourself — that's the teammates' job.
- If a teammate fails or stalls, message it directly via SendMessage rather
  than picking up its task.
- Stay in the `services/hyperapi-sdk/` working directory.
