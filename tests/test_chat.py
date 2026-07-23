"""Tests for the editor's AI assistant (fireflyer.web.chat).

The Anthropic client is faked — no network, no API key required. We assert the
glue around it: tool YAML is validated before being applied, invalid YAML is
repaired via the feedback loop, and a tool-less reply yields no YAML change.
"""

from types import SimpleNamespace

import pytest

from fireflyer.web import chat as chat_mod


def _text(s):
    return SimpleNamespace(type="text", text=s)


def _tool(yaml, summary="changed", tool_id="t1"):
    return SimpleNamespace(
        type="tool_use",
        id=tool_id,
        name="update_dashboard",
        input={"yaml": yaml, "summary": summary},
    )


class _FakeMessages:
    """Returns queued responses in order; records each create() call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self._responses.pop(0)
        return SimpleNamespace(content=content)


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)


@pytest.fixture
def fake_client(monkeypatch):
    def install(responses):
        client = _FakeClient(responses)
        monkeypatch.setattr(chat_mod, "_client", client)
        return client

    yield install
    monkeypatch.setattr(chat_mod, "_client", None)


def _valid_yaml(csv_path=None):
    return """name: Test dashboard
charts:
  t:
    type: table
    dataset: orders
    title: Orders
dashboard:
  - ["@40", "t:100"]
"""


def test_applies_valid_tool_yaml(fake_client, orders_csv):
    yaml = _valid_yaml(orders_csv)
    client = fake_client([[_text("Added a table."), _tool(yaml, "Added a table.")]])

    result = chat_mod.run_chat("add a table", "datasets: {}\ncharts: {}\ndashboard: []")

    assert result["yaml"] == yaml
    assert "Added a table." in result["reply"]
    assert len(client.messages.calls) == 1


def test_plain_reply_has_no_yaml(fake_client):
    fake_client([[_text("Pie charts show category share by count.")]])

    result = chat_mod.run_chat("what is a pie chart?", "datasets: {}")

    assert result["yaml"] is None
    assert "Pie charts" in result["reply"]


def test_repairs_invalid_yaml(fake_client, orders_csv):
    good = _valid_yaml(orders_csv)
    # First attempt: a zero width is rejected → DashboardError → repair → good.
    bad = good.replace('"t:100"', '"t:0"')
    client = fake_client([
        [_text("Adding it."), _tool(bad)],
        [_text("Fixed the width."), _tool(good)],
    ])

    result = chat_mod.run_chat("add a table", "datasets: {}")

    assert result["yaml"] == good
    assert len(client.messages.calls) == 2
    # The repair turn carried the validation error back as a tool_result.
    second = client.messages.calls[1]["messages"]
    tool_results = [
        block
        for m in second
        if isinstance(m["content"], list)
        for block in m["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert tool_results and tool_results[0]["is_error"]


def test_gives_up_after_max_attempts(fake_client, orders_csv):
    bad = _valid_yaml(orders_csv).replace('"t:100"', '"t:0"')
    client = fake_client([[_text("Trying."), _tool(bad)] for _ in range(chat_mod.MAX_ATTEMPTS)])

    result = chat_mod.run_chat("add a table", "datasets: {}")

    assert result["yaml"] is None
    assert "couldn't produce a valid layout" in result["reply"]
    assert len(client.messages.calls) == chat_mod.MAX_ATTEMPTS


def test_history_is_threaded_into_messages(fake_client):
    client = fake_client([[_text("Sure.")]])

    chat_mod.run_chat(
        "and bigger?",
        "datasets: {}",
        history=[
            {"role": "user", "content": "add a table"},
            {"role": "assistant", "content": "Added a table."},
        ],
    )

    sent = client.messages.calls[0]["messages"]
    # Two history turns + the new user turn.
    assert len(sent) == 3
    assert sent[0]["content"] == "add a table"
    assert "and bigger?" in sent[-1]["content"]
