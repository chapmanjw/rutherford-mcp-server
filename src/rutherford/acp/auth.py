# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""ACP authentication: read the sign-in methods an agent advertises and recognize "sign in first".

An ACP agent lists its ``authMethods`` in the ``initialize`` response and, when it holds no usable credential,
refuses ``session/new`` with the reserved ``-32000`` "Authentication required" error. The client then picks a
method and calls ``authenticate``. There are three method shapes and only one of them is something a headless
client can perform:

* ``agent`` (the default) -- the agent runs the sign-in itself, typically a browser OAuth flow, and caches the
  credential in its own store. Rutherford calls ``authenticate`` for these (the ``login`` tool).
* ``env_var`` -- the client supplies credentials as environment variables at launch. Nothing to call; the
  variables go in ``[agents.<id>.env]`` or the server's environment.
* ``terminal`` -- the client runs the agent binary with extra arguments in an interactive terminal. An MCP
  server has no terminal to hand the user, so the command is reported for them to run.

Pure and dependency-light so every branch is unit-testable without a subprocess.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import parse_qsl, unquote, urlsplit

from acp import RequestError

from ..runtime.logging import log_event

#: The JSON-RPC error code ACP reserves for "Authentication required" (``RequestError.auth_required``).
AUTH_REQUIRED_CODE = -32000

AuthKind = Literal["agent", "env_var", "terminal"]

#: An https URL in agent-authored text. Deliberately https-only: a sign-in link is the one URL worth surfacing,
#: and refusing every other scheme keeps a ``file:`` / ``javascript:`` string out of what a caller may open.
_HTTPS_URL = re.compile(r"https://[^\s\"'<>`]+")
#: Trailing punctuation a sentence leaves stuck to a URL ("...state=abc." or "(https://x)").
_URL_TRAILERS = ".,;:)]}"
#: The most URLs reported from one sign-in; a flow prints one, so more than this is noise.
MAX_SIGNIN_URLS = 3
#: Wording a sign-in prompt line carries. A URL is reported only from a line that matches, so an endpoint, a
#: docs link or an update check the agent logs before its prompt is never handed over as "the sign-in link".
_SIGNIN_WORDING = re.compile(r"(?i)\b(?:auth\w*|sign[\s-]?in|log[\s-]?in|oauth|consent|verif\w*|open)\b")
#: Wording that marks an ACP -32000 error as an authentication refusal. JSON-RPC uses -32000 as the start of
#: the generic server-error range, so the code alone does not mean "sign in first".
_AUTH_WORDING = re.compile(r"(?i)auth|sign[\s-]?in|log[\s-]?in|credential")


@dataclass(frozen=True, slots=True)
class AuthEnvVarInfo:
    """One environment variable an ``env_var`` method asks the client to set."""

    name: str
    label: str | None = None
    secret: bool = True
    optional: bool = False


@dataclass(frozen=True, slots=True)
class AuthMethodInfo:
    """One sign-in method an agent advertised at ``initialize``, normalized across the ACP method shapes."""

    id: str
    name: str
    kind: AuthKind = "agent"
    description: str | None = None
    #: For ``env_var``: the variables to set. Empty otherwise.
    env_vars: tuple[AuthEnvVarInfo, ...] = ()
    #: For ``env_var``: an optional page where the user gets the credential. Agent-authored; kept only when it
    #: passes :func:`is_safe_link`.
    link: str | None = None
    #: For ``terminal``: the extra arguments to launch the agent with. Empty otherwise.
    terminal_args: tuple[str, ...] = ()
    #: For ``terminal``: extra environment for that launch.
    terminal_env: dict[str, str] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        """The method as a plain dict for a tool envelope, omitting the fields its kind does not use."""
        payload: dict[str, Any] = {"id": self.id, "name": self.name, "kind": self.kind}
        if self.description:
            payload["description"] = self.description
        if self.kind == "env_var":
            payload["env_vars"] = [
                {"name": var.name, "label": var.label, "secret": var.secret, "optional": var.optional}
                for var in self.env_vars
            ]
            if self.link:
                payload["link"] = self.link
        if self.kind == "terminal":
            payload["terminal_args"] = list(self.terminal_args)
            payload["terminal_env_names"] = sorted(self.terminal_env)  # names only: a value may be a credential
        return payload


