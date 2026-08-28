# External strong-isolation runner protocol (v1)

Configure `execution_profile: strong-isolated` and set `isolation_runner` to an executable.
The Harness refuses the profile when the runner is missing or its description is invalid.

## Preflight

The Harness invokes:

```text
RUNNER describe --json
```

The runner must return exit code 0 and one JSON object:

```json
{
  "protocol": "gameforge-isolation-runner-v1",
  "isolation": "strong",
  "operations": ["shell", "workspace-agent"]
}
```

Only the operation needed by the selected backend is required. A declaration is a provider
attestation, not proof by GameForge; deployment documentation should identify the concrete
VM/container/remote implementation and its security controls.

## Invocation

The Harness uses one of:

```text
RUNNER shell --protocol gameforge-isolation-runner-v1 \
  --workspace ABSOLUTE_WORKSPACE --timeout-seconds N -- COMMAND...

RUNNER workspace-agent --protocol gameforge-isolation-runner-v1 \
  --workspace ABSOLUTE_WORKSPACE --timeout-seconds N -- COMMAND...
```

The runner must make the disposable workspace available at the supplied absolute path (or
provide an equivalent transparent path mapping), run only the command after `--`, stream
stdout/stderr, preserve the command exit code, terminate descendants on timeout, and make
committed workspace changes visible to the Host when it exits.

For Unity, the isolated environment must provide its licensed pinned Editor as `unity`.
Engine licensing, Package Manager state, caches, network, and generated C#/native plugins
must remain inside the per-task isolation boundary.
