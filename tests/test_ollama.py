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
        api="ollama",
        api_style="ollama",
        api_url="http://127.0.0.1:11434",
        api_path="/api/chat",  # what resolve_backend fills in from ollama.api_path
        model="qwen3.8:27b",
        chunk_size=500,
        num_ctx=0,
        debug_ai=False,
        think=None,
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
        ("responses", "/v1/chat/completions", "/v1/responses"),
        ("responses", "/custom/v1/responses", "/custom/v1/responses"),
        ("ollama", "/api/chat", "/api/chat"),
        ("ollama", "/custom/proxy/chat", "/custom/proxy/chat"),
        ("chat_completions", "/openai/v1/chat/completions", "/openai/v1/chat/completions"),
    ],
)
def test_effective_api_path_follows_style_unless_overridden(style, path, expected):
    assert ala.effective_api_path(style, path) == expected


def test_resolve_endpoint_and_legacy_full_url():
    assert ala.resolve_endpoint(_args()) == "http://127.0.0.1:11434/api/chat"
    assert ala.resolve_endpoint(_args(api_url="http://shark:11434/api/chat")) == "http://shark:11434/api/chat"
    assert ala.resolve_endpoint(_args(api="openai", api_style="chat_completions", api_path="/v1/chat/completions")) == "http://127.0.0.1:11434/v1/chat/completions"


def test_ollama_payload_shape_and_options():
    payload = ala.build_ollama_payload(_config(), _args(num_ctx=32768), "m", "sys", "user")
    assert payload["model"] == "m"
    assert payload["stream"] is False
    assert payload["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "user"}]
    assert payload["options"] == {"temperature": 0.1, "num_predict": 8192, "num_ctx": 32768}
    assert payload["think"] is True, "thinking is on by default"
    assert "keep_alive" not in payload
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


def test_think_cli_overrides_config():
    assert ala.build_ollama_payload(_config(think="true"), _args(think=False), "m", "s", "u")["think"] is False
    assert ala.build_ollama_payload(_config(think="false"), _args(think=True), "m", "s", "u")["think"] is True
    assert ala.build_ollama_payload(_config(think=""), _args(think=None), "m", "s", "u").get("think") is None


def test_think_omitted_once_model_reported_no_support():
    args = _args(think=True)
    args._ollama_think_unsupported = True
    assert "think" not in ala.build_ollama_payload(_config(), args, "m", "s", "u")


def test_default_config_think_is_true_and_empty_stays_valid(tmp_path):
    assert ala.ollama_think_setting(ala.DEFAULT_CONFIG) is True
    path = tmp_path / "t.conf"
    path.write_text('ollama.think = ""\n')
    assert ala.ollama_think_setting(ala.load_config(path)) is None
    path.write_text("ollama.think = false\n")
    assert ala.ollama_think_setting(ala.load_config(path)) is False


def test_ollama_config_values_parse_from_conf(tmp_path):
    path = tmp_path / "t.conf"
    path.write_text("ai.api = ollama\nollama.api_url = http://shark:11434\nollama.model = qwen3.8:27b\nollama.num_ctx = 65536\nollama.think = false\nollama.keep_alive = 0\n")
    config = ala.load_config(path)
    assert config["ai"]["api"] == "ollama"
    assert config["ollama"] == {"api_url": "http://shark:11434", "api_path": "/api/chat", "model": "qwen3.8:27b", "num_ctx": 65536, "think": False, "keep_alive": 0}
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
    code, _, err = run_cli(str(log_file(["x error"])), "--api", "ollama", "--num-ctx", "-1")
    assert code == 2 and "--num-ctx must be zero or greater" in err


def test_help_lists_backend_and_ollama_options(capsys, config_path):
    with pytest.raises(SystemExit):
        ala.main(["--config", str(config_path), "--help"])
    out = capsys.readouterr().out
    assert "--api {openai,ollama}" in out
    assert "{chat_completions,responses}" in out
    assert "--num-ctx N" in out


class _Resp:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, body):
    import email.message
    import io
    import urllib.error

    return urllib.error.HTTPError(url="http://x", code=code, msg="err", hdrs=email.message.Message(), fp=io.BytesIO(json.dumps(body).encode()))


