# Playable showcase evidence

This directory is a compact, public subset of the independent gameplay evidence produced during
the 2026-08-26 showcase audit. It contains silent MP4 replays, static preview frames, six compact
homepage GIF loops, input traces where available, and machine-readable recording receipts.

The evidence classes are deliberately separated:

- **Godot engine-input replay**: the evaluator injects normal Godot `InputEvent` actions into an
  unchanged generated project and records the resulting runtime.
- **Unity OS-native input replay**: the evaluator sends macOS CoreGraphics mouse/keyboard events to
  an unchanged standalone Player and records the window externally.
- **Unity project-authored behavior smoke**: the generated project advances its own deterministic
  gameplay smoke while the evaluator records it. This is useful runtime evidence, but it is not a
  real mouse/keyboard playthrough.

## Godot engine-input replays

| Game | Observed interaction | Replay | Evidence |
| --- | --- | --- | --- |
| Arena Survivor | Movement, aim, continuous fire, dash, score progression | [MP4](godot/arena-survivor/gameplay.mp4) | [receipt](godot/arena-survivor/recording-receipt.json) · [input](godot/arena-survivor/input-trace.json) |
| Tower Defense | Place two tower types, start wave, combat, 2× speed | [MP4](godot/tower-defense/gameplay.mp4) | [receipt](godot/tower-defense/recording-receipt.json) · [input](godot/tower-defense/input-trace.json) |
| Rhythm Game | Start and replay four lanes; score and accuracy change | [MP4](godot/rhythm-game/gameplay.mp4) | [receipt](godot/rhythm-game/recording-receipt.json) · [input](godot/rhythm-game/input-trace.json) |
| Local Co-op Arena | Select mode, move, pulse, enemy progression | [MP4](godot/local-coop-arena/gameplay.mp4) | [receipt](godot/local-coop-arena/recording-receipt.json) · [input](godot/local-coop-arena/input-trace.json) |
| Stealth Infiltration | Move, crouch, interact, guard simulation | [MP4](godot/stealth-infiltration/gameplay.mp4) | [receipt](godot/stealth-infiltration/recording-receipt.json) · [input](godot/stealth-infiltration/input-trace.json) |
| Match Three | Mouse selection and adjacent-swap feedback | [MP4](godot/match-three/gameplay.mp4) | [receipt](godot/match-three/recording-receipt.json) · [input](godot/match-three/input-trace.json) |
| Fishing Challenge | Start, cast, hook, reel, directional counterplay | [MP4](godot/fishing-challenge/gameplay.mp4) | [receipt](godot/fishing-challenge/recording-receipt.json) · [input](godot/fishing-challenge/input-trace.json) |
| Brick Breaker | Launch, keyboard/mouse paddle, collisions, score/lives | [MP4](godot/brick-breaker/gameplay.mp4) | [receipt](godot/brick-breaker/recording-receipt.json) · [input](godot/brick-breaker/input-trace.json) |

All eight cases completed the recorded input sequence without runtime script errors and passed a
manual gameplay-progress review. This proves the observed interactions, not completion, subjective
fun, or commercial quality.

## Unity native-input replay

| Game | Observed interaction | Replay | Evidence |
| --- | --- | --- | --- |
| Deckbuilding Duel | Pulse reduces enemy HP 34→27; Ward raises shield 0→6; Space advances to turn 2 | [MP4](unity/deckbuilding-duel-native/gameplay.mp4) | [receipt](unity/deckbuilding-duel-native/recording-receipt.json) · [manual review](unity/deckbuilding-duel-native/manual-playability-review.json) |

The final recording contains 3/3 delivered OS input events, 106 frames over 10.6 seconds, and no
runtime error. Earlier diagnostic recordings were excluded rather than silently counted.

## Unity project-authored behavior replays

| Game | Project-authored state progression | Replay | Evidence |
| --- | --- | --- | --- |
| Stealth Infiltration | Player, guards, cover, and objective in runtime state | [MP4](unity/stealth-infiltration-scripted/gameplay.mp4) | [receipt](unity/stealth-infiltration-scripted/recording-receipt.json) |
| Roguelike Dungeon | 20 consecutive player/enemy turns | [MP4](unity/roguelike-dungeon-scripted/gameplay.mp4) | [receipt](unity/roguelike-dungeon-scripted/recording-receipt.json) |
| Deckbuilding Duel | Card play, resolution, enemy action, turn 2 | [MP4](unity/deckbuilding-duel-scripted/gameplay.mp4) | [receipt](unity/deckbuilding-duel-scripted/recording-receipt.json) |
| Local Co-op Arena | Enemy spawn, P1 down, P2 approaches and revives | [MP4](unity/local-coop-arena-scripted/gameplay.mp4) | [receipt](unity/local-coop-arena-scripted/recording-receipt.json) |

## Reproduction

The recording tools live under `experiments/`:

- `record_godot_gameplay_showcases.py`
- `record_unity_behavior_showcases.py`
- `record_unity_native_input_showcase.py`
- `assets/showcase_record_probe.gd`
- `tools/macos_input_event.swift`
- `tools/png_sequence_to_mp4.swift`

These tools belong to the independent showcase/evaluator layer. They are not inserted into the
solver prompt and do not prescribe how the games are implemented.

See the [current evaluation index](../docs/harness-evaluation-index.md) for the benchmark,
open-ended creation, and playability claim boundaries.
