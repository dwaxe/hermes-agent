"""Safe approval diagnostics exercise the real Photon poll and approval queue."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from itertools import count
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import weakref

import pytest
import pytest_asyncio

from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
import gateway.run
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.session_identity import RoutingIdentity
from hermes_cli.config import atomic_config_write
from hermes_constants import get_hermes_home
from plugins.platforms.photon.adapter import PhotonAdapter
from tools import approval
from tools.approval_gateway_wait import _ApprovalEntry

CHAT = "+15551234567"
OTHER = "+15557654321"
SESSION = f"agent:main:photon:dm:{CHAT}"
PLATFORM = Platform("photon")
_VOTE_EPOCH = count(1791540000000)


def _vote(message_id, *, sender=CHAT, chat=CHAT, title="Allow Once", selected=True):
    action = "selected" if selected else "deselected"
    return {
        "messageId": f"{message_id}:{sender}:option-uuid:{action}:{next(_VOTE_EPOCH)}",
        "space": {"id": chat, "type": "dm"}, "sender": {"id": sender},
        "content": {"type": "poll_option", "title": title, "selected": selected},
    }


@pytest_asyncio.fixture
async def probe_env(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    home.mkdir()
    atomic_config_write(home / "config.yaml", {"approvals": {"timeout": 30}})
    (home / ".env").write_text("PROBE_OWNER_MARKER=owner-scope\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("hermes_constants.get_process_hermes_home", lambda: home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("PHOTON_NODE_BIN", "node")
    monkeypatch.setattr(approval, "_gateway_queues", {})
    monkeypatch.setattr(approval, "_session_approved", {})
    monkeypatch.setattr(approval, "_permanent_approved", set())
    monkeypatch.setattr(approval, "_permanent_approved_by_home", {})
    config = PlatformConfig(
        enabled=True, extra={"project_id": "test-project", "project_secret": "test-secret"},
        home_channel=HomeChannel(PLATFORM, CHAT, "Test DM", user_id=CHAT),
    )
    adapter = PhotonAdapter(config)
    adapter.set_authorization_check(lambda user, kind, chat: user == CHAT and kind == "dm" and chat == CHAT)
    adapter.handle_message = AsyncMock()
    sent = asyncio.Queue()

    async def transport(path, payload):
        from gateway.platforms._shared import get_scoped_secret

        message_id = f"probe-poll-{adapter._sidecar_call.await_count}"
        await sent.put((message_id, path, payload, get_hermes_home(), get_scoped_secret("PROBE_OWNER_MARKER")))
        return {"messageId": message_id}

    adapter._sidecar_call = AsyncMock(side_effect=transport)
    source = SessionSource(PLATFORM, CHAT, user_id=CHAT)
    source._identity = RoutingIdentity(
        "default", "default", home, home, transport=weakref.ref(adapter)
    )
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={PLATFORM: config}, multiplex_profiles=True)
    runner._primary_profile_name = "default"
    runner._served_profile_homes = {"default": home}
    runner.adapters = {PLATFORM: adapter}
    runner._profile_adapters = {}
    runner._executor = ThreadPoolExecutor(max_workers=4)
    runner.session_store = object()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store, lookup_by_session_key=AsyncMock(
            return_value=SimpleNamespace(origin=source, transport_profile="default", session_key=SESSION)
        ),
    )
    env = SimpleNamespace(home=home, runner=runner, adapter=adapter, source=source, sent=sent)
    yield env
    async def finish_probes():
        while getattr(runner, "_approval_probe_tasks", set()):
            for key in list(approval._gateway_queues):
                if key.startswith("approval-probe:"):
                    approval.resolve_gateway_approval(key, "deny", resolve_all=True)
            await asyncio.sleep(0.01)

    await asyncio.wait_for(finish_probes(), timeout=5)
    await asyncio.to_thread(runner._executor.shutdown, wait=True, cancel_futures=True)


async def _start(env, **params):
    from gateway.run_approval_probe import _start_probe

    return await _start_probe(env.runner, env.home, {"session_key": SESSION, **params})


async def _status(env, test_id, expected):
    from gateway.run_approval_probe import _probe_status

    async def wait():
        while True:
            result = _probe_status(env.runner, {"test_id": test_id})
            if result["status"] == expected:
                return result
            if result["status"] not in {"pending", "waiting"}:
                raise AssertionError(f"Unexpected approval probe outcome: {result}")
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(wait(), timeout=5)


@pytest.mark.asyncio
async def test_native_probe_resolves_once_in_owner_scope_and_keeps_permissions_unchanged(probe_env, tmp_path, monkeypatch):
    env = probe_env
    launch = tmp_path / "launch"
    launch.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch))
    config_before = (env.home / "config.yaml").read_bytes()
    result = await _start(env)
    message_id, path, payload, scope_home, secret = await asyncio.wait_for(env.sent.get(), timeout=5)
    waiting = await _status(env, result["test_id"], "waiting")
    assert path == "/send-poll" and payload["spaceId"] == CHAT
    assert payload["options"] == ["Allow Once", "Deny"]
    assert "true" in payload["title"] and "no command will execute" in payload["title"]
    assert scope_home == env.home and secret == "owner-scope"
    assert waiting["request_id"] == result["request_id"] and waiting["message_id"] == message_id
    await env.adapter._on_inbound_line(json.dumps(_vote(message_id)))
    resolved = await _status(env, result["test_id"], "resolved")
    assert resolved["resolved"] is True and resolved["choice"] == "once"
    assert not approval._gateway_queues
    assert not approval._session_approved and not approval._permanent_approved
    assert not any(approval._permanent_approved_by_home.values())
    assert (env.home / "config.yaml").read_bytes() == config_before
    env.adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_votes_are_bound_to_request_sender_chat_and_single_use(probe_env):
    env = probe_env
    sibling = _ApprovalEntry({"command": "true", "pattern_key": "approval-probe"})
    approval._gateway_queues[SESSION] = [sibling]
    first = await _start(env)
    first_id, *_ = await asyncio.wait_for(env.sent.get(), timeout=5)
    await _status(env, first["test_id"], "waiting")
    assert (await _start(env))["ok"] is False
    keys = list(approval._gateway_queues)
    assert set(keys) == {SESSION, f"approval-probe:{first['test_id']}"}
    assert all(entry.data["command"] == "true" for queue in approval._gateway_queues.values() for entry in queue)
    for event in (_vote(first_id, sender=OTHER), _vote(first_id, chat=OTHER),
                  _vote("unassociated-poll"), _vote(first_id, title="Always Allow"),
                  _vote(first_id, selected=False)):
        await env.adapter._on_inbound_line(json.dumps(event))
    await _status(env, first["test_id"], "waiting")
    await env.adapter._on_inbound_line(json.dumps(_vote(first_id)))
    assert (await _status(env, first["test_id"], "resolved"))["choice"] == "once"
    second = await _start(env)
    second_id, *_ = await asyncio.wait_for(env.sent.get(), timeout=5)
    await _status(env, second["test_id"], "waiting")
    assert first["test_id"] != second["test_id"] and first["request_id"] != second["request_id"]
    await env.adapter._on_inbound_line(json.dumps(_vote(first_id, title="Deny")))
    await _status(env, second["test_id"], "waiting")
    await env.adapter._on_inbound_line(json.dumps(_vote(second_id, title="Deny")))
    assert (await _status(env, second["test_id"], "resolved"))["choice"] == "deny"
    assert approval._gateway_queues == {SESSION: [sibling]}
    assert sibling.result is None and not sibling.event.is_set()
    approval._gateway_queues.pop(SESSION)
    env.adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["missing-session", "foreign-runtime", "foreign-authorization",
                                    "group", "unrelated-home", "unauthorized", "truthy-auth", "missing-adapter",
                                    "missing-authorization"])
async def test_start_refuses_unassociated_or_unauthorized_destinations_without_sending(probe_env, tmp_path, refusal):
    env = probe_env
    mutations = {
        "missing-session": lambda: setattr(env.runner.async_session_store.lookup_by_session_key, "return_value", None),
        "foreign-runtime": lambda: setattr(env.source, "_identity", replace(
            env.source._identity, runtime_home=tmp_path / "other-home")),
        "foreign-authorization": lambda: setattr(env.source, "_identity", replace(
            env.source._identity, authorization_home=tmp_path / "other-home")),
        "group": lambda: setattr(env.source, "chat_type", "group"),
        "unrelated-home": lambda: setattr(env.adapter.config, "home_channel", HomeChannel(
            PLATFORM, OTHER, "Other DM", user_id=OTHER)),
        "unauthorized": lambda: env.adapter.set_authorization_check(lambda *_: False),
        "truthy-auth": lambda: env.adapter.set_authorization_check(lambda *_: "yes"),
        "missing-adapter": env.runner.adapters.clear,
        "missing-authorization": lambda: setattr(env.source, "_identity", replace(
            env.source._identity, multiplexed=False)),
    }
    mutations[refusal]()

    result = await _start(env)

    assert (await _status(env, result["test_id"], "failed"))["ok"] is False
    env.adapter._sidecar_call.assert_not_called()
    assert not approval._gateway_queues


@pytest.mark.asyncio
@pytest.mark.parametrize("parameter", ["command", "chat_id", "profile"])
async def test_start_rejects_caller_controlled_payload_or_destination(probe_env, parameter):
    result = await _start(probe_env, **{parameter: "caller-controlled"})
    assert result["ok"] is False
    probe_env.adapter._sidecar_call.assert_not_called()
    assert not approval._gateway_queues


@pytest.mark.asyncio
async def test_real_timeout_cleans_queue_and_cannot_approve_a_later_request(probe_env):
    env = probe_env
    atomic_config_write(env.home / "config.yaml", {"approvals": {"timeout": 0}})
    result = await _start(env)
    message_id, *_ = await asyncio.wait_for(env.sent.get(), timeout=5)
    timed_out = await _status(env, result["test_id"], "timeout")
    assert timed_out["resolved"] is False and timed_out["choice"] is None
    assert not approval._gateway_queues
    replacement = _ApprovalEntry({"command": "true", "pattern_key": "different-request"})
    approval._gateway_queues[SESSION] = [replacement]
    await env.adapter._on_inbound_line(json.dumps(_vote(message_id)))
    assert replacement.result is None and not replacement.event.is_set()
    env.adapter.handle_message.assert_not_called()
    approval._gateway_queues.pop(SESSION)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["send-error", "missing-message-id"])
async def test_native_send_failure_fails_closed_and_cleans_queue(probe_env, failure):
    env = probe_env
    if failure == "send-error":
        env.adapter._sidecar_call.side_effect = RuntimeError("private transport detail")
    else:
        env.adapter._sidecar_call.side_effect = None
        env.adapter._sidecar_call.return_value = {}
    result = await _start(env)
    failed = await _status(env, result["test_id"], "failed")
    assert failed["resolved"] is False and failed["choice"] is None
    assert "private transport detail" not in json.dumps(failed)
    assert not approval._gateway_queues
    assert not getattr(env.adapter, "_approval_polls", {})
    assert [call.args[0] for call in env.adapter._sidecar_call.call_args_list] == ["/send-poll"]


@pytest.mark.asyncio
@pytest.mark.platforms("posix")
async def test_configured_sender_alias_routes_poll_to_existing_canonical_dm(probe_env):
    from gateway.run_approval_probe import approval_probe_verbs

    env = probe_env
    canonical_chat = "iMessage;-;canonical-dm-guid"
    canonical_key = f"agent:main:photon:dm:{canonical_chat}"
    env.source.chat_id = canonical_chat
    env.runner.async_session_store.lookup_by_session_key.return_value.session_key = canonical_key
    env.adapter.config.home_channel = HomeChannel(PLATFORM, CHAT, "Sender alias")
    env.adapter.set_authorization_check(lambda user, kind, chat: user == CHAT and kind == "dm" and chat == canonical_chat)
    verbs = approval_probe_verbs(env.runner, asyncio.get_running_loop())
    result = await asyncio.to_thread(verbs["approval-probe-start"], {"session_key": canonical_key})
    message_id, path, payload, *_ = await asyncio.wait_for(env.sent.get(), timeout=5)
    await _status(env, result["test_id"], "waiting")
    assert path == "/send-poll" and payload["spaceId"] == canonical_chat
    await env.adapter._on_inbound_line(json.dumps(_vote(message_id, chat=canonical_chat)))
    assert (await _status(env, result["test_id"], "resolved"))["choice"] == "once"
    status = await asyncio.to_thread(verbs["approval-probe-status"], {"test_id": result["test_id"]})
    assert status["choice"] == "once"
    env.adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before-enqueue", "after-waiting"])
async def test_gateway_task_cancellation_withdraws_probe_without_permissions(probe_env, stage):
    env = probe_env
    result = await _start(env)
    task = next(iter(env.runner._approval_probe_tasks))
    if stage == "after-waiting":
        await asyncio.wait_for(env.sent.get(), timeout=5)
        await _status(env, result["test_id"], "waiting")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    cancelled = await _status(env, result["test_id"], "cancelled")
    assert cancelled["resolved"] is False and cancelled["choice"] is None
    assert not approval._gateway_queues
    assert not approval._session_approved and not approval._permanent_approved
    if stage == "before-enqueue":
        env.adapter._sidecar_call.assert_not_called()


@pytest.mark.asyncio
async def test_cancelling_during_native_send_prevents_late_response_from_reviving_probe(probe_env):
    env = probe_env
    visible, release = asyncio.Event(), asyncio.Event()

    async def delayed_send(*_):
        visible.set()
        await release.wait()
        return {"messageId": "late-probe-poll"}

    env.adapter._sidecar_call.side_effect = delayed_send
    result = await _start(env)
    await asyncio.wait_for(visible.wait(), timeout=5)
    task = next(iter(env.runner._approval_probe_tasks))
    try:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await _status(env, result["test_id"], "cancelled")
        assert not approval._gateway_queues
        release.set()

        async def native_finished():
            while "late-probe-poll" not in getattr(env.adapter, "_approval_polls", {}):
                await asyncio.sleep(0.01)

        await asyncio.wait_for(native_finished(), timeout=5)
        await env.adapter._on_inbound_line(json.dumps(_vote("late-probe-poll")))
        cancelled = await _status(env, result["test_id"], "cancelled")
        assert cancelled["resolved"] is False and cancelled["choice"] is None
        assert not approval._gateway_queues
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("multiplex", [False, True], ids=["standalone", "multiplex"])
async def test_existing_owner_resolvers_authorize_legacy_home_session(probe_env, multiplex):
    env = probe_env
    env.runner.config.multiplex_profiles = multiplex
    env.runner.async_session_store.lookup_by_session_key.return_value.transport_profile = None
    del env.source._identity
    result = await _start(env)
    message_id, *_ = await asyncio.wait_for(env.sent.get(), timeout=5)
    await _status(env, result["test_id"], "waiting")
    await env.adapter._on_inbound_line(json.dumps(_vote(message_id)))
    assert (await _status(env, result["test_id"], "resolved"))["choice"] == "once"
    assert not hasattr(env.source, "_identity")
    assert not approval._gateway_queues


@pytest.mark.asyncio
async def test_legacy_session_owned_by_another_profile_cannot_use_current_profile_probe(probe_env):
    from hermes_cli.profiles import get_profile_dir

    env = probe_env
    foreign_home = get_profile_dir("other")
    foreign_home.mkdir(parents=True)
    atomic_config_write(foreign_home / "config.yaml", {})
    other_adapter = PhotonAdapter(env.adapter.config)
    other_adapter.set_authorization_check(lambda *_: True)
    other_adapter._sidecar_call = AsyncMock()
    env.runner._profile_adapters["other"] = {PLATFORM: other_adapter}
    env.source.profile = "other"
    env.runner.async_session_store.lookup_by_session_key.return_value.transport_profile = None
    del env.source._identity

    result = await _start(env)

    assert (await _status(env, result["test_id"], "failed"))["ok"] is False
    env.adapter._sidecar_call.assert_not_called()
    other_adapter._sidecar_call.assert_not_called()
    assert not approval._gateway_queues
