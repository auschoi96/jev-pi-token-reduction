# Databricks quickstart: test Jev token reduction with Pi on a Databricks AI Gateway

This repo measures how much Jev trims from Pi's context. This guide takes you from clone to a running,
Jev-enabled Pi session backed by a **Databricks AI Gateway**, with no dependency on `ucode` or `ug`.

There are **two independent things to authenticate**, and you need both:

1. **The Jev decision model** — the extension sends allowed tool output to Jev for scoring (Vercel AI
   Gateway or TypeSafe). Steps 2–3.
2. **Pi's model provider** — the coding model Pi talks to (here, your Databricks AI Gateway). Step 4.

If you don't use Databricks, do steps 1–3 and 5, and configure Pi's own `models.json` (e.g. an Anthropic
key) instead of step 4 — `make pi` leaves your Pi config untouched when `pi-agent/` doesn't exist.

## Prerequisites

- **Pi 0.84.x** (`pi --version`) and **Python 3.10+**.
- The **Databricks CLI** on `PATH` (`databricks --version`), with a **profile** in `~/.databrickscfg` for a
  workspace that has the AI Gateway coding-agent routes (`/ai-gateway/anthropic`, `/ai-gateway/codex/v1`)
  enabled.
- A **Jev API key** (see step 2).

## 1. Install the repo

```bash
git clone https://github.com/auschoi96/jev-pi-token-reduction.git
cd jev-pi-token-reduction
python3 -m pip install -e .
```

## 2. Give the extension a Jev API key

The extension calls the Jev model to decide what to trim. By default it uses the Vercel AI Gateway
(`https://ai-gateway.vercel.sh/v1/evaluate`, model `typesafe-ai/jev`):

```bash
export JEV_API_KEY=<your key>            # or: echo "<your key>" > ~/.jev_key
```

To use TypeSafe directly instead:

```bash
export JEV_API_URL=https://api.typesafe.ai/v1/systemone
export JEV_MODEL=jev-latest
export TYPESAFE_API_KEY=<your key>
```

## 3. Configure what Jev may see (`jev.config.json`)

```bash
cp jev.config.example.json jev.config.json
```

Then edit `jev.config.json` and set **`allow_roots`** to the absolute path(s) of the repositories whose
file reads may be sent to Jev:

```json
"allow_roots": ["/Users/you/code/the-repo-you-are-working-in"],
```

> **This is required for Jev to do anything.** `allow_roots` defaults to empty, and with it empty nothing is
> scored — Jev stays a no-op no matter which mode you run. Set it to the repo you'll actually be reading
> files from.

Other keys (`omit_risk`, `min_tokens`, `allow_commands`, …) are documented in the main README's
Configuration table; the defaults are fine to start.

## 4. Point Pi at the Databricks AI Gateway

Authenticate the CLI once per workspace if you haven't:

```bash
databricks auth login --host https://<your-workspace>.cloud.databricks.com --profile myprofile
databricks auth token --profile myprofile --output json    # sanity check: prints JSON with access_token
```

Generate Pi's provider config:

```bash
make pi-setup PROFILE=myprofile                              # or add MODEL=system.ai.claude-sonnet-5
```

This writes a private `pi-agent/` directory (`models.json` + `settings.json`) with `databricks-openai`
(codex) and `databricks-claude` providers pointing at your workspace's gateway. Each request mints a fresh
OAuth token via `databricks auth token` — nothing that expires is stored.

## 5. Run Pi with the Jev extension

The simplest way — this runs Jev **and** wires in the Databricks config:

```bash
make pi                     # MODE=arrival by default; e.g. make pi MODE=off
```

`make pi` is exactly the raw command below plus `PI_CODING_AGENT_DIR` (set when `pi-agent/` exists) and
`JEV_ROUTER_PYTHON`:

```bash
PI_CODING_AGENT_DIR=$PWD/pi-agent \
JEV_ROUTER_POLICY=$PWD/jev.config.json \
JEV_ROUTER_PYTHON=$(command -v python3) \
  pi -e $PWD/adapters/pi/extension.ts --jev arrival
```

