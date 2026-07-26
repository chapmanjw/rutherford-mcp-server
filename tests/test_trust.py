# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the global ``trust`` / ``untrust`` allowlist helpers and CLI."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from rutherford import server
from rutherford.config import trust as trust_module
from rutherford.config.loader import default_global_config_path, load_config
from rutherford.config.trust import (
    read_global_trusted_workspaces,
    trust_workspace,
    untrust_workspace,
)
from rutherford.domain.errors import ConfigError


def _redirect_global(tmp_path: Path, monkeypatch: Any) -> Path:
    """Point the platform global config dir at ``tmp_path`` on every OS."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return default_global_config_path()


def test_trust_creates_global_config_with_cwd(tmp_path: Path, monkeypatch: Any) -> None:
    config_path = _redirect_global(tmp_path, monkeypatch)
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)

    result = trust_workspace()

    assert result.action == "added"
    assert result.workspace == str(work.resolve())
    assert config_path.exists()
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["trusted_workspaces"] == [str(work.resolve())]


def test_trust_is_idempotent(tmp_path: Path, monkeypatch: Any) -> None:
    _redirect_global(tmp_path, monkeypatch)
    work = tmp_path / "repo"
    work.mkdir()

    first = trust_workspace(work)
    second = trust_workspace(work)

    assert first.action == "added"
    assert second.action == "unchanged"
    assert second.note is not None
    _, listed = read_global_trusted_workspaces()
    assert listed == [str(work.resolve())]


def test_trust_appends_without_clobbering_agents(tmp_path: Path, monkeypatch: Any) -> None:
    config_path = _redirect_global(tmp_path, monkeypatch)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        'default_timeout_s = 90\n\n[agents.fake]\ncommand = ["fake-acp"]\n',
        encoding="utf-8",
    )
    work = tmp_path / "repo"
    work.mkdir()

    trust_workspace(work)

    text = config_path.read_text(encoding="utf-8")
    assert 'command = ["fake-acp"]' in text
    assert "default_timeout_s = 90" in text
    parsed = tomllib.loads(text)
    assert parsed["trusted_workspaces"] == [str(work.resolve())]
    assert parsed["agents"]["fake"]["command"] == ["fake-acp"]


def test_trust_replaces_an_existing_multiline_assignment(tmp_path: Path, monkeypatch: Any) -> None:
    config_path = _redirect_global(tmp_path, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        f'trusted_workspaces = [\n    "{other.as_posix()}",\n]\nlog_level = "info"\n',
        encoding="utf-8",
    )
    work = tmp_path / "repo"
    work.mkdir()

    trust_workspace(work)

    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert set(parsed["trusted_workspaces"]) == {str(other.resolve()), str(work.resolve())}
    assert parsed["log_level"] == "info"
    # * Only one assignment remains in the file text.
    assert config_path.read_text(encoding="utf-8").count("trusted_workspaces") == 1


def test_untrust_removes_cwd(tmp_path: Path, monkeypatch: Any) -> None:
    _redirect_global(tmp_path, monkeypatch)
    work = tmp_path / "repo"
    work.mkdir()
    trust_workspace(work)

    result = untrust_workspace(work)

    assert result.action == "removed"
    assert result.trusted_workspaces == ()
    _, listed = read_global_trusted_workspaces()
    assert listed == []


def test_untrust_missing_is_idempotent(tmp_path: Path, monkeypatch: Any) -> None:
    _redirect_global(tmp_path, monkeypatch)
    work = tmp_path / "repo"
    work.mkdir()

    result = untrust_workspace(work)

    assert result.action == "missing"
    assert not default_global_config_path().exists()


def test_malformed_global_config_is_refused(tmp_path: Path, monkeypatch: Any) -> None:
    config_path = _redirect_global(tmp_path, monkeypatch)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("trusted_workspaces = [\n", encoding="utf-8")
    work = tmp_path / "repo"
    work.mkdir()

    with pytest.raises(ConfigError, match="not valid TOML"):
        trust_workspace(work)


def test_trusted_path_is_honored_by_load_config(tmp_path: Path, monkeypatch: Any) -> None:
    _redirect_global(tmp_path, monkeypatch)
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    trust_workspace()

    config = load_config(cwd=work)
    assert str(work.resolve()) in config.trusted_workspaces


def test_trust_cli_and_list(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    _redirect_global(tmp_path, monkeypatch)
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)

    server._trust_cli([])
    out = capsys.readouterr().out
    assert "added" in out and str(work.resolve()) in out

    server._trust_cli(["--list"])
    listed = capsys.readouterr().out
    assert "trusted_workspaces (1):" in listed
    assert str(work.resolve()) in listed

    server._untrust_cli([])
    removed = capsys.readouterr().out
    assert "removed" in removed


def test_trust_cli_rejects_extra_args(tmp_path: Path, monkeypatch: Any) -> None:
    _redirect_global(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        server._trust_cli(["a", "b"])
    assert exc.value.code == 2


def test_trust_cli_rejects_unknown_flags(tmp_path: Path, monkeypatch: Any) -> None:
    _redirect_global(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        server._trust_cli(["--global"])
    assert exc.value.code == 2


def test_trust_inserts_before_leading_agents_table(tmp_path: Path, monkeypatch: Any) -> None:
    config_path = _redirect_global(tmp_path, monkeypatch)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text('[agents.fake]\ncommand = ["fake-acp"]\n', encoding="utf-8")
    work = tmp_path / "repo"
    work.mkdir()

    trust_workspace(work)

    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["trusted_workspaces"] == [str(work.resolve())]
    assert parsed["agents"]["fake"]["command"] == ["fake-acp"]
    # * The allowlist assignment must appear before the first table header in the file text.
    text = config_path.read_text(encoding="utf-8")
    assert text.index("trusted_workspaces") < text.index("[agents.fake]")


# --- regression tests for the post-merge hardening ------------------------------------------------------


def _seed(config_path: Path, text: str) -> None:
    """Write a starting global config so a test can exercise the rewrite path against real content."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("bracketed", ["a[1]b", "a[unclosed", "closed]only"])
