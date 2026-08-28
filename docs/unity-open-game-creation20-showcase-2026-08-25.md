# Unity Open20 creation evaluation

## Summary

Using the unified `native-open` pipeline, GameForge completed 20 authored Unity game-creation briefs
from a minimal project. All 20 solver sessions completed, and all 20 artifacts passed an independent
Unity import, compile, build, and Player-launch hard gate.

This is open-ended Unity capability evidence. It is not an Official A/B and does not show that every
title received a complete human playthrough.

## Frozen conditions

- Solver: `GPT-5.6 Sol`, medium reasoning, Codex subscription backend.
- Execution: `native-open`, one attempt per brief, 1,200-second limit, no automatic repair.
- Engine: Unity `6000.3.20f1`.
- Start state: the same minimal Unity project for every disposable workspace.
- Scope: 20 desktop 2D genres with natural-language product goals and no prescribed source layout,
  Editor method, tool order, or intermediate gate.
- Evaluation: a solver-invisible copy was reimported, compiled, built as a macOS Player, launched,
  and captured at two times.

## Results

| Metric | Result |
| --- | ---: |
| Solver completion | **20 / 20** |
| Independent hard-gate PASS | **20 / 20** |
| Independent Player builds and launches | **20 / 20** |
| Valid 1280×720 evaluator screenshots | **40 / 40** |
| Infrastructure failure | **0** |
| Harness retry / automatic repair | **0 / 0** |
| Model-created standalone applications | 15 / 20 |
| Tool calls | 349 total; 16.5 median |

The 20 projects covered arena survival, platforming, brick breaker, tower defense, Sokoban,
turn-based combat, racing, rhythm, stealth, farming, match-three, roguelike, pinball, endless runner,
factory logistics, artillery, deckbuilding, physics sandbox, fishing, and local co-op.

A non-blind artifact review averaged 15.1/16 across brief fulfillment, gameplay systems, visual
coherence, and usability/feedback. It is descriptive showcase review, not a comparative benchmark.

## Usage

| Metric | Result |
| --- | ---: |
| Solver time total | 7,822.4 s |
| Solver median / P90 | 376.2 / 467.7 s |
| Independent build total | 282.6 s |
| Independent Player probe total | 163.9 s |
| Input / cached input tokens | 16,557,775 / 15,277,056 |
| Output tokens | 311,094 |

Subscription usage has no API invoice in this run and must not be relabeled as API cost.

## Playability evidence

The public showcase package contains one Unity game replayed with operating-system mouse/keyboard
events and four additional Unity artifacts replayed through disclosed project-authored behavior
smokes. These evidence classes remain separate from the 20/20 build/launch result. See the
[showcase evidence index](../showcases/README.md).

## Interpretation boundary

- There was no Official or alternate-profile condition, so this suite does not establish comparative
  performance, quality, or efficiency.
- The two-timepoint generic evaluator did not execute a task-specific complete playthrough.
- The authored briefs cover genres deliberately and are not a statistical sample of user requests.
- Build and launch evidence is necessary delivery proof, but it does not establish subjective fun or
  equal presentation quality across all 20 games.

## Reproduction sources

- Manifest: [`benchmarks/unity-open-game-creation20-v1.json`](../benchmarks/unity-open-game-creation20-v1.json)
- Runner: [`experiments/run_unity_open_game_creation20_showcase.py`](../experiments/run_unity_open_game_creation20_showcase.py)
- Analysis: [`experiments/analyze_unity_open_game_creation20_showcase.py`](../experiments/analyze_unity_open_game_creation20_showcase.py)