> Yes — `pi -e $PWD/adapters/pi/extension.ts --jev arrival` is what loads Jev. The extra bits matter:
> `JEV_ROUTER_POLICY` points Jev at your config, `JEV_ROUTER_PYTHON` ensures the router runs in the
> interpreter where you `pip install`ed this package (important if you use a venv), and
> `PI_CODING_AGENT_DIR` is what makes Pi use the Databricks gateway. Drop `PI_CODING_AGENT_DIR` and you're
> back to the `401 Credential was not sent…` error. Prefer `make pi` so you don't have to remember them.

`--jev` sets the mode for the run (overrides `JEV_ROUTER_MODE`); an unknown value falls back to `off`. Modes:

| `--jev` / `MODE` | Behavior |
| --- | --- |
| `arrival` | **Recommended.** Prune each result once when it arrives. This is the measured mode. |
| `shadow` | Default. Score and log proposed cuts, but show everything unchanged. |
| `off` | No Jev calls, output unchanged, but token usage is still recorded so runs can be compared. |
| `dynamic`, `cache_aware` | Experimental; can defeat prompt caching. |

In a session, `/jev off|shadow|arrival` switches modes live.

## 6. Confirm Jev is actually trimming

Run the same task once with Jev off and once on, and compare:

```bash
make pi MODE=off        # baseline
make pi MODE=arrival    # with Jev
```

Give each the same file-reading task (a task that reads whole files to answer a narrow question is where Jev
helps; grep-then-read-small sees little). When a `read`/`grep`/`bash` result over `min_tokens` inside an
`allow_roots` path is trimmed, you'll see a marker in the tool output and can recover the original with the
`jev_expand` tool. For quantitative comparison (token totals, list cost per run), enable MLflow tracing —
step 7.

## 7. (Optional) Trace runs to MLflow on Databricks

The extension can log each run as an MLflow trace in Unity Catalog, which is how the numbers in the README
were compared. This is independent of everything above: it works with or without the step-4 gateway, and
uses its own Databricks profile.

```bash
python3 -m pip install -e '.[mlflow]'                                 # MLflow >= 3.14
make mlflow-setup PROFILE=<profile> CATALOG=<catalog> SCHEMA=<schema> WAREHOUSE=<sql_warehouse_id>
make pi PY=.venv/bin/python                                           # auto-loads jev.mlflow.env (see caveats)
make mlflow-verify                                                    # confirm traces landed
```

`make pi` auto-loads `jev.mlflow.env` when it exists, so tracing works in any terminal without a manual
`source`. Two things still make tracing **silently produce nothing** (it never errors — a tracing failure
must not affect the agent):

- **`JEV_MLFLOW_EXPERIMENT_ID` isn't in the environment.** With `make pi` this is handled for you; but if
  you run Pi directly (`pi -e …` instead of `make pi`), you must `source jev.mlflow.env` first, or that
  terminal won't trace. The extension reads the flag once at startup, so a new terminal without it traces
  nothing — no error, just no trace.
- **MLflow isn't installed in the interpreter the router uses.** Tracing runs in the Python worker
  (`JEV_ROUTER_PYTHON`), not in Pi. If you installed `.[mlflow]` into a venv but `make pi` uses system
  `python3`, tracing is skipped — pass `make pi PY=.venv/bin/python`.

By default only metadata is logged. To capture full prompts, assistant text, tool arguments, and the tool
output the model saw, run `JEV_MLFLOW_CONTENT=full make pi` — a value set in your shell overrides the
`jev.mlflow.env` default, so you can toggle it per run.

