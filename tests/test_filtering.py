"""Hard filters, tail selection and chunking."""

import pytest

import ai_log_analyzer as ala


def test_keyword_filter_is_case_insensitive_substring():
    assert ala.should_exclude("Health Check passed", ["health check"], [])
    assert not ala.should_exclude("disk failure", ["health check"], [])


def test_regex_filter_and_invalid_regex():
    assert ala.should_exclude("svc: retry 3", [], [r"svc.*retry"])
    with pytest.raises(ala.AnalyzerError, match="Invalid regex"):
        ala.should_exclude("x", [], ["("])


def test_tail_applies_after_filtering(tmp_path):
    lines = [f"keep {i}\n" if i % 2 else f"noise {i}\n" for i in range(20)]
    path, count = ala.write_filtered_lines_to_temp(iter(lines), ["noise"], [], tail_lines=3)
    try:
        assert count == 3
        assert path.read_text().splitlines() == ["keep 15", "keep 17", "keep 19"]
    finally:
        path.unlink()


def test_temp_file_is_private_and_lines_get_newlines(tmp_path):
    path, count = ala.write_filtered_lines_to_temp(iter(["a", "b\n"]), [], [])
    try:
        assert count == 2
        assert path.read_text() == "a\nb\n"
        assert (path.stat().st_mode & 0o777) == 0o600
    finally:
        path.unlink()


def test_iter_chunks_splits_and_rejects_zero(tmp_path):
    path = tmp_path / "in.log"
    path.write_text("".join(f"l{i}\n" for i in range(7)))
    chunks = list(ala.iter_chunks(path, 3))
    assert [len(c) for c in chunks] == [3, 3, 1]
    assert chunks[0] == ["l0", "l1", "l2"]
    with pytest.raises(ala.AnalyzerError):
        list(ala.iter_chunks(path, 0))


def test_missing_and_unreadable_input_files(tmp_path):
    with pytest.raises(ala.AnalyzerError, match="not found"):
        list(ala.iter_input_files([str(tmp_path / "nope.log")]))
