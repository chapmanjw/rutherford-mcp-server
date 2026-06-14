# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ACP-native runtime: drive coding agents over Zed's Agent Client Protocol.

The heart of v3. :mod:`descriptors` declares the agents (the small replacement for a hand-written
subprocess adapter); :mod:`session` drives one prompt turn end to end; :mod:`client` implements
Rutherford's side of the protocol (the callbacks an agent invokes); :mod:`journal` is the event-sourced
record a result is projected from; and :mod:`permission` maps the universal safety mode to ACP
permission / filesystem / terminal decisions.
"""
