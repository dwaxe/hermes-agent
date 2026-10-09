"""POSIX native approval diagnostics with a real wait and no command execution."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
from pathlib import Path
import uuid


_COMMAND = "true"
_DESCRIPTION = "Safe approval test only; no command will execute"
_RESULT_LIMIT = 8
_PENDING = {"pending", "waiting"}
logger = logging.getLogger(__name__)


class ApprovalProbeRefused(ValueError):
    """A fixed, recipient-free refusal suitable for the local control response."""


def approval_probe_verbs(runner, loop: asyncio.AbstractEventLoop) -> dict:
    """Build POSIX control handlers; named-pipe dispatch cannot block its owning loop."""
    if os.name != "posix":
        return {}
    from hermes_cli.profiles import get_profile_dir

    profile = runner._primary_profile_name
    homes = getattr(runner, "_served_profile_homes", None) or {}
    home = Path(homes.get(profile) or get_profile_dir(profile))

    def start(params: dict) -> dict:
        return _control_result(_start_probe(runner, home, params), loop)

    def status(params: dict) -> dict:
        return _control_result(_status_on_loop(runner, params), loop)

    return {"approval-probe-start": start, "approval-probe-status": status}


def _control_result(coroutine, loop: asyncio.AbstractEventLoop) -> dict:
    future = asyncio.run_coroutine_threadsafe(coroutine, loop)
    try:
        return future.result(timeout=5.0)
    except concurrent.futures.TimeoutError:
        future.cancel()
        return {"ok": False, "error": "approval probe control request timed out"}


async def _status_on_loop(runner, params: dict) -> dict:
    return _probe_status(runner, params)


def _probe_status(runner, params: dict) -> dict:
    results = getattr(runner, "_approval_probe_results", None) or {}
    test_id = params.get("test_id")
    result = results.get(test_id) if isinstance(test_id, str) else None
    if result is None:
        return {"ok": False, "error": "approval probe not found"}
    return dict(result)


async def _start_probe(runner, home: Path, params: dict) -> dict:
    """Reserve one probe on the owning loop; session lookup and sending happen asynchronously."""
    if set(params) != {"session_key"}:
        return {"ok": False, "error": "only an existing session key may be supplied"}
    session_key = params.get("session_key")
    if not isinstance(session_key, str) or not session_key:
        return {"ok": False, "error": "an existing session key is required"}
    results = getattr(runner, "_approval_probe_results", None)
    if results is None:
        results = runner._approval_probe_results = {}
    if any(result["status"] in _PENDING for result in results.values()):
        return {"ok": False, "error": "another approval probe is pending"}
    while len(results) >= _RESULT_LIMIT:
        results.pop(next(iter(results)))
    test_id = uuid.uuid4().hex
    result = {"ok": True, "test_id": test_id, "request_id": uuid.uuid4().hex,
              "message_id": None, "status": "pending", "resolved": False, "choice": None}
    results[test_id] = result
    tasks = getattr(runner, "_approval_probe_tasks", None)
    if tasks is None:
        tasks = runner._approval_probe_tasks = set()
    task = asyncio.create_task(_run_probe(runner, Path(home), session_key, result))
    runner._retain_background_task(task)
    tasks.add(task)

    def done(completed: asyncio.Task) -> None:
        tasks.discard(completed)
        if completed.cancelled() and result["status"] != "cancelled":
            _cancel_probe(result)

    task.add_done_callback(done)
    return dict(result)


def _probe_adapter(runner, home: Path, source):
    """Require a restored DM to the current profile's configured home user and native adapter."""
    from gateway.run_turn_runner import _renders_exec_approval_buttons
    from hermes_constants import hermes_home_key

    if source is None:
        raise ApprovalProbeRefused("existing session is unavailable")
    home_key = hermes_home_key(home)
    authorization_home = runner._authorization_home_for_source(source)
    if authorization_home is None:
        if runner.config.multiplex_profiles:
            raise ApprovalProbeRefused("session authorization home is unavailable")
        authorization_home = home
    if (hermes_home_key(runner._resolve_profile_home_for_source(source)) != home_key
            or hermes_home_key(authorization_home) != home_key):
        raise ApprovalProbeRefused("session does not belong to the current profile")
    if source.chat_type != "dm" or source.is_bot or not source.user_id:
        raise ApprovalProbeRefused("approval probes require an authenticated direct message")
    target = runner.config.get_home_channel(source.platform)
    if (target is None or target.platform != source.platform
            or not any(value and value in (source.user_id, source.chat_id)
                       for value in (target.user_id, target.chat_id))):
        raise ApprovalProbeRefused("session is not the current platform home user")
    adapter = runner._delivery_adapter_for(source)
    if adapter is None or not _renders_exec_approval_buttons(type(adapter)):
        raise ApprovalProbeRefused("native approvals are unavailable")
    if adapter._is_sender_authorized(
            source.user_id, source.chat_type, source.chat_id, thread_id=source.thread_id) is not True:
        raise ApprovalProbeRefused("approval probe sender is not authorized")
    return adapter


