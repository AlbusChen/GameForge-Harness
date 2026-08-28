# Troubleshooting

## The solver backend is unavailable

Run `gameforge model-check --profile CONFIG --json`.

- For `codex-subscription`, verify that the configured Codex executable exists and is already signed
  in. This backend does not use an API key.
- For `openai-responses-api`, set the environment variable named by `api_key_env`—normally
  `OPENAI_API_KEY`—without writing it into repository configuration.

## The engine cannot be detected

`--engine auto` requires an unambiguous standard project marker: `project.godot` for Godot or
`ProjectSettings/ProjectVersion.txt` for Unity. Otherwise select `--engine godot` or
`--engine unity` explicitly and verify the project root.

## Unity import or build fails

Confirm the installed Editor matches the project version and that Unity licensing and Package
Manager access work for the current host user. Preserve the Editor log. A generated-project compile
error is a normal task failure; a licensing, IPC, or missing-Editor error is an environment failure.

## Godot exits unexpectedly

Record the exact executable, version, arguments, exit code, stderr, and whether the command combined
headless execution with movie capture. Do not classify the final game as failed until the independent
evaluator runs against the unchanged project.

## `strong-isolated` refuses to start

This profile fails closed unless the configured runner implements `gameforge-isolation-runner-v1`,
declares `isolation: strong`, and supports the required operation. See
[the runner protocol](ISOLATION_RUNNER_PROTOCOL.md).

## The solver says the game works but the run fails

The independent evaluator is authoritative. Check the receipt to distinguish build, launch, replay,
runtime, evaluator, and host failures. Solver self-report is never sufficient for a final PASS.