The tracing profile is **separate** from step 4: `DATABRICKS_CONFIG_PROFILE` (from `jev.mlflow.env`) decides
where traces are written; your Pi provider uses the `PROFILE` you passed to `make pi-setup`. They can be
different workspaces. For the full setup — table layout, `JEV_MLFLOW_CONTENT=full`, grants, and the
permanent experiment binding — see the README's
[MLflow section](README.md#optional-trace-pi-runs-to-mlflow-on-databricks-unity-catalog).

## What `pi-setup` generates

`pi-agent/models.json` registers two providers pointing at your workspace's gateway:

| Provider | API dialect | Gateway path |
| --- | --- | --- |
| `databricks-openai` | `openai-responses` | `<host>/ai-gateway/codex/v1` |
| `databricks-claude` | `anthropic-messages` | `<host>/ai-gateway/anthropic` |

Each provider's `apiKey` is a Pi `!command` that Pi runs per request:

```
!databricks auth token --profile myprofile --output json | python3 -c 'import sys, json; print(json.load(sys.stdin)["access_token"])'
```

So the token is minted on demand and never stored. The `databricks-claude` block carries
`compat.supportsEagerToolInputStreaming: false`, which the gateway's Anthropic translator requires on Pi's
streaming + tools path.

## Choosing models

- **Session default:** pass `MODEL=...` to `make pi-setup`, or override per run with `pi --model ...`.
- **Available models are workspace-dependent.** The generated list is a reasonable starting set; edit
  `pi-agent/models.json` to match what your workspace's gateway exposes (list its serving endpoints / model
  services in the Databricks UI or CLI to see what's available).

## Switching workspace

Re-run `make pi-setup PROFILE=<other-profile>` (optionally `PI_AGENT=<dir>` to write elsewhere). To go back
to Pi's own configuration entirely, delete the directory:

```bash
rm -rf pi-agent
```

`pi-agent/` is git-ignored, so it never gets committed.

## Troubleshooting

- **`401 Credential was not sent or was of an unsupported type for this API`** — Pi reached the gateway with
  no token. Either `pi-agent/` doesn't exist (run `make pi-setup`), or you started `pi` directly without
  `PI_CODING_AGENT_DIR` (use `make pi`, or export `PI_CODING_AGENT_DIR=$PWD/pi-agent`). Confirm the token
  command works on its own: `databricks auth token --profile myprofile --output json`.
- **Jev seems to do nothing (no trimming, no markers)** — check `allow_roots` in `jev.config.json` includes
  the absolute path you're reading files from, that results exceed `min_tokens` (1500), and that the Jev key
  from step 2 is set. `off`/`shadow` modes never change output by design; use `arrival`.
- **Router errors / `ModuleNotFoundError: jev_router`** — the interpreter running the router isn't the one
  where you installed the package. Pass `make pi PY=.venv/bin/python` (or set `JEV_ROUTER_PYTHON`).
- **MLflow traces don't appear** — if you ran Pi directly instead of `make pi`, the terminal lacked
  `JEV_MLFLOW_EXPERIMENT_ID` (`source jev.mlflow.env` first, or use `make pi`, which auto-loads it). Other
  causes: MLflow isn't in the `JEV_ROUTER_PYTHON` interpreter (use `make pi PY=.venv/bin/python`), or the run
  made no model calls. It fails silently by design; run `make mlflow-verify` to check.
- **Traces exist (`make mlflow-verify` finds them) but the UI Traces tab is empty** — the tab queries the UC
  tables through a **SQL warehouse you select at the top of the tab**; pick a running one and widen the time
  filter. A just-finished trace also takes a few seconds to become queryable.
- **`profile '<name>' with a host not found in ~/.databrickscfg`** — the `PROFILE=` value doesn't match a
  section with a `host`. Check `databricks auth profiles`.
- **404 / route not found on `/ai-gateway/...`** — the AI Gateway coding-agent routes aren't enabled on that
  workspace. Use a workspace where they are.
- **`command not found: databricks` or `python3`** — install both and put them on `PATH`; the `apiKey`
  command shells out to them at request time.

## Notes

- Up to three identities are in play and they're independent: the Jev key (step 2, Vercel/TypeSafe — not
  Databricks), Pi's Databricks provider (step 4, the `pi-setup` `PROFILE`), and MLflow tracing (step 7,
  `DATABRICKS_CONFIG_PROFILE`). A problem with one doesn't explain a failure in another.
- Pi's model auth is fully decoupled from `ucode`/`ug` — a Databricks CLI profile is the only credential.
