"""Native approval polls bound to a request, requester and Photon message ID."""
from __future__ import annotations

from dataclasses import dataclass, field
import time

from gateway.platforms.base import ExecApprovalPrompt, SendResult
from gateway.platforms.base_exec_approval import approval_timeout_seconds
from gateway.platforms.helpers import bounded_put


@dataclass(eq=False)
class _ApprovalPoll:
    session_key: str
    request_id: str
    chat_id: str
    requester: str
    choices: dict[str, str]
    expires_at: float
    early_votes: dict[str, dict] = field(default_factory=dict)


def poll_message_id(event: dict) -> str | None:
    """Spectrum 12.7 vote IDs are GUID:sender:option:action:epoch-ms.

    The poll object has no ID; use the SDK's envelope identity, never its title.
    GUIDs may contain colons, so split at the sender delimiter from the right.
    """
    sender = (event.get("sender") or {}).get("id")
    message_id = event.get("messageId")
    if not isinstance(sender, str) or not sender or not isinstance(message_id, str):
        return None
    guid, separator, suffix = message_id.rpartition(f":{sender}:")
    parts = suffix.rsplit(":", 2)
    if not separator or not guid or len(parts) != 3 or not parts[0]:
        return None
    if parts[1] not in {"selected", "deselected"} or not parts[2].isdigit():
        return None
    return guid


class PhotonApprovalMixin:
    async def _send_exec_approval_prompt(self, prompt: ExecApprovalPrompt) -> SendResult:
        from tools.approval import list_gateway_approvals

        metadata = prompt.metadata or {}
        request_id = metadata.get("approval_request_id")
        requester = metadata.get("requester_user_id")
        choices = {label: choice for label, choice, _ in prompt.actions}
        if (not request_id or not requester or not prompt.session_key
                or len(choices) != len(prompt.actions) or len(choices) < 2
                or not set(choices.values()) <= {"once", "session", "always", "deny"}):
            return SendResult(success=False, error="Unsupported or unbound approval poll")
        if not any(row.get("request_id") == request_id
                   for row in list_gateway_approvals(prompt.session_key)):
            return SendResult(success=False, error="Approval no longer pending")
        # Prune without a ticker: the central queue remains the authority after expiry,
        # teardown, text answers and restart. No persistent permission is stored here.
        polls = getattr(self, "_approval_polls", {})
        self._approval_polls = polls
        self._approval_poll_ids = getattr(self, "_approval_poll_ids", {})
        now = time.monotonic()
        for message_id, poll in list(polls.items()):
            if poll.expires_at <= now:
                polls.pop(message_id, None)
        timeout_s = approval_timeout_seconds()
        poll = _ApprovalPoll(prompt.session_key, request_id, prompt.chat_id, requester, choices, now + timeout_s)
        key = (prompt.chat_id, requester)
        self._approval_sends = getattr(self, "_approval_sends", {})
        sending = self._approval_sends.setdefault(key, [])
        sending.append(poll)
        try:
            result = await self._sidecar_send_poll(prompt.chat_id, prompt.text, list(choices))
            if result.success and result.message_id:
                bounded_put(self._approval_poll_ids, result.message_id, None, self._SENT_IDS_MAX)
                poll.expires_at = time.monotonic() + timeout_s
                polls[result.message_id] = poll
                early_vote = poll.early_votes.get(result.message_id)
                if early_vote is not None:
                    self._handle_approval_vote(early_vote)
            elif result.success:
                return SendResult(success=False, error="Photon did not return a poll message ID")
            return result
        finally:
            sending.remove(poll)
            if not sending:
                self._approval_sends.pop(key)
            poll.early_votes.clear()

    def _capture_early_approval_vote(self, event: dict, message_id: str | None) -> bool:
        # Photon may display a poll before the send response or local persistence finishes.
        # Buffer without blocking intake; only an exact returned ID can release a vote.
        content = event.get("content") or {}
        space = event.get("space") or {}
        sender = (event.get("sender") or {}).get("id")
        sending = getattr(self, "_approval_sends", {}).get((space.get("id"), sender), ())
        if not message_id or not sending or content.get("selected") is not True:
            return False
        chat_type = "group" if space.get("type") == "group" else "dm"
        if self._is_sender_authorized(sender, chat_type, space.get("id")) is not True:
            return False
        for poll in sending:
            if content.get("title") in poll.choices and len(poll.early_votes) < self._SENT_IDS_MAX:
                # First eligible selection wins, just as it does after registration.
                poll.early_votes.setdefault(message_id, event)
        return True

    def _handle_approval_vote(self, event: dict) -> bool:
        """Consume recognized polls inline so approval never queues behind its blocked turn."""
        from tools.approval import resolve_gateway_approval

        message_id = poll_message_id(event)
        polls = getattr(self, "_approval_polls", {})
        space = event.get("space") or {}
        sender = (event.get("sender") or {}).get("id")
        poll = polls.get(message_id)
        if poll is None:
            return (message_id in getattr(self, "_approval_poll_ids", {})
                    or self._capture_early_approval_vote(event, message_id))
        content = event.get("content") or {}
        if poll.expires_at <= time.monotonic():
            polls.pop(message_id, None)
            return True
        if (content.get("selected") is not True or space.get("id") != poll.chat_id
                or sender != poll.requester):
            return True
        chat_type = "group" if space.get("type") == "group" else "dm"
        if self._is_sender_authorized(sender, chat_type, poll.chat_id) is not True:
            return True
        choice = poll.choices.get(content.get("title"))
        if choice is None:
            return True
        # Exact request resolution is atomic and single-use in the existing queue.
        # Keep the poll until expiry to swallow replays and later deselections.
        resolve_gateway_approval(poll.session_key, choice, request_id=poll.request_id)
        return True
