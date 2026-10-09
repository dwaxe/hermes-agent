"""Npm findings identify the tree audited without misclassifying runtime dependencies."""

import json
from pathlib import Path
import subprocess

from hermes_cli import doctor, doctor_tools


def test_root_audit_does_not_claim_to_audit_pm_browser(monkeypatch, tmp_path):
    (tmp_path / "node_modules").mkdir()
    monkeypatch.setattr(doctor, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(doctor_tools, "_pm_tool_path", lambda _: Path("npm"))
    from gateway.platforms import whatsapp_common
    monkeypatch.setattr(whatsapp_common, "resolve_whatsapp_bridge_dir", lambda: tmp_path / "bridge")
    audits = []
    monkeypatch.setattr(doctor_tools, "_audit_one", lambda *args: audits.append(args))

    doctor_tools._check_npm_audit(False)

    root_audit = next(audit for audit in audits if audit[3] == ["--workspaces=false"])
    assert root_audit[1] == tmp_path
    assert root_audit[2] == "Root npm"


def test_workspace_findings_do_not_dismiss_runtime_dependencies(monkeypatch, capsys):
    result = subprocess.CompletedProcess([], 1, stdout=json.dumps({
        "metadata": {"vulnerabilities": {"high": 1}},
    }))
    monkeypatch.setattr(doctor_tools.subprocess, "run", lambda *args, **kwargs: result)
    issues = []

    doctor_tools._audit_one("npm", "/unused", "ui-tui workspace", ["--workspace", "ui-tui"], issues)

    output = capsys.readouterr().out
    assert "runtime and build tooling" in output
    assert "not runtime" not in output
    assert issues == ["ui-tui workspace has 1 npm vulnerability"]
