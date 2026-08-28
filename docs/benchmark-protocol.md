# Evaluation protocol

GameForge reports fixed-task benchmarks and open-ended creation suites separately. Both use the
same open solver session and a final evaluator that is outside the solver trajectory.

## Fixed-task benchmarks

Before execution, freeze:

1. benchmark repository and commit, task IDs, exclusions, and denominator;
2. exact GameForge commit, model, reasoning effort, sampling, retries, and timeout;
3. engine and evaluator versions, host architecture, assets, and parallelism;
4. baseline condition and all differences from the GameForge condition;
5. primary score and secondary reliability, usage, latency, and failure metrics.

Use fresh workspaces in every condition. Do not expose private evaluators, answers, or baseline
trajectories to the solver. A paired claim requires the same tasks and final evaluator. Aggregate
public scores without task-level outcomes are directional comparisons only.

## Open-ended creation

Freeze the authored briefs, starting project, engine, model, time budget, independent hard gates,
and any blinded quality rubric before running. Briefs may specify product goals and desired
mechanics, but should not prescribe source layout, node names, APIs, tool order, or intermediate
validation steps.

Report these evidence levels separately:

- solver completion;
- engine import, compile, build, and launch;
- scripted or engine-level input replay;
- operating-system input replay;
- manual playability review.

A build/launch pass is not a complete playthrough, and a curated showcase is not a representative
estimate of natural user requests.

## Incident handling

Runner defects, engine or host infrastructure failures, rate limits, invalid transport responses,
solver failures, and game defects remain distinct. If an unchanged final project survives a runner
incident, only the independent evaluator may be rerun; the model must not receive another attempt.
Any allowed retry must preserve the original failure, prove that no model-visible result or project
mutation was reused, and receive a new lineage identifier.

## Minimum evidence

Retain a sanitized task or brief, exact code/model/engine identity, attempt receipt, final evaluator
verdict, usage and timing, retry record, and relevant build/runtime evidence. For gameplay claims,
also retain the input trace or replay method and the evidence boundary. Screenshots and videos are
supporting evidence, not substitutes for structured engine results.

## Claim discipline

- Report the exact denominator and every exclusion.
- Keep infrastructure failures in the record rather than silently shrinking the denominator.
- Disclose judge transports, architecture substitutions, missing asset pools, and manual review.
- Do not convert subscription usage into API cost or leaderboard status.
- Do not infer efficiency superiority without comparable baseline usage data.
- Do not describe hard-gate coverage as subjective game quality.