def test_model_without_thinking_support_falls_back_once(monkeypatch, capsys):
    seen = []

    def fake_urlopen(request, timeout):
        body = json.loads(request.data.decode())
        seen.append(body.get("think"))
        if body.get("think") is True:
            raise _http_error(400, {"error": '"gemma4:26b" does not support thinking'})
        return _Resp({"message": {"content": "ok"}, "done": True, "done_reason": "stop"})

    monkeypatch.setattr(ala.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ala.time, "sleep", lambda s: pytest.fail("fallback must not wait"))
    args = _args(model="gemma4:26b")
    assert ala.call_ai(_config(), args, "s", "u") == "ok"
    assert ala.call_ai(_config(), args, "s", "u2") == "ok"
    assert seen == [True, None, None], "second call skips thinking without another 400"
    err = capsys.readouterr().err
    assert err.count("does not support thinking; continuing without it") == 1


def test_thinking_only_answer_is_reported_as_truncated_without_retry(monkeypatch):
    calls = []
    monkeypatch.setattr(ala.urllib.request, "urlopen", lambda request, timeout: calls.append(1) or _Resp({"message": {"content": "", "thinking": "very long"}, "done": True, "done_reason": "length"}))
    monkeypatch.setattr(ala.time, "sleep", lambda s: pytest.fail("no retry expected"))
    with pytest.raises(ala.TruncatedAIResponseError) as exc:
        ala.call_ai(_config(), _args(), "s", "u")
    assert calls == [1]
    assert "--no-think" in str(exc.value) and "ollama.think = false" in str(exc.value)


@pytest.mark.parametrize(
    "style,body",
    [
        ("chat_completions", {"error": {"message": "This model's maximum context length is 32768 tokens. However, you requested 40000 tokens", "code": "context_length_exceeded"}}),
        ("chat_completions", {"error": {"message": "litellm.BadRequestError: prompt is too long"}}),
        ("ollama", {"error": "input is too long for the model num_ctx"}),
    ],
)
def test_context_exceeded_http_error_gets_guidance(monkeypatch, style, body):
    monkeypatch.setattr(ala.urllib.request, "urlopen", lambda request, timeout: (_ for _ in ()).throw(_http_error(400, body)))
    monkeypatch.setattr(ala.time, "sleep", lambda s: None)
    with pytest.raises(ala.AnalyzerError) as exc:
        ala.call_ai(_config(), _args(api_style=style, num_ctx=32768), "s", "u", stage="chunk")
    text = str(exc.value)
    assert "exceeded the model context window" in text
    assert "--chunk-size 300" in text
    if style == "ollama":
        assert "--no-think" in text and "--num-ctx 65536" in text and "Current value: 32768" in text
    else:
        assert "--api ollama --no-think" in text


def test_unrelated_http_400_gets_no_context_guidance(monkeypatch):
    monkeypatch.setattr(ala.urllib.request, "urlopen", lambda request, timeout: (_ for _ in ()).throw(_http_error(400, {"error": "model not found"})))
    with pytest.raises(ala.AnalyzerError) as exc:
        ala.call_ai(_config(), _args(), "s", "u")
    assert "context window" not in str(exc.value)


def test_no_think_note_for_openai_backend(run_cli, log_file):
    code, _, err = run_cli(str(log_file(["quiet"])), "--no-think")
    assert code == 0
    assert "--think/--no-think and --num-ctx only affect --api ollama" in err
    code, _, err = run_cli(str(log_file(["quiet"])), "--api", "ollama", "--no-think")
    assert code == 0 and "only affect" not in err
    code, _, err = run_cli(str(log_file(["quiet"])), "--api", "ollama", "--api-style", "responses")
    assert code == 0 and "--api-style only affects --api openai" in err


