# Godot Open20 native-open evaluation

## Summary

GameForge and local Official each completed the same 20 authored, open-ended Godot creation briefs
from empty projects. Both conditions passed **20/20** independent import/runtime hard gates.

Anonymous quality scores were 15.15/16 for GameForge and 14.85/16 for Official. Pairwise preference
was 9:11 with a two-sided sign-test `p = 0.82380295`. The supported conclusion is quality parity and
complete runnable delivery, not a quality superiority claim.

## Frozen conditions

- 20 briefs spanning survival, platforming, puzzle, strategy, simulation, racing, rhythm, combat,
  sandbox, and local co-op mechanics.
- Empty Godot projects with no prescribed node names, source layout, APIs, tool order, or validation
  workflow.
- Solver: `GPT-5.6 Sol`, medium reasoning, one attempt per brief.
- GameForge profile: `native-open`, one complete disposable workspace per task.
- Local Official: same model, briefs, Godot version, timeout, and independent evaluator.
- Anonymous judge saw the brief, source summary, runtime evidence, and screenshots, but not condition,
  trajectory, usage, or timing.

## Results

| Metric | GameForge | Local Official |
| --- | ---: | ---: |
| Solver completion | **20 / 20** | **20 / 20** |
| Import/runtime hard gate | **20 / 20** | **20 / 20** |
| Infrastructure failure | 0 | 0 |
| Anonymous quality mean | **15.15 / 16** | 14.85 / 16 |
| Pairwise preference | 9 | 11 |

Quality dimensions were brief fulfillment, gameplay systems, visual coherence, and usability/
feedback, each scored 0–4. The +0.30 mean difference and zero median difference are too small and
too weakly sampled to establish a general ranking.

## Efficiency and stability

Against local Official, the GameForge condition used 9.79% less total solver time, had a 15.62%
lower median time, used 22.29% fewer input tokens, 11.02% fewer output tokens, and 9.73% fewer tool
calls; it was faster on 13/20 tasks. These are single-run efficiency signals, not a stable cost
guarantee.

Seven model-selected invocations combining headless Godot and movie capture exited abnormally. The
solver observed the errors, changed strategy, and completed each project. The independent evaluator
ran once per final artifact with no retry, so these events affected exploration cost rather than the
20/20 delivery result.

## Interpretation boundary

- The briefs were authored for genre coverage, not sampled from natural user traffic.
- Each condition ran once per brief; the judge and solver used the same model family.
- Hard gates prove importable, runnable projects and observed state change—not complete playthroughs,
  subjective fun, or commercial quality.
- The experiment supports an open creation capability claim and quality parity, not universal
  superiority over Official.

## Reproduction sources

- Manifest: [`benchmarks/godot-open-game-creation20-v1.json`](../benchmarks/godot-open-game-creation20-v1.json)
- Runner: [`experiments/run_godot_open_game_creation20_native_open_minimal.py`](../experiments/run_godot_open_game_creation20_native_open_minimal.py)
- Analysis: [`experiments/analyze_godot_open_game_creation20_native_open_minimal.py`](../experiments/analyze_godot_open_game_creation20_native_open_minimal.py)
