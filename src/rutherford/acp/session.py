# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ACP session: a live connection to one agent, driven across one or many prompt turns.

:class:`ACPSession` is the reusable primitive. It spawns the agent as an ACP server, performs the
``initialize`` / ``new_session`` handshake, and then runs any number of ``session/prompt`` turns on the
*same* live session -- the foundation for long-running conversations (a debate keeps one session per voice
across rounds, sending only deltas, instead of re-spawning and re-sending the whole transcript each time).
Each turn reduces its own event journal into a normalized :class:`~rutherford.domain.models.DelegationResult`
and classifies any failure's re-execution safety. :func:`run_acp_turn` is the one-shot wrapper (open, one
turn, close) used by ``delegate`` / ``consensus``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import subprocess
import time
from collections.abc import Coroutine
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, TypeVar

from acp import PROTOCOL_VERSION, spawn_agent_process, text_block
from acp.client.connection import ClientSideConnection
from acp.connection import StreamDirection, StreamEvent
from acp.schema import (
    AudioContentBlock,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    Implementation,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
    ResourceContentBlock,
    TextContentBlock,
)

from ..domain.enums import Effort, ReexecutionSafety
from ..domain.error_codes import ErrorCode
from ..domain.models import Cost, DelegationResult, ErrorInfo, Provenance, Target
from ..runtime.depth import child_env
from .client import RutherfordACPClient
from .descriptors import AgentDescriptor
from .effort import (
    EFFORT_CONFIG_OPTION_IDS,
    EffortOverride,
    clamp_to_supported,
    effort_overrides,
    launch_advertisement_compatible,
)
from .host_env import claude_bedrock_env
from .journal import EventJournal, journal_event_from_message
from .launch import prepare_argv
from .permission import PermissionPolicy
from .teardown import (
    await_without_cancelling,
    consume_task_result,
    count_descendants,
    reap,
    register_pending_cleanup,
    snapshot_within_deadline,
)

#: How often the live observed-agent sampler walks the agent's process tree during a turn (N1, item 3). A
#: coarse cadence: the sampler exists to catch a peak fan-out, not to track every transient process, and a
#: tighter loop would add psutil overhead to every turn for no extra fidelity.
_OBSERVE_INTERVAL_S = 0.5

#: How Rutherford identifies itself to an agent at ``initialize``.
_CLIENT_INFO = Implementation(name="rutherford-acp", version="3.0.0")
#: Max bytes in a single line of an agent's JSON-RPC stdout. asyncio's StreamReader default (64 KiB) is too
#: small for real agents -- one ``session/update`` can carry a big model list (kilo on OpenRouter enumerates
#: hundreds of models), a large file read, or a long tool output, and a line over the limit raises
#: "Separator is found, but chunk is longer than limit" and drops the connection. 16 MiB is generous for any
#: legitimate message while still bounding memory against a runaway agent.
_STREAM_LIMIT = 16 * 1024 * 1024
#: The ACP prompt content-block union (annotated so the single-text-block list types cleanly).
PromptBlock = (
    TextContentBlock | ImageContentBlock | AudioContentBlock | ResourceContentBlock | EmbeddedResourceContentBlock
)

#: Teardown RPCs are best-effort and must not turn a finite request deadline into an infinite wait.
_CANCEL_TIMEOUT_S = 1.0
#: The pre-kill descendant enumeration. Bounded because it runs before the adapter kill: a stage that can
#: hang there strands the adapter, so a partial descendant list is strictly better than no teardown.
_SNAPSHOT_TIMEOUT_S = 3.0
_TERMINAL_SHUTDOWN_TIMEOUT_S = 4.0
_DESCENDANT_REAP_TIMEOUT_S = 3.0
_DIRECT_PROCESS_KILL_WAIT_S = 2.0
#: The transport runs only after Rutherford has hard-killed its direct child, so this bounds connection cleanup.
_TRANSPORT_CLOSE_TIMEOUT_S = 5.0
#: ``close`` returns after this caller budget while its instance-owned teardown task continues in the background.
_SESSION_CLOSE_WAIT_S = 15.0
#: Bounds the wait for the stderr drain to observe EOF during teardown. The adapter is already hard-killed by
#: then, so the pipe closes on its own; this is only the backstop that stops a wedged reader stranding cleanup.
_STDERR_DRAIN_TIMEOUT_S = 2.0
#: Bytes of agent stderr retained for diagnostics. HEAD-bounded, not a tail ring: the failure this exists to
#: explain prints its one useful line FIRST and exits, and a ring would let a chatty agent evict exactly that
#: line. Draining CONTINUES past the cap (discarding), so the child never blocks on a full pipe.
_STDERR_CAPTURE_CAP = 8 * 1024
#: Chunk size for each stderr read. ``read(n)`` and never ``readline``/``readuntil``: those raise
#: ``LimitOverrunError`` on a newline-free run longer than the stream limit, so a binary-spewing agent would
#: kill the reader that exists to diagnose it.
_STDERR_CHUNK = 4096
#: Caps on what is SURFACED (separate from what is retained), so a blob cannot bloat an error envelope.
_STDERR_DETAIL_BYTES = 2048
_STDERR_DETAIL_LINES = 20
#: ANSI CSI / OSC escape sequences. Stripped because this text is agent-controlled and gets rendered in the
#: operator's terminal by the MCP host: OSC in particular can retitle a window, write the clipboard (OSC 52),
#: or forge a hyperlink (OSC 8). This is the one non-negotiable sanitizer.
_ANSI_ESCAPE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|\x1b\[[0-?]*[ -/]*[@-~]")
#: Remaining C0/C1 controls except tab and newline, plus the bidi overrides/isolates that can visually reorder
#: text so a rendered line reads differently from the bytes it came from.
_CONTROL_CHARS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")
#: Credential shapes masked out of captured stderr before it is surfaced.
#:
#: ``docs/security.md`` describes this as a MITIGATION, not a guarantee -- an unrecognized credential shape
#: passes through, so do not read this tuple as a security boundary. Rutherford handles no credential value
#: itself, but the agent subprocess inherits an
#: environment full of them, so a misconfigured adapter, proxy, or SDK printing a token on the way out would
#: otherwise carry it straight into a result the MCP client reads and a durable job persists.
#:
#: DELIBERATELY conservative -- shapes with a fixed, recognizable prefix, plus the assignment and header forms
#: that name a secret rather than guessing at one. There is no entropy heuristic: a long base64-ish run is far
#: more often a hash, a path, or a model id than a key, and redacting those would corrode the diagnostic this
#: exists to deliver. This is a mitigation, not a guarantee -- an unrecognized credential shape survives it,
#: which is why the byte cap and the diagnostic-only contract remain the primary controls.
_SECRET_PATTERNS = (
    # To END OF LINE, not ``\S+``: a header value is space-separated ("Bearer <token>"), so stopping at the
    # first whitespace masks the scheme and leaves the credential itself in the clear.
    re.compile(r"(?i)\b((?:proxy-)?authorization\s*:\s*).*"),
    re.compile(r"(?i)\b((?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]{8,}"),
    # A key named as a secret, in the shapes an env dump or a config echo produces. Two guards keep this --
    # the widest pattern here -- from eating the diagnostic it is embedded in:
    #
    # * The keyword must END the name, give or take a plural and further ``_``-delimited components. Allowing
    #   arbitrary trailing letters made "secretariat: fine" a match.
    # * The VALUE must look like a credential: at least 12 characters and not a bare number. Without that,
    #   "tokens: 1500" and "total_tokens=4096" both redacted -- and a token COUNT is one of the most common
    #   things an agent prints, so the pass was destroying exactly the diagnostics it exists to preserve.
    #
    # The floor means a short weak secret ("password: hunter2") survives. That is the accepted trade: this is
    # a shape-matching mitigation, the vendor-prefix patterns below catch real issued credentials on their own
    # regardless of the variable they are assigned to, and a pass that cries wolf on every token count would
    # be turned off or ignored.
    # The name half is an ATOMIC group. Without it this is quadratic: on an underscore-delimited run that
    # never reaches a separator (``A_TOKEN_A_TOKEN_...``), the leading ``[A-Z0-9_]*`` and the trailing
    # ``(?:_[A-Z0-9]+)*`` repartition the same span at every start position. Measured on the 8 KiB capture
    # cap: 177 ms, and doubling the input quadrupled it. Atomic, the same input is 0.2 ms.
    #
    # That matters because ``_sanitize_stderr`` runs synchronously on the event loop, and a panel opens one
    # session per voice concurrently -- so one failed open with crafted stderr would stall every other turn
    # in flight. The input is agent-controlled, which is the whole premise of sanitizing it.
    #
    # Atomic grouping never changes WHICH strings match here, only how fast a non-match is rejected: the name
    # half is followed by a mandatory separator, so any backtracking into it could only ever produce a shorter
    # name that still has to find the same separator. Group 1 is still the name-plus-separator.
    re.compile(
        r"(?i)\b((?>[A-Z0-9_]*(?:api[_-]?key|secret|token|password|passwd|credential)s?(?:_[A-Z0-9]+)*)"
        r"\s*[=:]\s*)(?!\d+(?:\s|$))[^\s,;]{12,}"
    ),
    # Vendor-prefixed keys: OpenAI/Anthropic, GitHub, AWS, Slack, Google.
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"),
    # Credentials embedded in a URL. In practice this is the LIKELIEST way one reaches stderr at all: git,
    # npm, pip and curl all echo the URL back on an auth failure, and an agent that shells out to git prints
    # exactly this. The scheme, user and host are kept, because which host rejected the login is the
    # diagnostic; only the password is dropped.
    re.compile(r"(://[^/\s:@]+:)[^/\s@]+(?=@)"),
    # A PEM block -- the highest-impact shape here, and a fixed marker rather than an entropy guess.
    #
    # The body is "anything that is not the start of another BEGIN marker", which satisfies two requirements
    # at once that a simpler form does not.
    #
    # PERFORMANCE. A plain ``.*?`` under DOTALL matches the hyphen, so a BEGIN with no matching END rescanned
    # the whole remaining input and every later BEGIN did it again: measured 15 ms at the 8 KiB cap, 55 ms at
    # 16 KiB, 213 ms at 32 KiB -- doubling quadrupled it. The lookahead stops the body dead at the next
    # marker, so a start position that cannot complete fails in constant time. Now 0.04 ms at 8 KiB, linear.
    #
    # CORRECTNESS. Restricting the body to base64-and-whitespace also fixes the performance, and was tried
    # first -- but it silently stopped matching an ENCRYPTED traditional-format key, whose body opens with
    # ``Proc-Type: 4,ENCRYPTED`` and ``DEK-Info: AES-128-CBC,...`` headers full of ``:``, ``,`` and ``-``.
    # Missing a private key is the one outcome this pattern exists to prevent, so the body has to admit those
    # characters while still refusing to swallow a following marker.
    #
    # MARKER SHAPE. Not every armored private key uses the five-dash PEM marker. PGP appends a word
    # (``BEGIN PGP PRIVATE KEY BLOCK``) and RFC 4716 uses four dashes with inner spaces
    # (``---- BEGIN SSH2 ENCRYPTED PRIVATE KEY ----``), so a marker pinned to the PEM spelling silently
    # surfaced both in full. Requiring the literal "PRIVATE KEY" is what keeps this from matching a public
    # key, a certificate, or a rule of dashes.
    #
    # Non-greedy, and the lookahead keeps two real blocks from merging into one match.
    re.compile(
        r"-{4,5} ?BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)? ?-{4,5}"
        r"(?:(?!-{4,5} ?BEGIN)[\s\S])*?"
        r"-{4,5} ?END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)? ?-{4,5}"
    ),
    # A JWT, and the signature parameters of a presigned URL.
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)([?&](?:x-amz-signature|sig|signature|access_token|token)=)[^&\s]+"),
)
_REDACTED = "[redacted]"
_T = TypeVar("_T")


