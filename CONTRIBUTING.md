# Contributing

Contributions should preserve the central invariant: the solver may freely choose its development
workflow inside the selected execution boundary, but only independent engine/runtime evidence can
mark a run successful.

## Development setup

1. Install Python 3.11 or 3.12 and `uv`.
2. Run `uv sync --locked --all-groups`.
3. Run `uv run ruff check gameforge tests` and `uv run pytest`.
4. For Unity changes, use the editor version pinned in `README.md` and run the relevant native Gate.

The default pytest collection is the frozen unified release-candidate contract suite. Additional
historical mechanism tests remain under `tests/` for version-specific audit; run the relevant files
explicitly when changing an archived mechanism or snapshot.

Never commit `.env.local`, model credentials, private game source, Unity caches, or bulk generated
run directories. Curated public showcase evidence belongs under `showcases/` and must include its
evidence class and receipt. New benchmark claims must state whether the adapter is a deterministic
control or a real model and must retain the exact release commit, model/profile, reasoning effort,
task manifest, engine/evaluator version, retry policy, judge model/transport, and raw
machine-readable result. Do not add duplicate source trees or historical version snapshots to the
public repository.

Please keep changes scoped, add tests for contract behavior, and document any external benchmark
commit or dataset version used by an adapter. Engine adapters should stay thin: do not add
task-specific routes, mandatory tool sequences, hidden-answer feedback, or automatic repair policy
to the common solver loop.
