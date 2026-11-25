"""Tests for the ACP surfaces (SPEC 2 Wave 7, C7.6).

The server front in-process (dispatch, error envelopes, fs scoping — the
``mcp_server`` suite's shape), the server front as a real child process over
real pipes (the streaming and cancel acceptance), and the client front
against a checked-in fake external agent spawned under a
:class:`~paa.sandbox.base.SandboxSession` (mediation: refused and journaled
policy violations, served file reads, cancelled turns). No network, no model
key: the chat side rides the deterministic ``echo`` seam.
"""
