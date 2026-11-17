"""Tests for the editor's AI assistant (fireflyer.web.chat).

The Anthropic and OpenAI clients are faked — no network, no API key required.
We assert the glue around them: tool YAML is validated before being applied,
invalid YAML is repaired via the feedback loop, and a tool-less reply yields no
YAML change.
"""

import json
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


@pytest.fixture(autouse=True)
def _no_ai_settings(monkeypatch):
    """The machine's own AI settings never reach a test."""
    for name in ("FIREFLYER_AI_PROVIDER", "FIREFLYER_AI_MODEL"):
        monkeypatch.delenv(name, raising=False)


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
layout:
  - ["@40", "t:100"]
"""


def test_applies_valid_tool_yaml(fake_client, orders_csv):
    yaml = _valid_yaml(orders_csv)
    client = fake_client([[_text("Added a table."), _tool(yaml, "Added a table.")]])

    result = chat_mod.run_chat("add a table", "datasets: {}\ncharts: {}\nlayout: []")

    assert result["yaml"] == yaml
    assert "Added a table." in result["reply"]
    assert len(client.messages.calls) == 1


def test_dataset_schemas_are_sent_to_the_model(fake_client):
    """The available datasets' column schemas (no data) ride in the user turn so
    the assistant builds charts/calcs from real columns."""
    client = fake_client([[_text("Sure.")]])
    schemas = [
        {"name": "orders", "columns": [
            {"name": "amount", "dtype": "Int64"},
            {"name": "status", "dtype": "String"},
        ]},
    ]

    chat_mod.run_chat("what columns exist?", "name: X", datasets=schemas)

    sent = client.messages.calls[0]["messages"][-1]["content"]
    assert "Available datasets" in sent
    assert "orders: amount:Int64, status:String" in sent


def test_no_datasets_notes_absence(fake_client):
    client = fake_client([[_text("Add a dataset first.")]])
    chat_mod.run_chat("add a chart", "name: X", datasets=[])
    sent = client.messages.calls[0]["messages"][-1]["content"]
    assert "none yet" in sent


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


# --- OpenAI -------------------------------------------------------------------


def _reply(text=None, yaml=None, summary="changed", call_id="c1", arguments=None):
    """An OpenAI chat completion: text, and a tool call when `yaml` (or raw
    `arguments`) is given."""
    calls = None
    if yaml is not None or arguments is not None:
        if arguments is None:
            arguments = json.dumps({"yaml": yaml, "summary": summary})
        calls = [SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(
            name="update_dashboard", arguments=arguments))]
    message = SimpleNamespace(content=text, tool_calls=calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


@pytest.fixture
def fake_openai(monkeypatch):
    monkeypatch.setenv("FIREFLYER_AI_PROVIDER", "openai")
    monkeypatch.setenv("FIREFLYER_AI_MODEL", "some-gpt")

    def install(responses):
        completions = _FakeCompletions(responses)
        monkeypatch.setattr(chat_mod, "_client",
                            SimpleNamespace(chat=SimpleNamespace(completions=completions)))
        return completions

    yield install
    monkeypatch.setattr(chat_mod, "_client", None)


def test_openai_applies_valid_tool_yaml(fake_openai, orders_csv):
    yaml = _valid_yaml(orders_csv)
    completions = fake_openai([_reply("Added a table.", yaml, "A table of orders.")])

    result = chat_mod.run_chat("add a table", "name: X")

    assert result["yaml"] == yaml
    assert "Added a table." in result["reply"] and "A table of orders." in result["reply"]
    sent = completions.calls[0]
    assert sent["model"] == "some-gpt"
    # The system prompt leads the messages; the tool is the same, in OpenAI's shape.
    assert sent["messages"][0] == {"role": "system", "content": chat_mod._SYSTEM_TEXT}
    assert "add a table" in sent["messages"][-1]["content"]
    function = sent["tools"][0]["function"]
    assert function["name"] == "update_dashboard"
    assert function["parameters"] == chat_mod.UPDATE_TOOL["input_schema"]


def test_openai_plain_reply_has_no_yaml(fake_openai):
    fake_openai([_reply("Pie charts show category share by count.")])

    result = chat_mod.run_chat("what is a pie chart?", "name: X")

    assert result == {"reply": "Pie charts show category share by count.", "yaml": None}


def test_openai_repairs_invalid_yaml(fake_openai, orders_csv):
    good = _valid_yaml(orders_csv)
    bad = good.replace('"t:100"', '"t:0"')
    completions = fake_openai([_reply(None, bad, call_id="c1"), _reply(None, good, call_id="c2")])

    result = chat_mod.run_chat("add a table", "name: X")

    assert result["yaml"] == good
    # The repair turn: the refused call, then a tool message answering it.
    call, answer = completions.calls[1]["messages"][-2:]
    assert call["role"] == "assistant" and call["tool_calls"][0]["id"] == "c1"
    assert answer["role"] == "tool" and answer["tool_call_id"] == "c1"
    assert "invalid" in answer["content"]


def test_openai_arguments_that_are_not_json_are_refused_and_retried(fake_openai, orders_csv):
    good = _valid_yaml(orders_csv)
    completions = fake_openai([_reply(None, arguments="{not json"), _reply(None, good)])

    result = chat_mod.run_chat("add a table", "name: X")

    assert result["yaml"] == good
    assert completions.calls[1]["messages"][-1]["role"] == "tool"


# --- Settings -----------------------------------------------------------------


def test_anthropic_is_the_default_with_its_own_model():
    env = {}
    assert chat_mod.provider(env) == "anthropic"
    assert chat_mod.key_name(env) == "ANTHROPIC_API_KEY"
    assert chat_mod.model(env) == chat_mod.PROVIDERS["anthropic"][1]
    assert chat_mod.model({"FIREFLYER_AI_MODEL": "claude-opus-5-5"}) == "claude-opus-5-5"


def test_openai_reads_its_own_key():
    env = {"FIREFLYER_AI_PROVIDER": " OpenAI "}
    assert chat_mod.provider(env) == "openai"
    assert chat_mod.key_name(env) == "OPENAI_API_KEY"


def test_an_unknown_provider_stops_the_start():
    with pytest.raises(SystemExit, match="anthropic or openai"):
        chat_mod.check_env({"FIREFLYER_AI_PROVIDER": "gemini"})


def test_openai_with_a_key_needs_a_model():
    env = {"FIREFLYER_AI_PROVIDER": "openai", "OPENAI_API_KEY": "sk-x"}
    with pytest.raises(SystemExit, match="FIREFLYER_AI_MODEL"):
        chat_mod.check_env(env)
    chat_mod.check_env({**env, "FIREFLYER_AI_MODEL": "some-gpt"})
    chat_mod.check_env({"FIREFLYER_AI_PROVIDER": "openai"})  # no key: the assistant is off
    chat_mod.check_env({"ANTHROPIC_API_KEY": "sk-ant-x"})


@pytest.mark.parametrize("name, key, sdk", [
    ("anthropic", "ANTHROPIC_API_KEY", "anthropic.Anthropic"),
    ("openai", "OPENAI_API_KEY", "openai.OpenAI"),
])
def test_the_client_is_the_providers_sdk(monkeypatch, name, key, sdk):
    import importlib

    monkeypatch.setenv("FIREFLYER_AI_PROVIDER", name)
    monkeypatch.setenv(key, "test-key")
    monkeypatch.setattr(chat_mod, "_client", None)
    module, cls = sdk.split(".")
    assert isinstance(chat_mod._get_client(), getattr(importlib.import_module(module), cls))
    monkeypatch.setattr(chat_mod, "_client", None)