def _fault_reason(exc: BaseException) -> str:
    """Render ``exc`` for an operator, never as an empty string.

    ``asyncio.TimeoutError`` stringifies to ``""``, so interpolating ``{exc}`` alone produced the literally
    useless "ACP handshake with kiro failed: " -- observed live against a child that wrote to stderr and then
    hung. Falling back to the type name keeps a timeout distinguishable from a closed pipe.
    """
    return str(exc) or type(exc).__name__


def _sanitize_stderr(raw: bytes) -> str:
    """Decode and neutralize captured agent stderr for inclusion in a failure detail.

    The bytes are produced by a spawned agent, so they are untrusted in the terminal-rendering sense even
    though Rutherford chose the binary: strip escape sequences and controls, mask credential shapes, then cap
    lines and bytes. This is a DIAGNOSTIC breadcrumb, not a log sink, and it is fenced by the caller so the
    boundary of agent-authored text is unambiguous to whoever (or whatever) reads the error.

    The three stages are ORDERED, and the order carries weight:

    1. Escapes and controls go first, so a credential cannot be split by an embedded escape sequence to slip
       past the masking below -- stripping after redaction would let ``sk-\\x1b[0mABC...`` evade the pattern
       and then reassemble on screen.
    2. Masking runs on the full text, BEFORE the caps, so a secret is never half-clipped into an unmatched
       fragment that the patterns no longer recognize.
    3. The caps are last, bounding what a blob can do to an error envelope.
    """
    # Line endings are normalized BEFORE the control strip, not after. `\r` is itself a control character, so
    # stripping first deleted it and made the normalization dead code -- and worse, silently joined the two
    # halves of a progress line ("Downloading...\rDone" became "Downloading...Done") instead of separating
    # them. A lone CR is a line break here because that is how a progress writer uses it.
    text = _ANSI_ESCAPE.sub("", raw.decode("utf-8", errors="replace"))
    text = _CONTROL_CHARS.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    for pattern in _SECRET_PATTERNS:
        # Group 1, where a pattern has one, is the NAME half (the header, the variable, the query key). Keeping
        # it is the difference between "a token leaked here" and an unreadable line -- the diagnostic survives
        # while the value does not.
        text = pattern.sub(lambda m: f"{m.group(1)}{_REDACTED}" if m.lastindex else _REDACTED, text)
    lines = [line.rstrip() for line in text.split("\n") if line.strip()]
    if not lines:
        return ""
    clipped = lines[:_STDERR_DETAIL_LINES]
    truncated = len(lines) > _STDERR_DETAIL_LINES
    joined = " | ".join(clipped)
    if len(joined.encode("utf-8")) > _STDERR_DETAIL_BYTES:
        joined = joined.encode("utf-8")[:_STDERR_DETAIL_BYTES].decode("utf-8", errors="ignore")
        truncated = True
    return f"{joined} [truncated]" if truncated else joined


class ACPHandshakeError(Exception):
    """A session could not be opened (spawn or handshake failed). Pre-prompt, so re-execution-safe.

    Carries the ACP error code and the re-execution-safety classification so a caller can turn it into a
    failed result or decide a fallback. Raised by :meth:`ACPSession.open`; :func:`run_acp_turn` converts it
    to a failed :class:`DelegationResult`.
    """

    def __init__(self, code: ErrorCode, message: str, safety: ReexecutionSafety) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.safety = safety


