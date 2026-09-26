PY ?= python3
CONFIG ?= jev.config.json
MODE ?= arrival

.PHONY: test check pi mlflow-setup mlflow-verify

# Unit tests; no network.
test:
	PYTHONPATH=. $(PY) -m unittest discover -s tests -v

# Type-check the Pi extension (run `npm --prefix adapters/pi ci` once first).
check:
	npm --prefix adapters/pi run check

# Interactive Pi with the extension. MODE: off | shadow | arrival | dynamic | cache_aware
pi:
	JEV_ROUTER_POLICY=$(abspath $(CONFIG)) JEV_ROUTER_MODE=$(MODE) \
	JEV_ROUTER_PYTHON=$(shell command -v $(PY)) pi -e $(abspath adapters/pi/extension.ts)

# Optional MLflow tracing on Databricks (Unity Catalog). Requires `pip install -e '.[mlflow]'`.
PREFIX ?= pi
mlflow-setup:
	$(PY) -m jev_router.mlflow_setup setup --profile $(PROFILE) --catalog $(CATALOG) --schema $(SCHEMA) \
	  --warehouse $(WAREHOUSE) --prefix $(PREFIX)

mlflow-verify:
	$(PY) -m jev_router.mlflow_setup verify
