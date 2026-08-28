# GameForge evaluation index

The repository root on `main` is the only distributed GameForge implementation. Public reports are
limited to current benchmark, open-ended creation, and playable-artifact evidence.

## Benchmark results

| Date | Evidence | Result and scope |
| --- | --- | --- |
| 2026-08-28 | [GameCraft-Bench full140](gamecraft-bench-full140-unified-native-open-evaluation-2026-08-28.md) | 140/140 solver completions, 140/140 official builds, 732/732 official replays, 0 infrastructure failures, and a disclosed 63.15% subscription-judge approximation |
| 2026-08-21 | [GameDevBench full332](gamedevbench-full332-evaluation-2026-08-21.md) | Same-task local comparison: GameForge 213/332 versus Official 184/332; higher token and solver-time cost is disclosed |

## Open-ended results

| Date | Evidence | Result and scope |
| --- | --- | --- |
| 2026-08-24 | [Godot Open20](godot-open20-unified-native-open-evaluation-2026-08-24.md) | GameForge and local Official both 20/20 hard-gate PASS; blinded quality is statistically indistinguishable |
| 2026-08-25 | [Unity Open20](unity-open-game-creation20-showcase-2026-08-25.md) | 20/20 solver completions and 20/20 independent import/compile/build/Player hard gates; capability evidence, not an Official A/B |
| 2026-08-26 | [Playable showcase evidence](../showcases/README.md) | Eight Godot engine-input replays, one Unity OS-native input replay, and four disclosed Unity behavior smokes |

## Interpretation rules

- GameCraft 63.15% is not a leaderboard submission because the visual judge used a disclosed
  subscription transport.
- GameDevBench supports a performance claim on the qualified local comparison, not an efficiency
  claim.
- Open20 hard gates prove runnable delivery, not complete playthroughs or commercial quality.
- Input replays, behavior smokes, screenshots, and manual review are identified separately.
- New claims should name the exact code commit, model, reasoning effort, task set, engine/evaluator,
  retry policy, and judge transport.

For implementation and operational boundaries, see [Architecture](architecture.md),
[Authentication and execution](AUTHENTICATION_AND_EXECUTION.md), and [Security](security.md).