class ACPSession:
    """A live ACP conversation with one agent: open once, run many prompt turns, close.

    Not safe for concurrent turns on one instance -- one turn at a time (a conversation is sequential). The
    journal is swapped per turn, and a synchronous stream observer records each turn's ``session/update``
    stream inline in receive order, so a turn's journal is complete the moment its prompt response resolves.
    """

    def __init__(
        self,
        descriptor: AgentDescriptor,
        *,
        policy: PermissionPolicy,
        cwd: str,
        model: str | None = None,
        effort: Effort | None = None,
        base_depth: int = 0,
        parent_run_id: str | None = None,
        sandbox_root: str | None = None,
        resume_session_id: str | None = None,
        handshake_timeout_s: float | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._policy = policy
        # The per-step budget for each handshake call (initialize / new_session / load_session / set_model):
        # the descriptor's default, unless a caller overrides it. A handshake-only connection probe passes its
        # own budget here so a generous local-model floor actually reaches the handshake (descriptor's fixed
        # default would otherwise dominate, falsely failing a slow cold-model handshake).
        self._handshake_timeout = (
            handshake_timeout_s if handshake_timeout_s is not None else descriptor.handshake_timeout_s
        )
        # Resume a prior agent session over ACP (``session/load``) instead of creating a fresh one
        # (``session/new``): the opaque id from an earlier turn's result, round-tripped back so the agent
        # reloads that conversation. ``None`` is the default fresh-session path. Gated at open() on the agent
        # advertising the ``loadSession`` capability; a resume against an agent that cannot reload its own
        # sessions is a clean ``RESUME_FAILED`` rather than a silent fresh session.
        self._resume_session_id = resume_session_id
        # N1 (item 3): how deep this run sits in a Rutherford-driving-Rutherford chain, and the panel parent
        # to correlate a voice back to. Layered onto the agent's environment at open() so a nested host reads
        # them back (the recursion guard) and an aggregate cap can reason across layers (count-first lineage).
        self._base_depth = base_depth
        self._parent_run_id = parent_run_id
        #: The peak local descendant count psutil observed while a turn was live (N1, item 3): the agent
        #: process plus its sub-processes, a FLOOR (remote agents invisible). ``None`` until a turn samples it.
        self._observed_peak_agents: int | None = None
        # ACP requires an absolute cwd in session/new (a relative one, e.g. ".", is rejected by agents like
        # goose). Resolve once here so every path -- delegate, consensus, debate, the conformance probe --
        # hands the agent an absolute working directory.
        self._cwd = str(Path(cwd).resolve())
        # Resolve effort to this agent's per-call ACP override (extra args / env / a rewritten model id), or a
        # reported no-op when the agent has no knob (F8a, 2-L). The override is computed against the RESOLVED
        # model so codex/cursor (which encode effort in the model id) rewrite the model the session will use.
        # requested_model is the pre-effort id (caller / descriptor default); target.model is post-rewrite.
        # _caller_model is exactly what the caller passed (None when omitted) so selection can tell an explicit
        # request from a descriptor-default-only soft path on a channel-less agent.
        self._caller_model = model
        self._requested_model = model or descriptor.default_model
        resolved_model = self._requested_model
        self._effort = effort
        self._override = effort_overrides(descriptor, effort, model=resolved_model)
        #: The tier this session actually applied. Seeded from the launch-time override (cline/kiro/junie/
        #: cursor/codex-with-model know it statically); the config-option path (claude_code, codex-no-model,
        #: and Codex 1.8's unadvertised-bracket fallback) updates it at open once the agent's advertised
        #: effort options are known. ``None`` for a no-op.
        self._effort_applied = self._override.applied
        #: When True, :meth:`_select_effort` must confirm the config-option ``current_value`` (Codex
        #: bracket-id not advertised). Missing option / unconfirmed set is a handshake error, never a claim
        #: that a matching bare model id applied the requested effort.
        self._effort_requires_confirmation = False
        self._target = Target(cli=descriptor.id, model=self._override.model or resolved_model)
        #: The effective model ACP in-session selection confirmed (config current_value after a set, or
        #: set_model success). ``None`` until :meth:`_select_model` confirms; never set from launch argv alone
        #: or from an already-current config echo without a verified channel that actually selected.
        self._selected_model: str | None = None
        #: Whether :meth:`_select_model` confirmed the effective model over an in-session ACP channel.
        self._model_confirmed = False
        # F2 replay-completeness: the LOGICAL launch argv (the agent's ACP-server command plus any
        # effort-override extra args and, when :attr:`AgentDescriptor.model_launch_flag` is set, the
        # flag+effective-model pair). Pinned here so a persisted run records what it was issued with. Kept
        # distinct from the platform-resolved spawn argv (``prepare_argv`` below), whose npm-shim resolution
        # bakes in machine-specific absolute paths a replay on another host could not reuse. The descriptor's
        # ``command`` tuple is never mutated -- model/effort args are layered onto a fresh list.
        self._launch_argv = [*descriptor.command, *self._override.extra_args]
        #: Whether the effective model was appended to launch argv via :attr:`AgentDescriptor.model_launch_flag`.
        #: Internal only: launch intent is not runtime provenance (ACP cannot attest inference).
        self._model_via_launch = False
        if descriptor.model_launch_flag and self._target.model:
            self._launch_argv.extend([descriptor.model_launch_flag, self._target.model])
            self._model_via_launch = True
        # The FileGateway / TerminalBroker confinement root for a mutating sandbox (the worktree / temp copy);
        # None for a non-sandboxed session, where reads are served anywhere and terminal stays denied. Resolved
        # so the client's path-escape guard compares against a canonical absolute root.
        self._sandbox_root = str(Path(sandbox_root).resolve()) if sandbox_root is not None else None
        self._journal = EventJournal()
        self._client = RutherfordACPClient(
            journal=self._journal, policy=policy, cwd=self._cwd, sandbox_root=self._sandbox_root
        )
        self._stack = AsyncExitStack()
        self._conn: ClientSideConnection | None = None
        self._session_id: str | None = None
        self._pid: int | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._close_task: asyncio.Task[None] | None = None
        #: Head-bounded capture of the agent's own stderr, for a failure detail. An agent that dies at launch
        #: often explains itself there and nowhere else -- a launcher stub handed a name it does not
        #: recognize prints one exact line and exits, which ACP can otherwise only report as "Connection lost".
        self._stderr_buffer = bytearray()
        self._stderr_truncated = False
        self._stderr_task: asyncio.Task[None] | None = None
        #: The model ids the agent advertised at session open (``session.models``), captured so a caller can
        #: see what the agent offered -- the "configure" signal of a handshake-only connection check. ``[]``
        #: when the agent advertises no selectable models (it runs on its own default).
        self._available_models: list[str] = []
        #: The model values advertised on the SECOND ACP model channel -- a ``configOptions`` "model" select
        #: option. Some harnesses (claude_code via the claude-agent-acp adapter) surface their selectable models
        #: HERE, not in ``session.models``; captured at open so :attr:`available_models` reports the union across
        #: both channels (otherwise ``connect_only`` misleadingly reports ``[]`` for such an agent).
        self._config_model_values: list[str] = []
        #: The session config options the agent advertised at open (``session.configOptions``), captured so
        #: the config-option effort path can find an advertised ``effort`` / ``reasoning_effort`` option and
        #: clamp the requested tier to its values. ``[]`` when the agent advertises none.
        self._config_options: list[object] = []

    @property
    def effort_applied(self) -> Effort | None:
        """The effort tier this session actually applied (clamped), or ``None`` for a no-op (F8a, 2-L).

        For a launch-time channel this is known before open; for the config-option channel (claude_code,
        codex-no-model, Codex unadvertised-bracket fallback) it is set during :meth:`open` once the agent's
        advertised effort options are read.
        """
        return self._effort_applied

    @property
    def observed_peak_agents(self) -> int | None:
        """The peak local descendant count sampled while a turn ran (N1, item 3); a floor, ``None`` if unsampled."""
        return self._observed_peak_agents

    @property
    def target(self) -> Target:
        """The resolved ``(cli, model)`` this session answers under (``model`` is post-effort-rewrite)."""
        return self._target

    @property
    def requested_model(self) -> str | None:
        """The model before effort rewrite (caller / descriptor default), or ``None`` on the agent-default path."""
        return self._requested_model

    @property
    def selected_model(self) -> str | None:
        """The effective model confirmed over an in-session ACP channel, or ``None`` when unconfirmed.

        Launch-argv selection (:attr:`AgentDescriptor.model_launch_flag`) does not populate this -- ACP
        cannot attest the runtime model, so intent stays on :attr:`requested_model` / :attr:`target`.
        """
        return self._selected_model

    @property
    def model_confirmed(self) -> bool:
        """Whether in-session ACP model selection verified the effective model for this session.

        ``True`` only after a verified ``set_config_option`` current_value match (including when the
        option was already current, so the RPC was skipped) or a successful ``session/set_model``.
        Launch-flag intent alone is never confirmation -- ACP does not attest runtime inference.
        """
        return self._model_confirmed

    @property
    def launch_argv(self) -> list[str]:
        """The logical launch argv this session was issued with (F2 replay-completeness; see __init__)."""
        return list(self._launch_argv)

    @property
    def session_id(self) -> str | None:
        """The agent's session id once opened, for provenance and a later resume; ``None`` before open."""
        return self._session_id

    @property
    def available_models(self) -> list[str]:
        """The models the agent advertised at open across BOTH ACP model channels: the ``session.models``
        (SessionModelState) ids first, then any ``configOptions`` "model" select values not already present
        (claude_code's claude-agent-acp surfaces its models here, not in SessionModelState). ``[]`` before open
        or when the agent offers neither -- a deterministic union so the order never depends on dict iteration."""
        union = list(self._available_models)
        seen = set(union)
        for value in self._config_model_values:
            if value not in seen:
                union.append(value)
                seen.add(value)
        return union

    @property
    def partial_text(self) -> str:
        """The answer text streamed so far on the CURRENT turn, for a time-budget harvest of a cut voice.

        Read after a voice is cut at a panel's deadline: the turn never resolved, so its journal holds only
        what the agent streamed before the cut. Empty when nothing was streamed (a single-shot agent that
        emits its answer only at the end yields no partial, which the harvest records honestly).
        """
        return self._journal.message_text()

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        """Read the agent's stderr to EOF, retaining a head-bounded prefix and discarding the rest.

        Reading CONTINUES after the cap is reached. That is the whole safety property: the retained bytes are
        bounded for memory, but the PIPE is drained for as long as the child holds it open, so the child can
        never block on a write and stall the handshake or teardown.

        ``read(n)`` rather than ``readline`` / ``readuntil``: those raise ``LimitOverrunError`` on a
        newline-free run longer than the stream limit, so an agent emitting binary or a very long unbroken
        line would kill the reader whose only job is to explain such an agent.
        """
        while True:
            try:
                chunk = await stream.read(_STDERR_CHUNK)
            except (OSError, ValueError):  # pragma: no cover - a torn-down transport races EOF
                return
            if not chunk:
                return
            room = _STDERR_CAPTURE_CAP - len(self._stderr_buffer)
            if room <= 0:
                self._stderr_truncated = True
                continue
            if len(chunk) > room:
                self._stderr_buffer.extend(chunk[:room])
                self._stderr_truncated = True
            else:
                self._stderr_buffer.extend(chunk)

    def _stderr_detail(self) -> str:
        """The captured stderr as a fenced clause to append to a failure message, or ``""`` when there is none.

        Read from the buffer SNAPSHOT rather than by awaiting the drain, because the two failure shapes want
        opposite things: on a died-at-launch failure the child is already gone and the buffer is complete,
        while on a handshake TIMEOUT the child is still running and the partial buffer is exactly the evidence
        wanted. Awaiting would return nothing useful in the second case and would add a wait to the first.

        The text is agent-authored, so it is sanitized and fenced -- the label is what tells a reader (or a
        model) where Rutherford's own words stop.
        """
        detail = _sanitize_stderr(bytes(self._stderr_buffer))
        if not detail:
            return ""
        suffix = " [truncated]" if self._stderr_truncated and "[truncated]" not in detail else ""
        return f' (agent stderr: "{detail}"{suffix})'

    def _handshake_failure(self, code: ErrorCode, message: str) -> ACPHandshakeError:
        """Build a handshake failure carrying whatever the agent said on stderr before it died."""
        return ACPHandshakeError(code, f"{message}{self._stderr_detail()}", ReexecutionSafety.SAFE)

    async def __aenter__(self) -> ACPSession:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def open(self) -> None:
        """Spawn the agent and complete the handshake, or raise :class:`ACPHandshakeError`."""
        # Layer this turn's effort override onto the launch: extra env on top of the resolved environment, and
        # extra args appended to the agent's own argv (e.g. cline's ``--thinking high``). A model-id-encoding
        # agent without :attr:`AgentDescriptor.model_launch_flag` (codex) carries its effort in
        # ``self._target.model``, applied via in-session set_model below; Cursor passes the rewritten id on
        # launch argv instead (in-session config/set_model are not authoritative for its inference).
        # N1 (item 3): the depth + count-first lineage env goes on last, so a spawned agent that is itself a
        # Rutherford host reads where it sits (the recursion guard) and the aggregate cap counts across layers.
        # claude_bedrock_env normalizes a Bedrock/Vertex Claude Code seat: it resolves a valid ANTHROPIC_MODEL
        # (so the claude-agent-acp adapter does not fall back to the bare cloud alias the provider rejects) from
        # base_env -- so it never clobbers an explicit env override (already in base_env, re-set to the same
        # value by precedence) -- and is a no-op ({}) for every other seat and every non-Bedrock host.
        base_env = _resolve_env(self._descriptor)
        env = {
            **base_env,
            **claude_bedrock_env(self._descriptor, base_env, self._cwd),
            **self._override.env_dict,
            **child_env(self._base_depth, parent_run_id=self._parent_run_id),
        }
        command, *args = prepare_argv(tuple(self._launch_argv))

        def _observe(event: StreamEvent) -> None:
            # SYNCHRONOUS observer: inline in receive order, so each turn's journal is complete before its
            # prompt response resolves. ``self._journal`` is swapped per turn, so this always writes the
            # current turn's journal.
            if event.direction is StreamDirection.INCOMING:
                entry = journal_event_from_message(event.message)
                if entry is not None:
                    self._journal.append(entry)

        try:
            conn, process = await self._stack.enter_async_context(
                spawn_agent_process(
                    self._client,
                    command,
                    *args,
                    env=env,
                    cwd=self._cwd,
                    # * Raw agent diagnostics are untrusted and must not inherit the MCP host's bounded stderr
                    # pipe. That is what ``None`` would mean here, and it is the deadlock this once caused:
                    # the host had no reader, so a chatty agent could wedge on a full pipe. DEVNULL fixed it by
                    # discarding -- which also discarded the one line a launch failure explains itself with.
                    # An OWNED pipe keeps both properties, but ONLY while something drains it, so the drain
                    # task below is part of this decision rather than an optimization on top of it.
                    transport_kwargs={"stderr": subprocess.PIPE, "limit": _STREAM_LIMIT},
                    observers=[_observe],
                )
            )
        except OSError as exc:
            # OSError, not just FileNotFoundError: a missing binary is FileNotFoundError, but a working_dir
            # that resolves to a file (NotADirectoryError) or an unexecutable command (PermissionError) is
            # also a launch failure, not an internal error. All map to a clean re-execution-safe spawn fail.
            await self.close()
            raise ACPHandshakeError(
                ErrorCode.ACP_SPAWN_FAILED,
                f"could not launch {self._descriptor.id} ({command!r}): {exc}",
                ReexecutionSafety.SAFE,
            ) from exc
        self._conn = conn
        self._process = process
        self._pid = process.pid
        # Started BEFORE the handshake, not after: the failure this captures happens during ``initialize``, and
        # a reader attached afterwards would be racing the very window it exists to observe. It also keeps the
        # pipe clear for the whole handshake, so a noisy agent cannot stall it.
        if process.stderr is not None:
            self._stderr_task = asyncio.create_task(
                self._drain_stderr(process.stderr), name=f"rutherford-acp-stderr-{process.pid}"
            )
            # Same durable-owner treatment every other detached cleanup task here gets: the loop holds tasks
            # weakly, and a reader dropped mid-flight is how a pipe stops being drained without anyone noticing.
            register_pending_cleanup(self._stderr_task)
        # A cancellation ANYWHERE in the handshake (initialize / new_session / load / set_model) is a
        # BaseException, so the per-stage ``except Exception`` guards below do NOT catch it. Without this outer
        # guard the just-spawned agent would be left registered on the exit stack but never torn down -- a
        # leaked process tree, since ``run_acp_turn`` enters the session with ``async with`` and Python skips
        # ``__aexit__`` when ``__aenter__`` (this ``open``) raises. Tear the agent down on a cancel, then
        # re-raise so the cancellation still propagates (the per-stage handlers already close on an Exception).
        try:
            try:
                init = await asyncio.wait_for(
                    conn.initialize(protocol_version=PROTOCOL_VERSION, client_info=_CLIENT_INFO),
                    timeout=self._handshake_timeout,
                )
            except Exception as exc:
                await self.close()
                raise self._handshake_failure(
                    ErrorCode.ACP_HANDSHAKE_FAILED,
                    f"ACP handshake with {self._descriptor.id} failed: {_fault_reason(exc)}",
                ) from exc
            # Everything below runs with the agent process ALREADY spawned and registered on the exit stack,
            # and open() is entered via ``async with`` in run_acp_turn -- Python skips ``__aexit__`` when
            # ``__aenter__`` raises, so ANY exception escaping here without a teardown orphans that process tree
            # with nothing left holding a reference to it. The guard is therefore structural rather than
            # per-call-site: it covers the session RPC, the plain reads of what that RPC returned, and model /
            # effort selection, so a post-handshake step added here later inherits the teardown instead of
            # having to remember it. ``_new_session`` / ``_resume`` still close on the faults they RAISE and
            # that stays as the first line of defence -- ``close`` is idempotent (it awaits one instance-owned
            # teardown task), so the redundancy costs nothing -- but a helper can only guard the failures it
            # raises, never the ones that happen while READING what it returned. That gap is exactly what let a
            # capability blob the ACP SDK had salvaged into a raw dict AttributeError straight out of open()
            # with the adapter still running.
            try:
                # Resume a prior session (session/load) when asked, else create a fresh one (session/new). The
                # resume path is gated on the agent's advertised loadSession capability and fails RESUME_FAILED
                # if unsupported.
                session: NewSessionResponse | LoadSessionResponse
                if self._resume_session_id is not None:
                    session = await self._resume(conn, init)
                else:
                    session = await self._new_session(conn)
                # * Legacy SessionModelState (session.models) is optional: ACP SDK 0.11+ removes the field;
                # access only via defensive helpers so a config-only response never AttributeErrors before
                # launch validation.
                self._available_models = _models_of(session)
                self._config_options = list(getattr(session, "config_options", None) or [])
                # Capture the SECOND model channel (a configOptions "model" select option) so available_models
                # can report the union -- claude_code's adapter advertises its models here, not in
                # SessionModelState.
                model_option = _model_config_option(self._config_options)
                self._config_model_values = list(model_option[2]) if model_option is not None else []
                # Model / effort selection can raise ACPHandshakeError (e.g. MODEL_UNAVAILABLE for a model the
                # agent advertises on no channel); under the confirmed-selection contract those fail the open
                # rather than degrading it quietly.
                await self._select_model(conn, session)
                await self._select_effort(conn)
            except Exception:
                await self.close()
                raise
        except asyncio.CancelledError:
            await self.close()
            raise

    async def _new_session(self, conn: ClientSideConnection) -> NewSessionResponse:
        """Create a fresh session (``session/new``); a failure is an ``ACP_HANDSHAKE_FAILED`` handshake fault."""
        try:
            session = await asyncio.wait_for(
                conn.new_session(cwd=self._cwd, mcp_servers=[]),
                timeout=self._handshake_timeout,
            )
        except Exception as exc:
            await self.close()
            raise self._handshake_failure(
                ErrorCode.ACP_HANDSHAKE_FAILED,
                f"ACP handshake with {self._descriptor.id} failed: {_fault_reason(exc)}",
            ) from exc
        self._session_id = session.session_id
        return session

    async def _resume(self, conn: ClientSideConnection, init: InitializeResponse) -> LoadSessionResponse:
        """Resume the prior session via ACP ``session/load`` instead of ``session/new``.

        Gated on the agent advertising the ``loadSession`` capability at initialize: an agent that does not
        persist and reload its own sessions cannot resume, so a resume against it is a clean ``RESUME_FAILED``
        rather than a silent fresh session. ``session/load`` does not mint a new id -- the loaded session keeps
        the requested one. SAFE re-execution: the resume is pre-prompt, with no side effect or cost.

        The capability is read through :func:`_load_session_capability`, never off the response attribute: ACP
        0.12 salvages a malformed ``agentCapabilities`` into a raw dict instead of failing the payload, so the
        attribute's declared type no longer describes what is there. An unreadable blob refuses the resume with
        its own wording -- it is not the same fact as an agent that answered and said it cannot reload, and an
        operator debugging one should not be told the other.
        """
        resume_id = self._resume_session_id
        assert resume_id is not None  # noqa: S101 - guarded by the caller (only taken when a resume id is set); narrowing for mypy, not a runtime check
        advertised = _load_session_capability(init)
        if advertised is not True:
            await self.close()
            if advertised is False:
                detail = (
                    "it does not advertise the ACP loadSession capability (it does not persist sessions for reload)"
                )
            else:
                detail = (
                    "its initialize response carried a malformed agentCapabilities blob (ACP 0.12 salvages one "
                    "into all-false defaults instead of failing the handshake), so no loadSession "
                    "advertisement could be read"
                )
            raise ACPHandshakeError(
                ErrorCode.RESUME_FAILED,
                f"{self._descriptor.id} cannot resume a session: {detail}",
                ReexecutionSafety.SAFE,
            )
        try:
            session = await asyncio.wait_for(
                conn.load_session(cwd=self._cwd, session_id=resume_id, mcp_servers=[]),
                timeout=self._handshake_timeout,
            )
        except Exception as exc:
            await self.close()
            raise self._handshake_failure(
                ErrorCode.RESUME_FAILED,
                f"resuming session {resume_id!r} on {self._descriptor.id} failed: {_fault_reason(exc)}",
            ) from exc
        self._session_id = resume_id
        return session

    async def _select_model(  # noqa: C901 - two model channels plus their fallbacks, enumerated once
        self, conn: ClientSideConnection, session: NewSessionResponse | LoadSessionResponse
    ) -> None:
        """Select or validate the effective model across ACP channels.

        When :attr:`AgentDescriptor.model_launch_flag` carried the effective model on argv, only validate
        that the agent advertises it on a model channel after ``new_session`` -- never call in-session
        ``set_config_option`` / ``set_model`` (Cursor echoes those without changing inference and may mutate
        global acp-config), and never set ``selected_model`` / ``confirmed`` (ACP PromptResponse carries no
        runtime model attestation; launch argv is intent, not provenance).

        Otherwise, when an effective model is set (caller / descriptor default / effort rewrite), selection
        prefers a verifiable config option, then ``session/set_model``. An unadvertised or unconfirmed model
        is a hard :class:`ACPHandshakeError` (``MODEL_UNAVAILABLE``) when the caller named a model, effort
        rewrote one, or the agent advertises model channels that omit the target -- never a silent
        fall-through that reports the request as selected. ``model=None`` with no descriptor default skips
        selection (the agent's own default path). A descriptor-default-only request on a channel-less agent
        also skips selection (ACP cannot confirm it) without claiming ``selected_model`` / ``confirmed``.

        Priority for in-session agents: when a ``session.configOptions`` model SELECT advertises the target,
        use ``session/set_config_option`` and verify the returned ``current_value``. Skip the RPC when
        ``current_value`` is already the target (still confirmed for that channel). Only when no suitable
        config option exists, use ``session/set_model`` for a model advertised in ``SessionModelState``; a
        successful ACP response is confirmation for set_model-only agents.

        Codex effort rewrite is capability-gated: ``base[tier]`` is selected only when advertised. An
        unadvertised rewrite with an advertised bare/base id falls back to that id plus confirmed
        ``reasoning_effort`` -- never a ``MODEL_UNAVAILABLE`` for a model the agent actually offers, and
        never ``effort_applied`` from a matching base id alone.
        """
        model = self._target.model
        if not model or self._session_id is None:
            return
        if self._model_via_launch:
            await self._validate_launch_model(session, model)
            return
        found = _model_config_option(self._config_options)
        in_config = found is not None and model in found[2]
        in_session = _advertises_model(session, model)
        if not in_config and not in_session:
            fallback = self._fallback_unadvertised_effort_model(session, found)
            if fallback is not None:
                model = fallback
                in_config = found is not None and model in found[2]
                in_session = _advertises_model(session, model)
        if not in_config and not in_session:
            self._reject_or_skip_unadvertised_model(model)
            return
        # * Prefer the verifiable config-option channel whenever it advertises the target.
        if in_config:
            assert found is not None  # noqa: S101 - narrowed by in_config; narrowing for mypy, not a runtime check
            config_id, current, _values = found
            if current == model:
                self._selected_model = model
                self._model_confirmed = True
                return
            try:
                response = await asyncio.wait_for(
                    conn.set_config_option(config_id=config_id, value=model, session_id=self._session_id),
                    timeout=self._handshake_timeout,
                )
            except Exception as exc:
                raise ACPHandshakeError(
                    ErrorCode.MODEL_UNAVAILABLE,
                    f"model {model!r} selection via set_config_option failed on {self._descriptor.id}: {exc}",
                    ReexecutionSafety.SAFE,
                ) from exc
            options = list(getattr(response, "config_options", None) or [])
            if options:
                self._config_options = options
                model_option = _model_config_option(options)
                self._config_model_values = list(model_option[2]) if model_option is not None else []
            if not _config_option_current_equals(response, config_id, model):
                raise ACPHandshakeError(
                    ErrorCode.MODEL_UNAVAILABLE,
                    f"model {model!r} selection was not confirmed by {self._descriptor.id}: "
                    "set_config_option did not return matching current_value",
                    ReexecutionSafety.SAFE,
                )
            self._selected_model = model
            self._model_confirmed = True
            return
        # set_model-only: legacy SessionModelState advertises the id. ACP SDK 0.11+ drops set_session_model --
        # call only when the connection still exposes it; otherwise a structured MODEL_UNAVAILABLE (not AttributeError).
        set_model = getattr(conn, "set_session_model", None)
        if not callable(set_model):
            raise ACPHandshakeError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"model {model!r} is not available on {self._descriptor.id}: "
                "advertised only via legacy session.models, but this ACP client has no set_session_model "
                "(use a model config option or a launch --model agent)",
                ReexecutionSafety.SAFE,
            )
        try:
            await asyncio.wait_for(
                set_model(model_id=model, session_id=self._session_id),
                timeout=self._handshake_timeout,
            )
        except Exception as exc:
            raise ACPHandshakeError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"model {model!r} selection via set_model failed on {self._descriptor.id}: {exc}",
                ReexecutionSafety.SAFE,
            ) from exc
        self._selected_model = model
        self._model_confirmed = True

    async def _validate_launch_model(self, session: NewSessionResponse | LoadSessionResponse, model: str) -> None:
        """Soft advertisement check for a launch-flag model: never fatal, never claims confirmation.

        A launch-flag agent (Cursor's ``--model``) is handed its model on the process argv, so the agent
        applies it regardless of what it advertises over ACP -- and acp 0.11 removed the legacy
        ``session.models`` channel, so an agent that surfaced its models only there now advertises nothing
        Rutherford can read. Raising ``MODEL_UNAVAILABLE`` on a missing ACP advertisement would therefore
        block the very launch-flag routing this method guards (the id is already on argv and will run). So it
        stays advisory: it returns whether or not the model is advertised and NEVER sets
        :attr:`selected_model` / :attr:`model_confirmed` (ACP does not attest a launch-argv model). A model
        the agent genuinely cannot run surfaces later as that agent's own prompt-time error, not a
        pre-emptive handshake failure here.

        Launch-only compatibility: an advertised compound id that differs solely in a boolean ``fast=`` value
        counts as the same model (exact argv / caller intent is unchanged). In-session :meth:`_select_model`
        keeps strict exact-match advertisement checks, because there the model IS selected over ACP.
        """
        found = _model_config_option(self._config_options)
        config_values = list(found[2]) if found is not None else []
        session_values = _models_of(session)
        # Advisory only: advertised or not, a launch-flag model proceeds (it is applied via argv). The check is
        # kept so the intent is explicit and so future non-fatal signalling has a single home.
        _advertised = _launch_model_advertised(model, config_values) or _launch_model_advertised(model, session_values)
        return

    def _reject_or_skip_unadvertised_model(self, model: str) -> None:
        """Hard-fail an actively selected unadvertised model, or soft-skip a descriptor default.

        Hard-fail only a model Rutherford is ACTIVELY selecting over ACP: an explicit caller model, or an
        effort-rewritten id, that the agent advertises on no channel -- never silently report it as
        selected. A descriptor-DEFAULT the agent does not advertise is a soft-skip, even when the agent
        advertises OTHER models on a channel: it may be applied out-of-band -- the agent's own default, or
        an injected ANTHROPIC_MODEL on a Bedrock/Vertex seat whose provider id is deliberately absent from
        the alias config option. Hard-failing that would break every turn of a config-advertising seat
        like claude_code on Bedrock (the shipped remediation sets default_model to a provider id). An
        effort rewrite that was never selected does not keep ``effort_applied``.
        """
        explicit = self._caller_model is not None
        rewritten = self._override.model is not None
        if explicit or rewritten:
            if rewritten:
                self._effort_applied = None
            raise ACPHandshakeError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"model {model!r} is not available on {self._descriptor.id}: "
                "not advertised by session.models or a model config option",
                ReexecutionSafety.SAFE,
            )
        return

    def _fallback_unadvertised_effort_model(
        self, session: NewSessionResponse | LoadSessionResponse, found: tuple[str, str | None, list[str]] | None
    ) -> str | None:
        """If an effort-rewritten model id is unadvertised, return an advertised bare/base id to select instead.

        Codex historically advertised ``base[tier]``; Codex ACP 1.8 advertises bare ids and a
        ``reasoning_effort`` config option. Falling back switches the target to the advertised base and
        routes effort through that option with confirmation required. A matching base id is never treated
        as proof the bracket effort applied.
        """
        rewritten = self._override.model
        if not self._override.config_option_fallback or rewritten is None:
            return None
        if self._target.model != rewritten:
            return None
        candidates: list[str] = []
        requested = self._requested_model
        if requested is not None and requested != rewritten:
            candidates.append(requested)
        base = rewritten.split("[", 1)[0]
        if base and base != rewritten and base not in candidates:
            candidates.append(base)
        for candidate in candidates:
            if _model_on_any_channel(session, candidate, found):
                self._switch_effort_to_config_option(candidate, rewritten)
                return candidate
        return None

    def _switch_effort_to_config_option(self, model: str, rewritten: str) -> None:
        """Drop the unadvertised bracket rewrite and require a confirmed ``reasoning_effort`` set."""
        self._override = EffortOverride(
            extra_args=self._override.extra_args,
            extra_env=self._override.extra_env,
            via_config_option=True,
            note=(
                f"reasoning effort via the 'reasoning_effort' config option "
                f"(bracket model id {rewritten!r} was not advertised)"
            ),
        )
        self._effort_applied = None
        self._effort_requires_confirmation = True
        self._target = Target(cli=self._descriptor.id, model=model)

    def _effort_unavailable(self, detail: str) -> ACPHandshakeError:
        """Handshake failure naming the requested effort, not a missing model."""
        requested = self._effort.value if self._effort is not None else "unknown"
        return ACPHandshakeError(
            ErrorCode.ACP_HANDSHAKE_FAILED,
            f"effort {requested!r} is not available on {self._descriptor.id}: {detail}",
            ReexecutionSafety.SAFE,
        )

    async def _select_effort(self, conn: ClientSideConnection) -> None:
        """Apply a config-option effort tier when the override routed here.

        The config-option effort channel (F8a): when the override routed this agent here
        (``via_config_option`` -- claude_code's ``effort`` option, codex's ``reasoning_effort`` option), find
        the advertised option among ``session.configOptions`` and set it to the requested tier, clamped to the
        option's own advertised values (so ``max`` on a codex option topping out at ``xhigh`` becomes
        ``xhigh``). ``effort_applied`` is updated to the tier actually set.

        When the Codex bracket-id channel was not advertised and this session fell back to the config
        option (``_effort_requires_confirmation``), a missing option or unconfirmed ``current_value`` is a
        hard :class:`ACPHandshakeError` -- never report ``effort_applied`` from a matching bare model id.
        Otherwise this remains non-fatal: an agent that advertises no such option is an honest no-op.
        """
        if self._effort is None or not self._override.via_config_option or self._session_id is None:
            return
        required = self._effort_requires_confirmation
        found = _effort_config_option(self._config_options)
        if found is None:
            if required:
                raise self._effort_unavailable(
                    f"not advertised by a reasoning_effort or effort config option ({self._override.note})"
                )
            return  # the agent advertised no effort option after all -- honest no-op (effort_applied stays None)
        config_id, supported = found
        applied = clamp_to_supported(self._effort, supported)
        if applied is None:
            if required:
                raise self._effort_unavailable(f"config option {config_id!r} advertises no reasoning-effort tiers")
            return
        try:
            response = await asyncio.wait_for(
                conn.set_config_option(config_id=config_id, value=applied.value, session_id=self._session_id),
                timeout=self._handshake_timeout,
            )
        except Exception as exc:
            if required:
                raise self._effort_unavailable(
                    f"set_config_option {config_id!r}={applied.value!r} failed: {exc}"
                ) from exc
            return
        if required and not _config_option_current_equals(response, config_id, applied.value):
            raise self._effort_unavailable(
                f"set_config_option {config_id!r}={applied.value!r} was not confirmed by current_value"
            )
        options = list(getattr(response, "config_options", None) or [])
        if options:
            self._config_options = options
        self._effort_applied = applied

    async def prompt(self, text: str, *, timeout_s: float) -> DelegationResult:
        """Run one prompt turn on the live session and return its normalized result.

        Never raises for an operational failure (timeout / refusal / empty / transport error): each becomes
        a failed :class:`DelegationResult` with an ACP error code. ``open`` must have succeeded first.
        """
        if self._conn is None or self._session_id is None:  # pragma: no cover - guarded by open()
            raise RuntimeError("ACPSession.prompt called before a successful open()")
        self._journal = EventJournal()
        self._client.journal = self._journal
        start = time.monotonic()
        blocks: list[PromptBlock] = [text_block(text)]
        # N1 (item 3): sample the agent's local process tree on a coarse timer for the duration of the turn,
        # keeping the peak descendant count -- a FLOOR for how many agents this voice spun up. Started here
        # and always stopped in the finally, so a timeout/error path still records what it saw before the cut.
        sampler = asyncio.create_task(self._sample_observed_agents())
        try:
            response = await asyncio.wait_for(
                self._conn.prompt(prompt=blocks, session_id=self._session_id),
                timeout=timeout_s,
            )
        except TimeoutError:
            await self.cancel()
            return self._stamp(
                _failed(
                    self._target,
                    self._policy,
                    start,
                    ErrorCode.ACP_TURN_TIMEOUT,
                    f"{self._descriptor.id} did not finish within {timeout_s:.0f}s",
                    _post_prompt_safety(self._journal),
                    partial=self._journal.message_text() or None,
                )
            )
        except Exception as exc:
            return self._stamp(
                _failed(
                    self._target,
                    self._policy,
                    start,
                    ErrorCode.ACP_TURN_ERROR,
                    f"ACP turn for {self._descriptor.id} failed: {exc}",
                    ReexecutionSafety.AMBIGUOUS,
                )
            )
        finally:
            # Stop the sampler and fold its final reading in, so even a timeout/error path records the peak it
            # saw. Cancel-then-await keeps no sampler task dangling on the loop. Best-effort: never raises.
            sampler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sampler
        result = _reduce(
            self._descriptor,
            self._target,
            self._policy,
            self._journal,
            response,
            self._session_id,
            start,
            model_confirmed=self._model_confirmed,
        )
        return self._stamp(result)

    async def _sample_observed_agents(self) -> None:
        """Poll the agent's process tree on a coarse timer, keeping the peak descendant count (N1, item 3).

        Runs off-thread (psutil is blocking) for the life of a turn; cancelled in :meth:`prompt`'s finally.
        Each sample is the agent pid plus its recursive children -- a FLOOR, since a sample can lose the race
        with a transient sub-process and psutil sees only local processes. A ``0`` sample (the pid already
        gone) never lowers the peak. Best-effort: the loop swallows everything but a cancellation.
        """
        pid = self._pid
        if pid is None:  # pragma: no cover - prompt() is guarded by a successful open() that set the pid
            return
        try:
            while True:
                count = await asyncio.to_thread(count_descendants, pid)
                if count > 0 and (self._observed_peak_agents is None or count > self._observed_peak_agents):
                    self._observed_peak_agents = count
                await asyncio.sleep(_OBSERVE_INTERVAL_S)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - a transparency sampler must never break the turn it observes
            return

    def _stamp(self, result: DelegationResult) -> DelegationResult:
        """Stamp the per-turn metadata onto the result: the effort tiers (F8a) and the observed peak (N1).

        ``effort`` / ``effort_applied`` echo what was requested and what the agent applied after clamping;
        ``observed_peak_agents`` carries the live sampler's high-water mark up so a panel can roll it into
        its :class:`~rutherford.domain.models.Topology` (a floor, ``None`` when nothing was sampled); ``argv``
        pins the logical launch for an F2 replay. ``requested_model`` / ``selected_model`` separate the
        pre-effort request from an in-session ACP-confirmed selection (launch-flag intent never fills
        ``selected_model``).
        """
        result.effort = self._effort
        result.effort_applied = self._effort_applied
        result.observed_peak_agents = self._observed_peak_agents
        result.argv = list(self._launch_argv)
        result.requested_model = self._requested_model
        result.selected_model = self._selected_model
        return result

    async def _bounded_cleanup(
        self,
        coro: Coroutine[Any, Any, _T],
        *,
        timeout_s: float,
        task_name: str,
        default: _T,
        cancel_on_timeout: bool = True,
    ) -> _T:
        """Await one cleanup step under a hard deadline; late work keeps the shared owner either way.

        ``cancel_on_timeout`` decides only what the deadline does to the WORK. A step that is pure waiting
        is cancelled, because abandoning the wait is the whole point and nothing is lost: ``session/cancel``
        hands a fully serialized line to the sender's own queue task, so giving up on the reply cannot
        truncate a write or strip the connection of anything.

        Every other step here must NOT be cancelled, and the test is whether the work can be REDONE, not
        whether it looks like waiting. Reaping a captured process tree obviously cannot -- nothing else
        knows that tree. Closing the transport looks like it could, and cannot: the SDK latches
        ``_closed`` before awaiting its own cleanup, so a cancel in the middle makes every later attempt a
        no-op and strands whatever had not run yet. Such a step defers to
        :func:`await_without_cancelling`, which bounds the wait and leaves the work to finish.

        Retention is NOT part of that choice, which is where this diverged from the terminal path before:
        the session used to keep its own capped set and silently stopped backing tasks past the cap, so a
        busy teardown could drop exactly the work this deadline promised to let finish.
        """
        if not cancel_on_timeout:
            return await await_without_cancelling(coro, timeout_s=timeout_s, task_name=task_name, default=default)
        task = asyncio.create_task(coro, name=task_name)
        register_pending_cleanup(task)
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout_s)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if task not in done:
            task.cancel()
            return default
        try:
            return task.result()
        except (Exception, asyncio.CancelledError):
            return default

    async def _await_stderr_drain(self) -> None:
        """Let the stderr reader finish on its own, then cancel it if it does not; never raise.

        The buffer is NOT cleared here. The handshake failure paths call ``close`` and only then build their
        message, so this is what makes the captured excerpt complete at the moment it is read.
        """
        task = self._stderr_task
        if task is None:
            return
        self._stderr_task = None
        done, _ = await asyncio.wait({task}, timeout=_STDERR_DRAIN_TIMEOUT_S)
        if task not in done:
            task.cancel()
        consume_task_result(task)

    async def _kill_direct_process(self, process: asyncio.subprocess.Process | None) -> None:
        """Hard-kill the direct ACP adapter and bound the wait for its process handle to signal exit."""
        if process is None or process.returncode is not None:
            return
        # * The descendant snapshot is complete before this call, so a hard kill freezes the tree before reap.
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()
        await await_without_cancelling(
            process.wait(),
            timeout_s=_DIRECT_PROCESS_KILL_WAIT_S,
            task_name="rutherford-acp-direct-process-wait",
            default=None,
        )

    async def cancel(self) -> None:
        """Best-effort bounded ``session/cancel`` for an in-flight turn; never raises operational failures."""
        if self._conn is not None and self._session_id is not None:
            await self._bounded_cleanup(
                self._conn.cancel(session_id=self._session_id),
                timeout_s=_CANCEL_TIMEOUT_S,
                task_name="rutherford-acp-session-cancel",
                default=None,
            )

    async def close(self) -> None:
        """Wait boundedly for one shared teardown task without letting caller cancellation abort process cleanup.

        The aggregate goes to the same owner as the stages inside it. The instance attribute alone is not
        enough: this caller budget is deliberately shorter than the stages it covers can take in the worst
        case, so ``close`` can return while ``_close_body`` is still running, and a session dropped in that
        window would leave the loop holding its teardown by a weak reference only.
        """
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_body(), name="rutherford-acp-session-close")
            register_pending_cleanup(self._close_task)
        done, _ = await asyncio.wait({self._close_task}, timeout=_SESSION_CLOSE_WAIT_S)
        if self._close_task in done:
            consume_task_result(self._close_task)

    async def _close_body(self) -> None:
        """Snapshot the live tree, kill its spawning parent, reap descendants, then close the ACP transport.

        The pre-parent-death snapshot starts on a dedicated thread rather than the shared executor and completes
        before the adapter is killed. This prevents executor starvation from postponing enumeration until after
        child reparenting. The direct adapter is then hard-killed independently of ACP SDK teardown, so a stuck
        ``stdin.wait_closed`` or ``ClientSideConnection.close`` cannot preserve the process. The public
        :meth:`close` has its own caller deadline while this instance-owned task continues safely in the background.
        """
        pid = self._pid
        process = self._process
        descendants: list[Any] = []
        try:
            if pid is not None:
                # * Bounded like every other stage. The snapshot runs FIRST, so an unbounded one never
                # returns, the body never reaches the kill below, and the `finally` never runs either --
                # a `finally` only fires when its block exits. The adapter would then outlive the server
                # process, which is the exact leak this teardown path exists to prevent.
                descendants = await snapshot_within_deadline(pid, timeout_s=_SNAPSHOT_TIMEOUT_S, source="session")
            # * Kill the spawning adapter immediately after its tree snapshot so it cannot create replacements.
            await self._kill_direct_process(process)
            # * Brokered commands retain their own shared kill tasks if this bounded aggregate is still running.
            await self._bounded_cleanup(
                self._client.shutdown_terminals(),
                timeout_s=_TERMINAL_SHUTDOWN_TIMEOUT_S,
                task_name="rutherford-acp-terminal-shutdown",
                default=None,
                cancel_on_timeout=False,
            )
            if descendants:
                await self._bounded_cleanup(
                    asyncio.to_thread(reap, descendants),
                    timeout_s=_DESCENDANT_REAP_TIMEOUT_S,
                    task_name="rutherford-acp-descendant-reap",
                    default=None,
                    cancel_on_timeout=False,
                )
            # * NOT cancellable, despite looking like the most cancellable stage here. The stack's exit
            # runs the SDK's `Connection.close` (acp/connection.py), which sets `_closed = True` and only
            # THEN rejects the pending outgoing requests and awaits the transport close and task shutdown;
            # `MessageSender.close` (acp/task/sender.py) latches the same way. A cancel landing in the
            # middle leaves the later steps undone forever, because the flag is already set and every
            # future `close()` returns immediately -- there is no second chance to finish them. Bounding
            # the wait is all that was ever needed here: the adapter is hard-killed above and again in the
            # `finally`, so the EOF this used to block on has already been delivered and the close
            # completes on its own.
            #
            # The LATCH is the load-bearing fact, not the list of steps inside it. ACP 0.12.1 deleted the
            # pluggable dispatcher/queue/state-store layer this comment used to enumerate, and the argument
            # survived unchanged because both `close` methods still return early on an already-set flag.
            await self._bounded_cleanup(
                self._stack.aclose(),
                timeout_s=_TRANSPORT_CLOSE_TIMEOUT_S,
                task_name="rutherford-acp-transport-close",
                default=None,
                cancel_on_timeout=False,
            )
            # * LAST, and deliberately so. The reader must stay live through every stage above: the adapter is
            # killed early, and anything it writes on the way out is exactly the diagnostic worth keeping, so
            # cancelling sooner would trade the evidence for nothing. By this point the child is dead and the
            # transport is closed, so stderr has EOF'd and the task ends on its own -- this awaits that rather
            # than forcing it, and the bound is only the backstop against a reader that somehow does not end.
            await self._await_stderr_drain()
        finally:
            # * Event-loop shutdown can cancel the shared task; the direct adapter still receives a hard kill.
            if process is not None and process.returncode is None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    process.kill()
            self._pid = None
            self._process = None
            self._conn = None


