# Changelog

All notable public release changes are recorded here. The repository root is the single installable
GameForge source tree; historical experiment labels are evidence conditions, not alternate releases.

## 0.1.0 — 2026-08-28

### Added

- Unified `gameforge run-game-workspace --engine auto|unity|godot` entrypoint.
- Thin Unity and Godot adapters behind one disposable-workspace and independent-evaluator contract.
- Codex-subscription and OpenAI Responses API solver backends.
- `native-open`, `supervised-native`, and fail-closed external `strong-isolated` execution profiles.
- Public GameCraft-Bench full140 report and frozen benchmark manifests.
- Curated 13-case gameplay evidence package with videos, previews, traces, and receipts.
- English and Simplified Chinese GitHub homepages with explicit benchmark and security limitations.
- Six optimized 256-color, auto-playing gameplay GIFs for the GitHub homepage, linked to full MP4
  evidence.
- Results organized into fixed-task benchmarks and independently verified open-ended game creation.
- Public documentation consolidated around the current release; historical candidate and iteration
  reports remain recoverable from Git history rather than appearing as supported release docs.

### Verified

- 34 unified release contract tests pass; 5 host/provider integration tests skip by design.
- Clean-checkout CI linting is reproducible without relying on local resolver caches.
- All 13 showcase MP4 files use H.264 with fast-start metadata for browser streaming.
- Wheel and sdist build successfully; the installed wheel exposes `run-game-workspace` and bundles
  backend profiles, execution contracts, and the minimal Unity creation template.
- GameCraft full140 completed 140/140 solver runs, 140/140 official builds, and 732/732 official
  interaction replays with zero infrastructure failure.

### Known limitations

- GameCraft 63.15% uses a disclosed subscription transport for the benchmark's GPT-5.5 judge and is
  not yet a strict leaderboard submission.
- The strong-isolation protocol is implemented, but no concrete VM/container provider is bundled.
- Unity and Godot are currently fully tested; additional engines such as Unreal are planned.
