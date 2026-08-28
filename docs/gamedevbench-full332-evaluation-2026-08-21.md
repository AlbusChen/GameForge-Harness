# GameDevBench full332 evaluation

## Summary

On the same 332 locally qualified GameDevBench tasks, the frozen GameForge minimal-open condition
passed **213/332 (64.16%)**, while the local Official condition passed **184/332 (55.42%)**. The
paired difference is +29 tasks and +8.73 percentage points.

This result supports a performance advantage on this task set. It does not support an efficiency
advantage: GameForge used 52.8% more tokens and 33.3% more solver time than local Official.

## Frozen conditions

- GameDevBench commit: `e3868bccbb88e86a3eb2d62154f1e9e0f3fd489a`.
- Solver: `GPT-5.6 Sol`, medium reasoning, provider-default sampling.
- Engine: Godot `4.4.1.stable.official.49a5bc7b6`.
- Task qualification: 332/333 tasks passed a pre-result ground-truth check; `task_0097` was excluded
  before candidate outcomes because the pinned Official evaluator failed twice.
- Denominator: 332; model failures, blocked attempts, and timeouts remained failures.
- Conditions: fresh workspaces, identical task order, four-way parallel execution, and the same
  Official evaluator.
- Candidate behavior: one open workspace session per task, no task router, fixed tool sequence,
  intermediate acceptance gate, or harness repair loop.

## Results

| Same-task condition | PASS | Pass rate |
| --- | ---: | ---: |
| GameForge | **213 / 332** | **64.16%** |
| Local Official | 184 / 332 | 55.42% |
| Difference | **+29 tasks** | **+8.73 pp** |

Paired outcomes were 178 both-pass, 35 GameForge-only, 6 Official-only, and 113 both-fail. The
two-sided exact McNemar result is `p = 4.8736e-06`; a bootstrap over complete task families gives a
95% interval of `+5.65` to `+12.25` percentage points. GameForge's Wilson 95% pass-rate interval is
58.86%–69.13%.

The public Official score is 195/333 (58.6%). Because public task-level outcomes are unavailable
and the qualified denominator differs, it is an aggregate directional reference rather than a
paired comparison.

| Task group | GameForge PASS |
| --- | ---: |
| 2D | 64 / 119 |
| 3D | 8 / 9 |
| Script/resource | 94 / 124 |
| UI | 47 / 80 |

## Reliability and efficiency

All 332 attempts received an Official evaluator verdict: 213 PASS and 119 ordinary FAIL, with no
BLOCKED, ERROR, infrastructure retry, or policy violation in the scored batch.

| Metric | GameForge | Local Official | Difference |
| --- | ---: | ---: | ---: |
| Tokens per task | 422,736 | 276,739 | +52.8% |
| Solver time total | 48,995 s | 36,756 s | +33.3% |
| Median solver time | 128.2 s | 98.7 s | +29.9% |
| Seconds per PASS | 230.0 s | 199.8 s | +15.1% |

The open session improved correctness while encouraging more model-led exploration. The evidence
therefore supports performance, reliability, and complete failure attribution—not lower cost.

## Interpretation boundary

- This is one complete Godot benchmark, not proof of superiority on every engine or workload.
- The score belongs to a frozen experiment from the minimal-open design line promoted into
  GameForge; later documentation and packaging changes were not used to rescore the 332 projects.
- The remaining failures include exact visual geometry and Godot semantic errors. The experiment
  does not prove that every failure is a model limitation.
- Public Official lacks paired outcomes, so statistical claims use only the local Official run.

## Reproduction sources

- [GameDevBench](https://github.com/waynchi/gamedevbench)
- Frozen task manifest SHA-256: `a36974f033b323b86bf5c3c3747a2beff478b9feb6048e9e83b7e27a02a9aff1`
- Runner: [`experiments/run_gamedevbench_full_candidate.py`](../experiments/run_gamedevbench_full_candidate.py)
- Analysis: [`experiments/analyze_gamedevbench_full.py`](../experiments/analyze_gamedevbench_full.py)