async def run_acp_turn(
    descriptor: AgentDescriptor,
    prompt: str,
    *,
    policy: PermissionPolicy,
    cwd: str,
    timeout_s: float,
    model: str | None = None,
    effort: Effort | None = None,
    base_depth: int = 0,
    parent_run_id: str | None = None,
    sandbox_root: str | None = None,
    resume_session_id: str | None = None,
) -> DelegationResult:
    """Open a one-shot session, run a single prompt turn, and return the normalized result.

    The spawn-per-delegation path for ``delegate`` / ``consensus``. ``effort`` is the reasoning-effort tier to
    apply over ACP (per-agent env / args / a model-id rewrite); it is echoed on the result as ``effort`` and
    ``effort_applied`` (F8a, 2-L). ``base_depth`` / ``parent_run_id`` are the N1 lineage signal layered onto
    the agent's environment so a Rutherford-driving-Rutherford chain is bounded. ``sandbox_root`` confines the
    agent's file/terminal callbacks to an isolated worktree / copy for a mutating run. ``resume_session_id``
    resumes a prior agent session (ACP ``session/load``) instead of opening a fresh one, where the agent
    supports it -- otherwise the turn fails ``RESUME_FAILED``. Never raises for an operational failure; a
    handshake / spawn / resume failure becomes a failed :class:`DelegationResult` (re-execution-safe), still
    carrying the requested effort.
    """
    start = time.monotonic()
    session = ACPSession(
        descriptor,
        policy=policy,
        cwd=cwd,
        model=model,
        effort=effort,
        base_depth=base_depth,
        parent_run_id=parent_run_id,
        sandbox_root=sandbox_root,
        resume_session_id=resume_session_id,
    )
    try:
        async with session:
            return await session.prompt(prompt, timeout_s=timeout_s)
    except ACPHandshakeError as exc:
        result = _failed(session.target, policy, start, exc.code, exc.message, exc.safety)
        result.effort = effort
        result.effort_applied = session.effort_applied
        result.argv = session.launch_argv  # F2: a spawn-failed leaf still records the argv it tried
        result.requested_model = session.requested_model
        result.selected_model = session.selected_model
        return result


