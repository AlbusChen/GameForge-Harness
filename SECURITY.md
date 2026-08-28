# Security policy

## Supported versions

Only the latest commit on `main` is distributed and supported during the alpha phase. Historical
experiment labels and reports are not alternate security-supported releases.

## Reporting a vulnerability

Use GitHub private vulnerability reporting for this repository. Do not open a public issue for
credential exposure, path traversal, unsafe project mutation, sandbox escape, runner-attestation
bypass, command-boundary confusion, or engine-process isolation flaws.

Include the affected commit, execution profile, solver backend, operating system, minimal
reproduction, impact, and suggested containment. Never attach live tokens, `.env.local`, private
game projects, or unsanitized run artifacts. If a credential may have been exposed, revoke it with
the provider before sharing a sanitized report.

## Security model in one page

GameForge has three execution profiles with deliberately different guarantees:

| Profile | Intended use | Security boundary |
| --- | --- | --- |
| `native-open` | Trusted model, task, and project; maximum native game-development capability | **Not an OS sandbox.** Model commands and generated engine code run with the host user's authority inside a disposable project copy. |
| `supervised-native` | Local execution with pinned engine provenance and lifecycle supervision | General work remains workspace-scoped and the pinned engine crosses a transparent host broker. Generated Unity C# or Godot scripts still execute as trusted host code. |
| `strong-isolated` | Untrusted projects or generated code | Delegates execution to a user-supplied VM/container/remote runner. GameForge validates a fail-closed protocol attestation, but the deployer must assess the concrete provider. |

The disposable workspace protects the original project from ordinary mutation and supports
reproducibility; it is not, by itself, a privilege or confidentiality boundary. Independent
evaluators reduce false success claims, but they also execute generated game artifacts and require
an appropriate execution profile.

Credentials for the Responses API remain in the host adapter and must not be passed into model
shell environments. The Codex-subscription backend uses the configured executable's saved login and
must not be represented as API-key authentication.

For the full threat model, credential rules, execution contracts, and known limits, read:

- [Detailed security model](docs/security.md)
- [Authentication and execution contracts](docs/AUTHENTICATION_AND_EXECUTION.md)
- [External strong-isolation runner protocol](docs/ISOLATION_RUNNER_PROTOCOL.md)
