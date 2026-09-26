# jev-pi-token-reduction

A [Pi](https://www.npmjs.com/package/@earendil-works/pi-coding-agent) extension that uses TypeSafe's **Jev**
decision model to trim what file reads, searches, and command logs put into the model's context, before the
model sees them. Omitted text is never lost: every cut leaves a marker, and the agent can recover the exact
original with the `jev_expand` tool.

On held-out source-reading tasks it cut list-price cost by **about 15%** with no loss of accuracy, at the price
of about **40% more latency**. It helps when the agent reads whole files to answer narrow questions; it does
nothing when the agent already retrieves only what it needs.

## Results

Pi 0.84.2 with `gpt-6-sol`, 30 held-out tasks (90 behavioral cases over the CPython `shlex`, `fractions`,
`statistics`, `ipaddress`, `calendar`, and `pprint` modules), with and without the extension, 3 repeats,
paired over 90 (task, repeat) runs. Costs are list prices and include Jev's own cost.

| Metric | With Jev vs without | 95% bootstrap interval |
| --- | ---: | --- |
| **List cost** | **-14.9%** | -18.8% to -10.9% |
| Cache writes (new context, the expensive part) | -22.3% | -26.2% to -18.1% |
| Total tokens | -8.8% | -17.1% to +1.7% |
| Model calls | +7.7% | +2.1% to +14.1% |
| Latency | +39% | +31% to +49% |
| Tasks answered correctly | 80/90 vs 78/90 | |

The decision threshold was tuned on a different set of 30 tasks (six other modules), where the saving was
-16.5%; the held-out result above uses the same setting with no re-tuning.

Where it **did not** help (same harness, measured): debugging sessions that run test suites and edit code, a
PR-review report task, and a docs-indexing pipeline. In all three the agent already read narrowly or piped
large output through its own scripts, so Jev had almost nothing to remove.

## How it works

1. A `read`, `grep`, or `bash` call finishes. Results over 1,500 tokens that the policy allows are
   split into chunks: Python by function and class, search output by file, logs by traceback.
2. Jev rates each chunk's needed visibility for the current task and step: hide, outline, condensed, or full.
3. Each chunk is shown at the most compressed level whose probability of needing more is at most
   `omit_risk` (0.2). Uncertain, failed, or unscored chunks stay in full.
4. Repeated tracebacks in test logs collapse to one line each without asking Jev.
5. The pruned result replaces the original once, when it arrives, so the conversation stays append-only and
   prompt caching keeps working. Originals are stored locally in SQLite for `jev_expand`.

Any router error keeps the original output.

## Requirements

- Pi 0.84.x (tested with 0.84.2)
- Python 3.10+ (no third-party runtime dependencies)
- A Jev API key. By default requests go to the Vercel AI Gateway (`https://ai-gateway.vercel.sh/v1/evaluate`,
  model `typesafe-ai/jev`); set `AI_GATEWAY_API_KEY`, or put the key in `~/.jev_key`. For TypeSafe directly,
  set `JEV_API_URL=https://api.typesafe.ai/v1/systemone`, `JEV_MODEL=jev-latest`, and `TYPESAFE_API_KEY`.
- Node.js only if you want to type-check the extension (`npm --prefix adapters/pi ci && npm --prefix adapters/pi run check`)

## Install and run

```bash
git clone https://github.com/auschoi96/jev-pi-token-reduction.git
cd jev-pi-token-reduction
python3 -m venv .venv                 # keep this project's packages out of shared or conda base Pythons
.venv/bin/pip install -e .            # or -e '.[mlflow]' for optional MLflow tracing
cp jev.config.example.json jev.config.json
# Edit jev.config.json: set "allow_roots" to the absolute paths of the repositories whose files may be sent to Jev.
```

Then start Pi with the extension:

```bash
JEV_ROUTER_POLICY=$PWD/jev.config.json JEV_ROUTER_PYTHON=$PWD/.venv/bin/python \
  pi -e $PWD/adapters/pi/extension.ts --jev arrival
```

or `make pi` (the `make` targets use `.venv/bin/python` when it exists). The extension works with any Pi model
provider: point Pi at an existing provider configuration with `PI_CODING_AGENT_DIR=<dir containing models.json>`,
or, if a launcher starts Pi for you and forwards extra arguments and the environment, append
`-e <path>/adapters/pi/extension.ts --jev arrival` to its command. The `--jev` flag sets the mode for that run and overrides the `JEV_ROUTER_MODE` environment
variable; an unknown value falls back to `off`. In Pi, `/jev off|shadow|arrival` switches modes during a session.

| `--jev` / `JEV_ROUTER_MODE` | Behavior |
| --- | --- |
| `arrival` | **Recommended.** Prune each result once when it arrives. This is the measured mode. |
| `shadow` | Default. Score and log proposed cuts, but show everything unchanged. |
| `off` | No Jev calls and tool output unchanged, but token usage is still recorded, so runs can be compared. |
| `dynamic`, `cache_aware` | Experimental: reconsider earlier results before each model call. Rewriting history can defeat prompt caching; `dynamic` cost more in our tests. |

Other environment variables: `JEV_ROUTER_DB` (default `~/.local/state/jev-router/router.sqlite`),
`JEV_ROUTER_PYTHON` (interpreter with this package installed), `JEV_GATEWAY_ONLY` (gateway provider pin,
default `typesafe-ai`). Experimental and off by default: `JEV_ROUTER_COMPACTION=jev` (cost more in our
tests) and `JEV_DELEGATE_MODEL` (a cheaper read-only sub-agent tool).

## Compare with and without Jev on your own workload

Run the same task with Jev off and on, with [MLflow tracing](#optional-trace-pi-runs-to-mlflow-on-databricks-unity-catalog)
enabled:

```bash
pi -e $PWD/adapters/pi/extension.ts --jev off     "<your task>"
pi -e $PWD/adapters/pi/extension.ts --jev arrival "<your task>"
```

Each run's root span (`pi_agent_run`) records the mode as `pi.mode`, alongside its token totals, list cost
including Jev, and retrieved tokens before and after Jev, so the runs can be compared in the MLflow UI or with
SQL on the `<prefix>_otel_spans` table. Start each comparison in a fresh session, and compare several runs per
mode: model runs vary by 20% or more on identical prompts.

## Configuration

All settings live in one JSON file (`JEV_ROUTER_POLICY`); every key is a field of `jev_router/policy.py`, and
unknown keys are rejected. The main ones:

| Key | Default | Effect |
| --- | --- | --- |
| `allow_roots` | `[]` | Absolute roots whose `read` results may be sent to Jev. Empty means nothing is scored. |
| `allow_commands` | `false` | Also route shell and search output (it can contain data from anywhere). |
| `min_tokens` | `1500` | Smaller results are never scored. |
| `omit_risk` | `0.2` | Higher cuts more aggressively. |
| `min_confidence` | `0.0` | Optional floor on Jev's self-reported confidence. |
| `dedupe_tracebacks` | `true` | Collapse repeated tracebacks in command output. |
| `retries`, `timeout_seconds`, `result_budget_seconds` | `3`, `8`, `20` | Jev call limits; on failure the original is kept. |

## What is sent to Jev

For allowed results only: the chunk text, its path and line range, your task prompt, the agent's public text
and tool call for the current step, and the error and summary lines of the last few permitted command
results. Private model reasoning is never sent. Files outside `allow_roots`, common credential paths
(`.env`, `.ssh`, `.aws`, key files, and similar), and text matching basic secret patterns (API keys, private
keys, JWTs) are withheld. This is a disclosure policy with basic safeguards, not a complete data-loss-prevention
system. The local SQLite store holds original tool output; treat it like an agent transcript.

## Limits

- **Latency** rises about 40%: each large result waits for Jev scoring.
- **Rate limits:** through the Vercel AI Gateway's shared credentials, Jev allowed about 30 requests per
  30-60 second window in our measurements. Large sessions can exceed that; unscored chunks are simply kept.
  Your own TypeSafe key may have different limits.
- **Workload-dependent:** savings come from agents over-reading. Agents that grep first and read small ranges
  see little benefit.
- Measured with one model (`gpt-6-sol`) through one harness; other models may read differently.

## Optional: trace Pi runs to MLflow on Databricks (Unity Catalog)

MLflow has no built-in Pi integration, so the extension builds the traces itself. After each Pi run
settles, the router turns the events it already recorded into one MLflow trace:

```
pi_agent_run (AGENT)      prompt, totals: tokens, list cost (incl. Jev), retrieval tokens before/after
├─ model_call (LLM)       token usage (mlflow.chat.tokenUsage), model, cache reads/writes, list cost
├─ read (TOOL)            tokens before/after Jev, hidden chunks, Jev calls and failures
│  └─ jev_visibility      per-level chunk counts, Jev latency
└─ model_call (LLM)       ...
```

Traces are stored in Unity Catalog following
[Store MLflow traces in Unity Catalog](https://docs.databricks.com/aws/en/mlflow3/genai/tracing/trace-unity-catalog).
Tracing runs after the agent finishes each run, and a tracing failure never affects the agent.

**Requirements:** a Unity Catalog workspace, a SQL warehouse you can use, `USE CATALOG`, `USE SCHEMA`, and
`CREATE TABLE` on the destination, and a Databricks CLI profile.

**Set up once:**

```bash
.venv/bin/pip install -e '.[mlflow]'           # MLflow >= 3.14, in the venv Pi's worker uses
.venv/bin/python -m jev_router.mlflow_setup setup \
  --profile <PROFILE> --catalog <CATALOG> --schema <SCHEMA> --warehouse <SQL_WAREHOUSE_ID>
```

Install into a dedicated virtual environment, not a shared or conda base Python: the MLflow extra can upgrade
NumPy underneath older compiled packages (for example `pyarrow` older than 16), which then print "A module that
was compiled using NumPy 1.x cannot be run in NumPy 2..." at import or fail outright.

or `make mlflow-setup PROFILE=... CATALOG=... SCHEMA=... WAREHOUSE=...`. This:
- checks your auth and the warehouse, and creates the schema if needed;
- creates the experiment `/Users/<you>/jev-pi-traces` (override with `--experiment`) bound to the Unity
  Catalog location, which creates `<prefix>_otel_spans`, `_otel_logs`, `_otel_metrics`, and
  `_otel_annotations` (prefix `pi` by default; override with `--prefix`);
- writes `jev.mlflow.env` with `MLFLOW_TRACKING_URI`, `DATABRICKS_CONFIG_PROFILE`,
  `JEV_MLFLOW_EXPERIMENT_ID`, `MLFLOW_TRACING_SQL_WAREHOUSE_ID`, and `JEV_MLFLOW_CONTENT`.

**Use it:** `source jev.mlflow.env`, then start Pi with the extension as usual, with `JEV_ROUTER_PYTHON`
set to the venv's interpreter (`make pi` does this). Check that traces arrived with
`.venv/bin/python -m jev_router.mlflow_setup verify` (or `make mlflow-verify`), or open the experiment's
**Traces** tab and choose the SQL warehouse.

**Notes:**
- By default only metadata is logged (token counts, costs, tool names, sizes, Jev decisions). Set
  `JEV_MLFLOW_CONTENT=full` to also log the prompt, assistant text, tool arguments, and the tool output the
  model saw. Unity Catalog tables are readable by anyone you grant access to.
- `DATABRICKS_CONFIG_PROFILE` is pinned because parts of MLflow build their own Databricks client from the
  default profile; if that points at another workspace, traces are dropped silently.
- An experiment's Unity Catalog binding is permanent. Re-running `setup` reuses a matching experiment and
  refuses one bound elsewhere.
- For other users to write traces, grant `USE CATALOG`, `USE SCHEMA`, and `MODIFY` and `SELECT` on each table.
- Unity Catalog trace ingestion is limited to 200 traces per second per workspace.
- Only tested with Databricks as the tracking server.

## Tests and benchmark

```bash
make test        # unit tests; no network
```

The held-out benchmark (`eval/`) runs Pi against a Databricks AI Gateway profile from `~/.databrickscfg`:

```bash
python3 eval/run_native_pilot.py --profile <PROFILE> --model gpt-6-sol \
  --oracle-python "$(command -v python3.12)" --modes off,arrival \
  --tasks-module eval/native_tasks_heldout.py --output results/heldout-r1
python3 eval/summarize_native_pilot.py results/heldout-r1
```

Expected answers come from running each case with the oracle interpreter (the reported run used CPython
3.12.5); a different CPython version can change them.

## Background

The design follows the "visibility ladder" idea from *Jev Engineering for Coding Agents*, an independent
synthesis of design notes by Diogo Almeida (TypeSafe). This project is not affiliated with or endorsed by
TypeSafe.

## License

MIT
