"""End-to-end runs of main() with the built-in mock AI or a patched call_ai."""

import io
import sys
import threading
import time

import pytest

import ai_log_analyzer as ala
from conftest import make_lines


def test_file_input_report_mode(run_cli, log_file):
    path = log_file(make_lines(5))
    code, out, err = run_cli(str(path))
    assert code == 0
    assert "Final report" in out
    assert "Chunk analysis results" not in out
    assert f"grep -E 'failed|error' {path}" in out
    assert "Collected 5 filtered log line(s)" in err


def test_stdin_input_uses_input_placeholder(run_cli):
    code, out, _ = run_cli("--mode", "errors", stdin="\n".join(make_lines(3)) + "\n")
    assert code == 0
    assert "# Chunk 1/1" in out
    assert "grep -E 'error|failed' <input>" in out
    assert "Final report" not in out


def test_mode_all_prints_both_sections(run_cli, log_file):
    code, out, _ = run_cli(str(log_file(make_lines(2))), "--mode", "all")
    assert code == 0
    assert out.index("Chunk analysis results") < out.index("Final report")


def test_dry_run_with_print_input_makes_no_ai_call(run_cli, log_file, monkeypatch):
    monkeypatch.setattr(ala, "call_ai", lambda *a, **k: pytest.fail("call_ai must not run in --dry-run"))
    path = log_file(["a error", "b noise", "c error"])
    code, out, err = run_cli(str(path), "--dry-run", "--print-input", "--exclude-pattern", "noise", mock=False)
    assert code == 0
    assert "Filtered input lines" in out
    assert "b noise" not in out and "c error" in out
    assert "Dry run complete" in err


def test_max_lines_exceeded_aborts(run_cli, log_file):
    code, _, err = run_cli(str(log_file(make_lines(30))), "--max-lines", "10")
    assert code == 1
    assert "exceeds the safety limit of 10 lines" in err


def test_all_lines_filtered_gives_empty_report(run_cli, log_file):
    code, out, _ = run_cli(str(log_file(["only noise", "more noise"])), "--exclude-pattern", "noise")
    assert code == 0
    assert "No input lines matched the selected filters" in out


def test_ok_chunks_skip_final_ai_call(run_cli, log_file, monkeypatch):
    calls = []
    original = ala.call_ai

    def counting(config, args, system_prompt, user_prompt, **kw):
        calls.append(kw.get("stage", "chunk"))
        return original(config, args, system_prompt, user_prompt, **kw)

    monkeypatch.setattr(ala, "call_ai", counting)
    code, out, _ = run_cli(str(log_file(["all quiet", "nothing happening"])))
    assert code == 0
    assert "No actionable log errors were found" in out
    assert calls == ["chunk"]


@pytest.mark.parametrize(
    "flag,value,message",
    [
        ("--max-parallel", "0", "--max-parallel must be greater than zero"),
        ("--chunk-size", "0", "--chunk-size must be greater than zero"),
        ("--max-final-input-chars", "-1", "--max-final-input-chars must be zero or greater"),
        ("--tail-lines", "-5", "--tail-lines must be zero or greater"),
    ],
)
def test_invalid_numeric_options(run_cli, log_file, flag, value, message):
    code, _, err = run_cli(str(log_file(make_lines(2))), flag, value)
    assert code == 2
    assert message in err


def _delayed_call_ai(delays):
    """Fake call_ai: chunk N sleeps delays[N-1] seconds; the final stage echoes chunk order."""
    active = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def fake(config, args, system_prompt, user_prompt, **kw):
        if kw.get("stage") == "final":
            headers = [line for line in user_prompt.splitlines() if line.startswith("# Chunk ")]
            return "Summary\n-------\nORDER: " + " ".join(h.split()[2] for h in headers)
        first_line = next(line for line in user_prompt.splitlines() if line.startswith("line-"))
        chunk_no = int(first_line.split("-")[1]) // 10 + 1
        with lock:
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
        time.sleep(delays[chunk_no - 1])
        with lock:
            active["now"] -= 1
        return f"* Error: {first_line} error\n* Search: grep -E 'x' <input>"

    return fake, active


