# GameCraft-Bench full140 evaluation

## Summary

GameForge completed all 140 public GameCraft-Bench tasks with `GPT-5.6 Sol`, high reasoning, the
`native-open` profile, and no harness-authored repair pass. Every delivered project built and every
official interaction replay completed.

The final score is a **63.15% subscription-judge approximation**. It is directionally above the
published Codex row at 60.50%, but it is not a leaderboard submission because the visual judge was
invoked through a different disclosed transport.

## Frozen conditions

- Suite: all 140 public tasks across 15 families.
- Solver: `GPT-5.6 Sol`, high reasoning.
- Execution: one open solver session per task; four workers for most of the run.
- Engine evaluation: official Godot build, interaction replay, frame sampling, requirement rubrics,
  aggregation, and score formula.
- Visual judge: the benchmark's default GPT-5.5 model through a Codex subscription transport.
- Assets: the Kenney asset library was available; the OpenGameArt pool was empty.
- Harness retries or repair passes: none in the final scored attempts.

## Results

| Full140 metric | GameForge | Published Codex | Difference |
| --- | ---: | ---: | ---: |
| Overall | **63.15%** | 60.50% | **+2.65 pp** |
| Core Mechanics | **76.15%** | 74.50% | +1.65 pp |
| Content Depth | **58.85%** | 56.10% | +2.75 pp |
| Functional Visuals | **68.74%** | 64.80% | +3.94 pp |
| Presentation & Art | **59.40%** | 57.00% | +2.40 pp |

| Delivery evidence | Result |
| --- | ---: |
| Solver completions | **140 / 140** |
| Official builds | **140 / 140** |
| Official interaction replays | **732 / 732** |
| Infrastructure failures | **0** |
| Harness repair passes | **0** |

Across tasks, the median score was 63.57%; 82/140 tasks reached 60%, 40/140 reached 70%, and
19/140 were below 50%. No low-scoring task received a second generation attempt or selective rerun.

## Reliability and usage

| Metric | Result |
| --- | ---: |
| Solver compute total | 1,392.08 minutes |
| Solver mean / median / P90 | 9.94 / 9.37 / 12.05 minutes |
| Build and replay total | 201.94 minutes |
| Tool calls | 3,491 |
| Input / cached input tokens | 212,100,397 / 199,713,920 |
| Output / reasoning output tokens | 3,613,849 / 696,514 |

The public Codex row does not provide directly comparable task-level latency, tool-call, or token
data, so this run does not establish an efficiency advantage.

## Comparison boundary

The run retained the official build, replay, frame selection, rubrics, aggregation, and task set.
Three differences prevent a strict leaderboard comparison:

1. the visual judge transport differed from the public verifier;
2. evaluation used the official Linux arm64 Godot build on Apple Silicon rather than x86_64;
3. the OpenGameArt asset directory was empty.

The saved replay frames can be rescored through the official API judge without rerunning the solver
or Godot. A causal harness advantage would additionally require a paired upstream condition with
the same tasks, assets, judge, and evaluator environment.

## Reproduction sources

- [GameCraft-Bench](https://github.com/FreedomIntelligence/gamecraft-bench)
- [GameCraft-Bench paper](https://arxiv.org/abs/2606.17861)
- Runner: [`experiments/run_gamecraft_bench_full140.py`](../experiments/run_gamecraft_bench_full140.py)
- Judge adapter: [`experiments/score_gamecraft_bench_full140_subscription.py`](../experiments/score_gamecraft_bench_full140_subscription.py)
