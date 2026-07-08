# GPT-5.3-Codex-Spark routing policy

Spark is a fast coding worker, not the default architect.

## Route to Spark

Use `openai-codex:gpt-5.3-codex-spark` for bounded, testable coding work:

- small local patches touching roughly <= 8 files;
- unit-test, lint, typecheck, import, and formatting loops;
- UI/layout variants where speed matters more than deep reasoning;
- repo-local Q&A and implementation sketches;
- subagent workers where a frontier model already decomposed the task.

## Keep frontier / reviewer models

Use `gpt-5.5`, GPT-5.3-Codex full, or Claude Opus for:

- architecture, product, security, auth, billing, or data-model changes;
- multi-repo migrations;
- ambiguous debugging with weak reproduction;
- final review before merge when Spark edited code.

## Escalation rules

Escalate away from Spark if it:

- touches more than 8 files;
- fails verification twice;
- changes security, auth, permissions, billing, migrations, or data boundaries;
- expands scope beyond the prompt;
- cannot produce a clean verifier run.

## Decision rule

Spark becomes default for `fast_code` only if local evals show:

- quality >= 90% of `gpt-5.5` on the same cases;
- end-to-end time-to-green >= 2x faster;
- no meaningful increase in manual review burden or regression rate.

If it is only faster at streaming tokens but produces more cleanup work, keep it opt-in. Fast slop is still slop.

## Live eval isolation

Run live evals through `scripts/model_routing_eval.py --live`. The runner creates isolated git worktrees under `tmp/model-routing-eval/...`; do not run live model edits in the developer checkout.
