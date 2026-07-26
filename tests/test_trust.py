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
from rutherford.config.workspace import breadth_warning
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


def test_a_multiline_string_with_brackets_is_scanned_correctly(tmp_path: Path, monkeypatch: Any) -> None:
    """A multi-line TOML string spans lines, so the bracket scan has to carry state across them.

    Brackets on a continuation line are string content, not array structure. Miscounting them walks the
    scan past the end of the assignment and drops the tables below it.
    """
    config_path = _redirect_global(tmp_path, monkeypatch)
    _seed(
        config_path,
        'trusted_workspaces = [\n    """/tmp/a\nseg[ment\nmore""",\n]\n\n[agents.fake]\ncommand = ["fake-acp"]\n',
    )
    work = tmp_path / "repo"
    work.mkdir()

    trust_workspace(work)

    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["agents"]["fake"]["command"] == ["fake-acp"]  # the table below survived
    assert str(work.resolve()) in parsed["trusted_workspaces"]
    assert len(parsed["trusted_workspaces"]) == 2  # the multi-line entry was kept, not dropped


def test_a_top_level_multiline_string_is_not_mistaken_for_a_table(tmp_path: Path, monkeypatch: Any) -> None:
    """A line beginning "[" INSIDE a multi-line string is not a table header.

    Without cross-line state the scan flips to in-table at that line, stops stripping the real
    assignment, and the re-inserted one becomes a duplicate key.
    """
    config_path = _redirect_global(tmp_path, monkeypatch)
    _seed(
        config_path,
        'note = """\n[not a table]\n"""\ntrusted_workspaces = ["/tmp/old"]\n\n[agents.fake]\ncommand = ["fake-acp"]\n',
    )
    work = tmp_path / "repo"
    work.mkdir()

    trust_workspace(work)

    text = config_path.read_text(encoding="utf-8")
    parsed = tomllib.loads(text)  # would raise on a duplicate key
    assert parsed["agents"]["fake"]["command"] == ["fake-acp"]
    assert parsed["note"] == "[not a table]\n"  # the string survived intact
    assert text.count("trusted_workspaces") == 1  # exactly one assignment, not two


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
        ('"""one line [x] here"""', 0),  # a multi-line delimiter opened and closed on one line
    ],
)
def test_scan_brackets_ignores_strings_and_comments(line: str, expected: int) -> None:
    delta, still_open = trust_module._scan_brackets(line)
    assert delta == expected
    assert still_open is None


def test_scan_brackets_carries_multiline_state_across_lines() -> None:
    """The state returned for one line must suppress structure on the next."""
    delta, state = trust_module._scan_brackets('trusted_workspaces = ["""')
    assert delta == 1 and state == '"""'  # array opened, string still open

    delta, state = trust_module._scan_brackets("[still][inside][the][string]", state)
    assert delta == 0 and state == '"""'  # every bracket here is string content

    delta, state = trust_module._scan_brackets('"""]', state)
    assert delta == -1 and state is None  # string closes, then the real array bracket counts


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
    # * A TOTAL ban on the module, checked structurally. Two weaker shapes were tried and rejected:
    # grepping for import spellings misses `from ..config import trust`, and inspecting each module for
    # forbidden ATTRIBUTE NAMES misses both an alias (`import trust_workspace as _apply`) and the module
    # form (`from ..config import trust; trust.trust_workspace(...)`). Banning the import outright has no
    # such gaps -- and it is only possible because the read-only breadth check lives in config.workspace,
    # so no tool has a legitimate reason to reach config.trust at all.
    import ast

    import rutherford.tools

    tools_dir = Path(rutherford.tools.__file__).parent
    offenders: list[str] = []
    for source in sorted(tools_dir.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):  # import rutherford.config.trust [as x]
                offenders += [f"{source.name}: import {a.name}" for a in node.names if a.name.endswith("config.trust")]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.endswith("config.trust"):  # from ..config.trust import anything
                    offenders.append(f"{source.name}: from {module} import ...")
                if module.endswith("config") or module == "":  # from ..config import trust
                    offenders += [f"{source.name}: from {module} import trust" for a in node.names if a.name == "trust"]
    assert offenders == [], f"a model-callable tool imports the allowlist editor: {offenders}"


def test_no_mcp_tool_reaches_the_trust_module_at_runtime() -> None:
    """Backstop for the one thing the AST ban cannot see: a dynamic ``importlib`` lookup.

    Static analysis covers every ordinary import form; this covers the deliberate evasion by checking
    what the loaded modules actually hold.
    """
    import importlib
    import pkgutil
    import sys

    import rutherford.tools

    trust_module_obj = sys.modules["rutherford.config.trust"]
    mutating = {"trust_workspace", "untrust_workspace", "_write_trusted_workspaces", "_atomic_write"}
    offenders: list[str] = []
    for info in pkgutil.walk_packages(rutherford.tools.__path__, prefix="rutherford.tools."):
        module = importlib.import_module(info.name)
        for attr_name, value in vars(module).items():
            if value is trust_module_obj:
                offenders.append(f"{info.name}.{attr_name} is the trust module itself")
            elif (
                getattr(value, "__module__", "") == "rutherford.config.trust"
                and getattr(value, "__name__", "") in mutating
            ):
                # * Keyed on the symbol's OWN name, so rebinding under an alias does not hide it.
                offenders.append(f"{info.name}.{attr_name} -> {value.__name__}")
    assert offenders == []