def _reduce(
    descriptor: AgentDescriptor,
    target: Target,
    policy: PermissionPolicy,
    journal: EventJournal,
    response: PromptResponse,
    session_id: str,
    start: float,
    *,
    model_confirmed: bool,
) -> DelegationResult:
    """Project the finished turn's journal + stop reason into a normalized result."""
    text = journal.message_text().strip()
    cost = journal.usage()
    if response.stop_reason == "refusal":
        return _failed(
            target,
            policy,
            start,
            ErrorCode.ACP_REFUSED,
            f"{descriptor.id} refused the request",
            ReexecutionSafety.DUPLICATE_COST,
            cost=cost,
        )
    if not text:
        return _failed(
            target,
            policy,
            start,
            ErrorCode.ACP_EMPTY_ANSWER,
            f"{descriptor.id} ended the turn ({response.stop_reason}) with no answer text",
            ReexecutionSafety.DUPLICATE_COST,
            cost=cost,
        )
    # Provenance.model is the EFFECTIVE model the turn ran under (target.model) -- the identity F3 diversity and
    # the correlation discount key on. ``confirmed`` alone attests whether an in-session ACP selection was
    # verified; the model id is never nulled when unconfirmed, or a launch-argv / env-injected model (which
    # still ran) would lose its lineage and two same-model voices would dodge the correlation discount.
    return DelegationResult(
        target=target,
        ok=True,
        text=text,
        cost=cost,
        session_id=session_id,
        duration_s=round(time.monotonic() - start, 3),
        provenance=Provenance(provider=descriptor.provider, model=target.model, confirmed=model_confirmed),
        safety_mode=policy.mode,
    )