def test_a_bracket_in_a_trusted_path_never_eats_the_tables_below_it(
    bracketed: str, tmp_path: Path, monkeypatch: Any
) -> None:
    """The array scan must ignore brackets INSIDE quoted paths.

    A raw ``str.count("[")`` walks past the end of the assignment and drops every following line, which
    silently deleted the user's ``[agents.*]`` tables while still reporting success -- the round-trip
    check could not see it, because ``trusted_workspaces`` itself round-tripped perfectly.
    """
    config_path = _redirect_global(tmp_path, monkeypatch)
    seeded = (tmp_path / bracketed).as_posix()
    _seed(
        config_path,
        f'default_timeout_s = 300\ntrusted_workspaces = [\n    "{seeded}",\n]\n\n'
        '[agents.fake]\ncommand = ["fake-acp"]\n',
    )
    work = tmp_path / "repo"
    work.mkdir()

    trust_workspace(work)

    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["agents"]["fake"]["command"] == ["fake-acp"]  # the table survived
    assert parsed["default_timeout_s"] == 300  # so did the unrelated key
    assert str(work.resolve()) in parsed["trusted_workspaces"]


def test_a_bracket_in_a_trailing_comment_never_eats_the_file(tmp_path: Path, monkeypatch: Any) -> None:
    """Brackets inside a comment are not structure either."""
    config_path = _redirect_global(tmp_path, monkeypatch)
    _seed(
        config_path,
        'trusted_workspaces = []  # see docs [section 3\n\n[agents.fake]\ncommand = ["fake-acp"]\n',
    )
    work = tmp_path / "repo"
    work.mkdir()

    trust_workspace(work)

    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["agents"]["fake"]["command"] == ["fake-acp"]


def test_an_unterminated_array_is_refused_before_the_write(tmp_path: Path, monkeypatch: Any) -> None:
    """A genuinely unclosed array is malformed TOML, so the pre-parse rejects it and nothing is written.

    The scan itself never sees this input -- ``_read_global`` refuses first. That precondition is what
    makes the balance guard below a backstop rather than the primary defense.
    """
    config_path = _redirect_global(tmp_path, monkeypatch)
    _seed(config_path, 'trusted_workspaces = [\n    "/tmp/a",\n')  # no closing bracket
    before = config_path.read_text(encoding="utf-8")

    with pytest.raises(ConfigError, match="not valid TOML"):
        trust_workspace(tmp_path / "repo")

    assert config_path.read_text(encoding="utf-8") == before  # untouched


