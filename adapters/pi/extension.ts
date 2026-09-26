/** Pi 0.84.2: tool_result interception, non-destructive context selection, Jev compaction, delegation. */
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";
import { open } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";
import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

type Pending = { resolve: (value: any) => void; reject: (error: Error) => void; cleanup: () => void };

export class Bridge {
  private child?: ChildProcessWithoutNullStreams;
  private pending = new Map<number, Pending>();
  private nextId = 0;
  private mode = process.env.JEV_ROUTER_MODE || "shadow";
  get currentMode() { return this.mode; }

  private start() {
    if (this.child) return;
    const root = fileURLToPath(new URL("../../", import.meta.url));
    const child = spawn(process.env.JEV_ROUTER_PYTHON || "python3", ["-m", "jev_router", "--mode", this.mode], {
      cwd: root, env: process.env, stdio: ["pipe", "pipe", "pipe"],
    });
    this.child = child;
    child.stderr.on("data", () => {}); // Drain; never contaminate Pi's JSON stream.
    child.stdin.on("error", () => this.fail(child, "Router stdin failed"));
    child.on("error", () => this.fail(child, "Router could not start"));
    child.on("exit", () => this.fail(child, "Router exited"));
    const lines = createInterface({ input: child.stdout });
    lines.on("line", (line) => {
      try {
        const response = JSON.parse(line);
        const item = this.pending.get(response.id);
        if (!item) return;
        this.pending.delete(response.id);
        item.cleanup();
        if (response.error) item.reject(new Error(response.error.message));
        else item.resolve(response.result);
      } catch { this.fail(child, "Invalid router response"); }
    });
  }

  private fail(child: ChildProcessWithoutNullStreams, message: string) {
    if (this.child !== child) return;
    this.child = undefined;
    child.kill();
    for (const item of this.pending.values()) {
      item.cleanup();
      item.reject(new Error(message));
    }
    this.pending.clear();
  }

  request(op: string, params: any, signal?: AbortSignal): Promise<any> {
    if (signal?.aborted) return Promise.reject(new Error("Aborted"));
    this.start();
    const child = this.child!;
    return new Promise((resolveRequest, reject) => {
      const id = ++this.nextId;
      const abort = () => this.fail(child, "Router request cancelled or timed out");
      const timeout = setTimeout(abort, Number(process.env.JEV_ROUTER_BRIDGE_TIMEOUT_MS || 25000));
      signal?.addEventListener("abort", abort, { once: true });
      this.pending.set(id, {
        resolve: resolveRequest, reject,
        cleanup: () => { clearTimeout(timeout); signal?.removeEventListener("abort", abort); },
      });
      child.stdin.write(JSON.stringify({ id, op, params }) + "\n");
    });
  }

  /** Choose the mode before the worker starts (the --jev flag); later changes go through setMode. */
  async useMode(mode: string) {
    if (this.child) await this.setMode(mode);
    else this.mode = mode;
  }

  async setMode(mode: string) {
    const result = await this.request("set_mode", { mode });
    this.mode = mode;
    return result;
  }

  close() { if (this.child) this.fail(this.child, "Session closed"); }
}

function messageText(message: any): string {
  if (typeof message?.content === "string") return message.content;
  return (message?.content || []).filter((b: any) => b.type === "text").map((b: any) => b.text).join("\n");
}

const ROLE = process.env.JEV_ROLE || "main";

