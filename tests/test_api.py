"""API request handling: retries, Retry-After, headers and empty-response diagnostics."""

import argparse
import copy
import datetime as dt
import email.message
import io
import json
import urllib.error

import pytest

import ai_log_analyzer as ala


def _args(**overrides):
    base = dict(
        mock_ai=False,
        api_style="chat_completions",
        api_url="https://api.example.test",
        api_path="/v1/chat/completions",
        model="test-model",
        chunk_size=500,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _config(**openai_overrides):
    config = copy.deepcopy(ala.DEFAULT_CONFIG)
    config["openai"].update({"max_retries": 2, "retry_backoff_seconds": 2, **openai_overrides})
    return config


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, retry_after=None):
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(url="https://api.example.test", code=code, msg="err", hdrs=headers, fp=io.BytesIO(b"{}"))


def _chat_payload(text):
    return {"choices": [{"message": {"content": text}}]}


@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr(ala.time, "sleep", lambda s: calls.append(s))
    return calls


def test_parse_retry_after_seconds_and_cap():
    assert ala.parse_retry_after("7") == 7.0
    assert ala.parse_retry_after("3600") == ala.MAX_RETRY_AFTER_SECONDS
    assert ala.parse_retry_after(None) is None
    assert ala.parse_retry_after("soon") is None


def test_parse_retry_after_http_date():
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=10)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    delay = ala.parse_retry_after(future)
    assert delay is not None and 8.0 <= delay <= 10.5
    past = "Wed, 21 Oct 2015 07:28:00 GMT"
    assert ala.parse_retry_after(past) == 0.0


def test_retry_after_header_overrides_backoff(monkeypatch, sleeps):
    attempts = iter([_http_error(429, retry_after="7"), _FakeResponse(_chat_payload("* Error: x"))])
    requests_seen = []

    def fake_urlopen(request, timeout):
        requests_seen.append(request)
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(ala.urllib.request, "urlopen", fake_urlopen)
    assert ala.call_ai(_config(), _args(), "sys", "user") == "* Error: x"
    assert sleeps == [7.0]
    assert len(requests_seen) == 2


def test_backoff_grows_with_attempt_without_retry_after(monkeypatch, sleeps):
    attempts = iter([_http_error(503), _http_error(503), _FakeResponse(_chat_payload("ok"))])

    def fake_urlopen(request, timeout):
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(ala.urllib.request, "urlopen", fake_urlopen)
    assert ala.call_ai(_config(), _args(), "sys", "user") == "ok"
    assert sleeps == [2.0, 4.0]


def test_non_retryable_status_fails_immediately(monkeypatch, sleeps):
    monkeypatch.setattr(ala.urllib.request, "urlopen", lambda request, timeout: (_ for _ in ()).throw(_http_error(401)))
    with pytest.raises(ala.AnalyzerError, match=r"failed after 1 attempt\(s\): API HTTP 401"):
        ala.call_ai(_config(), _args(), "sys", "user")
    assert sleeps == []


def test_empty_response_message_depends_on_stage(monkeypatch, sleeps):
    monkeypatch.setattr(ala.urllib.request, "urlopen", lambda request, timeout: _FakeResponse(_chat_payload("   ")))
    with pytest.raises(ala.AnalyzerError) as chunk_exc:
        ala.call_ai(_config(), _args(), "sys", "user")
    assert "--chunk-size 300" in str(chunk_exc.value)
    assert "3 API attempt(s)" in str(chunk_exc.value)

    with pytest.raises(ala.AnalyzerError) as final_exc:
        ala.call_ai(_config(), _args(), "sys", "x" * 1234, stage="final")
    assert "final report" in str(final_exc.value)
    assert "1234 characters" in str(final_exc.value)
    assert "--chunk-size" not in str(final_exc.value)


def test_authorization_header_only_with_real_key(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["headers"] = dict(request.header_items())
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(_chat_payload("ok"))

    monkeypatch.setattr(ala.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    ala.call_ai(_config(api_key="YOUR_KEY"), _args(), "sys", "user")
    assert "Authorization" not in seen["headers"]

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    ala.call_ai(_config(api_key=""), _args(), "sys", "user")
    assert seen["headers"]["Authorization"] == "Bearer sk-test"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"}
    assert seen["body"]["messages"][1] == {"role": "user", "content": "user"}
    assert seen["body"]["max_tokens"] == ala.DEFAULT_CONFIG["openai"]["max_output_tokens"]


def test_responses_style_payload_and_extraction(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse({"output": [{"content": [{"text": "from responses"}]}]})

    monkeypatch.setattr(ala.urllib.request, "urlopen", fake_urlopen)
    result = ala.call_ai(_config(), _args(api_style="responses", api_path="/v1/responses"), "sys", "user")
    assert result == "from responses"
    assert seen["body"]["instructions"] == "sys"
    assert seen["body"]["input"] == "user"
    assert "max_output_tokens" in seen["body"]


def test_non_ascii_model_output_is_replaced(monkeypatch):
    monkeypatch.setattr(ala.urllib.request, "urlopen", lambda request, timeout: _FakeResponse(_chat_payload("café — ok")))
    assert ala.call_ai(_config(), _args(), "sys", "user") == "caf? ? ok"