def test_the_scan_refuses_an_unterminated_array_rather_than_swallowing_the_file() -> None:
    """The balance guard is live code, not only a backstop.

    Valid TOML normally closes its arrays, but the per-line scanner has no cross-line string state, so a
    multi-line string holding a bracket also drives the balance positive. Either way the guard refuses
    rather than consuming the rest of the file.
    """
    with pytest.raises(ConfigError, match="unterminated array"):
        trust_module._strip_trusted_assignment('trusted_workspaces = [\n    "/tmp/a",\n[agents.x]\n')


def test_a_multiline_string_is_refused_fail_safe_not_silently_mangled(tmp_path: Path, monkeypatch: Any) -> None:
    """A valid-but-exotic config the scanner cannot model must be REFUSED, never partly rewritten.

    A multi-line TOML string with a bracket on a continuation line is valid TOML that the per-line
    scanner miscounts. The requirement is not that it succeeds -- it is that the user's config survives.
    """
    config_path = _redirect_global(tmp_path, monkeypatch)
    _seed(
        config_path,
        'trusted_workspaces = [\n    """/tmp/a\nseg[ment\nmore""",\n]\n\n[agents.fake]\ncommand = ["fake-acp"]\n',
    )
    before = config_path.read_bytes()

    with pytest.raises(ConfigError):
        trust_workspace(tmp_path / "repo")

    assert config_path.read_bytes() == before  # untouched, not partly rewritten


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("trusted_workspaces = [", 1),
        ('    "C:\\\\a[1]b",', 0),  # brackets inside a quoted path are not structure
        ('    "C:\\\\a[unclosed",', 0),
        ('    "closed]only",', 0),
        ("]  # trailing [comment", -1),  # a comment runs to end of line
        ("'literal [no escapes]'", 0),
        ('"escaped \\" quote [x"', 0),  # the \\" must not end the string early
    ],
)
def test_bracket_delta_ignores_strings_and_comments(line: str, expected: int) -> None:
    assert trust_module._bracket_delta(line) == expected


def test_a_control_character_in_a_path_is_escaped_not_written_raw(tmp_path: Path, monkeypatch: Any) -> None:
    """The shared hardened quoter escapes control chars, so the config still parses.

    The private quoter this replaced escaped only backslash and quote, so a newline in a workspace path
    wrote an unparseable global config -- and both trust and untrust then refused to repair the file
    they had just destroyed.
    """
    config_path = _redirect_global(tmp_path, monkeypatch)
    _seed(config_path, '[agents.fake]\ncommand = ["fake-acp"]\n')

    trust_workspace(f"{tmp_path}/a\nb\x07c")

    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))  # still valid TOML
    assert parsed["agents"]["fake"]["command"] == ["fake-acp"]
    assert len(parsed["trusted_workspaces"]) == 1


def test_an_unrepresentable_path_leaves_the_config_byte_identical(tmp_path: Path, monkeypatch: Any) -> None:
    """A lone surrogate has no TOML form; the refusal must land before the truncating write."""
    config_path = _redirect_global(tmp_path, monkeypatch)
    _seed(config_path, 'default_timeout_s = 300\n[agents.fake]\ncommand = ["fake-acp"]\n')
    before = config_path.read_bytes()

    with pytest.raises(ConfigError):
        trust_workspace(f"{tmp_path}/ws\udcff")

    assert config_path.read_bytes() == before  # not truncated to zero bytes
    assert not list(config_path.parent.glob("*.tmp"))  # and no temp file left behind


