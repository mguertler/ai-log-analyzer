"""scripts/install-simple.sh driven non-interactively through its stdin prompts (--config-only mode)."""

import subprocess
from pathlib import Path

import pytest

import ai_log_analyzer as ala

REPO = Path(__file__).resolve().parents[1]


def run_installer(answers, cwd=REPO):
    return subprocess.run(
        ["sh", "scripts/install-simple.sh", "--config-only"],
        cwd=cwd,
        input="\n".join(answers) + "\n",
        capture_output=True,
        text=True,
        timeout=60,
    )


def values(path: Path):
    """key -> raw value for simple `key = value` lines (heredocs skipped)."""
    result = {}
    in_heredoc = None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if in_heredoc:
            if stripped == in_heredoc:
                in_heredoc = None
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if "<<" in line and "=" not in line:
            in_heredoc = line.split("<<", 1)[1].strip()
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def test_fresh_install_ollama_branch(tmp_path):
    conf = tmp_path / "ai-log-analyzer.conf"
    proc = run_installer([str(conf), "y", "ollama", "http://shark:11434", "qwen3.8:27b", "32768", "n", "9000", "300", "2", "12000", "100"])
    assert proc.returncode == 0, proc.stderr
    got = values(conf)
    assert got["ai.api"] == "ollama"
    assert got["ai.max_output_tokens"] == "9000"
    assert got["ollama.api_url"] == "http://shark:11434"
    assert got["ollama.model"] == "qwen3.8:27b"
    assert got["ollama.num_ctx"] == "32768"
    assert got["ollama.think"] == "false"
    assert (got["logs.chunk_size"], got["logs.max_parallel"], got["logs.max_lines"], got["logs.tail_lines"]) == ("300", "2", "12000", "100")
    # openai section untouched at its defaults
    assert got["openai.api_url"] == "https://api.openai.com" and got["openai.model"] == "gpt-5-mini"
    assert oct(conf.stat().st_mode & 0o777) == "0o600"
    # the result is a config the tool accepts and resolves to the native endpoint
    config = ala.load_config(conf)
    args = ala.build_parser(config).parse_args([])
    ala.resolve_backend(config, args)
    assert ala.resolve_endpoint(args) == "http://shark:11434/api/chat" and args.num_ctx == 32768


def test_fresh_install_openai_branch(tmp_path):
    conf = tmp_path / "ai-log-analyzer.conf"
    proc = run_installer([str(conf), "y", "openai", "http://127.0.0.1:4000", "sk-test", "Gemma4-26b", "8192", "500", "1", "15000", "0"])
    assert proc.returncode == 0, proc.stderr
    got = values(conf)
    assert got["ai.api"] == "openai"
    assert got["openai.api_url"] == "http://127.0.0.1:4000"
    assert got["openai.api_key"] == "sk-test"
    assert got["openai.model"] == "Gemma4-26b"
    assert got["openai.api_style"] == "chat_completions"
    assert got["ollama.api_url"] == "http://127.0.0.1:11434", "ollama section keeps its defaults"
    config = ala.load_config(conf)
    args = ala.build_parser(config).parse_args([])
    ala.resolve_backend(config, args)
    assert ala.resolve_endpoint(args) == "http://127.0.0.1:4000/v1/chat/completions"


def test_invalid_backend_answer_is_asked_again(tmp_path):
    conf = tmp_path / "ai-log-analyzer.conf"
    proc = run_installer([str(conf), "y", "gpt", "ollama", "http://shark:11434", "qwen3.8:27b", "0", "y", "8192", "500", "1", "15000", "0"])
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.count("Backend (openai/ollama)") == 2
    assert values(conf)["ai.api"] == "ollama" and values(conf)["ollama.think"] == "true"


