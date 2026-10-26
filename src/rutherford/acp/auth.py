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

import re
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

from acp import RequestError

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
    for entry in raw:
        method = _method_from(entry)
        if method is not None:
            methods.append(method)
    return methods


#: Escape sequences and C0/C1 / bidi controls stripped from every agent-advertised method string, the same
#: classes the stderr sanitizer removes, so a ``name`` or ``description`` cannot reorder or hide text in a client.
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROLS = re.compile("[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")
#: The longest advertised method string kept; a method name or argument has no use for more.
_MAX_FIELD = 512


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
        for var in _read(entry, "vars") or []:
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
        if terminal is None:
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


#: Query parameters that carry a credential, and credential shapes that can sit anywhere in a URL. A link holding
#: one would put a live secret into a tool result (and any log or persisted job that keeps it), so it is dropped.
#: Mirrors what the stderr sanitizer masks, for links that reach the caller without passing through it.
_CREDENTIAL_QUERY_KEYS = frozenset(
    {
        "access_token",
        "id_token",
        "refresh_token",
        "token",
        "sig",
        "signature",
        "x-amz-signature",
        "x-goog-signature",
        "client_secret",
        "api_key",
        "apikey",
        "key",
        "password",
    }
)
_CREDENTIAL_SHAPE = re.compile(
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|AIza[0-9A-Za-z_-]{35}|sk-[A-Za-z0-9_-]{16,}"
    r"|(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}|(?:AKIA|ASIA)[0-9A-Z]{16}"
)


def carries_credential(url: str) -> bool:
    """Whether ``url`` carries a credential: a credential-named query parameter, or a known credential shape."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return True  # unparseable: never surfaced anyway, and refusing is the safe answer
    # The fragment too: OAuth implicit-style flows put the token there (``#access_token=...``), and an opaque
    # value there matches no credential shape, so only its parameter name gives it away.
    for component in (parts.query, parts.fragment):
        params = parse_qsl(component, keep_blank_values=True)
        if any(name.lower() in _CREDENTIAL_QUERY_KEYS for name, _value in params):
            return True
    return _CREDENTIAL_SHAPE.search(url) is not None


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
    the agent logs for another reason is not mistaken for the link to open. ``text`` must already be the
    SANITIZED stderr (controls stripped, credential shapes masked); this only finds links in it. The result is
    agent-authored and is surfaced as such, never opened by Rutherford.
    """
    urls: list[str] = []
    for line in text.split("\n"):
        if _SIGNIN_WORDING.search(_HTTPS_URL.sub(" ", line)) is None:
            continue  # the wording must be outside the URL, or every query string saying "auth" would pass
        for match in _HTTPS_URL.finditer(line):
            url = match.group(0).rstrip(_URL_TRAILERS)
            # The same two checks every agent-supplied link gets: safe to hand over, and carrying no credential
            # (stderr masking only catches some shapes; an opaque ``#access_token=`` passes it).
            if is_safe_link(url) and not carries_credential(url) and url not in urls:
                urls.append(url)
                if len(urls) >= MAX_SIGNIN_URLS:
                    return urls
    return urls
