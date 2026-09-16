"""Config parsing, URL normalization and small helpers."""

from pathlib import Path

import pytest

import ai_log_analyzer as ala


def _parse(tmp_path: Path, text: str):
    path = tmp_path / "t.conf"
    path.write_text(text, encoding="utf-8")
    return ala.load_config(path)


def test_key_value_types_are_cast_from_defaults(tmp_path):
    config = _parse(
        tmp_path,
        "logs.chunk_size = 300\nlogs.max_parallel = 4\nlogs.max_final_input_chars = 0\n"
        "openai.temperature = 0.5\nsafety.no_warn = yes\nopenai.model = local  # inline comment\n",
    )
    assert config["logs"]["chunk_size"] == 300
    assert config["logs"]["max_parallel"] == 4
    assert config["logs"]["max_final_input_chars"] == 0
    assert config["openai"]["temperature"] == 0.5
    assert config["safety"]["no_warn"] is True
    assert config["openai"]["model"] == "local"


def test_heredoc_and_list_values(tmp_path):
    config = _parse(
        tmp_path,
        "logs.exclude_patterns = health check, harmless retry\n"
        "logs.exclude_regex_patterns <<EOF\nfoo,bar\n^baz$\nEOF\n"
        "prompts.chunk_user_prefix <<EOF\nOperator note.\nEOF\n",
    )
    assert ala.split_config_list(config["logs"]["exclude_patterns"]) == ["health check", "harmless retry"]
    # Heredoc regexes stay intact even if they contain commas.
    assert config["logs"]["exclude_regex_patterns"] == "foo,bar\n^baz$"
    assert config["prompts"]["chunk_user_prefix"] == "Operator note."


def test_empty_quoted_string_is_empty(tmp_path):
    config = _parse(tmp_path, 'prompts.chunk_user_prefix = ""\nopenai.api_key = ""\n')
    assert config["prompts"]["chunk_user_prefix"] == ""
    assert config["openai"]["api_key"] == ""


def test_invalid_values_raise_friendly_errors(tmp_path):
    with pytest.raises(ala.AnalyzerError, match="Invalid integer"):
        _parse(tmp_path, "logs.chunk_size = many\n")
    with pytest.raises(ala.AnalyzerError, match="Invalid boolean"):
        _parse(tmp_path, "safety.no_warn = maybe\n")
    with pytest.raises(ala.AnalyzerError, match="expected key = value"):
        _parse(tmp_path, "this is not a config line\n")
    with pytest.raises(ala.AnalyzerError, match="Unterminated heredoc"):
        _parse(tmp_path, "prompts.chunk_system <<EOF\nnever closed\n")


def test_missing_config_reports_expected_paths(tmp_path):
    with pytest.raises(ala.AnalyzerError, match="Config file not found"):
        ala.load_config(tmp_path / "missing.conf")


def test_rendered_default_config_matches_example_file():
    example = Path(__file__).resolve().parents[1] / "ai-log-analyzer.conf.example"
    assert ala.render_default_config().strip() == example.read_text(encoding="utf-8").strip()


def test_rendered_default_config_round_trips_defaults(tmp_path):
    path = tmp_path / "default.conf"
    ala.write_default_config(path)
    loaded = ala.load_config(path)
    for section in ("logs", "openai", "safety", "timestamps"):
        for key, value in ala.DEFAULT_CONFIG[section].items():
            if key in ("api_key", "extra_headers"):
                continue
            assert loaded[section][key] == value, f"{section}.{key}"
    assert loaded["prompts"]["chunk_user_prefix"] == ""


@pytest.mark.parametrize(
    "url,path,expected",
    [
        ("https://api.openai.com", "/v1/chat/completions", "https://api.openai.com/v1/chat/completions"),
        ("http://127.0.0.1:4000/", "v1/chat/completions", "http://127.0.0.1:4000/v1/chat/completions"),
        ("http://127.0.0.1:11434/v1/chat/completions", "/v1/chat/completions", "http://127.0.0.1:11434/v1/chat/completions"),
        ("http://127.0.0.1:4000/v1/responses", "/v1/chat/completions", "http://127.0.0.1:4000/v1/responses"),
        ("http://host", "", "http://host"),
    ],
)
def test_normalize_api_url(url, path, expected):
    assert ala.normalize_api_url(url, path) == expected


def test_normalize_api_url_rejects_empty():
    with pytest.raises(ala.AnalyzerError):
        ala.normalize_api_url("", "/v1/chat/completions")


@pytest.mark.parametrize(
    "url,local",
    [
        ("http://127.0.0.1:4000/v1/chat/completions", True),
        ("http://localhost:11434/v1/chat/completions", True),
        ("http://[::1]:4000/v1", True),
        ("http://192.168.1.10:4000/v1", False),
        ("https://api.openai.com/v1/chat/completions", False),
    ],
)
def test_endpoint_is_local(url, local):
    assert ala.endpoint_is_local(url) is local


def test_response_is_ok_tolerates_punctuation_and_case():
    assert ala.response_is_ok("ok")
    assert ala.response_is_ok(" OK. ")
    assert ala.response_is_ok("Okay!")
    assert not ala.response_is_ok("ok, but check sshd")


def test_normalize_chunk_results_drops_ok_chunks_keeps_order():
    results = ["# Chunk 1/3\nok", "# Chunk 2/3\n* Error: b", "# Chunk 3/3\n* Error: c"]
    assert ala.normalize_chunk_results(results) == "# Chunk 2/3\n* Error: b\n\n# Chunk 3/3\n* Error: c"
    assert ala.normalize_chunk_results(["# Chunk 1/1\nOK"]) == "ok"