def test_merge_keeps_values_and_warns_about_unknown_keys(tmp_path):
    conf = tmp_path / "ai-log-analyzer.conf"
    old = ala.render_default_config().replace("logs.chunk_size = 500", "logs.chunk_size = 300").replace("ollama.num_ctx = 0", "ollama.num_ctx = 32768")
    old += "\nopenai.max_output_tokens = 12000\ncustom.flag = yes\n"
    conf.write_text(old, encoding="utf-8")
    proc = run_installer([str(conf), "y", "n"])  # merge yes, no interactive edit
    assert proc.returncode == 0, proc.stderr
    backups = list(tmp_path.glob("ai-log-analyzer.conf.backup.*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == old
    got = values(conf)
    assert got["logs.chunk_size"] == "300" and got["ollama.num_ctx"] == "32768"
    assert got["ai.max_output_tokens"] == "8192", "template default; the old openai.* key is not mapped"
    text = conf.read_text(encoding="utf-8")
    assert "# Preserved custom parameters from previous config" in text
    assert text.index("openai.max_output_tokens = 12000") > text.index("# Preserved custom parameters")
    assert "WARNING: The previous config contains parameters that are not part of the current example config" in proc.stderr
    assert "  openai.max_output_tokens" in proc.stderr and "  custom.flag" in proc.stderr
    assert "move the values manually" in proc.stderr


def test_merge_without_unknown_keys_prints_no_warning(tmp_path):
    conf = tmp_path / "ai-log-analyzer.conf"
    conf.write_text(ala.render_default_config().replace("logs.chunk_size = 500", "logs.chunk_size = 250"), encoding="utf-8")
    proc = run_installer([str(conf), "y", "n"])
    assert proc.returncode == 0, proc.stderr
    assert "WARNING" not in proc.stderr
    assert values(conf)["logs.chunk_size"] == "250"
    assert "Preserved custom parameters" not in conf.read_text(encoding="utf-8")


def test_existing_config_can_be_kept_unchanged(tmp_path):
    conf = tmp_path / "ai-log-analyzer.conf"
    conf.write_text("logs.chunk_size = 123\n", encoding="utf-8")
    proc = run_installer([str(conf), "n", "n", "n"])  # no merge, no overwrite, no edit
    assert proc.returncode == 0, proc.stderr
    assert conf.read_text(encoding="utf-8") == "logs.chunk_size = 123\n"


def test_dialog_proposes_current_values_and_enter_keeps_them(tmp_path):
    conf = tmp_path / "ai-log-analyzer.conf"
    first = run_installer([str(conf), "y", "openai", "http://127.0.0.1:4000", "sk-secret-123", "Gemma4-26b", "12000", "300", "2", "9000", "50"])
    assert first.returncode == 0, first.stderr
    before = values(conf)
    # Upgrade run: merge, then Enter through every question.
    second = run_installer([str(conf), "y", "y"] + [""] * 9)
    assert second.returncode == 0, second.stderr
    assert "Backend (openai/ollama) [openai]" in second.stderr
    assert "API base URL [http://127.0.0.1:4000]" in second.stderr
    assert "API key [keep existing key]" in second.stderr
    assert "sk-secret-123" not in second.stderr, "the key must never be echoed as a default"
    assert "Model [Gemma4-26b]" in second.stderr
    assert "Maximum output tokens [12000]" in second.stderr
    assert "Chunk size in log lines [300]" in second.stderr
    assert "Maximum parallel chunk requests (1 = sequential) [2]" in second.stderr
    assert "Maximum filtered lines before abort [9000]" in second.stderr
    assert "Default tail limit for input lines (0 = no limit) [50]" in second.stderr
    assert values(conf) == before, "Enter through the dialog must not change anything"


def test_dialog_new_key_replaces_old_and_empty_prompt_without_key(tmp_path):
    conf = tmp_path / "ai-log-analyzer.conf"
    fresh = run_installer([str(conf), "y", "openai", "", "", "", "", "", "", "", ""])
    assert fresh.returncode == 0, fresh.stderr
    assert "API key (empty = use OPENAI_API_KEY environment variable) []" in fresh.stderr
    assert values(conf)["openai.api_key"] == '""'
    replaced = run_installer([str(conf), "y", "y", "", "", "sk-new", "", "", "", "", "", ""])
    assert replaced.returncode == 0, replaced.stderr
    assert values(conf)["openai.api_key"] == "sk-new"


def test_dialog_ollama_defaults_follow_config_including_think(tmp_path):
    conf = tmp_path / "ai-log-analyzer.conf"
    text = ala.render_default_config()
    for old, new in (
        ("\nai.api = openai\n", "\nai.api = ollama\n"),
        ("\nollama.api_url = http://127.0.0.1:11434\n", "\nollama.api_url = http://shark:11434\n"),
        ("\nollama.model = gemma4:26b\n", "\nollama.model = qwen3.8:27b\n"),
        ("\nollama.num_ctx = 0\n", "\nollama.num_ctx = 32768\n"),
        ("\nollama.think = true\n", "\nollama.think = false\n"),
    ):
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    conf.write_text(text, encoding="utf-8")
    before = values(conf)
    proc = run_installer([str(conf), "y", "y"] + [""] * 10)
    assert proc.returncode == 0, proc.stderr
    assert "Backend (openai/ollama) [ollama]" in proc.stderr
    assert "Ollama server URL [http://shark:11434]" in proc.stderr
    assert "Ollama model tag (see: ollama list) [qwen3.8:27b]" in proc.stderr
    assert "Context window in tokens (num_ctx) [32768]" in proc.stderr
    assert "Use the thinking phase of the model? [n]" in proc.stderr
    assert values(conf) == before


def test_dialog_does_not_touch_settings_it_does_not_ask_about(tmp_path):
    conf = tmp_path / "ai-log-analyzer.conf"
    text = ala.render_default_config().replace("openai.api_style = chat_completions", "openai.api_style = responses").replace("timestamps.enabled = true", "timestamps.enabled = false")
    conf.write_text(text, encoding="utf-8")
    proc = run_installer([str(conf), "y", "y"] + [""] * 9)
    assert proc.returncode == 0, proc.stderr
    got = values(conf)
    assert got["openai.api_style"] == "responses" and got["timestamps.enabled"] == "false"