/** Run a read-only investigation on a cheaper model; its own tool output is routed by this extension. */
function runSubagent(prompt: string, cwd: string, provider: string, model: string, signal?: AbortSignal) {
  return new Promise<{ text: string; calls: number; seconds: number }>((resolveRun, reject) => {
    const started = Date.now();
    const child = spawn("pi", ["--offline", "-p", "--mode", "json", "--no-session", "-ne", "-ns", "-np", "-nc", "--no-themes",
      "--provider", provider, "--model", model, "--thinking", "low", "--tools", "read,grep,find,ls,bash",
      "-e", fileURLToPath(import.meta.url), prompt], {
      cwd, env: { ...process.env, JEV_ROLE: "subagent", JEV_DELEGATE_MODEL: "" }, stdio: ["ignore", "pipe", "ignore"],
    });
    let text = "", calls = 0;
    createInterface({ input: child.stdout }).on("line", (line) => {
      try {
        const event = JSON.parse(line);
        if (event.type !== "message_end" || event.message?.role !== "assistant") return;
        calls += 1;
        const body = messageText(event.message);
        if (event.message.stopReason === "stop" && body) text = body;
      } catch { /* non-JSON diagnostics */ }
    });
    const timer = setTimeout(() => child.kill(), Number(process.env.JEV_DELEGATE_TIMEOUT_MS || 300000));
    const abort = () => child.kill();
    signal?.addEventListener("abort", abort, { once: true });
    child.on("error", reject);
    child.on("close", () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      if (!text) reject(new Error("Sub-agent returned no answer"));
      else resolveRun({ text, calls, seconds: (Date.now() - started) / 1000 });
    });
  });
}

const MODES = ["off", "shadow", "arrival", "dynamic", "cache_aware"];

