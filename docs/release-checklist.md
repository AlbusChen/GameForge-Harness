# Release checklist

## Code and package

- [ ] The repository root is the only distributed GameForge source tree.
- [ ] `uv sync --locked --all-groups`, Ruff, pytest, shell validation, and `uv build` pass.
- [ ] A clean environment installs the wheel and exposes `gameforge` and `run-game-workspace`.
- [ ] Subscription and Responses API example profiles parse without embedded credentials.
- [ ] Unity and Godot adapters remain thin extensions of the common workspace pipeline.

## Documentation and evidence

- [ ] README claims match the current implementation and exact report limitations.
- [ ] Documentation contains no dead links, internal absolute paths, obsolete candidate names, or
  unreleased implementation plans.
- [ ] Benchmark reports state model, task set, evaluator, denominator, exclusions, retries, and
  comparison limits.
- [ ] Open-ended reports separate build/launch, input replay, and manual playability evidence.
- [ ] Current receipts, videos, traces, and checksums validate against their published artifacts.

## Security and privacy

- [ ] No tracked file contains a live token, private key, local credential file, or private path.
- [ ] `native-open` is described as Host-authority execution, not an OS sandbox.
- [ ] `strong-isolated` fails closed when no valid external provider is configured.
- [ ] Generated projects, engine plugins, dependencies, and external assets retain their trust and
  license disclosures.

## GitHub

- [ ] CI passes on the release commit.
- [ ] The version, changelog, release digest, tag, and release notes agree.
- [ ] The homepage shows benchmark and open-ended results with direct links to current evidence.
