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
_MAX_URLS = 3


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
    #: For ``env_var``: an optional page where the user gets the credential.
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
    method_id = _read(entry, "id")
    if not isinstance(method_id, str) or not method_id:
        return None
    name = _read(entry, "name")
    description = _read(entry, "description")
    kind_raw = _read(entry, "type")
    kind: AuthKind = kind_raw if kind_raw in ("env_var", "terminal") else "agent"
    env_vars: list[AuthEnvVarInfo] = []
    if kind == "env_var":
        for var in _read(entry, "vars") or []:
            var_name = _read(var, "name")
            if isinstance(var_name, str) and var_name:
                label = _read(var, "label")
                env_vars.append(
                    AuthEnvVarInfo(
                        name=var_name,
                        label=label if isinstance(label, str) else None,
                        secret=_read(var, "secret") is not False,
                        optional=_read(var, "optional") is True,
                    )
                )
    args = _read(entry, "args") if kind == "terminal" else None
    env = _read(entry, "env") if kind == "terminal" else None
    link = _read(entry, "link") if kind == "env_var" else None
    return AuthMethodInfo(
        id=method_id,
        name=name if isinstance(name, str) and name else method_id,
        kind=kind,
        description=description if isinstance(description, str) else None,
        env_vars=tuple(env_vars),
        link=link if isinstance(link, str) else None,
        terminal_args=tuple(str(arg) for arg in args) if isinstance(args, list) else (),
        terminal_env={str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {},
    )


def is_auth_required(exc: BaseException) -> bool:
    """Whether ``exc`` is the agent's ACP "Authentication required" refusal (the reserved ``-32000`` code)."""
    return isinstance(exc, RequestError) and exc.code == AUTH_REQUIRED_CODE


def describe_methods(methods: list[AuthMethodInfo]) -> str:
    """A one-line human list of the advertised methods, e.g. ``oauth-personal (Log in with Google)``."""
    if not methods:
        return "it advertises no sign-in methods"
    return "it advertises: " + ", ".join(f"{method.id} ({method.name})" for method in methods)


def extract_urls(text: str) -> list[str]:
    """The distinct https URLs in ``text``, in order, at most a few -- the sign-in link an agent printed.

    ``text`` must already be the SANITIZED stderr (controls stripped, credential shapes masked); this only
    finds links in it. The result is agent-authored and is surfaced as such, never opened by Rutherford.
    """
    urls: list[str] = []
    for match in _HTTPS_URL.finditer(text):
        url = match.group(0).rstrip(_URL_TRAILERS)
        if len(url) > len("https://") and url not in urls:
            urls.append(url)
            if len(urls) >= _MAX_URLS:
                break
    return urls
