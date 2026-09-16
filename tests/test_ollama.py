"""Native Ollama API support (openai.api_style = ollama) and finish-reason handling."""

import argparse
import copy
import json
from pathlib import Path

import pytest

import ai_log_analyzer as ala


def _args(**overrides):
    base = dict(
        mock_ai=False,
        api_style="ollama",
        api_url="http://127.0.0.1:11434",
        api_path="/v1/chat/completions",
        model="qwen3.8:27b",
        chunk_size=500,
        num_ctx=0,
        debug_ai=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _config(**ollama_overrides):
    config = copy.deepcopy(ala.DEFAULT_CONFIG)
    config["ollama"].update(ollama_overrides)
    return config


@pytest.mark.parametrize(
    "style,path,expected",
    [
        ("chat_completions", "/v1/chat/completions", "/v1/chat/completions"),
        ("ollama", "/v1/chat/completions", "/api/chat"),
        ("ollama", "", "/api/chat"),
        ("responses", "/v1/chat/completions", "/v1/responses"),
        ("ollama", "/custom/proxy/chat", "/custom/proxy/chat"),
        ("chat_completions", "/openai/v1/chat/completions", "/openai/v1/chat/completions"),
    ],
)
def test_effective_api_path_follows_style_unless_overridden(style, path, expected):
    assert ala.effective_api_path(style, path) == expected


def test_resolve_endpoint_and_legacy_full_url():
    assert ala.resolve_endpoint(_args()) == "http://127.0.0.1:11434/api/chat"
    assert ala.resolve_endpoint(_args(api_url="http://shark:11434/api/chat")) == "http://shark:11434/api/chat"
    assert ala.resolve_endpoint(_args(api_style="chat_completions")) == "http://127.0.0.1:11434/v1/chat/completions"


def test_ollama_payload_shape_and_options():
    payload = ala.build_ollama_payload(_config(), _args(num_ctx=32768), "m", "sys", "user")
    assert payload["model"] == "m"
    assert payload["stream"] is False
    assert payload["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "user"}]
    assert payload["options"] == {"temperature": 0.1, "num_predict": 8192, "num_ctx": 32768}
    assert "think" not in payload and "keep_alive" not in payload
    json.dumps(payload)  # must be serializable


def test_ollama_num_ctx_from_config_when_cli_is_zero():
    payload = ala.build_ollama_payload(_config(num_ctx=65536), _args(num_ctx=0), "m", "s", "u")
    assert payload["options"]["num_ctx"] == 65536
    payload = ala.build_ollama_payload(_config(num_ctx=65536), _args(num_ctx=8192), "m", "s", "u")
    assert payload["options"]["num_ctx"] == 8192, "CLI overrides config"
    payload = ala.build_ollama_payload(_config(num_ctx=0), _args(num_ctx=0), "m", "s", "u")
    assert "num_ctx" not in payload["options"]


@pytest.mark.parametrize(
    "raw,expected",
    [("", None), (False, False), (True, True), ("false", False), ("true", True), ("no", False), ("  ", None)],
)
def test_ollama_think_tri_state(raw, expected):
    payload = ala.build_ollama_payload(_config(think=raw), _args(), "m", "s", "u")
    if expected is None:
        assert "think" not in payload
    else:
        assert payload["think"] is expected


@pytest.mark.parametrize("raw,expected", [("", None), ("5m", "5m"), (0, 0), ("0", "0"), (-1, -1), ("1h", "1h")])
def test_ollama_keep_alive_passthrough(raw, expected):
    payload = ala.build_ollama_payload(_config(keep_alive=raw), _args(), "m", "s", "u")
    assert payload.get("keep_alive") == expected


def test_ollama_config_values_parse_from_conf(tmp_path):
    path = tmp_path / "t.conf"
    path.write_text("openai.api_style = ollama\nollama.num_ctx = 65536\nollama.think = false\nollama.keep_alive = 0\n")
    config = ala.load_config(path)
    assert config["openai"]["api_style"] == "ollama"
    assert config["ollama"] == {"num_ctx": 65536, "think": False, "keep_alive": 0}
    payload = ala.build_ollama_payload(config, _args(), "m", "s", "u")
    assert payload["think"] is False and payload["keep_alive"] == 0 and payload["options"]["num_ctx"] == 65536


def test_extract_ollama_response_with_usage_and_done_reason():
    data = {"message": {"role": "assistant", "content": "* Error: x", "thinking": "..."}, "done": True, "done_reason": "stop", "prompt_eval_count": 10, "eval_count": 3}
    assert ala.extract_response(data, "ollama") == ("* Error: x", "stop", {"prompt": 10, "output": 3})
    assert ala.extract_response({"response": "gen", "done_reason": "length"}, "ollama") == ("gen", "length", {})
    with pytest.raises(ala.AnalyzerError, match="Ollama error: model not found"):
        ala.extract_response({"error": "model not found"}, "ollama")
    with pytest.raises(ala.AnalyzerError, match="Could not extract"):
        ala.extract_response({"done": True}, "ollama")


def test_extract_chat_and_responses_finish_reasons():
    chat = {"choices": [{"message": {"content": "t"}, "finish_reason": "length"}], "usage": {"prompt_tokens": 5, "completion_tokens": 7}}
    assert ala.extract_response(chat, "chat_completions") == ("t", "length", {"prompt": 5, "output": 7})
    assert ala.extract_response({"choices": [{"message": {"content": "t"}}]}, "chat_completions") == ("t", "", {})
    resp = {"output_text": "r", "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "usage": {"input_tokens": 1, "output_tokens": 2}}
    assert ala.extract_response(resp, "responses") == ("r", "max_output_tokens", {"prompt": 1, "output": 2})
    assert ala.extract_response({"output_text": "r", "status": "completed"}, "responses") == ("r", "", {})


def test_truncated_chat_completion_aborts_without_retry(monkeypatch):
    class Resp:
        def __init__(self, payload):
            self.body = json.dumps(payload).encode()

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    calls = []
    monkeypatch.setattr(ala.urllib.request, "urlopen", lambda request, timeout: calls.append(1) or Resp({"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]}))
    monkeypatch.setattr(ala.time, "sleep", lambda s: pytest.fail("no retry expected"))
    with pytest.raises(ala.TruncatedAIResponseError, match="finish reason: length"):
        ala.call_ai(_config(), _args(api_style="chat_completions"), "sys", "user", stage="final")
    assert calls == [1]


def test_thinking_only_answer_gives_empty_response_hint(monkeypatch):
    class Resp:
        def __init__(self, payload):
            self.body = json.dumps(payload).encode()

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ala.urllib.request, "urlopen", lambda request, timeout: Resp({"message": {"content": "", "thinking": "long"}, "done": True, "done_reason": "length"}))
    monkeypatch.setattr(ala.time, "sleep", lambda s: None)
    with pytest.raises(ala.AnalyzerError) as exc:
        ala.call_ai(_config(), _args(), "sys", "user")
    assert "ollama.think = false" in str(exc.value)


def test_num_ctx_negative_is_rejected(run_cli, log_file):
    code, _, err = run_cli(str(log_file(["x error"])), "--num-ctx", "-1")
    assert code == 2 and "--num-ctx must be zero or greater" in err


def test_help_lists_ollama_style(capsys, config_path):
    with pytest.raises(SystemExit):
        ala.main(["--config", str(config_path), "--help"])
    out = capsys.readouterr().out
    assert "{chat_completions,responses,ollama}" in out
    assert "--num-ctx N" in out
