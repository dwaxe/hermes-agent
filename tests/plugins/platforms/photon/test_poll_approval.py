"""Photon approval polls exercise the real runner, adapter and approval queue."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import ExecApprovalPrompt
import gateway.run  # initialize gateway imports before per-test filesystem guards
from gateway.run_turn_runner import TurnRunner
from plugins.platforms.photon.adapter import PhotonAdapter
from tools import approval
from tools.approval_gateway_wait import _ApprovalEntry, _await_gateway_decision

CHAT = "+15551234567"
OTHER = "+15557654321"
SESSION = f"agent:main:photon:dm:{CHAT}"
POLL = "iMessage;-;poll-guid"
DATA = {"command": "rm -rf /tmp/build", "description": "recursive delete", "pattern_key": "delete"}


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("PHOTON_PROJECT_ID", "test-project")
    monkeypatch.setenv("PHOTON_PROJECT_SECRET", "test-secret")
    monkeypatch.setattr("plugins.platforms.photon.adapter.find_node_executable", lambda _: "node")
    adapter = PhotonAdapter(PlatformConfig(enabled=True, extra={}))
    adapter.set_authorization_check(lambda user, kind, chat: user == CHAT and chat == CHAT)
    adapter._sidecar_call = AsyncMock(return_value={"messageId": POLL})
    adapter.handle_message = AsyncMock()
    monkeypatch.setattr(approval, "_gateway_queues", {})
    entry = _ApprovalEntry(DATA)
    approval._gateway_queues[SESSION] = [entry]
    return adapter, entry


def runner(adapter):
    turn = object.__new__(TurnRunner)
    turn._ctx = SimpleNamespace(
        _status_adapter=adapter, _status_chat_id=CHAT, _status_thread_metadata=None,
        session_key=SESSION, source=SimpleNamespace(user_id=CHAT, chat_id=CHAT, platform="photon"),
    )
    turn._close_native_stream_boundary = lambda _: None
    def schedule(coro, label):
        result = asyncio.run(coro)
        return SimpleNamespace(result=lambda timeout: result)
    turn._schedule = schedule
    return turn


def vote(*, poll=POLL, sender=CHAT, chat=CHAT, title="Allow Once", selected=True):
    action = "selected" if selected else "deselected"
    return {
        "messageId": f"{poll}:{sender}:option-uuid:{action}:1791540000000",
        "space": {"id": chat, "type": "dm"}, "sender": {"id": sender},
        "content": {"type": "poll_option", "title": title, "selected": selected, "pollTitle": "Poll"},
    }


def send(adapter, entry, **flags):
    runner(adapter)._approval_notify_sync({**entry.data, **flags})


@pytest.mark.parametrize("choice,title", [("once", "Allow Once"), ("session", "Allow Session"),
                                         ("always", "Always Allow"), ("deny", "Deny")])
def test_runner_poll_and_vote_resolve_exact_request(setup, choice, title):
    adapter, entry = setup
    sibling = _ApprovalEntry({**DATA, "command": "rm -rf /tmp/other"})
    approval._gateway_queues[SESSION].insert(0, sibling)
    # Mark the adapter busy: the vote must bypass both text-message guards.
    adapter._active_sessions[SESSION] = asyncio.Event()
    send(adapter, entry)
    call = adapter._sidecar_call.call_args
    assert call.args[0] == "/send-poll"
    assert call.args[1]["spaceId"] == CHAT
    assert DATA["command"] in call.args[1]["title"]
    assert "recursive delete" in call.args[1]["title"]
    assert "5 minutes" in call.args[1]["title"]
    assert call.args[1]["options"] == ["Allow Once", "Allow Session", "Always Allow", "Deny"]
    asyncio.run(adapter._dispatch_inbound(vote(title=title)))
    assert entry.result == choice and entry.event.is_set()
    assert sibling.result is None and not sibling.event.is_set()
    # Replays, changes and deselections cannot resolve another request.
    asyncio.run(adapter._dispatch_inbound(vote(title="Always Allow")))
    asyncio.run(adapter._dispatch_inbound(vote(selected=False)))
    assert sibling.result is None
    adapter.handle_message.assert_not_called()


@pytest.mark.parametrize("suffix,valid", [
    (":option:selected:42", True),
    (":option:selected:42:seq:0", True),
    (":option:with:colons:deselected:42:seq:17", True),
    (":option:selected:42:seq", False),
    (":option:selected:42:seq:", False),
    (":option:selected:42:seq:-1", False),
    (":option:selected:42:seq:nope", False),
    (":option:selected:42:seq:²", False),
    (":option:selected:42:seq:17:trailing", False),
    (":option:unknown:42:seq:17", False),
    (":option:selected:unknown:seq:17", False),
])
def test_poll_message_id_accepts_only_optional_unsigned_sequence(suffix, valid):
    from plugins.platforms.photon.approval import poll_message_id

    poll, sender = "iMessage:poll:with:colons", "requester:with:colons"
    event = {"messageId": f"{poll}:{sender}{suffix}", "sender": {"id": sender}}
    assert poll_message_id(event) == (poll if valid else None)


def test_sequenced_votes_preserve_bound_and_single_use_approval(setup):
    adapter, entry = setup
    sibling = _ApprovalEntry({**DATA, "command": "rm -rf /tmp/other"})
    approval._gateway_queues[SESSION].insert(0, sibling)
    send(adapter, entry)

    def sequenced(sequence=42, **changes):
        event = vote(**changes)
        event["messageId"] += f":seq:{sequence}"
        return event

    for event in [sequenced(sender=OTHER), sequenced(chat=OTHER),
                  sequenced(poll="another-poll"), sequenced(selected=False),
                  sequenced(selected="true"), sequenced(sequence=-1),
                  sequenced(sequence="")]:
        asyncio.run(adapter._dispatch_inbound(event))
        assert entry.result is None and not entry.event.is_set()

    accepted = sequenced(sequence=0)
    asyncio.run(adapter._dispatch_inbound(accepted))
    assert entry.result == "once" and entry.event.is_set()
    assert sibling.result is None and not sibling.event.is_set()
    for event in [accepted, sequenced(title="Always Allow"), sequenced(selected=False)]:
        asyncio.run(adapter._dispatch_inbound(event))
    assert entry.result == "once" and sibling.result is None
    adapter.handle_message.assert_not_called()


@pytest.mark.parametrize("change", [
    {"sender": OTHER}, {"chat": OTHER}, {"poll": "another-poll"},
    {"title": "/approve always"}, {"title": "unknown"}, {"selected": False},
])
def test_invalid_votes_cannot_approve(setup, change):
    adapter, entry = setup
    send(adapter, entry)
    asyncio.run(adapter._dispatch_inbound(vote(**change)))
    assert entry.result is None and not entry.event.is_set()
    adapter.handle_message.assert_not_called()


@pytest.mark.parametrize("auth", [False, None, "yes"])
def test_authorization_is_checked_again_and_fails_closed(setup, auth):
    adapter, entry = setup
    send(adapter, entry)
    adapter.set_authorization_check(lambda *args: auth)
    asyncio.run(adapter._dispatch_inbound(vote()))
    assert entry.result is None


def test_missing_explicit_selection_or_sender_cannot_approve(setup):
    adapter, entry = setup
    send(adapter, entry)
    for field, container in [("selected", "content"), ("id", "sender")]:
        event = vote()
        del event[container][field]
        asyncio.run(adapter._dispatch_inbound(event))
    assert entry.result is None


@pytest.mark.parametrize("pending", [True, False])
def test_expired_duplicate_votes_are_ignored(setup, monkeypatch, pending):
    adapter, entry = setup
    send(adapter, entry)
    expires = adapter._approval_polls[POLL].expires_at
    monkeypatch.setattr("plugins.platforms.photon.approval.time.monotonic", lambda: expires + 1)
    if not pending:
        approval.clear_session(SESSION)
    asyncio.run(adapter._dispatch_inbound(vote()))
    asyncio.run(adapter._dispatch_inbound(vote(title="Always Allow")))
    assert entry.result is None
    adapter.handle_message.assert_not_called()


@pytest.mark.parametrize("settled", ["text", "teardown"])
def test_stale_poll_cannot_approve_replacement_request(setup, settled):
    adapter, entry = setup
    send(adapter, entry)
    if settled == "text":
        approval.resolve_gateway_approval(SESSION, "deny", request_id=entry.data["request_id"])
    else:
        approval.clear_session(SESSION)
    replacement = _ApprovalEntry(DATA)
    approval._gateway_queues[SESSION] = [replacement]
    asyncio.run(adapter._dispatch_inbound(vote(title="Always Allow")))
    assert replacement.result is None
    adapter.handle_message.assert_not_called()


@pytest.mark.parametrize("flags,options", [
    ({"smart_denied": True}, ["Allow Once", "Deny"]),
    ({"allow_session": False}, ["Allow Once", "Deny"]),
    ({"allow_permanent": False}, ["Allow Once", "Allow Session", "Deny"]),
])
def test_poll_offers_only_allowed_tiers(setup, flags, options):
    adapter, entry = setup
    send(adapter, entry, **flags)
    assert adapter._sidecar_call.call_args.args[1]["options"] == options
    asyncio.run(adapter._dispatch_inbound(vote(title="Always Allow")))
    assert entry.result is None


@pytest.mark.parametrize("failure", ["send", "missing_id", "missing_requester", "missing_request"])
def test_runner_falls_back_to_text(setup, failure):
    adapter, entry = setup
    if failure == "send":
        adapter._sidecar_call.side_effect = [RuntimeError("unsupported /send-poll"), {"messageId": "text"}]
    elif failure == "missing_id":
        adapter._sidecar_call.side_effect = [{"messageId": None}, {"messageId": "text"}]
    turn = runner(adapter)
    data = dict(entry.data)
    if failure == "missing_requester":
        turn._ctx.source.user_id = None
    elif failure == "missing_request":
        data.pop("request_id")
    turn._approval_notify_sync(data)
    calls = adapter._sidecar_call.call_args_list
    assert calls[-1].args[0] == "/send"
    assert "/approve" in calls[-1].args[1]["text"]
    assert not getattr(adapter, "_approval_polls", {})
    assert entry.result is None


def test_unsupported_choices_fail_without_sending(setup):
    adapter, entry = setup
    prompt = ExecApprovalPrompt(CHAT, SESSION, "Approval", [("Run", "yolo", ""), ("Deny", "deny", "")],
                                "cmd", "reason", False,
                                {"approval_request_id": entry.data["request_id"], "requester_user_id": CHAT})
    assert not asyncio.run(adapter._send_exec_approval_prompt(prompt)).success
    adapter._sidecar_call.assert_not_called()


def test_real_wait_timeout_rejects_late_vote(setup, monkeypatch):
    adapter, _ = setup
    approval._gateway_queues.clear()
    monkeypatch.setattr("tools.approval_gateway_wait._poll_event", lambda *a, **kw: "timeout")
    decision = _await_gateway_decision(SESSION, lambda data: runner(adapter)._approval_notify_sync(data), DATA)
    assert decision["choice"] is None and not decision["resolved"]
    replacement = _ApprovalEntry(DATA)
    approval._gateway_queues[SESSION] = [replacement]
    asyncio.run(adapter._dispatch_inbound(vote()))
    assert replacement.result is None


def test_unrelated_poll_cannot_become_typed_approval(setup):
    adapter, entry = setup
    approval.clear_session(SESSION)
    asyncio.run(adapter._dispatch_inbound(vote(poll="unrelated", title="/approve")))
    assert entry.result is None
    adapter.handle_message.assert_not_called()


def test_group_poll_requires_original_requester(setup):
    adapter, entry = setup
    adapter.set_authorization_check(lambda *args: True)
    send(adapter, entry)
    for sender in [OTHER, CHAT]:
        event = vote(sender=sender)
        event["space"]["type"] = "group"
        asyncio.run(adapter._dispatch_inbound(event))
        assert entry.result == ("once" if sender == CHAT else None)


def test_malformed_vote_identity_and_other_adapter_cannot_approve(setup, monkeypatch):
    adapter, entry = setup
    send(adapter, entry)
    for message_id in [POLL, f"{POLL}:{CHAT}:option:selected:unknown", f"{POLL}:{OTHER}:option:selected:42"]:
        event = vote()
        event["messageId"] = message_id
        asyncio.run(adapter._dispatch_inbound(event))
    other = PhotonAdapter(PlatformConfig(enabled=True, extra={}))
    other.handle_message = AsyncMock()
    other.set_authorization_check(lambda *args: True)
    asyncio.run(other._dispatch_inbound(vote()))
    assert entry.result is None
    other.handle_message.assert_not_called()


def test_real_wait_once_is_single_use(setup, monkeypatch):
    adapter, _ = setup
    approval._gateway_queues.clear()
    def poll_event(*args, **kwargs):
        asyncio.run(adapter._dispatch_inbound(vote()))
        return "set"
    monkeypatch.setattr("tools.approval_gateway_wait._poll_event", poll_event)
    decision = _await_gateway_decision(SESSION, lambda data: runner(adapter)._approval_notify_sync(data), DATA)
    assert decision == {"resolved": True, "choice": "once", "reason": None}
    replacement = _ApprovalEntry(DATA)
    approval._gateway_queues[SESSION] = [replacement]
    asyncio.run(adapter._dispatch_inbound(vote()))
    assert replacement.result is None


def test_readonly_sidecar_mirror_can_normalize_approval_vote(tmp_path, monkeypatch):
    import json
    import subprocess
    import shutil
    from plugins.platforms.photon import sidecar_paths

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is not installed")
    monkeypatch.delenv("PHOTON_SIDECAR_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sidecar_paths, "_dir_writable", lambda _: False)
    # Force the mirror path even on hosts with dependencies baked into the source.
    monkeypatch.setattr(sidecar_paths, "_lock_newer_than_install", lambda _: True)
    mirror = sidecar_paths.resolve_sidecar_dir()
    script = (f"import {{ normalizePollVote }} from {json.dumps((mirror / 'poll-vote.mjs').as_uri())};"
              "console.log(JSON.stringify(normalizePollVote({title:'Allow Once',selected:true})))")
    result = subprocess.run([node, "--input-type=module", "-e", script],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {
        "type": "poll_option", "title": "Allow Once", "selected": True, "pollTitle": None}



@pytest.mark.asyncio
@pytest.mark.parametrize("gap", ["response", "persistence"])
@pytest.mark.parametrize("target", [POLL, "unrelated-poll"])
async def test_vote_before_send_finishes_uses_exact_poll_identity(setup, monkeypatch, gap, target):
    adapter, entry = setup
    sibling = _ApprovalEntry({**DATA, "command": "rm -rf /tmp/other"})
    approval._gateway_queues[SESSION].insert(0, sibling)
    visible, release, received = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def wait_for_send(*args, **kwargs):
        visible.set()
        await release.wait()
        return {"messageId": POLL}
    if gap == "response":
        adapter._sidecar_call = wait_for_send
    else:
        monkeypatch.setattr("plugins.platforms.photon.adapter._record_sent_text_async", wait_for_send)
    dispatch = adapter._dispatch_inbound
    async def receive(event):
        received.set()
        await dispatch(event)
    monkeypatch.setattr(adapter, "_dispatch_inbound", receive)
    sending = asyncio.create_task(adapter.send_exec_approval(
        CHAT, DATA["command"], SESSION,
        metadata={"approval_request_id": entry.data["request_id"], "requester_user_id": CHAT}))
    await visible.wait()
    event = json.dumps(vote(poll=target))
    inbound = asyncio.create_task(adapter._on_inbound_line(event))
    await received.wait()
    assert entry.result is None
    release.set()
    result, _ = await asyncio.gather(sending, inbound)
    assert result.success
    assert entry.result == ("once" if target == POLL else None)
    assert sibling.result is None
    await adapter._on_inbound_line(event)
    assert sibling.result is None
    assert not getattr(adapter, "_approval_sends", {})
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_inflight_send_releases_early_vote_without_approval(setup, monkeypatch, cancelled):
    adapter, entry = setup
    visible, release, received = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def fail_send(*args):
        visible.set()
        await release.wait()
        raise RuntimeError("poll send failed")
    adapter._sidecar_call = fail_send
    dispatch = adapter._dispatch_inbound
    async def receive(event):
        received.set()
        await dispatch(event)
    monkeypatch.setattr(adapter, "_dispatch_inbound", receive)
    sending = asyncio.create_task(adapter.send_exec_approval(
        CHAT, DATA["command"], SESSION,
        metadata={"approval_request_id": entry.data["request_id"], "requester_user_id": CHAT}))
    await visible.wait()
    inbound = asyncio.create_task(adapter._on_inbound_line(json.dumps(vote())))
    await received.wait()
    if cancelled:
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
    else:
        release.set()
        assert not (await sending).success
    await inbound
    assert not getattr(adapter, "_approval_sends", {})
    assert entry.result is None
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["target", "sibling"])
async def test_concurrent_sends_do_not_block_votes_or_control_messages(setup, first):
    adapter, entry = setup
    sibling = _ApprovalEntry({**DATA, "command": "sibling-command"})
    approval._gateway_queues[SESSION].append(sibling)
    visible = {name: asyncio.Event() for name in ("target", "sibling")}
    release = {name: asyncio.Event() for name in visible}
    async def send_poll(path, payload):
        name = "sibling" if "sibling-command" in payload["title"] else "target"
        visible[name].set()
        await release[name].wait()
        return {"messageId": POLL if name == "target" else "sibling-poll"}
    adapter._sidecar_call = send_poll
    sending = {
        name: asyncio.create_task(adapter.send_exec_approval(
            CHAT, item.data["command"], SESSION,
            metadata={"approval_request_id": item.data["request_id"], "requester_user_id": CHAT}))
        for name, item in (("target", entry), ("sibling", sibling))
    }
    inbound = None
    try:
        await asyncio.gather(*(ready.wait() for ready in visible.values()))
        inbound = asyncio.create_task(adapter._on_inbound_line(json.dumps(vote())))
        await asyncio.wait_for(asyncio.shield(inbound), timeout=2)
        assert entry.result is None
        await adapter._on_inbound_line(json.dumps({
            "messageId": "control", "space": {"id": CHAT, "type": "dm"},
            "sender": {"id": CHAT}, "content": {"type": "text", "text": "/stop"}}))
        assert adapter.handle_message.call_args.args[0].text == "/stop"
        adapter.handle_message.reset_mock()
        release[first].set()
        assert (await sending[first]).success
        assert entry.result == ("once" if first == "target" else None)
        second = "sibling" if first == "target" else "target"
        release[second].set()
        assert (await sending[second]).success
        assert entry.result == "once"
        assert sibling.result is None
        assert not getattr(adapter, "_approval_sends", {})
        adapter.handle_message.assert_not_called()
    finally:
        for ready in release.values():
            ready.set()
        await asyncio.gather(*sending.values(), *([inbound] if inbound else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_first_early_selection_cannot_become_permanent_approval(setup):
    adapter, entry = setup
    visible, release = asyncio.Event(), asyncio.Event()
    async def send_poll(*args):
        visible.set()
        await release.wait()
        return {"messageId": POLL}
    adapter._sidecar_call = send_poll
    sending = asyncio.create_task(adapter.send_exec_approval(
        CHAT, DATA["command"], SESSION,
        metadata={"approval_request_id": entry.data["request_id"], "requester_user_id": CHAT}))
    await visible.wait()
    try:
        await asyncio.wait_for(adapter._on_inbound_line(json.dumps(vote())), timeout=2)
        later = vote(title="Always Allow")
        later["messageId"] = later["messageId"].replace("option-uuid", "always-option")
        await adapter._on_inbound_line(json.dumps(later))
        release.set()
        assert (await sending).success
        assert entry.result == "once"
    finally:
        release.set()
        await sending
