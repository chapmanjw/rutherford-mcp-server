# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Built-in role personas, shipped as package data.

Each ``<id>.md`` is a reusable system-prompt persona: a YAML-ish frontmatter block (``name`` and
``description``) followed by the markdown body, which IS the role's prompt. They are loaded at
startup by :class:`rutherford.services.roles.RoleStore` via :mod:`importlib.resources`, so they
resolve whether Rutherford runs from a source checkout or an installed wheel. A caller selects one
with ``role="<id>"`` on ``delegate`` / ``consensus`` / ``debate`` and enumerates them with
``list_roles``; a ``role_dirs`` directory may override a built-in of the same id.
"""