def test_parallel_keeps_chunk_order_and_really_overlaps(run_cli, log_file, monkeypatch):
    fake, active = _delayed_call_ai([0.6, 0.3, 0.05])  # chunk 1 slowest, chunk 3 fastest
    monkeypatch.setattr(ala, "call_ai", fake)
    lines = [f"line-{i:02d}" for i in range(30)]
    started = time.monotonic()
    code, out, _ = run_cli(str(log_file(lines)), "--chunk-size", "10", "--max-parallel", "3", "--mode", "all", mock=False, stdin="")
    elapsed = time.monotonic() - started
    assert code == 0
    assert active["peak"] == 3
    assert elapsed < 0.9, "chunks did not run concurrently"
    assert "ORDER: 1/3 2/3 3/3" in out
    assert out.index("# Chunk 1/3") < out.index("# Chunk 2/3") < out.index("# Chunk 3/3")


def test_sequential_default_never_overlaps(run_cli, log_file, monkeypatch):
    fake, active = _delayed_call_ai([0.05, 0.05, 0.05])
    monkeypatch.setattr(ala, "call_ai", fake)
    lines = [f"line-{i:02d}" for i in range(30)]
    code, out, _ = run_cli(str(log_file(lines)), "--chunk-size", "10", mock=False)
    assert code == 0
    assert active["peak"] == 1
    assert "ORDER: 1/3 2/3 3/3" in out


def test_failing_chunk_aborts_without_final_report(run_cli, log_file, monkeypatch):
    def fake(config, args, system_prompt, user_prompt, **kw):
        if "line-10" in user_prompt:
            raise ala.AnalyzerError("simulated API failure")
        if kw.get("stage") == "final":
            pytest.fail("final report must not be created after a chunk failure")
        return "ok"

    monkeypatch.setattr(ala, "call_ai", fake)
    lines = [f"line-{i:02d}" for i in range(30)]
    for parallel in ("1", "3"):
        code, out, err = run_cli(str(log_file(lines)), "--chunk-size", "10", "--max-parallel", parallel, mock=False)
        assert code == 1, parallel
        assert "simulated API failure" in err
        assert "Final report" not in out


def test_final_input_guard_preserves_chunk_results(run_cli, log_file, tmp_path):
    save_dir = tmp_path / "out"
    code, out, err = run_cli(str(log_file(make_lines(5))), "--max-final-input-chars", "50", "--save-dir", str(save_dir))
    assert code == 1
    assert "exceeds the final report input limit of 50 characters" in err
    assert "--max-final-input-chars 250000" in err
    assert "Chunk analysis results (final report failed)" in out
    assert "Final report" not in out
    saved = sorted(p.name for p in save_dir.iterdir())
    assert len(saved) == 1 and saved[0].startswith("ai-log-chunk-results-")


def test_final_input_guard_disabled_with_zero(run_cli, log_file):
    code, out, _ = run_cli(str(log_file(make_lines(5))), "--max-final-input-chars", "0")
    assert code == 0
    assert "Final report" in out


def test_final_report_api_failure_preserves_chunk_results(run_cli, log_file, monkeypatch):
    def fake(config, args, system_prompt, user_prompt, **kw):
        if kw.get("stage") == "final":
            raise ala.AnalyzerError("final stage exploded")
        return "* Error: something failed\n* Search: grep -E 'failed' <input>"

    monkeypatch.setattr(ala, "call_ai", fake)
    code, out, err = run_cli(str(log_file(make_lines(3))), mock=False)
    assert code == 1
    assert "final stage exploded" in err
    assert "Chunk analysis results (final report failed)" in out
    assert "* Error: something failed" in out


def test_progress_bar_stays_on_one_line(capsys, config_path, log_file):
    path = log_file(make_lines(30))
    code = ala.main(["--config", str(config_path), "--mock-ai", "--chunk-size", "10", "--max-parallel", "2", str(path)])
    err = capsys.readouterr().err
    assert code == 0
    progress_lines = [line for line in err.split("\n") if "%" in line]
    assert len(progress_lines) == 1, progress_lines
    updates = progress_lines[0].split("\r")
    assert updates[-1].startswith("[####")
    assert "100.0% (3/3)" in updates[-1]
    assert "estimated time left: 00:00" in updates[-1]


def test_privacy_confirmation_abort_and_proceed(run_cli, log_file, monkeypatch):
    monkeypatch.setattr(ala, "call_ai", lambda *a, **k: "ok")
    path = log_file(make_lines(2))

    monkeypatch.setattr(ala, "read_confirmation_from_user", lambda: "no")
    code, _, err = run_cli(str(path), mock=False, warn=True)
    assert code == 1
    assert "Aborted by user" in err
    assert "WARNING: 2 log lines will be sent" in err

    monkeypatch.setattr(ala, "read_confirmation_from_user", lambda: "yes")
    code, out, _ = run_cli(str(path), mock=False, warn=True)
    assert code == 0
    assert "No actionable log errors" in out


