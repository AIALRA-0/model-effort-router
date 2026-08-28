# Local history audit policy

## Purpose

The history audit measures observed Codex route usage, token volume, task shape, verification evidence, and prospective outcomes without creating a second conversation archive

## Data boundary

The parser reads source JSONL files in place and never writes to them

Derived records may contain HMAC-pseudonymized session, turn, and project identifiers, day-level timestamps, route labels, token counts, tool categories, deterministic task labels, and fixed-format summaries

Derived records must not contain prompts, model responses, code, diffs, tool arguments or outputs, logs, file names, paths, URLs, email addresses, account identifiers, secrets, or exact source locations

Raw text may exist in process memory only while deterministic classification rules are evaluated

## Interpretation boundary

- A completed Codex turn is not automatically an accepted result
- Historical route differences are observational and do not establish causal model quality
- `likely_overrouted` means the observed task shape appears compatible with a lower route, not that a lower route has already passed
- `lower_route_validated` requires prospective comparable outcomes that pass the configured quality gates
- Mixed-route turns retain total usage but are excluded from fair route comparisons
- ChatGPT conversations without model and token metadata may inform behavior categories but not model-cost comparisons

## Prospective gate

The first policy review requires at least 50 completed runs, 3 projects, 14 active days, 2 comparable routes, and 10 runs per compared route

These thresholds trigger review and do not claim statistical significance

Any severe defect, scope violation, or downgrade regression requires immediate route reversion for the affected task class and manual review