def test_backend_resolution_uses_selected_section(tmp_path):
    path = tmp_path / "t.conf"
    path.write_text(
        "ai.api = openai\nopenai.api_url = http://litellm:4000\nopenai.model = proxy-model\n"
        "ollama.api_url = http://shark:11434\nollama.model = qwen3.8:27b\nollama.num_ctx = 4096\n"
    )
    config = ala.load_config(path)

    args = ala.build_parser(config).parse_args([])
    ala.resolve_backend(config, args)
    assert (args.api, args.api_url, args.model, args.api_style, args.num_ctx) == ("openai", "http://litellm:4000", "proxy-model", "chat_completions", 0)
    assert ala.resolve_endpoint(args) == "http://litellm:4000/v1/chat/completions"

    args = ala.build_parser(config).parse_args(["--api", "ollama"])
    ala.resolve_backend(config, args)
    assert (args.api, args.api_url, args.model, args.api_style, args.num_ctx) == ("ollama", "http://shark:11434", "qwen3.8:27b", "ollama", 4096)
    assert ala.resolve_endpoint(args) == "http://shark:11434/api/chat"

    args = ala.build_parser(config).parse_args(["--api", "ollama", "--api-url", "http://other:11434", "--model", "gemma4:26b", "--num-ctx", "0"])
    ala.resolve_backend(config, args)
    assert (args.api_url, args.model, args.num_ctx) == ("http://other:11434", "gemma4:26b", 0)


def test_shared_ai_settings_feed_both_backends():
    config = copy.deepcopy(ala.DEFAULT_CONFIG)
    config["ai"].update({"max_output_tokens": 1234, "temperature": 0.5})
    chat = ala.build_chat_payload(config, "m", "s", "u")
    assert chat["max_tokens"] == 1234 and chat["temperature"] == 0.5
    native = ala.build_ollama_payload(config, _args(), "m", "s", "u")
    assert native["options"]["num_predict"] == 1234 and native["options"]["temperature"] == 0.5
    responses = ala.build_responses_payload(config, "m", "s", "u")
    assert responses["max_output_tokens"] == 1234


def test_ollama_backend_sends_no_authorization_header(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["headers"] = dict(request.header_items())
        return _Resp({"message": {"content": "ok"}, "done": True, "done_reason": "stop"})

    monkeypatch.setattr(ala.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    config = _config()
    config["openai"]["extra_headers"] = {"X-Proxy": "secret"}
    assert ala.call_ai(config, _args(), "s", "u") == "ok"
    assert "Authorization" not in seen["headers"] and "X-proxy" not in seen["headers"] and "X-Proxy" not in seen["headers"]


def test_help_lists_think_flags(capsys, config_path):
    with pytest.raises(SystemExit):
        ala.main(["--config", str(config_path), "--help"])
    out = capsys.readouterr().out
    assert "--think" in out and "--no-think" in out


@pytest.mark.parametrize(
    "line,message",
    [
        ("openai.api_style = ollama", "openai.api_style must be one of chat_completions, responses; got 'ollama'"),
        ("openai.api_style = grpc", "openai.api_style must be one of"),
        ("ai.api = gpt", "ai.api must be one of openai, ollama; got 'gpt'"),
    ],
)
def test_invalid_backend_values_from_config_are_rejected(tmp_path, capsys, line, message):
    path = tmp_path / "t.conf"
    path.write_text(line + "\n")
    code = ala.main(["--config", str(path), "--mock-ai", "--dry-run", str(tmp_path / "t.conf")])
    err = capsys.readouterr().err
    assert code == 2
    assert message in err
    if "api_style" in line and "ollama" in line:
        assert "set ai.api = ollama instead" in err


def test_ollama_style_no_longer_maps_the_openai_default_path():
    assert ala.effective_api_path("ollama", "/v1/chat/completions") == "/v1/chat/completions"