async def _run_probe(runner, home: Path, session_key: str, result: dict) -> None:
    from gateway.run import _async_profile_runtime_scope

    try:
        async with _async_profile_runtime_scope(home):
            entry = await runner.async_session_store.lookup_by_session_key(session_key)
            if entry is None or entry.session_key != session_key or entry.origin is None:
                raise ApprovalProbeRefused("existing session is unavailable")
            source = runner._restored_source(entry)
            adapter = _probe_adapter(runner, home, source)
            decision = await runner._run_in_executor_with_context(
                _wait_for_probe, runner, home, source, adapter, result, asyncio.get_running_loop())
        result["resolved"] = decision.get("resolved") is True
        result["choice"] = decision.get("choice")
        if decision.get("notify_failed"):
            result.update(ok=False, status="failed")
            result.setdefault("error", "native approval send failed")
        elif decision.get("cancelled"):
            result["status"] = "cancelled"
        else:
            result["status"] = "resolved" if result["resolved"] else "timeout"
    except asyncio.CancelledError:
        _cancel_probe(result)
        raise
    except ApprovalProbeRefused as exc:
        result.update(ok=False, status="failed", error=str(exc))
    except Exception:
        logger.exception("Approval probe failed")
        result.update(ok=False, status="failed", error="approval probe failed")


def _cancel_probe(result: dict) -> None:
    from tools.approval import withdraw_gateway_approval

    result.update(status="cancelled", resolved=False, choice=None)
    withdraw_gateway_approval(
        f"approval-probe:{result['test_id']}", result["request_id"], "approval probe cancelled")


def _wait_for_probe(runner, home: Path, source, adapter, result: dict, loop) -> dict:
    """Use the real queue's timeout and cleanup without the execution or persistence callers."""
    from tools.approval_gateway_wait import _await_gateway_decision

    queue_key = f"approval-probe:{result['test_id']}"
    approval_data = {"command": _COMMAND, "description": _DESCRIPTION,
                     "request_id": result["request_id"], "pattern_key": "approval-probe",
                     "allow_session": False, "allow_permanent": False}

    def notify(data: dict) -> None:
        if result["status"] == "cancelled":
            raise RuntimeError("approval probe cancelled")
        future = asyncio.run_coroutine_threadsafe(
            _send_probe(runner, home, source, adapter, queue_key, data, result), loop)
        try:
            future.result(timeout=15.0)
        except Exception as exc:
            future.cancel()
            raise RuntimeError("native approval probe could not be delivered") from exc

    return _await_gateway_decision(queue_key, notify, approval_data)


async def _send_probe(runner, home: Path, source, adapter, queue_key: str,
                      data: dict, result: dict) -> None:
    from gateway.run import _async_profile_runtime_scope

    async with _async_profile_runtime_scope(home):
        if result["status"] == "cancelled":
            raise ApprovalProbeRefused("approval probe cancelled")
        if _probe_adapter(runner, home, source) is not adapter:
            raise ApprovalProbeRefused("approval probe adapter changed")
        sent = await adapter.send_exec_approval(
            chat_id=source.chat_id, command=_COMMAND, session_key=queue_key,
            description=_DESCRIPTION, allow_session=False, allow_permanent=False,
            metadata={"approval_request_id": data["request_id"],
                      "requester_user_id": source.user_id})
    if result["status"] == "cancelled":
        raise ApprovalProbeRefused("approval probe cancelled")
    if sent.success is not True:
        result["error"] = "native approval send failed"
        raise ApprovalProbeRefused(result["error"])
    if not isinstance(sent.message_id, str) or not sent.message_id:
        result["error"] = "native approval did not return a message ID"
        raise ApprovalProbeRefused(result["error"])
    result.update(message_id=sent.message_id, status="waiting")
