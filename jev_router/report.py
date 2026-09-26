"""Compare recent Pi sessions from the local router store, e.g. the same task with `--jev off` and
`--jev arrival`:  python3 -m jev_router --report [N]"""
from datetime import datetime
import json

from .cost import jev_cost, usage_cost

USAGE = ("input", "cacheRead", "cacheWrite", "output")


def sessions(store, limit=10, prices=None):
    rows = store.db.execute("SELECT session, min(time), max(time) FROM events GROUP BY session "
                            "ORDER BY min(time) DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for sid, start, end in rows:
        events = [(k, json.loads(d)) for k, d in store.db.execute("SELECT kind,data FROM events WHERE session=? ORDER BY id", (sid,))]
        usage = [d for k, d in events if k == "model_usage"]
        tokens = {k: sum((d.get("usage") or {}).get(k) or 0 for d in usage) for k in USAGE}
        cost, priced = 0.0, True
        for d in usage:
            try:
                cost += usage_cost(d.get("usage") or {}, d.get("model"), **({"prices": prices} if prices else {}))
            except KeyError:
                priced = False
        results = [d.get("metrics", {}) for k, d in events if k == "tool_result"]
        routes = [d.get("metrics", {}) for k, d in events if k == "routing"]
        jev = jev_cost([u for m in routes for u in m.get("jev_usage", [])])
        task = store.db.execute("SELECT data FROM snapshots WHERE session=? LIMIT 1", (sid,)).fetchone()
        modes = list(dict.fromkeys(d.get("mode") for k, d in events if k == "session_config" and d.get("mode")))
        out.append({
            "session": sid, "started": start, "seconds": round(end - start, 1), "modes": modes or ["?"],
            "model_calls": len(usage), **tokens,
            "list_cost_usd": round(cost + jev, 6) if priced else None, "jev_cost_usd": round(jev, 6),
            "retrieved_tokens": sum(m.get("tokens_before_est", 0) for m in results if m.get("eligible_tokens_est")),
            "shown_tokens": sum(m.get("tokens_shown_est", 0) for m in results if m.get("eligible_tokens_est")),
            "jev_calls": sum(m.get("jev_calls", 0) for m in routes), "jev_failures": sum(m.get("jev_failures", 0) for m in routes),
            "expansions": sum(k == "expansion" for k, _ in events),
            "task": (json.loads(task[0]).get("task") or "") if task else "",
        })
    return out


def render(rows):
    if not rows:
        return "No sessions recorded yet. Run Pi with the extension, then try again."
    header = f"{'started':16} {'mode':8} {'calls':>5} {'input+cache':>11} {'cache wr':>9} {'output':>7} {'list cost':>10} " \
             f"{'retrieved':>9} {'shown':>7} {'cut':>5} {'jev':>7} {'expand':>6} {'secs':>6}  task"
    lines = [header, "-" * len(header)]
    for r in rows:
        prompt = r["input"] + r["cacheRead"] + r["cacheWrite"]
        cut = f"{100 * (1 - r['shown_tokens'] / r['retrieved_tokens']):.0f}%" if r["retrieved_tokens"] else "-"
        cost = f"${r['list_cost_usd']:.4f}" if r["list_cost_usd"] is not None else "unpriced"
        lines.append(f"{datetime.fromtimestamp(r['started']).strftime('%Y-%m-%d %H:%M'):16} {'+'.join(r['modes']):8.8} "
                     f"{r['model_calls']:>5} {prompt:>11,} {r['cacheWrite']:>9,} {r['output']:>7,} {cost:>10} "
                     f"{r['retrieved_tokens']:>9,} {r['shown_tokens']:>7,} {cut:>5} {r['jev_calls']:>3}/{r['jev_failures']:<3} "
                     f"{r['expansions']:>6} {r['seconds']:>6}  {' '.join(r['task'].split())[:40]}")
    lines.append("List cost uses jev_router/cost.py prices (add your model under `prices` in the config). "
                 "Model runs vary; compare several runs per mode.")
    return "\n".join(lines)
