# Task segment continuity policy

## Purpose

A task segment is one continuous unit whose goal, frozen decisions, allowed scope, baseline, and acceptance checks remain stable

The selected model and reasoning effort stay locked for the segment so a new recommendation cannot silently replace an active route

## Lock invariant

- Starting a segment hashes the private task and project keys, freezes the contract digest, and records one exact Sol, Terra, or Luna route
- A route check permits the locked route and blocks a different route until a valid handoff exists
- A changed contract cannot be checkpointed or completed as the original segment
- A clean boundary requires a passed milestone and all mandatory checks
- A hard escalation boundary requires a blocked milestone plus two failed hypotheses, conflicting evidence, or increased risk

## Handoff contract

The handoff package contains the current contract plus confirmed facts, frozen decisions, failed hypotheses, completed actions, remaining work, and current verification

The package rejects URLs, absolute paths, email addresses, code blocks, source file names, and secret-shaped values; use stable evidence aliases instead

The user must explicitly name a private output file when the handoff text should be persisted

Long-term segment state stores only the handoff digest, target route, fixed checkpoint fields, and timestamps; it does not retain handoff text

## Switch acceptance

A handoff is not a confirmed model switch

The target Codex task must expose an exact model and effort in `turn_context`; only a matching readback closes the source segment and opens a new locked segment

Missing or mismatched readback leaves the source segment active and the switch unaccepted

## Reports

The segment report contains counts of active, completed, rejected, and handed-off segments, verified transition labels, blocked switch attempts, and accepted outcomes

It cannot reconstruct prompts, code, paths, logs, task names, project names, or handoff text
