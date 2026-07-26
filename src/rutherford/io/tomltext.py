# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""TOML basic-string quoting, shared by every writer that emits a config fragment.

One implementation on purpose. Rutherford writes TOML by hand -- no toml-writer dependency -- from two
places (``setup`` scaffolds a starter ``config.toml``, ``discover`` appends proposed ``[agents.<id>]``
blocks), and both quote values that can legitimately contain a character the grammar forbids raw:

* a backslash or a double quote (every Windows path, and any quoted arg), and
* a control character -- on Linux and macOS every byte except ``/`` and NUL is a legal filename byte,
  so a newline or tab can reach the quoter inside a working directory or a registry-supplied arg.

An unescaped control char produces a file that is not valid TOML, which ``tomllib`` then refuses on
every subsequent load. That failure is durable rather than transient: ``setup`` never clobbers an
existing config, so it cannot rewrite the file it corrupted. Keeping a single hardened quoter here
means a second, weaker copy cannot drift back in beside it.

A string that cannot be represented in TOML at all is rejected here rather than written -- see
:func:`toml_str`. The caller's write truncates its destination before it encodes, so a failure raised
any later than this leaves a ruined file behind.

Pure and dependency-free, in the bottom layer so any writer can use it.
"""

from __future__ import annotations

#: TOML basic-string named escapes; every other control char (U+0000-001F and U+007F) is emitted as \uXXXX.
_TOML_ESCAPES = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def toml_str(value: str) -> str:
    """Quote ``value`` as a VALID TOML basic string, escaping every char that would otherwise break it.

    Named escapes for the common controls; any other control char becomes a ``\\uXXXX`` escape, matching
    the TOML basic-string grammar. The result round-trips through :mod:`tomllib` back to ``value``.

    Raises :class:`ValueError` on a lone surrogate (U+D800-U+DFFF) -- the form Python produces when it
    surrogate-escapes a filename byte that is not valid UTF-8, so it arrives here inside a path. There is
    no correct way to emit one, and both alternatives to raising leave wreckage on disk:

    * Escaping it writes ``\\uD800``-``\\uDFFF``, which ``tomllib`` REJECTS on load ("not a Unicode scalar
      value"). That is a file which can never be read back -- precisely the durable corruption this
      module exists to prevent.
    * Passing it through defers the failure to the caller's ``write_text(..., encoding="utf-8")``, which
      opens and TRUNCATES the destination before it encodes. The ``UnicodeEncodeError`` then leaves a
      zero-byte config behind, and because ``setup`` never clobbers an existing file, that empty config
      is permanent.

    Failing here, before the caller opens anything, is the only option that leaves nothing behind.
    """
    out: list[str] = []
    for ch in value:
        if ch in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[ch])
        elif "\ud800" <= ch <= "\udfff":
            raise ValueError(
                f"cannot represent U+{ord(ch):04X} in TOML: it is a lone surrogate, which Python produces "
                "for a path or argument that is not valid UTF-8"
            )
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'
