"""One-time setup that points Pi at a Databricks AI Gateway, without ucode.

    python -m jev_router.pi_setup setup --profile P [--model M] [--dir pi-agent]

Writes a private Pi agent config (`pi-agent/models.json` and `settings.json`) that talks to your
workspace's AI Gateway: the workspace host comes from `~/.databrickscfg`, and each request mints a
fresh OAuth token with the Databricks CLI (`databricks auth token`), so nothing that expires is
stored. `make pi` uses this config automatically when it exists; otherwise Pi falls back to its own
configuration, so anyone can instead point Pi at any provider themselves.

Requires: a Databricks CLI profile whose workspace has the AI Gateway coding-agent routes
(`/ai-gateway/anthropic`, `/ai-gateway/codex/v1`) enabled, and `databricks` and `python3` on PATH.
"""
import argparse
import configparser
import json
import shlex
from pathlib import Path

# Foundation models are served per family behind dedicated AI Gateway paths, and each family speaks
# its own Pi `api` dialect (anthropic-messages appends /v1/messages, openai-responses appends
# /responses), so the base URLs below stop just before that suffix. Availability is workspace-
# dependent: edit pi-agent/models.json to match what your workspace's gateway actually exposes.
CODEX_MODELS = ["system.ai.gpt-6-sol", "system.ai.gpt-6-luna", "system.ai.gpt-6-astra"]
CLAUDE_MODELS = ["system.ai.claude-opus-5-5", "system.ai.claude-sonnet-5", "system.ai.claude-haiku-4-5"]
DEFAULT_MODEL = "system.ai.gpt-6-sol"


def host_for_profile(profile: str) -> str:
    """Workspace host for a Databricks CLI profile, read from ~/.databrickscfg."""
    config = configparser.ConfigParser()
    config.read(Path.home() / ".databrickscfg")
    if profile not in config or "host" not in config[profile]:
        raise SystemExit(f"profile {profile!r} with a host not found in ~/.databrickscfg")
    return config[profile]["host"].rstrip("/")


def auth_key_command(profile: str) -> str:
    """Pi `!command` apiKey that mints a fresh workspace OAuth token per request, no jq.

    Pi runs a leading-`!` apiKey value through a shell and uses its stdout, resolving it per request
    rather than once, so the token is never written to the config. `databricks auth token` prints
    JSON; a stdlib Python one-liner extracts the bearer, keeping the no-third-party-dependency rule."""
    return ("!databricks auth token --profile " + shlex.quote(profile) + " --output json"
            " | python3 -c 'import sys, json; print(json.load(sys.stdin)[\"access_token\"])'")


def _canonical(model: str) -> str:
    return model if model.startswith("system.ai.") else "system.ai." + model


def _provider_for(model: str) -> str:
    return "databricks-claude" if "claude" in model else "databricks-openai"


def build_models_json(host: str, profile: str, default_model: str) -> dict:
    """Two Databricks providers (codex + Claude) with the given model as the session default."""
    default_model = _canonical(default_model)
    codex, claude = list(CODEX_MODELS), list(CLAUDE_MODELS)
    provider = _provider_for(default_model)
    target = claude if provider == "databricks-claude" else codex
    if default_model not in target:
        target.insert(0, default_model)
    api_key = auth_key_command(profile)
    return {
        "model": f"{provider}/{default_model}",
        "providers": {
            "databricks-openai": {
                "baseUrl": f"{host}/ai-gateway/codex/v1",
                "api": "openai-responses",
                "apiKey": api_key,
                "authHeader": True,
                "models": [{"id": m} for m in codex],
            },
            "databricks-claude": {
                "baseUrl": f"{host}/ai-gateway/anthropic",
                "api": "anthropic-messages",
                "apiKey": api_key,
                "authHeader": True,
                # The gateway's Anthropic translator rejects per-tool `eager_input_streaming` on Pi's
                # streaming + tools path; with this false Pi sends the legacy beta header instead.
                "compat": {"supportsEagerToolInputStreaming": False},
                "models": [{"id": m} for m in claude],
            },
        },
    }


def setup(args):
    host = host_for_profile(args.profile)
    default_model = _canonical(args.model)
    provider = _provider_for(default_model)
    agent = Path(args.dir)
    agent.mkdir(mode=0o700, parents=True, exist_ok=True)
    models = build_models_json(host, args.profile, default_model)
    # Pin the default provider/model so bare `pi` (no --model) doesn't fall through to an
    # env-key-backed provider when picking an initial model.
    settings = {"defaultProvider": provider, "defaultModel": default_model}
    (agent / "models.json").write_text(json.dumps(models, indent=2) + "\n")
    (agent / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
    print(f"wrote {agent}/models.json and settings.json for {host}")
    print(f"default model: {provider}/{default_model}")
    print(f"`make pi` will use PI_CODING_AGENT_DIR={agent.resolve()} automatically.")
    print("Edit models.json if your workspace's AI Gateway exposes different models.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--profile", required=True, help="Databricks CLI profile (~/.databrickscfg)")
    s.add_argument("--model", default=DEFAULT_MODEL, help=f"default model (default: {DEFAULT_MODEL})")
    s.add_argument("--dir", default="pi-agent", help="Pi agent config dir to write (default: pi-agent)")
    args = parser.parse_args()
    setup(args)


if __name__ == "__main__":
    main()
