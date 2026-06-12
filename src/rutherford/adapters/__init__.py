# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Adapters: one hand-written code adapter per CLI.

The orchestration core depends only on the :class:`~rutherford.adapters.base.CLIAdapter`
interface and never imports a concrete adapter; adapters are wired in through the registry.
"""