def _failed(
    target: Target,
    policy: PermissionPolicy,
    start: float,
    code: ErrorCode,
    message: str,
    safety: ReexecutionSafety,
    *,
    partial: str | None = None,
    cost: Cost | None = None,
) -> DelegationResult:
    """Build a failed result carrying the ACP error code and its re-execution-safety classification."""
    return DelegationResult(
        target=target,
        ok=False,
        duration_s=round(time.monotonic() - start, 3),
        error=ErrorInfo(code=code, message=message, reexecution_safety=safety),
        partial=partial,
        cost=cost,
        safety_mode=policy.mode,
    )


def _post_prompt_safety(journal: EventJournal) -> ReexecutionSafety:
    """Classify how unsafe a re-run is after the prompt was accepted, from what the journal observed."""
    if journal.saw_side_effect():
        return ReexecutionSafety.SIDE_EFFECTED
    if journal.saw_tool_activity():
        return ReexecutionSafety.AMBIGUOUS
    return ReexecutionSafety.DUPLICATE_COST


def _resolve_env(descriptor: AgentDescriptor) -> dict[str, str]:
    """The environment for the agent subprocess: inherited (or allowlisted), then config overrides on top."""
    if descriptor.env_passthrough is None:
        env = dict(os.environ)
    else:
        env = {name: os.environ[name] for name in descriptor.env_passthrough if name in os.environ}
    env.update(descriptor.env_overrides)
    return env


