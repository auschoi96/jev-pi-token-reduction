"""Explicit endpoint disclosure policy; not a general-purpose secret detector."""
from dataclasses import asdict, dataclass, field
from pathlib import Path
import hashlib
import json
import re

from .cost import PRICES

SECRET = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\b(?:sk-[A-Za-z0-9_-]{20,}|dapi[a-f0-9]{32})\b|"
    r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+|"
    r"(?im:^\s*(?:export\s+)?(?:[A-Z_]*(?:API_KEY|SECRET|PASSWORD|ACCESS_TOKEN))\s*=\s*[^\s]{8,})"
)


@dataclass(frozen=True)
class Policy:
    version: str = "visibility-v3"
    allow_roots: tuple[str, ...] = ()
    allow_commands: bool = False
    # Failed shell commands (test runs, builds) are the largest outputs; route them like any command output.
    route_failed_commands: bool = True
    min_tokens: int = 1500
    # Ask Jev for visibility. False = structural condensation only (for attribution / Jev outages).
    score_with_jev: bool = True
    # Collapse repeated tracebacks in command output (deterministic, recoverable).
    dedupe_tracebacks: bool = True
    # A chunk is shown at the most compressed rung whose probability of needing more is <= omit_risk.
    # 0.2 / 0.0 calibrated on a 30-task development set (eval/native_tasks.py); see README for held-out results.
    omit_risk: float = 0.2
    # Floor on Jev's self-reported confidence; below it the chunk stays full.
    min_confidence: float = 0.0
    batch_chars: int = 10000
    batch_size: int = 12
    timeout_seconds: float = 8.0
    result_budget_seconds: float = 20.0
    retries: int = 3
    retry_base_seconds: float = 0.5
    # Score cheap outlines first; only chunks not confidently hidden are scored on full text.
    outline_first: bool = False
    outline_batch_size: int = 40
    # cache_aware selection: rebuild history only when the provider prefix cache is already cold
    # or when list-price arithmetic says the rebuild pays for itself.
    cache_ttl_seconds: float = 300.0
    expected_remaining_calls: int = 5
    compaction_budget_tokens: int = 12000
    subcontext_budget_tokens: int = 6000
    # Optional per-model price overrides, merged over jev_router.cost.PRICES.
    prices: dict = field(default_factory=dict)
    denied_names: tuple[str, ...] = field(default=(
        ".env", ".ssh", ".aws", ".databrickscfg", ".jev_key",
        "auth.json", "credentials", "secrets.json", "id_rsa", "id_ed25519",
    ))

    def __post_init__(self):
        if not (0 <= self.min_confidence <= 1) or not (0 <= self.omit_risk < 1) or self.min_tokens < 0:
            raise ValueError("Invalid confidence, risk, or token threshold")
        if min(self.batch_chars, self.batch_size, self.outline_batch_size, self.timeout_seconds,
               self.result_budget_seconds, self.expected_remaining_calls) <= 0 or self.retries < 0:
            raise ValueError("Batch limits, timeouts, and call estimates must be positive")

    @property
    def price_table(self):
        return {**PRICES, **self.prices}

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

    def permits(self, tool, text, task, step):
        # Judge the entire outbound state, not just the candidate text.
        if SECRET.search(text + "\n" + task + "\n" + step):
            return False, "secret_pattern"
        path = tool.get("path") or tool.get("file_path")
        if path and tool["name"].lower() == "read":
            p = Path(path).expanduser().resolve()
            if any(x in self.denied_names or x.startswith(".env.") or x.endswith((".pem", ".key")) for x in p.parts):
                return False, "denied_path"
            if any(p.is_relative_to(Path(root).expanduser().resolve()) for root in self.allow_roots):
                return True, "allowed_root"
            return False, "outside_allowed_roots"
        # An opaque shell/search output can contain data from arbitrary paths.
        return (True, "commands_opted_in") if self.allow_commands else (False, "commands_not_allowed")
