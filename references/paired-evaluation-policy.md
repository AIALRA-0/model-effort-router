# Paired route evaluation policy

## Purpose

The paired evaluator asks whether the configured route is sufficient for a bounded task class, not which model is universally best

Each valid pair runs the same frozen task contract from the same baseline in isolated environments, with route order randomized and model identity hidden from reviewers

## Evidence layers

- Hard acceptance uses tests, hidden checks, regression evidence, scope compliance, data safety, and external-write boundaries
- Bounded review sees only the user request, final response, and user-visible behavior
- Forensic review may inspect patches, tool evidence, tests, failure paths, and hidden constraints, but not route identity
- User review is required for bounded-versus-forensic disagreement, high-risk work, policy-changing ties, or low-confidence automation

## Decisions

- `preset_sufficient` requires four valid equivalent pairs across two projects and two execution shapes, with no hard failure or unresolved user review
- `surface_only` means bounded review accepts the preset result while forensic evidence finds a material defect that the ceiling route avoids
- `material_gap` requires the ceiling to pass while the preset fails, followed by a reversed-order reproduction for the same case
- `both_failed` means both arms fail hard acceptance and the pair cannot establish a lower-route deficiency
- `indeterminate` preserves the current route when evidence is incomplete or conflicting

## Privacy

The evaluator persists only HMAC pseudonyms, task-cell labels, route assignments and readback, numeric judgments, verification counts, and controlled outcome flags

Prompts, responses, code, patches, logs, file names, paths, URLs, account identifiers, and secrets must remain in the original Codex tasks or isolated workspaces and never enter evaluator records

## Interpretation boundary

Four pairs support a conservative local routing decision for the observed task shapes only

They do not establish statistical equivalence, universal model rankings, or causal claims outside the paired experiment