def auth_methods_of(init: object) -> list[AuthMethodInfo]:
    """The sign-in methods an ``InitializeResponse`` advertised, or ``[]`` when it offered none.

    Read defensively, like every other agent-controlled field here: ACP 0.12 salvages an entry it cannot parse
    into a raw dict rather than failing the payload, so each entry may be a typed model OR a dict. An entry
    with no usable ``id`` is dropped, since there is nothing to call ``authenticate`` with.
    """
    raw = getattr(init, "auth_methods", None)
    if not isinstance(raw, list):
        return []
    methods: list[AuthMethodInfo] = []
    for position, entry in enumerate(raw):
        # This runs after EVERY initialize, signing in or not, so a malformed entry must never fail the session:
        # anything unreadable about one method drops that method and nothing else.
        try:
            method = _method_from(entry)
        except Exception as exc:
            # The entry's contents are agent-authored and never logged; its position and the error type are.
            log_event("auth_method_unreadable", level=logging.WARNING, index=position, error=type(exc).__name__)
            method = None
        if method is not None:
            methods.append(method)
            if len(methods) >= _MAX_METHODS:
                break  # an agent offers a handful; more is noise that would only bloat every error and answer
    return methods


#: Escape sequences and C0/C1 / bidi controls stripped from every agent-advertised method string, the same
#: classes the stderr sanitizer removes, so a ``name`` or ``description`` cannot reorder or hide text in a client.
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROLS = re.compile("[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")
#: The longest advertised method string kept; a method name or argument has no use for more. A string that must
#: be exact (an id, a variable name, a terminal argument) and is longer is dropped rather than truncated, since
#: :func:`_clean` caps it and so no longer compares equal.
_MAX_FIELD = 512
#: The most sign-in methods read from one agent. Real agents advertise one to four.
_MAX_METHODS = 16


def _clean(value: object) -> str | None:
    """An agent-advertised string with escapes and controls stripped and its length capped, else ``None``."""
    if not isinstance(value, str):
        return None
    return _CONTROLS.sub("", _ANSI.sub("", value))[:_MAX_FIELD]


def _read(entry: object, *names: str) -> Any:
    """The first present attribute / key among ``names`` on a typed model or a salvaged dict."""
    for name in names:
        if isinstance(entry, dict):
            if name in entry:
                return entry[name]
        elif hasattr(entry, name):
            return getattr(entry, name)
    return None


