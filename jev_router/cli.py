"""JSON Lines protocol: one request/response per line, stdout is protocol-only."""
import argparse
from dataclasses import fields
import json
import os
from pathlib import Path
import sys

from .policy import Policy
from .router import Router


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.environ.get("JEV_ROUTER_DB"))
    parser.add_argument("--policy", default=os.environ.get("JEV_ROUTER_POLICY"))
    parser.add_argument("--mode", default=os.environ.get("JEV_ROUTER_MODE", "shadow"))
    parser.add_argument("--selection", default=os.environ.get("JEV_ROUTER_SELECTION", "arrival"))
    parser.add_argument("--export-session")
    args = parser.parse_args()
    config = json.loads(Path(args.policy).read_text()) if args.policy else {}
    unknown = set(config) - {f.name for f in fields(Policy)}
    if unknown:
        parser.error("Unknown policy keys: " + ", ".join(sorted(unknown)))
    router = Router(args.db, policy=Policy(**config), mode=args.mode, selection=args.selection)
    try:
        if args.export_session:
            for row in router.store.db.execute("SELECT id,call_id,data FROM snapshots WHERE session=?", (args.export_session,)):
                print(json.dumps({"event": "snapshot", "snapshot_id": row[0], "call_id": row[1], **json.loads(row[2])}, ensure_ascii=False))
            for row in router.store.db.execute("SELECT kind,time,data FROM events WHERE session=? ORDER BY id", (args.export_session,)):
                print(json.dumps({"event": row[0], "time": row[1], **json.loads(row[2])}, ensure_ascii=False))
            return
        for line in sys.stdin:
            request = {}
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("Request must be an object")
                op, params = request["op"], request.get("params", {})
                if op in {"route_tool_result", "select_context", "expand_chunk", "inherit_context", "compact", "subcontext"}:
                    result = getattr(router, op)(**params)
                elif op == "record":
                    router.store.event(params["session_id"], params["event"], params["data"])
                    result = {"recorded": True}
                elif op == "set_mode":
                    replacement = Router(args.db, policy=router.policy, mode=params["mode"], selection=params.get("selection", router.selection))
                    router.close()
                    router = replacement
                    result = {"mode": router.mode, "selection": router.selection}
                else:
                    raise ValueError("Unknown operation")
                response = {"id": request.get("id"), "result": result}
            except Exception as error:
                response = {"id": request.get("id") if isinstance(request, dict) else None,
                            "error": {"type": type(error).__name__, "message": "Router operation failed; retain original output"}}
            print(json.dumps(response, ensure_ascii=False, allow_nan=False), flush=True)
    finally:
        router.close()


if __name__ == "__main__":
    main()
