"""One-time setup of MLflow tracing for Pi on Databricks, with traces stored in Unity Catalog.

    python -m jev_router.mlflow_setup setup --profile P --catalog C --schema S --warehouse W [--prefix pi]
    python -m jev_router.mlflow_setup verify [--env-file jev.mlflow.env]

`setup` follows https://docs.databricks.com/aws/en/mlflow3/genai/tracing/trace-unity-catalog: it creates the
schema if needed, binds an MLflow experiment to the Unity Catalog location (tables <prefix>_otel_spans,
_logs, _metrics, _annotations), and writes the environment variables Pi needs to an env file.
`verify` checks the experiment's trace location and lists the latest traces and their spans.
"""
import argparse
import os
from pathlib import Path
import re
import sys

MIN_MLFLOW = (3, 14)
TABLES = ("otel_spans", "otel_logs", "otel_metrics", "otel_annotations")


def _mlflow():
    try:
        import mlflow
    except ImportError:
        sys.exit("mlflow is not installed: pip install -e '.[mlflow]'")
    version = tuple(int(x) for x in re.findall(r"\d+", mlflow.__version__)[:2])
    if version < MIN_MLFLOW:
        sys.exit(f"mlflow {mlflow.__version__} is too old; Unity Catalog trace storage needs >= 3.14")
    return mlflow


def setup(args):
    mlflow = _mlflow()
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.errors import NotFound
    from mlflow.entities.trace_location import UnityCatalog

    # Some MLflow internals build their own WorkspaceClient() from the default profile; pin it so every
    # call targets the same workspace (a wrong default profile drops traces silently).
    os.environ["DATABRICKS_CONFIG_PROFILE"] = args.profile
    w = WorkspaceClient(profile=args.profile)
    user = w.current_user.me().user_name  # Fails fast on expired or wrong-workspace auth.
    w.warehouses.get(args.warehouse)
    try:
        w.schemas.get(f"{args.catalog}.{args.schema}")
    except NotFound:
        w.schemas.create(name=args.schema, catalog_name=args.catalog)
        print(f"created schema {args.catalog}.{args.schema}")
    os.environ["MLFLOW_TRACING_SQL_WAREHOUSE_ID"] = args.warehouse
    tracking_uri = f"databricks://{args.profile}"
    mlflow.set_tracking_uri(tracking_uri)
    name = args.experiment or f"/Users/{user}/jev-pi-traces"
    wanted = UnityCatalog(catalog_name=args.catalog, schema_name=args.schema, table_prefix=args.prefix)
    existing = mlflow.get_experiment_by_name(name)
    location = getattr(existing, "trace_location", None) if existing else None
    if existing and isinstance(location, UnityCatalog):
        same = (location.catalog_name, location.schema_name, location.table_prefix) == (args.catalog, args.schema, args.prefix)
        if not same:
            sys.exit(f"{name} is already bound to {location.catalog_name}.{location.schema_name} "
                     f"(prefix {location.table_prefix}); a binding is permanent. Use --experiment with a new name.")
        experiment = existing
        print(f"reusing {name} (already bound to {args.catalog}.{args.schema}, prefix {args.prefix})")
    else:
        experiment = mlflow.set_experiment(experiment_name=name, trace_location=wanted)
        print(f"bound {name} to {args.catalog}.{args.schema} (prefix {args.prefix})")
    missing = [t for t in TABLES if not w.tables.exists(f"{args.catalog}.{args.schema}.{args.prefix}_{t}").table_exists]
    if missing:
        sys.exit(f"trace tables not found after binding: {missing}")
    env = {"MLFLOW_TRACKING_URI": tracking_uri, "DATABRICKS_CONFIG_PROFILE": args.profile,
           "JEV_MLFLOW_EXPERIMENT_ID": experiment.experiment_id,
           "MLFLOW_TRACING_SQL_WAREHOUSE_ID": args.warehouse, "JEV_MLFLOW_CONTENT": "metadata"}
    Path(args.env_file).write_text("".join(f"export {k}={v}\n" for k, v in env.items()))
    host = w.config.host.rstrip("/")
    print(f"tables: {', '.join(f'{args.catalog}.{args.schema}.{args.prefix}_{t}' for t in TABLES)}")
    print(f"experiment: {host}/ml/experiments/{experiment.experiment_id}")
    print(f"wrote {args.env_file}; run `source {args.env_file}` before starting Pi with the extension.")
    print("To let other users write traces, grant USE CATALOG, USE SCHEMA, and MODIFY + SELECT on each table.")


def _env(path):
    for line in Path(path).read_text().splitlines():
        key, _, value = line.removeprefix("export ").partition("=")
        os.environ.setdefault(key, value)


def verify(args):
    if Path(args.env_file).exists():
        _env(args.env_file)
    mlflow = _mlflow()
    from mlflow.entities.trace_location import UnityCatalog

    experiment_id = os.environ.get("JEV_MLFLOW_EXPERIMENT_ID")
    if not experiment_id:
        sys.exit("JEV_MLFLOW_EXPERIMENT_ID is not set; run setup first")
    mlflow.set_tracking_uri(os.environ.get("MLFLOW_TRACKING_URI", "databricks"))
    experiment = mlflow.get_experiment(experiment_id)
    location = getattr(experiment, "trace_location", None)
    print(f"experiment {experiment.name}: trace location {location}")
    if not isinstance(location, UnityCatalog):
        sys.exit("experiment is not bound to a Unity Catalog trace location")
    traces = mlflow.search_traces(locations=[experiment_id], max_results=args.limit, order_by=["timestamp_ms DESC"], return_type="list")
    print(f"found {len(traces)} recent trace(s)")
    for trace in traces:
        spans = trace.data.spans
        print(f"- {trace.info.trace_id}: {len(spans)} spans ({', '.join(sorted({s.name for s in spans}))})")
    if not traces:
        sys.exit("no traces yet: run Pi with the extension after `source`-ing the env file")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--profile", required=True, help="Databricks CLI profile (~/.databrickscfg)")
    s.add_argument("--catalog", required=True)
    s.add_argument("--schema", required=True)
    s.add_argument("--warehouse", required=True, help="SQL warehouse ID (CAN USE)")
    s.add_argument("--prefix", default="pi", help="table prefix (default: pi)")
    s.add_argument("--experiment", help="experiment path (default: /Users/<you>/jev-pi-traces)")
    s.add_argument("--env-file", default="jev.mlflow.env")
    v = sub.add_parser("verify")
    v.add_argument("--env-file", default="jev.mlflow.env")
    v.add_argument("--limit", type=int, default=3)
    args = parser.parse_args()
    (setup if args.command == "setup" else verify)(args)


if __name__ == "__main__":
    main()