def _method_from(entry: object) -> AuthMethodInfo | None:
    """One advertised method, normalized; every string the agent supplied is cleaned with :func:`_clean`.

    Display text (name, description, labels) is cleaned. Anything echoed back or acted on -- the method id, an
    env-var name, a terminal method's argv and env -- must be exactly what the agent sent: a value that
    cleaning would change drops the variable (env_var) or the whole method (an id, or any terminal part).
    """
    method_id = _read(entry, "id")
    if not isinstance(method_id, str) or not method_id or _clean(method_id) != method_id:
        return None
    name = _clean(_read(entry, "name"))
    description = _clean(_read(entry, "description"))
    kind_raw = _read(entry, "type")
    kind: AuthKind = kind_raw if kind_raw in ("env_var", "terminal") else "agent"
    env_vars: list[AuthEnvVarInfo] = []
    if kind == "env_var":
        raw_vars = _read(entry, "vars")
        for var in raw_vars if isinstance(raw_vars, list) else []:
            var_name = _read(var, "name")
            if isinstance(var_name, str) and var_name and _clean(var_name) == var_name:
                label = _clean(_read(var, "label"))
                env_vars.append(
                    AuthEnvVarInfo(
                        name=var_name,
                        label=label,
                        secret=_read(var, "secret") is not False,
                        optional=_read(var, "optional") is True,
                    )
                )
    terminal_args: tuple[str, ...] = ()
    terminal_env: dict[str, str] = {}
    if kind == "terminal":
        terminal = _exact_terminal(_read(entry, "args"), _read(entry, "env"))
        # The argv is returned verbatim in a tool result, which is logged and persisted. It cannot be masked
        # without presenting a command the agent did not send, so a method passing a credential is refused.
        if terminal is None:
            return None
        if _args_carry_credential(list(terminal[0])):
            # Logged by id and reason only, so an operator can see why `login` lists fewer methods than offered.
            log_event("auth_method_refused", level=logging.WARNING, method=method_id, reason="credential_in_args")
            return None
        terminal_args, terminal_env = terminal
    link = _read(entry, "link") if kind == "env_var" else None
    return AuthMethodInfo(
        id=method_id,
        name=name or method_id,
        kind=kind,
        description=description,
        env_vars=tuple(env_vars),
        # A credential page link reaches the caller verbatim (it never passes through the stderr masking), so
        # one that carries a credential itself is dropped rather than echoed into a result or a log.
        link=link if isinstance(link, str) and is_safe_link(link) and not carries_credential(link) else None,
        terminal_args=terminal_args,
        terminal_env=terminal_env,
    )


def _exact_terminal(args: object, env: object) -> tuple[tuple[str, ...], dict[str, str]] | None:
    """A terminal method's argv and env exactly as advertised, or ``None`` if any part would need rewriting.

    These are shown as a command for the user to run, so they are never altered: a non-string, an empty
    variable name, or anything carrying an escape or control character rejects the whole method rather than
    presenting a command the agent did not send.
    """
    exact_args: list[str] = []
    if args is not None:
        if not isinstance(args, list):
            return None
        for arg in args:
            if not isinstance(arg, str) or _clean(arg) != arg:
                return None
            exact_args.append(arg)
    exact_env: dict[str, str] = {}
    if env is not None:
        if not isinstance(env, dict):
            return None
        for key, value in env.items():
            if not (isinstance(key, str) and key and _clean(key) == key):
                return None
            if not (isinstance(value, str) and _clean(value) == value):
                return None
            exact_env[key] = value
    return tuple(exact_args), exact_env


#: Where a URL of any scheme, in any case, starts inside an argument (``--open=HTTPS://...`` included).
_URL_START = re.compile(r"(?i)[a-z][a-z0-9+.-]*://")
#: What separates a flag's name from an inline value: ``--token=v`` and ``--token:v``.
_FLAG_VALUE_SPLIT = re.compile(r"[=:]")
#: What separates a bare argument's name from its value: ``api_key=v`` only.
_ASSIGNMENT_SPLIT = re.compile(r"=")


def _url_arg_carries_credential(url: str) -> bool:
    """:func:`carries_credential`, plus any userinfo at all: in a terminal argument (unlike a link, which
    :func:`is_safe_link` refuses for any ``@``), ``https://<token>@host/`` is the usual git / PAT form."""
    try:
        if urlsplit(url).username:
            return True
    except ValueError:
        return True
    return carries_credential(url)


