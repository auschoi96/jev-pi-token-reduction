# The project venv when present (python3 -m venv .venv), otherwise python3 on PATH.
PY ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
CONFIG ?= jev.config.json
MODE ?= arrival
PI_AGENT ?= pi-agent
MLFLOW_ENV ?= jev.mlflow.env

.PHONY: test check pi pi-setup mlflow-setup mlflow-verify

# Unit tests; no network.
test:
	PYTHONPATH=. $(PY) -m unittest discover -s tests -v

# Type-check the Pi extension (run `npm --prefix adapters/pi ci` once first).
check:
	npm --prefix adapters/pi run check

# Point Pi at your workspace's Databricks AI Gateway (writes pi-agent/, no ucode).
# Usage: make pi-setup PROFILE=<databrickscfg profile> [MODEL=system.ai.gpt-6-sol]
pi-setup:
	$(PY) -m jev_router.pi_setup setup --profile $(PROFILE) $(if $(MODEL),--model $(MODEL),) --dir $(PI_AGENT)

# Interactive Pi with the extension. MODE: off | shadow | arrival | dynamic | cache_aware
# Loads jev.mlflow.env when present so MLflow tracing works in any terminal without a manual `source`.
# Values already set in your shell win (the file is only a default), so `JEV_MLFLOW_CONTENT=full make pi`
# overrides it. Uses the pi-agent/ config from `make pi-setup` when present, else Pi's own config.
pi:
	if [ -f $(MLFLOW_ENV) ]; then \
	  while IFS= read -r l; do \
	    case "$$l" in export\ *=*) kv=$${l#export }; printenv "$${kv%%=*}" >/dev/null || export "$$kv";; esac; \
	  done < $(MLFLOW_ENV); \
	fi; \
	$(if $(wildcard $(PI_AGENT)/models.json),PI_CODING_AGENT_DIR=$(abspath $(PI_AGENT)) ,)JEV_ROUTER_POLICY=$(abspath $(CONFIG)) JEV_ROUTER_PYTHON=$(abspath $(shell command -v $(PY) || echo $(PY))) \
	  pi -e $(abspath adapters/pi/extension.ts) --jev $(MODE)

# Optional MLflow tracing on Databricks (Unity Catalog). Requires `pip install -e '.[mlflow]'`.
PREFIX ?= pi
mlflow-setup:
	$(PY) -m jev_router.mlflow_setup setup --profile $(PROFILE) --catalog $(CATALOG) --schema $(SCHEMA) \
	  --warehouse $(WAREHOUSE) --prefix $(PREFIX)

mlflow-verify:
	$(PY) -m jev_router.mlflow_setup verify
