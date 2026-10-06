"""Gmail availability remains replayable independently of turn state."""

import asyncio
from types import SimpleNamespace

import pytest

from assistant_agent.chat.conversation import Conversation


@pytest.mark.parametrize("resolved_status", ["ready", "unavailable"])
async def test_availability_snapshot_and_replay_survive_turn_completion(resolved_status):
    conversation = Conversation("alice", SimpleNamespace(), asyncio.create_task)
    assert conversation.snapshot()["gmail_status"] == "pending"
    conversation.active_turn_id = "first-turn"
    conversation.transcript_messages = [{"role": "assistant", "text": ""}]
    conversation.turn_started_monotonic = 0
    conversation.turn_timeout_handle = asyncio.get_running_loop().call_later(60, lambda: None)

    conversation.set_gmail_status(resolved_status)
    conversation.set_gmail_status(resolved_status)
    conversation.receive({"type": "result", "subtype": "success", "result": "Hello"})

    assert conversation.snapshot()["gmail_status"] == resolved_status
    assert conversation.snapshot()["active_turn"] is None
    status_events = [event for event in conversation.replay_events if event["type"] == "gmail_status"]
    assert len(status_events) == 1
    assert status_events[0]["gmail_status"] == resolved_status
    assert status_events[0]["conversation_id"] == conversation.conversation_id


async def test_retired_conversation_does_not_publish_late_availability():
    conversation = Conversation("alice", SimpleNamespace(), asyncio.create_task)
    conversation.retire()
    conversation.set_gmail_status("unavailable")
    assert conversation.gmail_status == "pending"
    assert all(event["type"] != "gmail_status" for event in conversation.replay_events)