def _args_carry_credential(args: list[str]) -> bool:
    """Whether a terminal method's argv passes a credential value.

    Caught: a known credential shape anywhere, a URL anywhere in an argument (nested ones included) that
    :func:`carries_credential`, a bare ``api_key=v`` assignment, and a flag whose name says it holds a
    credential (``--token``, ``--api-key``) given a value, inline (``--token=v``, ``--token:v``) or as the next
    argument (``--api-key v``). Such a flag with no value is a switch and passes. Name matching is broad on
    purpose (``--token-file path`` is refused too): refusing a harmless method costs a sign-in path, passing a
    credential leaks it.
    """
    for index, arg in enumerate(args):
        # Each scheme start is checked from there to the end, so a URL nested in another's query (or a second
        # one later in a comma list) gets its own check instead of reading as an opaque value.
        if _CREDENTIAL_SHAPE.search(arg) or any(
            _url_arg_carries_credential(arg[m.start() :]) for m in _URL_START.finditer(arg)
        ):
            return True
        # A flag's value follows `=` or `:`; a bare argument is an assignment only with `=`, since `auth:login`
        # is an ordinary `topic:command` subcommand.
        is_flag = arg.startswith("-")
        name, *inline = (_FLAG_VALUE_SPLIT if is_flag else _ASSIGNMENT_SPLIT).split(arg.lstrip("-"), maxsplit=1)
        if not _credential_name(name):
            continue
        if inline:
            return True  # `--token=v`, `--token:v`, or a bare `api_key=v`
        if not is_flag:
            continue
        following = args[index + 1] if index + 1 < len(args) else None
        if following is not None and not following.startswith("-"):
            return True
    return False


def is_auth_required(exc: BaseException) -> bool:
    """Whether ``exc`` is the agent's ACP "Authentication required" refusal.

    ACP reserves ``-32000`` for it, but JSON-RPC also uses that code for any generic server error, so the
    message must say so too. An agent whose ``session/new`` fails for an unrelated reason keeps reporting a
    plain handshake failure instead of being sent to ``login``.
    """
    return (
        isinstance(exc, RequestError) and exc.code == AUTH_REQUIRED_CODE and _AUTH_WORDING.search(str(exc)) is not None
    )


def describe_methods(methods: list[AuthMethodInfo]) -> str:
    """A one-line human list of the advertised methods, e.g. ``oauth-personal (Log in with Google)``."""
    if not methods:
        return "it advertises no sign-in methods"
    return "it advertises: " + ", ".join(f"{method.id} ({method.name})" for method in methods)


#: Credential-bearing parameter NAMES, matched as a fragment of the lower-cased, separator-stripped name
#: (``X-Amz-Signature`` -> ``xamzsignature`` contains ``signature``), so a vendor's variant of a known name is
#: caught without listing every spelling. A link holding one would put a live secret into a tool result (and any
#: log or persisted job that keeps it), so it is dropped.
#:
#: Deliberately NOT here: ``code`` (an authorization request carries ``code_challenge``, and a returned code is
#: single-use and bound to a PKCE verifier only the agent holds), ``state`` and ``nonce`` (anti-forgery values),
#: and ``client_id`` / ``redirect_uri`` / ``scope`` (public). Those are exactly what a real sign-in link has.
_CREDENTIAL_NAME_PARTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "signature",
    "apikey",
    "privatekey",
    "assertion",
    "authorization",
    "bearer",
    "session",
    "cookie",
)
#: Short names that are credentials only as the WHOLE name (as a fragment they would match ``keyword`` etc.).
_CREDENTIAL_NAMES_EXACT = frozenset({"key", "sig", "auth", "pwd", "jwt"})
#: Anchored on the LEFT to a non-token character (or the start), like the stderr masker's ``\b``: a PKCE
#: ``code_challenge`` or ``state`` is a random base64url string, and unanchored, one that merely contains
#: ``...Xsk-`` followed by 16 more characters would drop the real sign-in link.
_CREDENTIAL_SHAPE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|AIza[0-9A-Za-z_-]{35}|sk-[A-Za-z0-9_-]{16,}"
    r"|(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}|(?:AKIA|ASIA)[0-9A-Z]{16}"
    r"|xox[abposr]-[A-Za-z0-9-]{10,}|glpat-[A-Za-z0-9_-]{16,}"
    r")"
)
#: Every vendor-prefixed shape the stderr masker in ``session.py`` redacts is listed above too. The sign-in link
#: scan reads stderr UNMASKED, so this pattern is the only shape check those links get; keep the two in step.


