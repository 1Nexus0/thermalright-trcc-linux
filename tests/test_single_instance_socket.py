"""``SingleInstance``'s accept loop — the socket a second launch talks to.

A second ``trcc gui`` does not start a second app: it finds the running one's
socket, sends ``{"raise": true}`` and exits, and the running window comes to
the front.  ``_accept_loop`` is what receives that.

It measured depth 5 with NO test naming it (16 of the 39 functions at depth
>= 4 were in that state on 2026-09-10).  The depth is not the problem —
accept, read, decode, dispatch is honestly that deep.  What was missing is
that every one of those layers swallows its own errors on purpose, and
nothing checked that swallowing an error leaves the loop ALIVE.  A loop that
dies on the first malformed byte looks identical to a healthy one until the
day a user's second launch stops raising the window.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from trcc.ipc import SingleInstance, _instance_socket_path


@pytest.fixture(autouse=True)
def _isolated_runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind under a per-test directory, never the developer's real one."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))


def _needs_af_unix() -> None:
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("AF_UNIX unavailable (legacy Windows takes the msvcrt path)")


def _send(name: str, payload: bytes) -> None:
    """Talk to the instance socket exactly as a second launch would."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2.0)
    try:
        client.connect(str(_instance_socket_path(name)))
        client.sendall(payload)
    finally:
        client.close()


def _instance(name: str) -> SingleInstance:
    inst = SingleInstance(name)
    assert inst is not None, "no peer should hold this per-test socket"
    return inst


def test_a_second_launch_raises_the_running_window() -> None:
    """The whole point: `{"raise": true}` reaches `on_raise`."""
    _needs_af_unix()
    raised = threading.Event()
    inst = _instance("gate-raise")
    inst.on_raise = raised.set
    try:
        _send("gate-raise", b'{"raise": true}\n')
        assert raised.wait(3.0), "on_raise never fired for a valid message"
    finally:
        inst.close()


def test_malformed_traffic_does_not_deafen_the_loop() -> None:
    """A bad peer must not cost every LATER launch its window-raise.

    Each layer swallows and continues on purpose — this asserts the
    `continue`, not the `except`, by sending a valid message afterwards.
    """
    _needs_af_unix()
    calls: list[str] = []
    heard = threading.Event()

    def _on_raise() -> None:
        calls.append("raise")
        heard.set()

    inst = _instance("gate-malformed")
    inst.on_raise = _on_raise
    try:
        _send("gate-malformed", b"not json at all\n")
        _send("gate-malformed", b"\xff\xfe binary garbage\n")
        _send("gate-malformed", b"")                       # connect, say nothing
        _send("gate-malformed", json.dumps({"other": 1}).encode() + b"\n")

        _send("gate-malformed", b'{"raise": true}\n')
        assert heard.wait(3.0), (
            "the loop stopped listening after malformed traffic — a second "
            "launch would silently fail to raise the window")
        assert calls, "on_raise never fired"
    finally:
        inst.close()


def test_a_callback_that_raises_does_not_kill_the_loop() -> None:
    """`on_raise` runs UI code on the accept thread; if it throws, the next
    launch must still be heard."""
    _needs_af_unix()
    calls: list[int] = []
    second = threading.Event()

    def _on_raise() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("slot blew up")
        second.set()

    inst = _instance("gate-callback")
    inst.on_raise = _on_raise
    try:
        _send("gate-callback", b'{"raise": true}\n')
        _send("gate-callback", b'{"raise": true}\n')
        assert second.wait(3.0), (
            "one exception from on_raise deafened the loop permanently")
    finally:
        inst.close()


def test_the_socket_is_per_ui_flavour() -> None:
    """`trcc gui` and `trcc qtgui` are separate instances, so separate sockets."""
    assert _instance_socket_path("gui") != _instance_socket_path("qtgui")
    assert _instance_socket_path("gui").parent.name == SingleInstance._DIR_NAME
    assert os.environ["XDG_RUNTIME_DIR"] in str(_instance_socket_path("gui"))


def test_only_a_raise_request_raises() -> None:
    """A well-formed message that does NOT ask to raise must not raise.

    The negative needs care.  Both a wrongly-accepted message and the valid
    one that follows call the SAME callback, so "did it fire?" cannot tell
    them apart, and an assertion taken the moment the valid one arrives passes
    either way — measured: the mutation that accepts any dict slipped straight
    through two earlier versions of this check.

    So the loop is allowed to go quiet first (it is serial, one connection at
    a time), and the count is taken after a bounded settle.
    """
    _needs_af_unix()
    calls: list[str] = []
    inst = _instance("gate-selective")
    inst.on_raise = lambda: calls.append("raise")
    try:
        for payload in (b'{"other": 1}\n', b'{"raise": false}\n', b'[]\n',
                        b'"a string"\n'):
            _send("gate-selective", payload)
        time.sleep(0.2)
        assert calls == [], (
            f"a message that never asked to raise triggered one: {calls}")
    finally:
        inst.close()
