# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Adapters: one per CLI, plus a config-driven generic adapter.

The orchestration core depends only on the :class:`~rutherford.adapters.base.CLIAdapter`
interface and never imports a concrete adapter; adapters are wired in through the registry.
"""
