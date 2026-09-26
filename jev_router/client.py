#!/usr/bin/env python3
"""Shared TypeSafe Jev client — used by the PreToolUse hook and the eval scripts.

Single source of truth for calling Jev and parsing choice/score answers, so the
validator and the measurement harness can't drift apart.
"""
import json
import os
import ssl
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

JEV_URL = os.environ.get("JEV_API_URL", "https://ai-gateway.vercel.sh/v1/evaluate")
JEV_MODEL = os.environ.get("JEV_MODEL", "typesafe-ai/jev")
# The gateway tries DigitalOcean first for typesafe-ai/jev; it 503'd on every request measured
# 2026-09-24, so pin TypeSafe's own endpoint. Comma-separated; empty = gateway default routing.
JEV_GATEWAY_ONLY = [p for p in os.environ.get("JEV_GATEWAY_ONLY", "typesafe-ai").split(",") if p]


def _api_key():
    key = os.environ.get("AI_GATEWAY_API_KEY") or os.environ.get("TYPESAFE_API_KEY", "")
    if not key:
        try:
            key = Path(os.environ.get("JEV_KEY_FILE", str(Path.home() / ".jev_key"))).read_text().strip()
        except Exception:
            key = ""
    return key


def call_jev(state, questions, *, timeout=15):
    """POST {model, state, questions} to Jev; return the parsed response dict.
    JEV_MOCK (JSON of the `answers` object) short-circuits the network for testing."""
    mock = os.environ.get("JEV_MOCK")
    if mock:
        return {"answers": json.loads(mock), "mock": True}
    key = _api_key()
    if not key:
        raise ValueError("No Jev API key configured")
    gateway = urlsplit(JEV_URL).hostname == "ai-gateway.vercel.sh"
    wire_questions = {
        name: {**question, "type": "boolean"} if gateway and question.get("type") == "noul" else question
        for name, question in questions.items()
    }
    payload = {"model": JEV_MODEL, "state": state, "questions": wire_questions}
    if gateway and JEV_GATEWAY_ONLY:
        payload["providerOptions"] = {"gateway": {"only": JEV_GATEWAY_ONLY}}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        JEV_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    context = ssl.create_default_context()
    if not context.cert_store_stats()["x509"] and Path("/etc/ssl/cert.pem").is_file():
        context = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    with urllib.request.urlopen(req, timeout=timeout, context=context) as response:
        result = json.loads(response.read())
    if gateway:
        for name, question in questions.items():
            answer = result.get("answers", {}).get(name)
            if question.get("type") == "noul" and isinstance(answer, dict) and answer.get("type") == "boolean":
                result["answers"][name] = {"type": "noul", "noul": answer.get("probability")}
    return result


def parse_choice(answer):
    """-> (choice, confidence, probabilities dict)."""
    return answer.get("choice"), float(answer.get("confidence") or 0), (answer.get("probabilities") or {})


def parse_score(answer, levels):
    """Best-effort 0-based level index + confidence, robust to index/string-keyed
    probabilities or a bare scalar score."""
    conf = float(answer.get("confidence") or 0)
    probs = answer.get("probabilities") or {}
    if probs:
        key = max(probs, key=lambda k: probs[k])
        try:
            return int(key), conf
        except (ValueError, TypeError):
            if key in levels:
                return levels.index(key), conf
    s = answer.get("score")
    if isinstance(s, (int, float)):
        return (int(round(s)) if s > 1 else int(round(s * (len(levels) - 1)))), conf
    return None, conf
