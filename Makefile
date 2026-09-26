PY ?= python3
CONFIG ?= jev.config.json
MODE ?= arrival

.PHONY: test check pi report mlflow-setup mlflow-verify

# Unit tests; no network.
test:
	PYTHONPATH=. $(PY) -m unittest discover -s tests -v

# Type-check the Pi extension (run `npm --prefix adapters/pi ci` once first).
check:
	npm --prefix adapters/pi run check

# Interactive Pi with the extension. MODE: off | shadow | arrival | dynamic | cache_aware
pi:
	JEV_ROUTER_POLICY=$(abspath $(CONFIG)) JEV_ROUTER_PYTHON=$(shell command -v $(PY)) \
	  pi -e $(abspath adapters/pi/extension.ts) --jev $(MODE)

# Compare the N most recent sessions (for example the same task with MODE=off and MODE=arrival).
N ?= 10
report:
	$(PY) -m jev_router --report $(N)

# Optional MLflow tracing on Databricks (Unity Catalog). Requires `pip install -e '.[mlflow]'`.
PREFIX ?= pi
mlflow-setup:
	$(PY) -m jev_router.mlflow_setup setup --profile $(PROFILE) --catalog $(CATALOG) --schema $(SCHEMA) \
	  --warehouse $(WAREHOUSE) --prefix $(PREFIX)

mlflow-verify:
	$(PY) -m jev_router.mlflow_setup verify