export default function (pi: ExtensionAPI) {
  const bridge = new Bridge();
  // `pi --jev off` runs with the extension loaded but no Jev calls and unchanged tool output, so the same
  // workload can be compared with and without Jev (`python3 -m jev_router --report`).
  pi.registerFlag("jev", { type: "string", description: `Jev mode for this run: ${MODES.join(" | ")} (overrides JEV_ROUTER_MODE)` });
  let task = "", step = "", turn = 0, modelCall = 0, lastText = "";
  const calls = new Map<string, string>();
  const evidence: string[] = []; // Signal lines of recent command/search results, for Jev queries only.
  const session = (ctx: any) => ctx.sessionManager.getSessionId();
  const report = (ctx: any, error: unknown) => {
    const label = error instanceof Error ? error.message : "Router failed";
    if (ctx.hasUI) ctx.ui.notify(`Jev: ${label}. Keeping original content.`, "warning");
    // Local stderr also makes bridge failures visible in headless traces.
    process.stderr.write(`[jev-router] ${label}; original content retained\n`);
  };

  pi.on("session_start", async (_event, ctx) => {
    const flag = pi.getFlag("jev");
    if (typeof flag === "string" && flag) {
      // An unknown value must never send content to Jev by accident.
      if (!MODES.includes(flag)) process.stderr.write(`[jev-router] unknown --jev value "${flag}"; using off\n`);
      await bridge.useMode(MODES.includes(flag) ? flag : "off");
    }
    try {
      await bridge.request("record", { session_id: session(ctx), event: "session_config", data: {
        mode: bridge.currentMode, source: flag ? "flag" : process.env.JEV_ROUTER_MODE ? "env" : "default" } });
    } catch (error) { report(ctx, error); }
    // Reload/resume/fork each get a fresh extension instance. Only Pi's declared
    // parent and the messages actually retained in this branch are inherited.
    const parent = ctx.sessionManager.getHeader()?.parentSession;
    if (!parent) return;
    let file;
    try {
      file = await open(parent, "r");
      const buffer = Buffer.alloc(16384);
      const { bytesRead } = await file.read(buffer, 0, buffer.length, 0);
      const header = JSON.parse(buffer.subarray(0, bytesRead).toString().split("\n")[0]);
      if (header.type !== "session" || typeof header.id !== "string") return;
      const messages = ctx.sessionManager.getBranch().flatMap(entry => entry.type === "message" ? [entry.message] : []);
      await bridge.request("inherit_context", { session_id: session(ctx), parent_session_id: header.id, messages });
    } catch (error) { report(ctx, error); }
    finally { await file?.close(); }
  });

  // Optional MLflow tracing (JEV_MLFLOW_EXPERIMENT_ID): the worker builds one trace per run from these records.
  const tracing = Boolean(process.env.JEV_MLFLOW_EXPERIMENT_ID);
  const fullContent = process.env.JEV_MLFLOW_CONTENT === "full";
  pi.on("before_agent_start", async (event, ctx) => {
    task = event.prompt; step = task;
    if (!tracing) return;
    try {
      await bridge.request("record", { session_id: session(ctx), event: "agent_start", data: {
        prompt_chars: task.length, mode: bridge.currentMode, ...(fullContent ? { prompt: task } : {}) } });
    } catch (error) { report(ctx, error); }
  });
  pi.on("agent_settled", async (_event, ctx) => {
    if (!tracing) return;
    try {
      await bridge.request("record", { session_id: session(ctx), event: "agent_settled", data: fullContent ? { text: lastText } : {} });
      await bridge.request("trace_run", { session_id: session(ctx) });
    } catch (error) { report(ctx, error); }
  });
  pi.on("turn_start", async () => { turn += 1; });
  pi.on("message_end", async (event, ctx) => {
    if (event.message.role !== "assistant") return;
    const text = messageText(event.message);
    if (text) lastText = text;
    const toolCalls = event.message.content.filter(b => b.type === "toolCall");
    if (text || toolCalls.length) {
      step = [text, ...toolCalls.map(b => `Tool: ${b.name} ${JSON.stringify(b.arguments)}`),
        ...(evidence.length ? ["Recent tool evidence:", ...evidence] : [])].filter(Boolean).join("\n");
    }
    for (const b of toolCalls) calls.set(b.id, step);
    try {
      await bridge.request("record", { session_id: session(ctx), event: "model_usage", data: {
        model_call: modelCall, turn, role: ROLE, model: event.message.model, provider: event.message.provider,
        usage: event.message.usage, stop_reason: event.message.stopReason,
        error: event.message.errorMessage, tool_calls: toolCalls.length, tool_call_names: toolCalls.map(b => b.name),
        ...(tracing && fullContent ? { text } : {}),
      }});
    } catch (error) { report(ctx, error); }
  });
  pi.on("tool_result", async (event, ctx) => {
    if (event.toolName === "jev_expand") return; // Expansion can never be routed again.
    const input: Record<string, any> = { ...event.input };
    if (typeof input.path === "string") input.path = resolve(ctx.cwd, input.path);
    if (typeof input.file_path === "string") input.file_path = resolve(ctx.cwd, input.file_path);
    try {
      const result = await bridge.request("route_tool_result", {
        session_id: session(ctx), turn_id: String(turn), tool_call_id: event.toolCallId,
        task, step: calls.get(event.toolCallId) || step, tool: { ...input, name: event.toolName },
        raw_output: { content: event.content, isError: event.isError },
      }, ctx.signal);
      if (result.evidence) { evidence.push(result.evidence); evidence.splice(0, Math.max(0, evidence.length - 3)); }
      return { content: result.visible_output.content };
    } catch (error) { report(ctx, error); }
    finally { calls.delete(event.toolCallId); }
  });
  pi.on("context", async (event, ctx) => {
    modelCall += 1;
    const latestUser = [...event.messages].reverse().find(m => m.role === "user");
    const currentTask = latestUser ? messageText(latestUser) : task;
    // Explicit user goal plus the latest public assistant step; private reasoning is never exported.
    try {
      const result = await bridge.request("select_context", {
        session_id: session(ctx), current_step: `Current task: ${currentTask}\nCurrent step: ${step}`,
        model_bound_messages: event.messages, model: ctx.model?.id,
      }, ctx.signal);
      return { messages: result.selected_messages };
    } catch (error) { report(ctx, error); }
  });
  pi.on("session_before_compact", async (event, ctx) => {
    if (process.env.JEV_ROUTER_COMPACTION !== "jev") return;
    const prep = event.preparation;
    try {
      const result = await bridge.request("compact", {
        session_id: session(ctx), current_step: `Current task: ${task}\nCurrent step: ${step}`,
        messages: [...prep.messagesToSummarize, ...prep.turnPrefixMessages], previous_summary: prep.previousSummary,
      }, event.signal);
      if (!result.metrics.within_budget) return; // Cannot fit; let Pi's own compaction run.
      return { compaction: { summary: result.summary, firstKeptEntryId: prep.firstKeptEntryId,
        tokensBefore: prep.tokensBefore, details: { jev: result.metrics } } };
    } catch (error) { report(ctx, error); } // Pi's own LLM compaction runs instead.
  });
  pi.on("session_compact", async (event, ctx) => {
    // Host compaction is a separate model call that never emits message_end; count it.
    const usage = event.compactionEntry.usage;
    try {
      await bridge.request("record", { session_id: session(ctx), event: event.fromExtension ? "compaction_applied" : "model_usage", data: {
        model_call: modelCall, turn, role: ROLE, kind: "compaction", reason: event.reason, from_extension: event.fromExtension,
        model: ctx.model?.id, provider: ctx.model?.provider, usage, tokens_before: event.compactionEntry.tokensBefore,
      }});
    } catch (error) { report(ctx, error); }
  });
  const delegateModel = process.env.JEV_DELEGATE_MODEL;
  if (delegateModel && ROLE === "main") pi.registerTool({
    name: "jev_delegate", label: "Delegate investigation",
    description: "Run a read-only investigation (run tests, read tracebacks and source, locate the cause) on a cheaper sub-agent model. "
      + "It receives only the tool output Jev selects for the request, not this transcript, and returns a concise report with file:line evidence. "
      + "It runs a much cheaper model, so prefer it for running test suites and scanning large outputs or files. "
      + "Give a self-contained request. The sub-agent must not edit files; apply fixes yourself.",
    parameters: Type.Object({ task: Type.String({ description: "Self-contained investigation request" }) }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const messages = ctx.sessionManager.getBranch().flatMap(entry => entry.type === "message" ? [entry.message] : []);
      const sub = await bridge.request("subcontext", { session_id: session(ctx), subtask: params.task, messages }, signal);
      const prompt = "You are a read-only investigation sub-agent for a coding agent. Do not modify, create, or delete files.\n\n"
        + (sub.context ? "Parent context selected by Jev for this request (hidden_ref markers are not expandable here; re-read files instead):\n"
          + sub.context + "\n\n" : "")
        + "Request: " + params.task + "\n\nReply with a concise report (at most 250 words): the findings with file:line evidence.";
      const provider = process.env.JEV_DELEGATE_PROVIDER || ctx.model?.provider || "";
      const result = await runSubagent(prompt, ctx.cwd, provider, delegateModel, signal);
      await bridge.request("record", { session_id: session(ctx), event: "delegation", data: {
        turn, model: delegateModel, calls: result.calls, seconds: result.seconds, subcontext: sub.metrics, answer_chars: result.text.length } });
      const note = "\n\n[jev_delegate: to edit, read only the cited line ranges (read offset/limit) instead of whole files.]";
      return { content: [{ type: "text", text: result.text + note }], details: { model: delegateModel, calls: result.calls } };
    },
  });
  pi.registerTool({
    name: "jev_expand", label: "Expand Jev snapshot",
    description: "Recover the exact original text behind a Jev omitted-range marker in this session. Use before relying on omitted evidence. The snapshot remains valid after files change.",
    parameters: Type.Object({ hidden_ref: Type.String() }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const text = await bridge.request("expand_chunk", { session_id: session(ctx), hidden_ref: params.hidden_ref }, signal);
      return { content: [{ type: "text", text }], details: { hidden_ref: params.hidden_ref } };
    },
  });
  pi.registerCommand("jev", {
    description: "Set Jev mode: off, shadow, arrival, dynamic, or cache_aware",
    handler: async (args, ctx) => {
      const mode = args.trim();
      if (!MODES.includes(mode)) {
        ctx.ui.notify(`Usage: /jev ${MODES.join("|")}`, "info");
        return;
      }
      try {
        await bridge.setMode(mode);
        await bridge.request("record", { session_id: session(ctx), event: "session_config", data: { mode, source: "command" } });
        ctx.ui.notify(`Jev mode: ${mode}`, "info");
      }
      catch (error) { report(ctx, error); }
    },
  });
  pi.on("session_shutdown", async () => {
    // MLflow logs traces asynchronously; flush before the worker is stopped.
    if (tracing) { try { await bridge.request("trace_flush", {}); } catch { /* never block shutdown */ } }
    bridge.close();
  });
}
