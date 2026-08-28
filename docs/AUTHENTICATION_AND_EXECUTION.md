# Authentication and execution contracts

Authentication and execution authority are independent selections.

## Authentication

`codex-subscription` starts the configured local Codex executable non-interactively. The
executable uses its own saved ChatGPT/Codex login. This path does not expose an API key and
must not be described as API usage.

`openai-responses-api` sends requests to the configured Responses API endpoint using the
environment variable named by `api_key_env` (normally `OPENAI_API_KEY`). API requests use
the API account's normal usage billing. The credential is held only by the Host HTTP
adapter; tool subprocess environments do not receive it.

Both backends implement one common operation: run an open model session in a complete
disposable workspace and return a final message, usage, transport metadata, and passive
tool audit. Benchmark and evaluator code does not depend on which backend was selected.

## Unified engine entrypoint

`gameforge run-game-workspace --engine auto|unity|godot` is the public project runner.
Auto-selection uses only standard engine project markers. Project copying, solver/backend
selection, execution profile, budgets, receipts, final result semantics, and cleanup are
shared. Engine adapters contain only what cannot honestly be generic: project detection,
the pinned executable, command alias, and engine-native final evaluator.

The legacy `run-unity-workspace` command is a compatibility alias into this same pipeline.
Unity is therefore an adapter, not a separately orchestrated product. No adapter may add a
task router, required Editor method, fixed tool order, intermediate acceptance gate, or
automatic repair.

## Execution profiles

`native-open` is the default because it best represents GameForge's motivation: preserve
the native model's ability to inspect, code, use the official engine, create its own helper
scripts, and choose when and how to validate. The project copy is disposable and audited,
but Host reads, network, and execution are not a security boundary.

`supervised-native` uses workspace-restricted general tools and a transparent broker for a
pinned Host engine. The broker:

- accepts arbitrary model-selected string arguments without rewriting them;
- accepts cwd only inside the disposable workspace;
- preserves stdout, stderr, exit code, and a single model-selected attempt;
- removes unrelated API credentials from the engine process environment;
- enforces a timeout, owns the process group, and emits content-free receipts.

It does not select an Editor method, add retries, block a known argument combination, or
decide that an engine invocation is required. Generated engine code still executes on the
Host and therefore remains trusted-code operation.

`strong-isolated` delegates the full command/workspace boundary to a separately configured
runner. GameForge validates the protocol and its `strong` declaration before use. Operators
remain responsible for selecting a real VM/container/remote implementation whose mounts,
network, secrets, Unity licensing, caches, and teardown satisfy their threat model.

## Evidence wording

Development may use a subscription-backed run to validate the common Harness chain. Such
evidence proves the subscription backend and shared workspace/engine/evaluator layers. It
does not prove service-side Responses API parity. Conversely, mocked API transport tests
prove request/tool/error semantics but do not establish model quality, latency, or cost.
