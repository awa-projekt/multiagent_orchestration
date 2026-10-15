# Scenario 7: template to application

**Process.** `awa build` turns a plain-language description of a process into a tested project: it
writes acceptance cases and sample data first, designs the agents, evaluates them on a local workspace
and repairs the design in bounded rounds. This scenario hands it the description of the document intake
of [scenario 4](../04_document_intake/README.md) (extract, validate against the master data and the
order, book, an approval above 10,000 EUR, hold with a reason) and then uses the project it wrote.

**Capabilities.** `awa.blueprints.build_project` (the API behind `awa build`): rounds, pass rate and
why the build stopped; the written project validates with `awa validate` and answers a case of its own
when run from its folder.

| Check | Meaning |
| --- | --- |
| `build_passed` | the build reached its target pass rate on its own acceptance cases |
| `project_validates` | `awa validate blueprint.yaml` of the written project exits 0 |
| `approval_in_design` | the approval step above the limit is part of the design |
| `project_runs` | the written project is loaded from its folder and answers its first eval case |

The results record the rounds, the pass rate and the reason the build stopped (`target_reached`, rounds
or usage limit used up) next to the scores. Model calls of the build and of the run count towards the
ledger; the build is bounded by the run's call cap (`--max-run-calls`, default 150).

```console
awa build "<the description of cases.yaml>" --out intake_desk     # what the case does by hand
python examples/benchmarks/core/run.py --live --only 7 --runs 1
```
