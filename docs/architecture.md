# Architecture

GameForge separates an open implementation session from an independent delivery decision.

```text
Game brief / change request / benchmark task
                    │
                    ▼
          complete disposable project
                    │
       ┌────────────┴────────────┐
       ▼                         ▼
Codex subscription       OpenAI Responses API
       └────────────┬────────────┘
                    ▼
             open solver session
     files · shell · images · official engine
                    │
                    ▼
           complete game project
                    │
                    ▼
       solver-invisible engine evaluator
      build · launch · replay · logs · evidence
                    │
                    ▼
        result · receipt · failure attribution
```

## Design boundary

The solver owns exploration and implementation. It may inspect the full project, create helpers,
choose tools, call the official engine, examine real feedback, revise its approach, and decide when
it is finished. GameForge does not require a task router, fixed tool sequence, intermediate
acceptance loop, or harness-authored repair pass.

The harness owns the boundaries around that session:

- copy the complete project into a disposable workspace;
- select the model backend and execution profile;
- resolve and supervise the requested engine where applicable;
- record passive lineage, usage, timing, and process evidence;
- run a separate engine-native evaluator after the solver exits;
- preserve build, launch, replay, screenshot, log, and receipt evidence;
- distinguish solver, engine, host, evaluator, and playability failures.

## One pipeline, thin engine adapters

Workspace lifecycle, model backends, authority profiles, budgets, receipts, result semantics, and
cleanup are engine-neutral. An adapter contains only the parts that cannot honestly be generic:

| Adapter responsibility | Unity | Godot |
| --- | --- | --- |
| Project detection | `ProjectSettings/ProjectVersion.txt` | `project.godot` |
| Engine resolution | pinned Unity Editor | pinned Godot executable |
| Final evaluation | import, compile, build, Player launch | import, scene/runtime probe, replay |

Additional engines should extend these boundaries rather than introduce another solver workflow.

## Backends and authority

Authentication and execution authority are independent. A run may use a saved Codex subscription
login or the OpenAI Responses API, combined with `native-open`, `supervised-native`, or an external
`strong-isolated` provider. See [Authentication and execution](AUTHENTICATION_AND_EXECUTION.md) and
[Security](security.md).

## Result classes

The same pipeline supports two evaluation modes:

- **Benchmark results** use frozen tasks and predefined evaluators or rubrics for repeatable,
  quantitative comparison.
- **Open-ended results** begin with a broad natural-language game brief and an empty or minimal
  project, then judge the delivered artifact through independent engine gates and gameplay evidence.

Neither mode changes the solver into a prescribed workflow. Only the goal and final evaluation
contract differ.

## Non-claims

Independent gates can establish that a project imports, builds, launches, receives an input trace,
and changes state. They do not prove subjective fun, commercial quality, complete playthrough
coverage, or safety against hostile generated code.
