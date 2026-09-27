#!/usr/bin/env python3
"""Resumable, sequential Pi pilot with fresh snapshots and independent oracles."""
import argparse
import ast
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from jev_router.pi_setup import auth_key_command, host_for_profile
MODES = ("off", "shadow", "arrival", "dynamic")


def required_spans(path, names):
    tree = ast.parse(path.read_text())
    found = {}
    def walk(nodes, prefix=""):
        for node in nodes:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + node.name
                if name in names:
                    found[name] = [node.lineno, node.end_lineno]
                if isinstance(node, ast.ClassDef):
                    walk(node.body, name + ".")
    walk(tree.body)
    return found


def prompt_for(task):
    cases = "\n\n".join(f"Case {c['name']}:\n```python\n{c['code']}\n```" for c in task["cases"])
    return (
        "Read source.py to analyze the exact behavior of this CPython module. Start with a broad read of the file "
        "using the read tool; if truncated, continue reading as needed. You may use grep to locate relevant branches. "
        "The snippets below run independently with m bound to this exact module; each return is inside a function. "
        "Infer every returned value or raised exception by reading the source. Do not execute the snippets or use "
        "outside files. If Jev hides evidence you need, recover it with jev_expand. Do not expand unrelated content.\n\n"
        "Return ONLY one JSON object mapping each case name to {\"value\": <JSON value>} or "
        "{\"error\": \"ExceptionClassName\"}. Convert tuples to JSON arrays. Preserve whitespace exactly "
        "inside strings. Use a decimal number for ratios. Do not include explanations or markdown.\n\n" + cases
    )