def _credential_name(name: str) -> bool:
    """Whether a URL parameter name says it holds a credential (see :data:`_CREDENTIAL_NAME_PARTS`)."""
    folded = re.sub(r"[^a-z0-9]", "", name.lower())
    return folded in _CREDENTIAL_NAMES_EXACT or any(part in folded for part in _CREDENTIAL_NAME_PARTS)


#: How many levels of URL-inside-a-parameter :func:`carries_credential` follows.
_MAX_NESTED_URL_DEPTH = 3


def carries_credential(url: str, *, _depth: int = 0) -> bool:
    """Whether ``url`` carries a credential, judged on the percent-DECODED URL.

    A query or fragment parameter whose name says it holds a credential, or a known credential shape (JWT, a
    vendor key prefix) anywhere -- in a path segment or in any parameter's value, whatever its name. Decoding
    first is what stops ``%65yJ...`` slipping past the shape match. The fragment is read too: implicit-style
    flows put the token there (``#access_token=...``).
    """
    try:
        parts = urlsplit(url)
        if parts.password:
            return True  # `https://user:secret@host/` (a link also fails is_safe_link; a terminal argument would not)
    except ValueError:
        return True  # unparseable: never surfaced anyway, and refusing is the safe answer
    for component in (parts.query, parts.fragment):
        # `;` is a parameter separator too (``#state=x;access_token=...``), and parse_qsl splits only on `&`. The
        # swap happens before decoding, so an encoded ``%3B`` inside a value is left alone.
        for name, value in parse_qsl(component.replace(";", "&"), keep_blank_values=True):  # decodes both
            if _credential_name(name) or _CREDENTIAL_SHAPE.search(value):
                return True
            # A percent-encoded redirect (`?next=https%3A%2F%2F...`) decodes to a URL of its own: check it too,
            # a few levels deep at most.
            if _depth < _MAX_NESTED_URL_DEPTH and "://" in value and carries_credential(value, _depth=_depth + 1):
                return True
    return _CREDENTIAL_SHAPE.search(unquote(url)) is not None


def is_safe_link(url: str) -> bool:
    """Whether an agent-authored ``url`` is fit to hand a caller: https, a parseable host, and no userinfo.

    The one policy for every link an agent supplies -- a sign-in link printed on stderr and an ``env_var``
    method's credential page alike. Anything else (``javascript:`` / ``file:`` / plain http, a malformed host
    or port) is dropped rather than passed on to a client that may render or open it. Userinfo is refused
    because ``https://accounts.example.com@attacker.test/`` displays one host and opens another, and no
    sign-in link needs it.
    """
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        _ = parts.port  # raises on an out-of-range or non-numeric port
    except ValueError:
        return False
    if parts.scheme != "https" or not hostname or "@" in parts.netloc:
        return False
    return _HTTPS_URL.fullmatch(url) is not None  # no whitespace, quotes, or angle brackets inside it


def extract_urls(text: str) -> list[str]:
    """The distinct https URLs on sign-in prompt lines of ``text``, in order, at most a few.

    Only a line carrying sign-in wording ("open the following link to authenticate") contributes, so a URL
    the agent logs for another reason is not mistaken for the link to open. ``text`` must already have escapes
    and control characters stripped; it is deliberately NOT credential-masked (masking shaped for free-form log
    text can mangle a link), and :func:`carries_credential` is the credential gate every returned link passes.
    The result is agent-authored and is surfaced as such, never opened by Rutherford.
    """
    urls: list[str] = []
    for line in text.split("\n"):
        if _SIGNIN_WORDING.search(_HTTPS_URL.sub(" ", line)) is None:
            continue  # the wording must be outside the URL, or every query string saying "auth" would pass
        for match in _HTTPS_URL.finditer(line):
            url = match.group(0).rstrip(_URL_TRAILERS)
            # The same two checks every agent-supplied link gets: safe to hand over, and carrying no credential.
            if is_safe_link(url) and not carries_credential(url) and url not in urls:
                urls.append(url)
                if len(urls) >= MAX_SIGNIN_URLS:
                    return urls
    return urls
