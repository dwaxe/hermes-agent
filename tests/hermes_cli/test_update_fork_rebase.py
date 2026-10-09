"""Live-Git contracts for opt-in fork rebasing during ``hermes update``."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli import main as hermes_main
from hermes_cli import update_cmd, update_cmd_commit
from hermes_cli.update_cmd import _updates_config as _load_updates_config


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, encoding="utf-8", capture_output=True, check=check
    )


def _head(repo: Path, ref: str = "HEAD") -> str:
    return _git(repo, "rev-parse", ref).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _head(repo)


@pytest.fixture
def fork_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Hermes Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "hermes@example.test")
    monkeypatch.setattr(update_cmd, "_updates_config", dict)
    update_cmd_commit.begin_update_attempt()

    upstream = tmp_path / "upstream-work"
    upstream.mkdir()
    _git(upstream, "init", "-b", "main")
    (upstream / "shared.txt").write_text("base\n", encoding="utf-8")
    _commit(upstream, "base")
    upstream_bare, fork = tmp_path / "upstream.git", tmp_path / "fork.git"
    _git(tmp_path, "clone", "--bare", str(upstream), str(upstream_bare))
    _git(tmp_path, "clone", "--bare", str(upstream_bare), str(fork))
    _git(upstream, "remote", "add", "origin", str(upstream_bare))
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", str(fork), str(checkout))
    _git(checkout, "remote", "add", "upstream", str(upstream_bare))
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", checkout)
    yield upstream, fork, checkout
    update_cmd_commit.begin_update_attempt()


def _enable_rebase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        update_cmd, "_updates_config", lambda: {"fork_sync_strategy": "rebase"}
    )


def _prepare():
    return update_cmd._prepare_checkout_for_update(
        ["git"], "main", "main", is_fork=True, assume_yes=True, gateway_mode=False,
        gw_input_fn=None, switch_branch=False, target_ref=None, _windows_gateway_resume=None,
    )


def _pull(plan):
    return update_cmd._pull_updates(
        ["git"], "main", plan.auto_stash_ref,
        prompt_for_restore=plan.prompt_for_restore, gw_input_fn=None,
        discard_local_changes=False, keep_stash=False,
        sync_upstream=(plan.fork_sync_strategy != "rebase"), assume_yes=True,
        pre_sync_sha=plan.pre_sync_sha, rollback_branch=plan.rollback_branch,
        in_place_update=plan.in_place_update,
    )


def _assert_one_worktree(checkout: Path) -> None:
    registered = _git(checkout, "worktree", "list", "--porcelain").stdout.splitlines()
    assert [line for line in registered if line.startswith("worktree ")] == [f"worktree {checkout}"]


@pytest.mark.parametrize("checkout_behind", [False, True], ids=["at-origin", "behind-origin"])
def test_fork_rebase_routes_remote_move_through_normal_update_path(
    fork_checkout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checkout_behind: bool,
) -> None:
    upstream, fork, checkout = fork_checkout
    personal_work = checkout
    if checkout_behind:
        personal_work = tmp_path / "personal-work"
        _git(tmp_path, "clone", str(fork), str(personal_work))
    _git(personal_work, "checkout", "-b", "personal")
    (personal_work / "personal.txt").write_text("kept\n", encoding="utf-8")
    _commit(personal_work, "personal change")
    _git(personal_work, "checkout", "main")
    _git(personal_work, "merge", "--no-ff", "personal", "-m", "Merge personal change")
    _git(personal_work, "push", "origin", "main")
    _git(checkout, "fetch", "origin", "main")
    old_head = _head(checkout)

    (upstream / "upstream.txt").write_text("new\n", encoding="utf-8")
    upstream_head = _commit(upstream, "upstream change")
    _git(upstream, "push", "origin", "main")

    _enable_rebase(monkeypatch)
    plan = _prepare()
    assert _head(checkout) == old_head
    assert plan.commit_count > 0
    if not checkout_behind:
        assert _head(checkout, "refs/remotes/origin/main") != old_head
    pre_pull_sha = _pull(plan)
    rebased_head = _head(fork, "refs/heads/main")

    assert pre_pull_sha == old_head
    assert _head(checkout) == rebased_head
    assert _head(checkout, "refs/remotes/origin/main") == rebased_head
    assert _git(checkout, "merge-base", "--is-ancestor", upstream_head, rebased_head).returncode == 0
    assert (checkout / "personal.txt").read_text(encoding="utf-8") == "kept\n"
    assert "Merge personal change" in _git(
        checkout, "log", "--merges", "--format=%s", rebased_head
    ).stdout
    _assert_one_worktree(checkout)


def test_fork_rebase_conflict_leaves_checkout_and_remote_unchanged(
    fork_checkout, monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream, fork, checkout = fork_checkout
    (checkout / "shared.txt").write_text("personal\n", encoding="utf-8")
    old_head = _commit(checkout, "personal conflict")
    _git(checkout, "push", "origin", "main")
    (upstream / "shared.txt").write_text("upstream\n", encoding="utf-8")
    _commit(upstream, "upstream conflict")
    _git(upstream, "push", "origin", "main")

    _enable_rebase(monkeypatch)
    with pytest.raises(SystemExit) as refused:
        update_cmd._sync_with_upstream_if_needed(["git"], checkout, assume_yes=True)

    assert refused.value.code not in (0, None)
    assert _head(checkout) == _head(checkout, "refs/remotes/origin/main") == old_head
    assert _head(fork, "refs/heads/main") == old_head
    assert (checkout / "shared.txt").read_text(encoding="utf-8") == "personal\n"
    assert _git(checkout, "status", "--porcelain").stdout == ""
    _assert_one_worktree(checkout)


def test_exact_lease_rejects_a_remote_change_even_when_tracking_ref_was_refreshed(
    fork_checkout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream, fork, checkout = fork_checkout
    (checkout / "personal.txt").write_text("kept\n", encoding="utf-8")
    old_head = _commit(checkout, "personal change")
    _git(checkout, "push", "origin", "main")
    (upstream / "upstream.txt").write_text("new\n", encoding="utf-8")
    _commit(upstream, "upstream change")
    _git(upstream, "push", "origin", "main")

    other = tmp_path / "other-checkout"
    _git(tmp_path, "clone", str(fork), str(other))
    (other / "racing.txt").write_text("someone else's change\n", encoding="utf-8")
    racing_head = _commit(other, "concurrent fork change")
    _git(other, "push", "origin", "HEAD:refs/heads/racing")
    (checkout / "shared.txt").write_text("uncommitted work\n", encoding="utf-8")
    status_before = _git(checkout, "status", "--porcelain").stdout

    wrapper = tmp_path / "racing_git.py"
    wrapper.write_text(
        "import subprocess, sys\n"
        "args = sys.argv[1:]\n"
        "if 'push' in args and 'origin' in args:\n"
        f"    subprocess.run(['git', '--git-dir', {str(fork)!r}, 'update-ref', "
        f"'refs/heads/main', {racing_head!r}], check=True)\n"
        "    subprocess.run(['git', 'fetch', 'origin', 'main', '--quiet'], check=True)\n"
        "sys.exit(subprocess.run(['git', *args]).returncode)\n",
        encoding="utf-8",
    )
    _enable_rebase(monkeypatch)
    with pytest.raises(SystemExit) as refused:
        update_cmd._sync_with_upstream_if_needed([sys.executable, str(wrapper)], checkout, assume_yes=True)

    assert refused.value.code not in (0, None)
    assert _head(checkout) == old_head
    assert _head(checkout, "refs/remotes/origin/main") == racing_head
    assert _head(fork, "refs/heads/main") == racing_head
    assert _git(checkout, "show", f"{racing_head}:racing.txt").stdout == "someone else's change\n"
    assert (checkout / "shared.txt").read_text(encoding="utf-8") == "uncommitted work\n"
    assert _git(checkout, "status", "--porcelain").stdout == status_before
    _assert_one_worktree(checkout)


@pytest.mark.parametrize("upstream_reverts", [False, True], ids=["equivalent-kept", "equivalent-reverted"])
def test_cherry_equivalent_personal_commits_remain_after_fork_rebase(
    fork_checkout, monkeypatch: pytest.MonkeyPatch, upstream_reverts: bool,
) -> None:
    upstream, fork, checkout = fork_checkout
    (checkout / "personal.txt").write_text("kept\n", encoding="utf-8")
    _commit(checkout, "personal patch to retain")
    (checkout / "extra.txt").write_text("also kept\n", encoding="utf-8")
    _commit(checkout, "another personal patch")
    old_head = _head(checkout)
    _git(checkout, "push", "origin", "main")

    (upstream / "personal.txt").write_text("kept\n", encoding="utf-8")
    equivalent = _commit(upstream, "upstream equivalent patch")
    if upstream_reverts:
        _git(upstream, "revert", "--no-edit", equivalent)
    _git(upstream, "push", "origin", "main")
    _enable_rebase(monkeypatch)
    assert update_cmd._sync_with_upstream_if_needed(["git"], checkout, assume_yes=True) is True

    rebased_head = _head(fork, "refs/heads/main")
    assert rebased_head != old_head
    assert _head(checkout) == old_head
    assert _git(checkout, "show", f"{rebased_head}:personal.txt").stdout == "kept\n"
    assert _git(checkout, "show", f"{rebased_head}:extra.txt").stdout == "also kept\n"
    subjects = _git(checkout, "log", "--format=%s", f"{_head(upstream)}..{rebased_head}").stdout.splitlines()
    assert "personal patch to retain" in subjects
    assert "another personal patch" in subjects
    _assert_one_worktree(checkout)


def test_default_strategy_preserves_divergent_fork_history(fork_checkout) -> None:
    upstream, fork, checkout = fork_checkout
    (checkout / "personal.txt").write_text("kept\n", encoding="utf-8")
    old_head = _commit(checkout, "personal change")
    _git(checkout, "push", "origin", "main")
    (upstream / "upstream.txt").write_text("new\n", encoding="utf-8")
    _commit(upstream, "upstream change")
    _git(upstream, "push", "origin", "main")

    assert update_cmd._sync_with_upstream_if_needed(["git"], checkout, assume_yes=True) is True

    assert _head(checkout) == _head(checkout, "refs/remotes/origin/main") == old_head
    assert _head(fork, "refs/heads/main") == old_head
    assert (checkout / "personal.txt").read_text(encoding="utf-8") == "kept\n"
    assert not (checkout / "upstream.txt").exists()
    _assert_one_worktree(checkout)


def test_rebase_strategy_fast_forwards_a_fork_without_personal_patches(
    fork_checkout, monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream, fork, checkout = fork_checkout
    old_head = _head(checkout)
    (upstream / "upstream.txt").write_text("new\n", encoding="utf-8")
    upstream_head = _commit(upstream, "upstream change")
    _git(upstream, "push", "origin", "main")
    _enable_rebase(monkeypatch)

    plan = _prepare()
    assert _head(checkout) == old_head
    assert plan.commit_count > 0
    assert _head(checkout, "refs/remotes/origin/main") == upstream_head
    assert _head(fork, "refs/heads/main") == upstream_head
    assert _pull(plan) == old_head
    assert _head(checkout) == upstream_head
    assert (checkout / "upstream.txt").read_text(encoding="utf-8") == "new\n"
    _assert_one_worktree(checkout)


def test_broken_critical_file_is_refused_before_publishing_rebased_fork(
    fork_checkout, monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream, fork, checkout = fork_checkout
    (checkout / "personal.txt").write_text("kept\n", encoding="utf-8")
    old_head = _commit(checkout, "personal change")
    _git(checkout, "push", "origin", "main")
    (upstream / "hermes_constants.py").write_text("def broken(:\n", encoding="utf-8")
    _commit(upstream, "broken upstream critical file")
    _git(upstream, "push", "origin", "main")
    _enable_rebase(monkeypatch)

    with pytest.raises(update_cmd.UpstreamTargetBroken) as refused:
        update_cmd._sync_with_upstream_if_needed(["git"], checkout, assume_yes=True)

    assert refused.value.path == "hermes_constants.py"
    assert _head(checkout) == _head(checkout, "refs/remotes/origin/main") == old_head
    assert _head(fork, "refs/heads/main") == old_head
    assert (checkout / "personal.txt").read_text(encoding="utf-8") == "kept\n"
    assert not (checkout / "hermes_constants.py").exists()
    _assert_one_worktree(checkout)


def test_blobless_fork_rebase_preserves_personal_modify_delete_history(
    fork_checkout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream, fork, original = fork_checkout
    history_file = original / "history.txt"
    history_file.write_text("historical personal content\n", encoding="utf-8")
    historical = _commit(original, "personal history addition")
    historical_blob = _git(original, "rev-parse", f"{historical}:history.txt").stdout.strip()
    history_file.write_text("modified personal content\n", encoding="utf-8")
    _commit(original, "personal history modification")
    history_file.unlink()
    _commit(original, "personal history deletion")
    (original / "shared.txt").write_text("personal tip\n", encoding="utf-8")
    _commit(original, "personal tip modification")
    _git(original, "push", "origin", "main")
    _git(fork, "config", "uploadpack.allowFilter", "true")
    upstream_bare = tmp_path / "upstream.git"
    _git(upstream_bare, "config", "uploadpack.allowFilter", "true")
    partial = tmp_path / "blobless-checkout"
    _git(tmp_path, "clone", "--filter=blob:none", fork.as_uri(), str(partial))
    _git(partial, "remote", "add", "upstream", upstream_bare.as_uri())
    missing = _git(partial, "rev-list", "--objects", "--all", "--missing=print").stdout.splitlines()
    assert f"?{historical_blob}" in missing
    old_head = _head(partial)

    (upstream / "upstream.txt").write_text("new\n", encoding="utf-8")
    upstream_head = _commit(upstream, "upstream change")
    _git(upstream, "push", "origin", "main")
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", partial)
    _enable_rebase(monkeypatch)
    assert update_cmd._sync_with_upstream_if_needed(["git"], partial, assume_yes=True) is True

    rebased_head = _head(fork, "refs/heads/main")
    assert _head(partial) == old_head
    assert _git(partial, "merge-base", "--is-ancestor", upstream_head, rebased_head).returncode == 0
    assert _git(partial, "show", f"{rebased_head}:shared.txt").stdout == "personal tip\n"
    assert "history.txt" not in _git(partial, "ls-tree", "--name-only", rebased_head).stdout.splitlines()
    subjects = _git(partial, "log", "--format=%s", f"{upstream_head}..{rebased_head}").stdout.splitlines()
    assert {"personal history addition", "personal history modification", "personal history deletion"} <= set(subjects)
    _assert_one_worktree(partial)


def test_manually_amended_merge_is_refused_before_rewriting_fork(
    fork_checkout, monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream, fork, checkout = fork_checkout
    _git(checkout, "checkout", "-b", "personal")
    (checkout / "personal.txt").write_text("kept\n", encoding="utf-8")
    _commit(checkout, "personal change")
    _git(checkout, "checkout", "main")
    _git(checkout, "merge", "--no-ff", "--no-commit", "personal")
    (checkout / "mergefix.txt").write_text("manual merge amendment\n", encoding="utf-8")
    old_head = _commit(checkout, "Merge personal with manual amendment")
    _git(checkout, "push", "origin", "main")
    (upstream / "upstream.txt").write_text("new\n", encoding="utf-8")
    _commit(upstream, "upstream change")
    _git(upstream, "push", "origin", "main")
    _enable_rebase(monkeypatch)

    with pytest.raises(SystemExit) as refused:
        update_cmd._sync_with_upstream_if_needed(["git"], checkout, assume_yes=True)

    assert refused.value.code not in (0, None)
    assert _head(checkout) == _head(checkout, "refs/remotes/origin/main") == old_head
    assert _head(fork, "refs/heads/main") == old_head
    assert (checkout / "mergefix.txt").read_text(encoding="utf-8") == "manual merge amendment\n"
    assert _git(checkout, "status", "--porcelain").stdout == ""
    _assert_one_worktree(checkout)


def test_config_yaml_opt_in_rebases_fork_and_preserves_personal_file(
    fork_checkout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli.config import atomic_config_write

    upstream, fork, checkout = fork_checkout
    configured_home = tmp_path / "configured-home"
    configured_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(configured_home))
    atomic_config_write(
        configured_home / "config.yaml", {"updates": {"fork_sync_strategy": "rebase"}}
    )
    monkeypatch.setattr(update_cmd, "_updates_config", _load_updates_config)
    (checkout / "personal.txt").write_text("kept\n", encoding="utf-8")
    old_head = _commit(checkout, "personal change")
    _git(checkout, "push", "origin", "main")
    (upstream / "upstream.txt").write_text("new\n", encoding="utf-8")
    upstream_head = _commit(upstream, "upstream change")
    _git(upstream, "push", "origin", "main")

    assert update_cmd._sync_with_upstream_if_needed(["git"], checkout, assume_yes=True) is True

    rebased_head = _head(fork, "refs/heads/main")
    assert rebased_head != old_head
    assert _head(checkout) == old_head
    assert _head(checkout, "refs/remotes/origin/main") == rebased_head
    assert _git(checkout, "merge-base", "--is-ancestor", upstream_head, rebased_head).returncode == 0
    assert _git(checkout, "show", f"{rebased_head}:personal.txt").stdout == "kept\n"
    _assert_one_worktree(checkout)


@pytest.mark.parametrize("switched_earlier", [False, True], ids=["already-running", "checkout-moved"])
def test_publishing_already_rebased_head_preserves_only_needed_update_completion(
    fork_checkout, monkeypatch: pytest.MonkeyPatch, switched_earlier: bool,
) -> None:
    upstream, fork, checkout = fork_checkout
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-10-09T12:00:00Z")
    (checkout / "personal.txt").write_text("kept\n", encoding="utf-8")
    old_origin = _commit(checkout, "personal change")
    _git(checkout, "push", "origin", "main")
    (upstream / "upstream.txt").write_text("new\n", encoding="utf-8")
    upstream_head = _commit(upstream, "upstream change")
    _git(upstream, "push", "origin", "main")
    _git(checkout, "fetch", "upstream", "main")
    _git(checkout, "rebase", "--rebase-merges", "--reapply-cherry-picks", "--empty=keep", upstream_head)
    running_head = _head(checkout)
    assert int(_git(checkout, "rev-list", "--count", f"{running_head}..{old_origin}").stdout) > 0
    if switched_earlier:
        _git(checkout, "checkout", "-b", "parked", old_origin)
    _enable_rebase(monkeypatch)

    plan = update_cmd._prepare_checkout_for_update(
        ["git"], "main", "parked" if switched_earlier else "main",
        is_fork=True, assume_yes=True, gateway_mode=False, gw_input_fn=None,
        switch_branch=switched_earlier, target_ref=None, _windows_gateway_resume=None,
    )

    assert _head(checkout) == _head(fork, "refs/heads/main") == running_head
    assert _head(checkout, "refs/remotes/origin/main") == running_head
    if switched_earlier:
        assert plan.commit_count != 0
        assert plan.pre_sync_sha == old_origin
        assert _pull(plan) == old_origin
    else:
        assert plan.commit_count == 0
    _assert_one_worktree(checkout)