def equivalent(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (float, int)) and isinstance(b, (float, int)):
        return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(equivalent(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(equivalent(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def final_answer(path):
    answers = []
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = event.get("message", {})
        if event.get("type") == "message_end" and message.get("role") == "assistant" and message.get("stopReason") == "stop":
            answers.append("\n".join(b["text"] for b in message.get("content", []) if b["type"] == "text"))
    text = answers[-1] if answers else ""
    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return text, json.loads(clean)
    except ValueError:
        return text, None


def collect(run_dir, task):
    answer_text, answer = final_answer(run_dir / "pi.jsonl")
    results = {name: isinstance(answer, dict) and name in answer and equivalent(value, answer[name])
               for name, value in task["gold"].items()}
    row = {"task": task["id"], "module": task["module"], "passed_cases": sum(results.values()),
           "cases": len(results), "correct": all(results.values()), "case_results": results,
           "answer": answer, "answer_text": answer_text}
    db = run_dir / "router.sqlite"
    if not db.exists():
        return {**row, "missing_trace": True}
    conn = sqlite3.connect(db)
    events = [(kind, json.loads(data)) for kind, data in conn.execute("SELECT kind,data FROM events ORDER BY id")]
    snapshots = [json.loads(x[0]) for x in conn.execute("SELECT data FROM snapshots")]
    conn.close()
    routes = [data for kind, data in events if kind == "routing"]
    usages = [data for kind, data in events if kind == "model_usage"]
    contexts = [data for kind, data in events if kind == "context"]
    expansions = [data for kind, data in events if kind == "expansion"]
    jm = [r["metrics"] for r in routes]
    ju = [u for m in jm for u in m["jev_usage"]]
    errors = sorted({d["error"] for r in routes for d in r["decisions"] if d.get("error")})
    # Exact required function text is a conservative span-retention measure. It
    # does not imply every line in that function was necessary for the answer.
    lines = (run_dir / "repo/source.py").read_text().splitlines(keepends=True)
    spans = task["required_spans"]
    retained = {}
    recovered = {}
    for name, (start, end) in spans.items():
        span = "".join(lines[start-1:end])
        retained[name] = any(span in b.get("text", "") for c in contexts for message in c["messages"]
                             if message.get("role") == "toolResult" and message.get("toolName") != "jev_expand"
                             for b in message.get("content", []) if isinstance(b, dict))
        recovered[name] = any(span in e["original_text"] for e in expansions)
    raw_chars = sum(len(b.get("text", "")) for s in snapshots for b in s.get("raw", {}).get("content", []) if isinstance(b, dict))
    row.update(
        missing_trace=not bool(contexts), model_calls=len(usages),
        model_errors=[u.get("error", u.get("stop_reason")) for u in usages if u.get("error") or u.get("stop_reason") == "error"],
        model_usage={k:sum(u.get("usage", {}).get(k, 0) for u in usages) for k in ("input", "cacheRead", "cacheWrite", "output", "reasoning", "totalTokens")},
        jev_calls=sum(m["jev_calls"] for m in jm), jev_failures=sum(m["jev_failures"] for m in jm),
        jev_ms=round(sum(m["jev_ms"] for m in jm), 2), unscored_chunks=sum(m["unscored_chunks"] for m in jm),
        jev_errors=errors, jev_input=sum(u.get("inputTokens", u.get("input_tokens", 0)) for u in ju),
        jev_output=sum(u.get("outputTokens", u.get("output_tokens", 0)) for u in ju),
        expansions=len(expansions), raw_retrieval_characters=raw_chars,
        required_spans_retained=retained, required_spans_recovered=recovered,
        context_metrics=[c["metrics"] for c in contexts],
        routing_metrics=jm, billed_cost=None,
    )
    return row


def run_one(task, mode, run_dir, config):
    run_dir.mkdir(parents=True)
    repo = run_dir / "repo"
    repo.mkdir()
    shutil.copyfile(task["snapshot"], repo / "source.py")
    policy = {"allow_roots": [str(repo)], "allow_commands": False, "min_tokens": 1500}
    (run_dir / "policy.json").write_text(json.dumps(policy))
    prompt = prompt_for(task)
    (run_dir / "prompt.txt").write_text(prompt)
    # Clean slate per run: a fresh Pi agent dir (settings, trust, packages) and a private TMPDIR
    # (Pi's own temp files and the extension compile cache).
    agent = run_dir / "pi-agent"
    agent.mkdir(mode=0o700)
    for name in ("models.json", "settings.json"):
        shutil.copyfile(Path(config["agent_dir"]) / name, agent / name)
    (run_dir / "tmp").mkdir(mode=0o700)
    env = {**os.environ, "PI_CODING_AGENT_DIR": str(agent), "PI_OFFLINE":"1", "PI_TELEMETRY":"0",
           "JEV_ROUTER_DB":str(run_dir / "router.sqlite"), "JEV_ROUTER_POLICY":str(run_dir / "policy.json"),
           "JEV_ROUTER_MODE":mode, "JEV_ROUTER_PYTHON":sys.executable,
           "TMPDIR":str(run_dir / "tmp") + "/", "TMP":str(run_dir / "tmp"), "TEMP":str(run_dir / "tmp")}
    command = ["pi", "--offline", "-p", "--mode", "json", "--no-session", "-ne", "-ns", "-np", "-nc", "--no-themes",
               "--provider", "jev-native-pilot", "--model", config["model"], "--thinking", "medium",
               "--tools", "read,grep,find,ls,jev_expand", "-e", str(ROOT / "adapters/pi/extension.ts"), prompt]
    start = time.monotonic()
    timeout = False
    with (run_dir / "pi.jsonl").open("w") as stdout, (run_dir / "pi.stderr").open("w") as stderr:
        proc = subprocess.Popen(command, cwd=repo, env=env, stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            code = proc.wait(timeout=config["timeout"])
        except subprocess.TimeoutExpired:
            timeout = True
            os.killpg(proc.pid, signal.SIGTERM)
            try: code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL); code = proc.wait()
    row = {**collect(run_dir, task), "mode":mode, "seconds":round(time.monotonic()-start,2),
           "exit_code":code, "timeout":timeout, "path":str(run_dir), "source_sha256":task["source_sha256"]}
    (run_dir / "result.json").write_text(json.dumps(row, indent=2))
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--oracle-python", default=sys.executable)
    parser.add_argument("--modes", default=",".join(MODES))
    parser.add_argument("--tasks", default="")
    parser.add_argument("--seed", type=int, default=20260924)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--tasks-module", type=Path, default=ROOT / "eval/native_tasks.py",
                        help="task generator run under --oracle-python (e.g. eval/native_tasks_heldout.py)")
    args = parser.parse_args()
    modes = args.modes.split(",")
    if set(modes) - set(MODES): parser.error("Unknown mode")
    if os.environ.get("JEV_MOCK"): parser.error("Unset JEV_MOCK for this pilot")
    out = args.output.resolve()
    manifest_path = out / "manifest.json"
    model = args.model if args.model.startswith("system.ai.") else "system.ai." + args.model
    if not manifest_path.exists():
        out.mkdir(parents=True, mode=0o700, exist_ok=True)
        tasks = json.loads(subprocess.check_output([args.oracle_python, str(args.tasks_module.resolve())], text=True))
        if args.tasks: tasks = [t for t in tasks if t["id"] in args.tasks.split(",")]
        if not tasks: parser.error("No matching tasks")
        source_dir = out / "sources"; source_dir.mkdir(exist_ok=True)
        for t in tasks:
            source = Path(t["source_path"])
            dest = source_dir / (t["module"].replace("/", "_") + ".py")
            if not dest.exists(): shutil.copyfile(source, dest)
            t["snapshot"] = str(dest)
            t["source_sha256"] = hashlib.sha256(dest.read_bytes()).hexdigest()
            t["required_spans"] = required_spans(dest, t["symbols"])
            if len(t["required_spans"]) != len(t["symbols"]):
                parser.error(f"Unresolved answer spans: {t['id']}: {set(t['symbols'])-set(t['required_spans'])}")
        host = host_for_profile(args.profile)
        agent = out / "pi-agent"; agent.mkdir(mode=0o700, exist_ok=True)
        (agent / "models.json").write_text(json.dumps({"providers":{"jev-native-pilot":{
            "baseUrl":host+"/ai-gateway/codex/v1", "api":"openai-responses", "authHeader":True,
            "apiKey":auth_key_command(args.profile),
            "models":[{"id":model,"reasoning":True,"input":["text"],"contextWindow":200000,"maxTokens":8192}]}}},indent=2))
        (agent / "settings.json").write_text(json.dumps({"retry":{"enabled":False}}))
        rng = random.Random(args.seed)
        schedule = []
        rng.shuffle(tasks)
        for t in tasks:
            order = modes[:]; rng.shuffle(order)
            schedule.extend({"task":t["id"],"mode":mode} for mode in order)
        manifest = {"profile":args.profile,"model":model,"seed":args.seed,"modes":modes,"timeout":args.timeout,
                    "tasks_module":str(args.tasks_module.resolve().relative_to(ROOT)),
                    "pi_version":subprocess.check_output(["pi","--version"],text=True).strip(),
                    "oracle_python":args.oracle_python,"agent_dir":str(agent),"tasks":tasks,"schedule":schedule}
        manifest["implementation_sha256"] = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                                             for p in [*sorted((ROOT/"jev_router").glob("*.py")), ROOT/"adapters/pi/extension.ts"]}
        manifest_path.write_text(json.dumps(manifest,indent=2))
    else:
        manifest = json.loads(manifest_path.read_text())
        if manifest["profile"] != args.profile or manifest["model"] != model or manifest["modes"] != modes:
            parser.error("Resume settings differ from frozen manifest")
    if args.prepare_only:
        print(json.dumps({"tasks":len(manifest["tasks"]),"runs":len(manifest["schedule"]),"manifest":str(manifest_path)})); return
    tasks = {t["id"]:t for t in manifest["tasks"]}
    rows = []
    for index, item in enumerate(manifest["schedule"]):
        path = out / "runs" / (item["task"]+"_"+item["mode"])
        result_file = path / "result.json"
        if result_file.exists(): row = json.loads(result_file.read_text())
        else:
            if path.exists(): path.rename(path.with_name(path.name+"_interrupted_"+str(int(time.time()))))
            row = run_one(tasks[item["task"]], item["mode"], path, manifest)
        rows.append(row)
        (out / "rows.json").write_text(json.dumps(rows,indent=2))
        print(json.dumps({"progress":f"{index+1}/{len(manifest['schedule'])}", **{k:row.get(k) for k in
                         ("task","mode","correct","passed_cases","cases","model_calls","jev_calls","jev_failures","expansions","seconds","timeout")}}),flush=True)
    print(json.dumps({"complete":True,"rows":str(out/"rows.json")}))


if __name__ == "__main__":
    main()
