# Security model and deployment boundaries

GameForge intentionally separates solver authentication from execution authority. Selecting a
subscription or API backend does not select a sandbox, and selecting an execution profile does not
change which model provider is used.

## Assets being protected

- the user's original game project and unrelated host files;
- model/API credentials and saved subscription authentication;
- integrity of pinned engine and evaluator binaries;
- correctness of receipts, provenance, and final verdicts;
- availability of the host after timeout, output flooding, child-process leaks, or engine crashes;
- private evaluator data that must not influence the solver trajectory.

## Trust assumptions

The default `native-open` path assumes the model, task, project, dependencies, and generated code
are trusted enough to run with the current user's host authority. The project is copied into a
disposable workspace and audited, but that copy is not an OS sandbox. A malicious process can still
attempt to read other host files, use the network, access inherited user services, or spawn child
processes outside the workspace.

Use `strong-isolated` with a reviewed external provider when any of those inputs are untrusted.

## Execution profiles

### `native-open` — default

Purpose: preserve the model's native game-development ability.

- The solver receives a complete disposable project.
- It may use normal shell, coding, image, process, and pinned engine capabilities.
- GameForge does not rewrite the model's tool order, engine arguments, stopping policy, or repair
  strategy.
- Commands and generated engine code execute with host-user authority.
- Independent evaluators run only after the solver session, but they also execute the generated
  artifact with host authority.

This profile provides artifact separation, lifecycle management, passive receipts, and independent
verification. It does **not** provide process, network, credential, or kernel isolation.

### `supervised-native`

Purpose: keep an open workflow while reducing engine executable ambiguity and lifecycle leakage.

- General work remains scoped to the disposable workspace.
- Only the pinned engine executable is delegated through a transparent host broker.
- Model-selected argv, cwd, stdout, stderr, exit code, timeout, and single-attempt semantics are
  preserved.
- The broker does not add task-specific argument guards, hidden retries, or mandatory validation.
- Credentials are removed from delegated subprocess environments.

This is a provenance and lifecycle boundary, not a strong code sandbox. Generated Unity C# and
Godot scripts execute inside the host engine and must still be trusted.

### `strong-isolated`

Purpose: execute untrusted projects or generated code inside a separately administered boundary.

- The deployer configures an external VM, container, or remote runner.
- GameForge calls `RUNNER describe --json` and requires the
  `gameforge-isolation-runner-v1` protocol, `isolation: strong`, and the operation required by the
  selected backend.
- Missing, invalid, or insufficient attestation fails closed; it never silently falls back to the
  host.
- The provider is responsible for filesystem, process, network, secret, resource, and cleanup
  guarantees.

The declaration is a provider attestation, not independent proof by GameForge. Review the concrete
provider before processing hostile input. See [ISOLATION_RUNNER_PROTOCOL.md](ISOLATION_RUNNER_PROTOCOL.md).

## Credential handling

- Never store live credentials in repository YAML, prompts, traces, game assets, reports, diffs, or
  build output.
- API profiles name an environment variable such as `OPENAI_API_KEY`; the key remains in the host
  HTTP adapter and is removed from model tool subprocess environments.
- Subscription profiles invoke a configured local agent executable that uses its own saved login.
  This does not create, reveal, or emulate an API key.
- `.env`, `.env.local`, `.env.*.local`, and `model.local.yaml` are ignored by Git.
- Error messages and receipts should contain model identity, hashes, usage, timing, and status—not
  prompt bodies, response bodies, or credentials.
- If a key is suspected to be exposed, revoke it before collecting or sharing sanitized evidence.

## Project and evaluator integrity

- Runs operate on explicit project roots and record before/after state.
- The original project is not the solver's disposable workspace.
- Engine detection fails closed for unknown or ambiguous project markers.
- Pinned engine resolution and hashes are recorded where the adapter supports them.
- Private evaluator logic and acceptance data are attached or executed after the solver session.
- A solver statement that the game works is never sufficient for a final PASS.
- Runtime/build evidence, interaction evidence, and manual playability review remain separate.
- Benchmark claims must retain the exact code snapshot, model/reasoning setting, task manifest,
  engine/evaluator version, retries, judge model, and judge transport.

## External assets and dependencies

- Record source and license for externally acquired game assets.
- Treat package installers, model-created scripts, engine plugins, Unity packages, Godot addons,
  and project-local agent instructions as executable or influential code.
- Do not expose private game repositories to public benchmark or showcase tooling.
- Unity account login, license acceptance, administrator prompts, Gatekeeper approvals, Accessibility,
  and Screen Recording permissions remain explicit human actions.
- Publishing or uploading a generated build is outside the automated workflow.

## Availability and cleanup

The harness has bounded timeouts, output limits, process-group cleanup, and receipts for supported
paths, but no local mechanism can guarantee recovery from every engine, GPU-driver, kernel, or host
service failure. A strong external provider should enforce its own CPU, memory, disk, process,
network, and wall-clock quotas and destroy the complete environment after each task.

## Non-claims

GameForge does not currently claim:

- `native-open` or `supervised-native` is safe for hostile generated code;
- a disposable directory is equivalent to a VM or container;
- the external runner attestation proves a provider's implementation;
- independent evaluation proves subjective fun or commercial quality;
- Unity/Godot engine execution is free of platform-level vulnerabilities;
- secrets are safe if users deliberately place them inside the model workspace.

Report security issues privately using the process in [../SECURITY.md](../SECURITY.md).
