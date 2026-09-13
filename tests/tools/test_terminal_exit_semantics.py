"""Tests for terminal command exit code semantic interpretation."""

import pytest

from tools.terminal_tool import _interpret_exit_code, _is_expected_nonzero_exit


class TestInterpretExitCode:
    """Test _interpret_exit_code returns correct notes for known command semantics."""

    # ---- exit code 0 always returns None ----

    def test_success_returns_none(self):
        assert _interpret_exit_code("grep foo bar", 0) is None
        assert _interpret_exit_code("diff a b", 0) is None
        assert _interpret_exit_code("test -f /etc/passwd", 0) is None

    # ---- grep / rg family: exit 1 = no matches ----

    @pytest.mark.parametrize("cmd", [
        "grep 'pattern' file.txt",
        "egrep 'pattern' file.txt",
        "fgrep 'pattern' file.txt",
        "rg 'foo' .",
        "ag 'foo' .",
        "ack 'foo' .",
    ])
    def test_grep_family_no_matches(self, cmd):
        result = _interpret_exit_code(cmd, 1)
        assert result is not None
        assert "no matches" in result.lower()


    # ---- diff: exit 1 = files differ ----

    def test_diff_files_differ(self):
        result = _interpret_exit_code("diff file1 file2", 1)
        assert result is not None
        assert "differ" in result.lower()

    def test_colordiff_files_differ(self):
        result = _interpret_exit_code("colordiff file1 file2", 1)
        assert result is not None
        assert "differ" in result.lower()


    # ---- test / [: exit 1 = condition false ----

    def test_test_condition_false(self):
        result = _interpret_exit_code("test -f /nonexistent", 1)
        assert result is not None
        assert "false" in result.lower()


    # ---- find: exit 1 = partial success ----


    # ---- curl: various informational codes ----


    # ---- git: exit 1 is context-dependent ----


    # ---- pipeline / chain handling ----


    # ---- full paths ----


    # ---- env var prefix ----


    # ---- unknown commands return None ----


    # ---- edge cases ----


    def test_only_env_vars(self):
        """Command with only env var assignments, no actual command."""
        assert _interpret_exit_code("FOO=bar", 1) is None

    @pytest.mark.parametrize(
        "command",
        ["./grep x file", "/tmp/diff a b", "PATH=/tmp grep x file"],
    )
    def test_custom_executable_does_not_receive_system_utility_semantics(
        self, command
    ):
        assert _interpret_exit_code(command, 1) is None
        assert _is_expected_nonzero_exit(command, 1) is False

    def test_standard_absolute_executable_keeps_known_semantics(self):
        assert _interpret_exit_code("/usr/bin/grep x file", 1) == (
            "No matches found (not an error)"
        )
        assert _is_expected_nonzero_exit("/usr/bin/grep x file", 1) is True

    def test_bare_executable_is_advisory_but_not_required_health_evidence(self):
        assert _interpret_exit_code("grep x file", 1) == (
            "No matches found (not an error)"
        )
        assert _is_expected_nonzero_exit("grep x file", 1) is False

    @pytest.mark.parametrize(
        "command",
        [
            "cd /missing && grep x file",
            "false || grep x file",
            "printf x | grep y",
            "true; grep x file",
            "grep x file > /missing/out",
            "grep x file < /missing/in",
            "grep `false` file",
            "grep $(false) file",
        ],
    )
    def test_compound_commands_do_not_receive_simple_exit_semantics(self, command):
        assert _interpret_exit_code(command, 1) is None
        assert _is_expected_nonzero_exit(command, 1) is False

    @pytest.mark.parametrize(
        "command",
        [
            "grep 'a|b;c' file",
            r"grep a\|b file",
            "grep 'a>b' file",
            r"grep a\>b file",
            "grep '$(' file",
            'grep "a;b" file',
        ],
    )
    def test_quoted_or_escaped_literals_keep_simple_exit_semantics(self, command):
        assert _interpret_exit_code(command, 1) == "No matches found (not an error)"
        assert _is_expected_nonzero_exit(command, 1) is False