# --- breadth warning and the write lock -----------------------------------------------------------------


def test_breadth_warning_flags_a_filesystem_root() -> None:
    root = Path(Path.cwd().anchor)
    warning = breadth_warning(root)
    assert warning is not None and "root" in warning


def test_breadth_warning_flags_the_home_directory() -> None:
    warning = breadth_warning(Path.home())
    assert warning is not None and "home directory" in warning


def test_breadth_warning_is_silent_for_an_ordinary_project(tmp_path: Path) -> None:
    """tmp_path is several levels deep, which is what a real repo checkout looks like."""
    project = tmp_path / "projects" / "myapp"
    project.mkdir(parents=True)
    assert breadth_warning(project) is None


def test_trust_cli_warns_on_a_broad_workspace(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    """The caution has to reach the operator, not just exist as a helper."""
    _redirect_global(tmp_path, monkeypatch)
    server._trust_cli([str(Path.home())])
    captured = capsys.readouterr()
    assert "added" in captured.out
    assert "warning:" in captured.err and "home directory" in captured.err


def test_trust_cli_is_quiet_for_a_normal_workspace(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    _redirect_global(tmp_path, monkeypatch)
    work = tmp_path / "projects" / "myapp"
    work.mkdir(parents=True)
    server._trust_cli([str(work)])
    captured = capsys.readouterr()
    assert "added" in captured.out
    assert "warning:" not in captured.err


def test_a_held_lock_blocks_a_concurrent_edit(tmp_path: Path, monkeypatch: Any) -> None:
    """A second writer must refuse rather than compute from a stale read.

    The atomic replace stops a torn read; it does nothing about a lost update. An interleaved
    trust + untrust could otherwise write back an entry the user had just revoked.
    """
    config_path = _redirect_global(tmp_path, monkeypatch)
    monkeypatch.setattr(trust_module, "_LOCK_TIMEOUT_S", 0.15)
    lock = config_path.with_name(config_path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("999999", encoding="utf-8")  # a live lock held by someone else

    with pytest.raises(ConfigError, match="another process is editing"):
        trust_workspace(tmp_path / "repo")

    assert not config_path.exists()  # nothing was written behind the lock


def test_the_lock_is_released_even_when_the_edit_fails(tmp_path: Path, monkeypatch: Any) -> None:
    """A refused write must not strand the lock and wedge every later trust/untrust."""
    config_path = _redirect_global(tmp_path, monkeypatch)
    _seed(config_path, "default_timeout_s = 300\n")
    lock = config_path.with_name(config_path.name + ".lock")

    with pytest.raises(ConfigError):
        trust_workspace(f"{tmp_path}/ws\udcff")  # unrepresentable path -> refused

    assert not lock.exists()
    assert trust_workspace(tmp_path / "repo").action == "added"  # still usable


# --- hardening found by review of the fixes themselves ---------------------------------------------------


def test_an_escaped_quote_does_not_end_a_multiline_basic_string() -> None:
    """A backslash-escaped quote is content, not a terminator.

    Ending the string early makes every following bracket look like array structure, which is the same
    failure the scanner exists to prevent -- just reached from a different direction.
    """
    escaped = '"""a \\' + '"""' + " still inside [x"
    delta, still_open = trust_module._scan_brackets(escaped)
    assert still_open == '"""'  # the escaped quote did NOT close it
    assert delta == 0  # so the bracket after it is string content


def test_a_literal_multiline_string_has_no_escapes() -> None:
    """''' strings take no escapes, so a backslash before the delimiter is just a backslash."""
    delta, still_open = trust_module._scan_brackets("'''a \\''' [x]")
    assert still_open is None  # it DID close, backslash notwithstanding
    assert delta == 0  # [x] is balanced


def test_the_lock_is_not_released_by_a_process_that_no_longer_owns_it(tmp_path: Path, monkeypatch: Any) -> None:
    """An overrunning writer must not drop a successor's lock.

    Unconditional unlink is ownership-blind: an edit that exceeded the stale timeout would otherwise
    delete whichever lock now holds the path, putting two writers in the critical section.
    """
    config_path = _redirect_global(tmp_path, monkeypatch)
    lock = config_path.with_name(config_path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("someone-elses-token", encoding="utf-8")

    trust_module._release_lock(lock, "my-token")

    assert lock.exists()  # left alone: a foreign lock ages out on its own
    trust_module._release_lock(lock, "someone-elses-token")
    assert not lock.exists()  # the real owner can release it


def test_a_leftover_lock_reports_how_to_recover_rather_than_being_broken(tmp_path: Path, monkeypatch: Any) -> None:
    """An interrupted edit leaves its lock, and the error must say exactly what to delete.

    Auto-breaking on age was considered and rejected: age proves the holder is slow, not gone, and
    breaking on it lets two waiters both claim the lock. One manual delete is the cheaper failure.
    """
    config_path = _redirect_global(tmp_path, monkeypatch)
    monkeypatch.setattr(trust_module, "_LOCK_TIMEOUT_S", 0.15)
    lock = config_path.with_name(config_path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("interrupted-owner", encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        trust_workspace(tmp_path / "repo")

    assert str(lock) in str(exc.value)  # names the file to delete
    assert "delete" in str(exc.value)
    assert lock.exists()  # and did NOT break it itself
    assert not config_path.exists()