def test_no_warn_skips_confirmation(run_cli, log_file, monkeypatch):
    monkeypatch.setattr(ala, "call_ai", lambda *a, **k: "ok")
    monkeypatch.setattr(ala, "read_confirmation_from_user", lambda: pytest.fail("must not ask"))
    code, _, err = run_cli(str(log_file(make_lines(2))), "--no-warn", mock=False, warn=True)
    assert code == 0
    assert "WARNING" not in err


def test_bare_interactive_invocation_prints_help(capsys, config_path, monkeypatch):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Tty())
    code = ala.main(["--config", str(config_path)])
    err = capsys.readouterr().err
    assert code == 2
    assert "no log input provided" in err
    assert "usage:" in err


def test_scope_flags_reach_system_prompt(run_cli, log_file, monkeypatch):
    seen = {}

    def fake(config, args, system_prompt, user_prompt, **kw):
        seen.setdefault(kw.get("stage", "chunk"), system_prompt)
        return "ok"

    monkeypatch.setattr(ala, "call_ai", fake)
    code, _, _ = run_cli(str(log_file(make_lines(2))), "--focus-on", "security incidents", "--ignore", "printer noise", "--gently-ignore", "desktop", mock=False)
    assert code == 0
    chunk_prompt = seen["chunk"]
    assert "Focus only on findings related to: security incidents" in chunk_prompt
    assert "- printer noise" in chunk_prompt
    assert "- desktop" in chunk_prompt
    assert "Untrusted input handling" in chunk_prompt


def test_user_message_contains_only_marked_data(run_cli, log_file, monkeypatch):
    seen = {}

    def fake(config, args, system_prompt, user_prompt, **kw):
        seen["user"] = user_prompt
        seen["system"] = system_prompt
        return "ok"

    monkeypatch.setattr(ala, "call_ai", fake)
    code, _, _ = run_cli(str(log_file(["first line", "second line"])), mock=False)
    assert code == 0
    lines = seen["user"].splitlines()
    assert lines[0].startswith("=== BEGIN LOG DATA ") and lines[0].endswith(" ===")
    assert lines[1:3] == ["first line", "second line"]
    assert lines[3].startswith("=== END LOG DATA ")
    nonce = lines[0].split()[4]
    assert f"=== BEGIN LOG DATA {nonce} ===" in seen["system"]


def test_mock_final_stage_detection_is_not_fooled_by_log_words(run_cli, log_file):
    lines = ["app: prioritized chunk analyses failed to load", "app: error in prioritized queue"]
    code, out, _ = run_cli(str(log_file(lines)), "--mode", "errors")
    assert code == 0
    assert "* Error: app: prioritized chunk analyses failed to load" in out


def test_startup_shows_config_path_backend_and_model(run_cli, log_file, config_path):
    code, _, err = run_cli(str(log_file(["quiet"])))
    assert code == 0
    assert f"Config: {config_path}" in err
    assert "Analyzing with endpoint: https://api.openai.com/v1/chat/completions (backend: openai, model: gpt-5-mini)" in err
    code, _, err = run_cli(str(log_file(["quiet"])), "--api", "ollama", "--model", "qwen3.8:27b")
    assert code == 0
    assert "Endpoint appears to be local: http://127.0.0.1:11434/api/chat (backend: ollama, model: qwen3.8:27b)" in err


def test_privacy_warning_names_backend_and_model(run_cli, log_file, monkeypatch):
    monkeypatch.setattr(ala, "call_ai", lambda *a, **k: "ok")
    monkeypatch.setattr(ala, "read_confirmation_from_user", lambda: "no")
    _, _, err = run_cli(str(log_file(["quiet"])), "--api", "ollama", mock=False, warn=True)
    assert "(backend: ollama, model: gemma4:26b)" in err


def test_print_config_path_reports_the_resolved_path(capsys, config_path, monkeypatch, tmp_path):
    assert ala.main(["--config", str(config_path), "--print-config-path"]) == 0
    assert capsys.readouterr().out.strip() == str(config_path)
    env_conf = tmp_path / "env.conf"
    monkeypatch.setenv("AI_LOG_ANALYZER_CONFIG", str(env_conf))
    assert ala.main(["--print-config-path"]) == 0
    assert capsys.readouterr().out.strip() == str(env_conf)
    # no "Config:" chatter on stderr for this query
    ala.main(["--config", str(config_path), "--print-config-path"])
    assert "Config:" not in capsys.readouterr().err