def test_untrusting_an_unlisted_path_is_a_no_op_even_with_duplicates(tmp_path: Path, monkeypatch: Any) -> None:
    """`kept` is measured against the NORMALIZED list, which de-duplicates.

    Comparing it to the raw list made a config holding a duplicate or an alias report "removed" -- and
    rewrite itself -- for a path that was never trusted.
    """
    config_path = _redirect_global(tmp_path, monkeypatch)
    dup = (tmp_path / "dup").as_posix()
    _seed(config_path, f'trusted_workspaces = [\n    "{dup}",\n    "{dup}/",\n]\n')
    before = config_path.read_text(encoding="utf-8")

    result = untrust_workspace(tmp_path / "never-trusted")

    assert result.action == "unchanged"
    assert config_path.read_text(encoding="utf-8") == before  # no gratuitous rewrite


def test_untrusting_a_real_entry_still_removes_it(tmp_path: Path, monkeypatch: Any) -> None:
    """Guard the other direction of the fix above."""
    _redirect_global(tmp_path, monkeypatch)
    work = tmp_path / "repo"
    work.mkdir()
    trust_workspace(work)

    result = untrust_workspace(work)

    assert result.action == "removed"
    assert result.trusted_workspaces == ()


def test_the_managed_header_is_written_exactly_once_however_many_edits(tmp_path: Path, monkeypatch: Any) -> None:
    """Repeated edits must not accumulate the tool's own header comment.

    The strip step passes comment lines through and the format step re-emits the header, so without an
    explicit drop the global config grew one header (and one blank line) per trust/untrust, unbounded.
    Nothing caught it: comments are inert to tomllib, so the round-trip check sees a perfect file.
    """
    config_path = _redirect_global(tmp_path, monkeypatch)

    for name in ("a", "b", "c", "d"):
        trust_workspace(tmp_path / name)
    untrust_workspace(tmp_path / "b")

    text = config_path.read_text(encoding="utf-8")
    assert text.count(trust_module._TRUST_HEADER_PREFIX) == 1
    parsed = tomllib.loads(text)
    assert len(parsed["trusted_workspaces"]) == 3


def test_a_header_from_an_older_wording_is_collapsed_not_duplicated(tmp_path: Path, monkeypatch: Any) -> None:
    """Matching the stable PREFIX means a config written by an earlier version is cleaned up, not doubled."""
    config_path = _redirect_global(tmp_path, monkeypatch)
    old = f"{trust_module._TRUST_HEADER_PREFIX} (managed by `some old wording`).\n"
    _seed(config_path, f'{old}trusted_workspaces = [\n    "{(tmp_path / "old").as_posix()}",\n]\n')

    trust_workspace(tmp_path / "new")

    text = config_path.read_text(encoding="utf-8")
    assert text.count(trust_module._TRUST_HEADER_PREFIX) == 1
    assert "some old wording" not in text


def test_the_generated_header_does_not_advertise_a_command_we_do_not_ship() -> None:
    """The header is written into the user's config, so it must name a command that actually exists.

    The bare `rutherford` executable is deliberately not shipped (it would collide with rutherford-cli),
    so pointing users at it from inside their own config would be worse than unhelpful.
    """
    assert "rutherford-mcp-server trust" in trust_module._TRUST_HEADER
    assert "`rutherford trust`" not in trust_module._TRUST_HEADER


@pytest.mark.parametrize("cli", ["_trust_cli", "_untrust_cli"])
def test_cli_refuses_an_empty_path_argument(cli: str, tmp_path: Path, monkeypatch: Any) -> None:
    """An empty PATH resolves to cwd; an unset shell variable must not silently trust the wrong tree."""
    _redirect_global(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        getattr(server, cli)([""])
    assert exc.value.code == 2


def test_no_mcp_tool_imports_the_trust_helpers() -> None:
    """trust/untrust are a human CLI path; nothing model-driven may reach them.

    The safety value of the allowlist depends on a model never being able to extend it, so wiring these
    helpers into a tool must be a deliberate act that breaks this test first.
    """
    tools_dir = Path(server.__file__).parent / "tools"
    # * Match every spelling that reaches the module, not just the dotted one: `from ..config import
    # trust` renders as "config import trust" and would otherwise slip straight past this guard. rglob
    # so a future tools/<subpackage>/ is covered too.
    spellings = ("config.trust", "config import trust", "import trust")
    offenders = [p.name for p in tools_dir.rglob("*.py") if any(s in p.read_text(encoding="utf-8") for s in spellings)]
    assert offenders == []