def _legacy_model_state(session: object) -> object | None:
    """The unstable ``session.models`` SessionModelState when the SDK still exposes it, else ``None``.

    ACP 0.10.x typed responses carry an optional ``models`` field; ACP 0.11+ removes the attribute entirely.
    Production code must never require ``.models`` -- a missing attribute is treated as no legacy channel.
    """
    return getattr(session, "models", None)


def _advertises_model(session: object, model_id: str) -> bool:
    """Whether the legacy SessionModelState channel advertised ``model_id`` (so set_model is safe)."""
    return model_id in _models_of(session)


def _model_on_any_channel(session: object, model_id: str, found: tuple[str, str | None, list[str]] | None) -> bool:
    """Whether ``model_id`` is advertised on the config-option channel or legacy ``session.models``."""
    in_config = found is not None and model_id in found[2]
    return in_config or _advertises_model(session, model_id)


def _launch_model_advertised(model: str, advertised: list[str]) -> bool:
    """Whether launch validation accepts ``model`` against an advertised id list.

    Exact membership first; then :func:`launch_advertisement_compatible` (boolean ``fast=`` only).
    """
    if model in advertised:
        return True
    return any(launch_advertisement_compatible(model, item) for item in advertised)


def _load_session_capability(init: object) -> bool | None:
    """Whether the agent advertised the ACP ``loadSession`` capability, or ``None`` when that cannot be read.

    Never touch ``InitializeResponse.agent_capabilities`` directly. Its ``AgentCapabilities | None`` annotation
    stopped being a guarantee in ACP SDK 0.12, which made deserialization lenient: the field carries a
    salvage-on-error wrap validator whose fallback substitutes a RAW DICT of all-false capability defaults when
    an agent sends a blob that fails validation, where 0.11 rejected the whole payload. A wrap validator's
    return value is not re-validated, so at runtime the attribute really is a ``dict`` while a type checker
    still reads the annotation and sees a model -- it cannot catch the ``caps.load_session`` AttributeError
    that follows, and neither can a test whose fake agent only ever builds a well-formed response.

    That AttributeError is not merely an ugly error. It is raised after the agent subprocess is spawned and
    from inside ``__aenter__``, which Python answers by skipping ``__aexit__`` -- the orphaned-process class
    this module's teardown exists to prevent. So the capability gets the same discipline :func:`_models_of`
    applies to the legacy ``session.models`` channel, for the same reason: an SDK-typed field whose value an
    agent controls is untrusted input, and this codebase reads untrusted input through a helper.

    Three outcomes, kept distinct so the caller can say WHY a resume was refused. ``True``: advertised.
    ``False``: the agent answered and the answer was no (including a well-formed response that omits
    capabilities entirely -- omitting an advertisement is a plain "no"). ``None``: nothing could be read.

    A ``dict`` is reported unreadable rather than mined for a ``loadSession`` key, and that is deliberate
    rather than lazy. Measured against the 0.12.0 wheel, the attribute is only ever a ``dict`` when the whole
    field failed validation, and the fallback the SDK substitutes is a hard-coded literal -- so its
    ``loadSession`` is always ``False`` no matter what the agent sent. A mapping the agent actually supplied
    still coerces to a real ``AgentCapabilities``, salvaging per-field. Reading the dict would therefore report
    the SDK's placeholder as the agent's own answer, telling an operator their agent cannot reload sessions
    when the truth is that it is emitting invalid protocol.
    """
    caps = getattr(init, "agent_capabilities", None)
    if caps is None:
        return False
    if isinstance(caps, dict):
        return None
    advertised = getattr(caps, "load_session", None)
    return advertised if isinstance(advertised, bool) else None


