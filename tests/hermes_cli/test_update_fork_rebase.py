"""Live-Git contracts for opt-in fork rebasing during ``hermes update``."""

from pathlib import Path
import subprocess

import pytest

from hermes_cli import main as hermes_main
from hermes_cli import update_cmd


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=check
    )


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _fork_checkout(tmp_path: Path) -> tuple[Path, Path, Path]:
    upstream_work = tmp_path / "upstream-work"
    upstream_work.mkdir()
    _git(upstream_work, "init", "-b", "main")
    _git(upstream_work, "config", "user.name", "Hermes Test")
    _git(upstream_work, "config", "user.email", "hermes@example.test")
    _write(upstream_work / "shared.txt", "base\n")
    _commit(upstream_work, "base")

    upstream_bare = tmp_path / "upstream.git"
    fork_bare = tmp_path / "fork.git"
    _git(tmp_path, "clone", "--bare", str(upstream_work), str(upstream_bare))
    _git(tmp_path, "clone", "--bare", str(upstream_bare), str(fork_bare))
    _git(upstream_work, "remote", "add", "origin", str(upstream_bare))

    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", str(fork_bare), str(checkout))
    _git(checkout, "config", "user.name", "Hermes Test")
    _git(checkout, "config", "user.email", "hermes@example.test")
    _git(checkout, "remote", "add", "upstream", str(upstream_bare))
    return upstream_work, fork_bare, checkout


def _enable_rebase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        update_cmd, "_updates_config", lambda: {"fork_sync_strategy": "rebase"}
    )


def test_fork_rebase_routes_remote_move_through_normal_update_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream, fork, checkout = _fork_checkout(tmp_path)

    _git(checkout, "checkout", "-b", "personal")
    _write(checkout / "personal.txt", "kept\n")
    _commit(checkout, "personal change")
    _git(checkout, "checkout", "main")
    _git(checkout, "merge", "--no-ff", "personal", "-m", "Merge personal change")
    old_head = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    _git(checkout, "push", "origin", "main")

    _write(upstream / "upstream.txt", "new\n")
    upstream_head = _commit(upstream, "upstream change")
    _git(upstream, "push", "origin", "main")

    _enable_rebase(monkeypatch)
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", checkout)
    plan = update_cmd._prepare_checkout_for_update(
        ["git"],
        "main",
        "main",
        is_fork=True,
        assume_yes=True,
        gateway_mode=False,
        gw_input_fn=None,
        switch_branch=False,
        _windows_gateway_resume=None,
    )

    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == old_head
    assert plan.commit_count > 0
    rebased_head = _git(checkout, "rev-parse", "origin/main").stdout.strip()
    assert rebased_head != old_head
    assert _git(fork, "rev-parse", "main").stdout.strip() == rebased_head
    assert _git(checkout, "merge-base", "--is-ancestor", upstream_head, rebased_head).returncode == 0
    assert _git(checkout, "show", f"{rebased_head}:personal.txt").stdout == "kept\n"
    assert "Merge personal change" in _git(
        checkout, "log", "--merges", "--format=%s", rebased_head
    ).stdout
    monkeypatch.setattr(update_cmd, "_rollback_if_pulled_syntax_error", lambda *_args: None)
    pre_pull_sha = update_cmd._pull_updates(
        ["git"],
        "main",
        plan.auto_stash_ref,
        prompt_for_restore=plan.prompt_for_restore,
        gw_input_fn=None,
        discard_local_changes=False,
        keep_stash=False,
    )
    assert pre_pull_sha == old_head
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == rebased_head
    assert len(_git(checkout, "worktree", "list", "--porcelain").stdout.split("worktree ")) == 2


def test_fork_rebase_conflict_leaves_checkout_and_remote_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream, fork, checkout = _fork_checkout(tmp_path)

    _write(checkout / "shared.txt", "personal\n")
    old_head = _commit(checkout, "personal conflict")
    _git(checkout, "push", "origin", "main")

    _write(upstream / "shared.txt", "upstream\n")
    _commit(upstream, "upstream conflict")
    _git(upstream, "push", "origin", "main")

    _enable_rebase(monkeypatch)
    with pytest.raises(SystemExit) as exc_info:
        update_cmd._sync_with_upstream_if_needed(["git"], checkout)

    assert exc_info.value.code == 1
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == old_head
    assert _git(checkout, "rev-parse", "origin/main").stdout.strip() == old_head
    assert _git(fork, "rev-parse", "main").stdout.strip() == old_head
    assert _git(checkout, "status", "--porcelain").stdout == ""
    assert len(_git(checkout, "worktree", "list", "--porcelain").stdout.split("worktree ")) == 2
