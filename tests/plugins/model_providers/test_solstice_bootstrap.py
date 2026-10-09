"""Solstice discovery in the dependency-light updater, followed by runtime inference."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import httpx


def test_solstice_discovery_without_application_dependencies(tmp_path):
    root = Path(__file__).resolve().parents[3]
    script = """
import importlib.util
import sys
sys.path.insert(0, sys.argv[1])
assert importlib.util.find_spec('httpx') is None
import providers
profiles = {p.name: p for p in providers.list_providers()}
profile = profiles['solstice']
assert providers.get_provider_profile('solstice-oauth') is profile
assert callable(profile.auth_handler)
assert callable(profile.refresh_credential)
print(profile.name)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", script, str(root)],
        env={**os.environ, "HERMES_HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "solstice"


def test_discovered_solstice_client_uses_bearer_and_per_user_quota():
    from providers import get_provider_profile

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "candidates": [{"content": {"role": "model", "parts": [{"text": "hello"}]},
                            "finishReason": "STOP"}],
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        client = get_provider_profile("solstice").create_client(
            api_key="test-bearer", http_client=http_client,
        )
        response = client.chat.completions.create(
            model="gemini-flash-latest", messages=[{"role": "user", "content": "hello"}],
        )

    assert response.choices[0].message.content == "hello"
    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == "Bearer test-bearer"
    assert requests[0].url.path == "/v1alpha/models/gemini-flash-latest:generateContentPerUserQuota"
