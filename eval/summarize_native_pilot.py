#!/usr/bin/env python3
"""Reproduce paired pilot aggregates without discarding failures or retries."""
import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import shutil
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from jev_router.cost import PRICE_SOURCE, jev_cost, usage_cost  # noqa: E402

MODES = ("off", "shadow", "arrival", "dynamic")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def list_cost(usage, model, jev_input, jev_output):
    coding = usage_cost(usage, model)
    jev = jev_cost([{"input_tokens": jev_input, "output_tokens": jev_output}])
    return {"coding_model": round(coding, 6), "jev": round(jev, 6), "total": round(coding + jev, 6), "price_source": PRICE_SOURCE}


def summarize(folder, freeze=False):
    manifest = json.loads((folder / "manifest.json").read_text())
    rows = json.loads((folder / "rows.json").read_text())
    expected = {(r["task"], r["mode"]) for r in manifest["schedule"]}
    assert len(rows) == len(expected) == len(manifest["schedule"]), "Incomplete or duplicate runs"
    assert {(r["task"], r["mode"]) for r in rows} == expected
    frozen = folder / "implementation"
    for name, checksum in manifest["implementation_sha256"].items():
        target = frozen / name
        if freeze and not target.exists():
            assert sha(ROOT / name) == checksum, f"Implementation changed before freeze: {name}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        assert sha(target if target.exists() else ROOT / name) == checksum, name
    for task in manifest["tasks"]:
        prompts = set()
        for mode in manifest["modes"]:
            run = folder / "runs" / f"{task['id']}_{mode}"
            assert sha(run / "repo/source.py") == task["source_sha256"], str(run)
            prompts.add((run / "prompt.txt").read_text())
        assert len(prompts) == 1, f"Prompts differ: {task['id']}"

    per_call, arms, pairs = [], {}, []
    base = {r["task"]: r for r in rows if r["mode"] == "off"}
    for row in rows:
        metrics = row["context_metrics"]
        row["eligible_exposure_est"] = sum(c["eligible_tokens_est"] for c in metrics)
        row["shown_eligible_exposure_est"] = sum(r["tokens_shown_est"] for c in metrics for r in c["results"] if r["eligible_tokens_est"])
        row["pruned_exposure_est"] = row["eligible_exposure_est"] - row["shown_eligible_exposure_est"]
        for index, context in enumerate(metrics):
            per_call.append({"task": row["task"], "mode": row["mode"], "model_call": index + 1,
                             **{k: context[k] for k in ("eligible_tokens_est", "tokens_shown_est", "retrieval_results", "intercepted_retrieval_results", "intercepted_pct")}})
        baseline = base[row["task"]]
        if row["mode"] != "off":
            pairs.append({"task": row["task"], "mode": row["mode"],
                          "model_token_delta": row["model_usage"]["totalTokens"] - baseline["model_usage"]["totalTokens"],
                          "seconds_delta": round(row["seconds"] - baseline["seconds"], 2),
                          "model_call_delta": row["model_calls"] - baseline["model_calls"],
                          "correct": row["correct"], "off_correct": baseline["correct"]})
    for mode in manifest["modes"]:
        rr = [r for r in rows if r["mode"] == mode]
        pp = [p for p in pairs if p["mode"] == mode]
        usage = {k: sum(r["model_usage"][k] for r in rr) for k in rr[0]["model_usage"]}
        contexts = [c for r in rr for c in r["context_metrics"]]
        intercepted = sum(c["intercepted_retrieval_results"] for c in contexts)
        retrieval = sum(c["retrieval_results"] for c in contexts)
        eligible = sum(r["eligible_exposure_est"] for r in rr)
        shown = sum(r["shown_eligible_exposure_est"] for r in rr)
        arms[mode] = {
            "runs": len(rr), "correct": sum(r["correct"] for r in rr),
            "passed_cases": sum(r["passed_cases"] for r in rr), "cases": sum(r["cases"] for r in rr),
            "model_usage": usage, "model_calls": sum(r["model_calls"] for r in rr),
            "mean_seconds": statistics.mean(r["seconds"] for r in rr),
            "median_seconds": statistics.median(r["seconds"] for r in rr),
            "total_seconds": sum(r["seconds"] for r in rr),
            "jev_calls": sum(r["jev_calls"] for r in rr), "jev_failures": sum(r["jev_failures"] for r in rr),
            "jev_seconds": sum(r["jev_ms"] for r in rr) / 1000,
            "jev_input": sum(r["jev_input"] for r in rr), "jev_output": sum(r["jev_output"] for r in rr),
            "jev_runs_with_failure": sum(bool(r["jev_failures"]) for r in rr),
            "unscored_chunk_evaluations": sum(r["unscored_chunks"] for r in rr),
            "jev_errors_run_counts": dict(Counter(e for r in rr for e in r["jev_errors"])),
            "expansions": sum(r["expansions"] for r in rr),
            "eligible_exposure_est": eligible, "shown_eligible_exposure_est": shown,
            "exposure_reduction_pct": 100 * (eligible - shown) / eligible if eligible else 0,
            "runs_with_pruning": sum(r["pruned_exposure_est"] > 0 for r in rr),
            "runs_with_proposed_pruning": sum(any(m["tokens_proposed_est"] < m["tokens_before_est"] for m in r["routing_metrics"]) for r in rr),
            "intercepted_retrieval_result_exposures": intercepted,
            "retrieval_result_exposures": retrieval, "intercepted_pct": 100 * intercepted / retrieval if retrieval else 0,
            "required_spans_retained": sum(sum(r["required_spans_retained"].values()) for r in rr),
            "required_spans_total": sum(len(r["required_spans_retained"]) for r in rr),
            "required_spans_recovered": sum(sum(r["required_spans_recovered"].values()) for r in rr),
            "timeouts": sum(r["timeout"] for r in rr), "model_errors": sum(len(r["model_errors"]) for r in rr),
            "missing_traces": sum(r["missing_trace"] for r in rr),
            "paired_fewer_model_tokens": sum(p["model_token_delta"] < 0 for p in pp),
            "paired_more_model_tokens": sum(p["model_token_delta"] > 0 for p in pp),
            "paired_median_token_delta": statistics.median(p["model_token_delta"] for p in pp) if pp else 0,
            "paired_model_call_delta": sum(p["model_call_delta"] for p in pp),
            "billed_cost": None,
            "list_cost_usd": list_cost(usage, manifest["model"], sum(r["jev_input"] for r in rr), sum(r["jev_output"] for r in rr)),
        }
    for mode, arm in arms.items():
        base_cost = arms["off"]["list_cost_usd"]["total"]
        arm["list_cost_usd"]["total_vs_off_pct"] = round(100 * (arm["list_cost_usd"]["total"] - base_cost) / base_cost, 2)
    tasks = {t["id"]: t for t in manifest["tasks"]}
    failures = [{"task": r["task"], "mode": r["mode"], "retained": r["required_spans_retained"],
                 "cases": {name: {"expected": tasks[r["task"]]["gold"][name], "actual": r["answer"].get(name) if isinstance(r["answer"], dict) else r["answer"]}
                           for name, passed in r["case_results"].items() if not passed}} for r in rows if not r["correct"]]
    summary = {"profile": manifest["profile"], "model": manifest["model"], "pi_version": manifest["pi_version"],
               "tasks": len(tasks), "runs": len(rows), "implementation_verified": True,
               "source_and_prompt_pairs_verified": True, "arms": arms, "failures": failures}
    (folder / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    for name, data in (("per-model-call.csv", per_call), ("paired-deltas.csv", pairs)):
        with (folder / name).open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(data[0])); writer.writeheader(); writer.writerows(data)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("--freeze-implementation", action="store_true")
    args = parser.parse_args()
    print(json.dumps(summarize(args.folder.resolve(), args.freeze_implementation), indent=2))


if __name__ == "__main__":
    main()
