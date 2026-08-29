# Model switch loss evaluation policy

## Question

The switch evaluator asks whether a verified route handoff changes quality or recovery cost compared with continuing the same task from the same checkpoint on the source route

It does not rank models globally and does not mix historical observations into paired conclusions

## Pair design

- The continuation arm resumes on the source model and effort
- The switched arm receives the verified handoff and resumes on the target model and effort
- Both arms start from the same checkpoint, task contract, tool permissions, time limit, and acceptance checks
- A and B identity is randomized; a claimed material loss or benefit requires reversed-order reproduction
- Real external writes, publication, deletion, payment, and production changes must not be executed twice

## Review layers

- Bounded review records acceptance, correction count, recovery minutes, repeated completed actions, preference, and confidence
- Forensic review adds five 0–4 scores, mandatory-check status, severe defects, scope violations, regressions, and missing context items
- User review may resolve a policy-changing preference or disagreement without revealing route identity

User review becomes mandatory when risk is at least 3, either automatic review has low confidence, the bounded and forensic preferences conflict, or their acceptance judgments disagree; a pending user review cannot satisfy the transition completion gate

When the user has already declined participation or an honest user judgment cannot be obtained, `resolve-review --disposition unavailable` closes the pending action without inventing a judgment; the pair remains `indeterminate`, is excluded from the transition completion gate, and may later accept a real user judgment

## Pair verdicts

- `no_material_switch_loss` means both arms pass and the switched arm stays within every configured recovery and quality threshold
- `recoverable_switch_loss` means both arms pass but the switched arm requires excessive recovery, repetition, correction, or loses required context
- `material_switch_loss` means continuation passes while the switched arm fails hard acceptance, followed by reversed-order reproduction
- `switch_benefit` means the switched arm passes while continuation fails, followed by reversed-order reproduction
- `both_failed` means neither arm can establish a usable result
- `indeterminate` preserves the current continuity policy when evidence is incomplete or conflicting

## Transition completion gate

A transition can be marked validated only after four judged pairs across at least two projects and two phases

The public policy budget is 12 pairs, with at most four pairs for any exact source-to-target route

These gates support a conservative local routing decision for observed transitions only; they do not prove universal equivalence or a general switching penalty

## Privacy

The evaluator persists HMAC pseudonyms, route assignments and readback, a handoff digest, fixed review fields, controlled outcome flags, and numeric verification counts

The task contract text, handoff text, prompts, outputs, code, patches, logs, file names, paths, URLs, accounts, and secrets remain outside the evaluation dataset