def _config_option_current_equals(response: object, config_id: str, model: str) -> bool:
    """Whether a ``set_config_option`` response confirms ``config_id``'s ``current_value`` equals ``model``."""
    options = getattr(response, "config_options", None)
    if not options:
        return False
    for option in options:
        if getattr(option, "id", None) != config_id:
            continue
        current = getattr(option, "current_value", None)
        return isinstance(current, str) and current == model
    return False


def _models_of(session: object) -> list[str]:
    """Legacy SessionModelState model ids, or ``[]`` when the channel is absent / empty.

    Safe on ACP 0.10.1 typed objects (``models`` may be ``None``) and on ACP 0.11+ / duck-typed responses that
    omit the attribute entirely. Only string ``model_id`` values are kept.
    """
    state = _legacy_model_state(session)
    if state is None:
        return []
    available = getattr(state, "available_models", None)
    if not available:
        return []
    ids: list[str] = []
    for info in available:
        model_id = getattr(info, "model_id", None)
        if isinstance(model_id, str):
            ids.append(model_id)
    return ids


def _effort_config_option(options: list[object]) -> tuple[str, list[Effort]] | None:
    """The advertised reasoning-effort config option as ``(config_id, supported_tiers)``, or ``None``.

    Matched by id against :data:`~rutherford.acp.effort.EFFORT_CONFIG_OPTION_IDS` (codex's
    ``reasoning_effort``, claude_code's ``effort``), so a new agent advertising one of those ids is covered
    without a code change. A boolean option (no ``options`` list, e.g. codex's ``fast-mode``) is skipped.
    ``supported_tiers`` are the option's select values parsed to :class:`Effort` -- each value is an id
    (``low`` / ``medium`` / ... and sometimes ``default`` / ``off``), and only the ones naming a real tier are
    kept, so ``default`` does not masquerade as one. A grouped option list (entries without a flat ``value``)
    yields no tiers rather than raising.
    """
    for option in options:
        config_id = getattr(option, "id", None)
        values = getattr(option, "options", None)
        if config_id not in EFFORT_CONFIG_OPTION_IDS or values is None:
            continue
        supported: list[Effort] = []
        for entry in values:
            raw = getattr(entry, "value", None)
            if not isinstance(raw, str):
                continue
            try:
                supported.append(Effort(raw))
            except ValueError:
                continue  # "default" / "off" and other non-tier option values are not effort tiers
        return str(config_id), supported
    return None


def _parse_model_option(option: object) -> tuple[str, str | None, list[str]] | None:
    """Parse one config option as a model SELECT option, or ``None`` when it is not a usable select option.

    A boolean option carries no value list and is skipped; only entries with a STRING ``value`` are kept, so a
    grouped-option header (no flat ``value``) cannot leak a non-string into the model list or a
    ``set_config_option`` call. ``current_value`` is returned when it is a string, else ``None``.
    """
    config_id = getattr(option, "id", None)
    values = getattr(option, "options", None)
    if values is None or not isinstance(config_id, str):
        return None  # a boolean option (no value list) or a malformed option is not the model channel
    selectable: list[str] = []
    for entry in values:
        raw = getattr(entry, "value", None)
        if isinstance(raw, str):
            selectable.append(raw)  # a grouped-option header has no flat str value and is skipped
    current = getattr(option, "current_value", None)
    return config_id, (current if isinstance(current, str) else None), selectable


_NON_MODEL_CATEGORIES = frozenset({"mode", "model_config", "thought_level"})


def _option_category(option: object) -> str | None:
    """This config option's semantic ACP category as a plain string, or ``None`` when it carries none we can read.

    ACP's own ``SessionConfigOptionCategory`` is a closed-ish set of STRINGS -- ``mode`` / ``model`` /
    ``model_config`` / ``thought_level``, plus any other string (names not starting with ``_`` are reserved
    for the spec, ``_``-prefixed ones are free for vendor use). There is no object form of a category on the
    wire in any published schema version. The ACP SDK 0.12.0 nonetheless generates the field as
    ``Optional[Union[str, Dict[str, Any]]]`` -- a code-generation artifact of that multi-variant ``anyOf``,
    not a protocol change -- so from 0.12.0 on, a malformed object-valued ``category`` PARSES into a raw dict
    where 0.11.0's ``Optional[str]`` rejected the whole payload and raised it as a loud ACP_HANDSHAKE_FAILED.
    Comparing that dict against a category name would just evaluate False, which is the silent-mismatch shape
    this codebase refuses: the reader cannot tell a deliberate "no match" from an unhandled type. So narrow to
    ``str`` here, once, and report anything else as UNTAGGED -- which is also what the spec demands of us, in
    its own words: a category "MUST NOT be required for correctness. Clients MUST handle missing or unknown
    categories gracefully."
    """
    category = getattr(option, "category", None)
    return category if isinstance(category, str) else None


def _model_config_option(options: list[object]) -> tuple[str, str | None, list[str]] | None:
    """The advertised model SELECT config option as ``(config_id, current_value, selectable_values)``, or ``None``.

    The SECOND ACP model channel. Some harnesses advertise their selectable models NOT in ``session.models``
    (SessionModelState) but as a ``session.configOptions`` select option -- claude_code's claude-agent-acp does
    exactly this (its ``session.models`` is empty; the model lives in a select option whose values are aliases
    like ``default`` / ``sonnet`` / ``haiku``). A semantic ``category == "model"`` option is AUTHORITATIVE; only
    when none is advertised does a literal ``id == "model"`` option serve as the fallback -- two passes so the
    advertised ORDER cannot let the id fallback win over a category-tagged option (a UX category is optional, so
    the id is the fallback, not a co-equal match). The category is authoritative in BOTH directions: an option
    the agent explicitly tagged with a spec-reserved category naming a *different* channel (``mode``,
    ``model_config``, ``thought_level``) is disqualified from the id fallback, so a mode selector that happens
    to be keyed ``model`` cannot be driven as the model channel. Unknown and ``_``-prefixed vendor categories
    are deliberately NOT disqualifying -- the spec reserves those for custom use, and an agent is free to put
    one on its genuine model selector, so those still reach the id fallback.
    """
    for option in options:
        if _option_category(option) == "model":
            parsed = _parse_model_option(option)
            if parsed is not None:
                return parsed
    for option in options:
        if getattr(option, "id", None) != "model":
            continue
        if _option_category(option) in _NON_MODEL_CATEGORIES:
            continue  # a spec-reserved category naming a DIFFERENT channel outranks a coincidental id
        parsed = _parse_model_option(option)
        if parsed is not None:
            return parsed
    return None
