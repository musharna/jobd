"""What the network actually told us — and whether the CLI says it out loud.

On 2026-09-02 the gt76 broker was healthy: container up, listening on :8765,
freshly auto-deployed to 0.5.43. `job ping` reported `health: unreachable`.
The real cause was a Tailscale ACL silently dropping packets, because this
machine had re-registered as a second, untagged node. An operator who knows
this codebase well read that flat "unreachable" as "the broker is down and the
fleet never upgraded", and reported that to the user before checking the host.

The client had the information needed to prevent that and threw it away. The
distinction it flattened is exactly the one that decides what you do next:

    refused  -> something answered and said no. The path is fine;
                the broker process is not running.
    timeout  -> nothing answered at all. The broker is probably fine;
                the path is not (firewall, ACL, routing).

Both surfaced identically, as
`BrokerUnreachable("<ExcName>: <str(e)> (JOBD_URL=...)")`.

**On the loopback probes below.** The obvious way to produce ECONNREFUSED is to
connect to a closed port on 127.0.0.1. That is not portable, and this repo's
own dev machine is the counter-example: under WSL2 `networkingMode=mirrored`,
IPv4 loopback to an unbound port is silently DROPPED (it times out after the
full connect timeout) while IPv6 `::1` still returns RST immediately. A test
that assumed refusal on 127.0.0.1 passes on a normal Linux box and fails here —
so these tests ask the kernel which loopback address does what, and skip with a
stated reason rather than asserting into an environment that cannot produce the
condition. The probe doubles as the positive control: it only returns an
address after observing the exact errno the test depends on.
"""

from __future__ import annotations

import socket
import threading

import httpx
import pytest
from typer.testing import CliRunner

from jobd.client import (
    _UNREACHABLE_HINTS,
    BrokerUnreachable,
    JobdClient,
    classify_connect_failure,
)


def _closed_port(host: str, family: int) -> int:
    """Bind a port (so it is certainly free of a listener) and close it."""
    s = socket.socket(family)
    s.bind((host, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _loopback_that(behaviour: str) -> tuple[str, int] | None:
    """Return a (host, closed_port) whose connect actually does `behaviour`.

    `behaviour` is "refuse" or "drop". Returns None when no loopback family on
    this machine produces it, so the caller can skip honestly instead of
    asserting against a condition the OS will not create.
    """
    for family, host in ((socket.AF_INET6, "::1"), (socket.AF_INET, "127.0.0.1")):
        try:
            port = _closed_port(host, family)
        except OSError:
            continue  # family unavailable
        try:
            socket.create_connection((host, port), timeout=1.5).close()
        except ConnectionRefusedError:
            if behaviour == "refuse":
                return host, port
        except (TimeoutError, OSError):
            if behaviour == "drop":
                return host, port
    return None


def _url(host: str, port: int) -> str:
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


# --------------------------------------------------------------------------
# The assumption the branch order rests on.
# --------------------------------------------------------------------------


def test_connect_timeout_is_not_a_subclass_of_connect_error():
    """`classify_connect_failure` checks ConnectTimeout BEFORE walking the cause
    chain for a refused connection. That ordering is only safe while httpx keeps
    these two as siblings under TransportError. If it ever inverts, a refused
    connection would report as a timeout and send operators hunting a firewall
    that isn't there. Pin it.
    """
    assert not issubclass(httpx.ConnectTimeout, httpx.ConnectError)
    assert issubclass(httpx.ConnectTimeout, httpx.TransportError)
    assert issubclass(httpx.ConnectError, httpx.TransportError)


def test_every_kind_has_an_actionable_hint():
    """A kind with no hint tells the operator nothing, which is the bug this
    module exists to fix. `network` is the deliberate exception: it means "we
    could not tell", and inventing advice there is worse than silence.
    """
    for kind, hint in _UNREACHABLE_HINTS.items():
        if kind == "network":
            assert hint == ""
            continue
        assert hint, f"kind {kind!r} carries no hint"
        assert len(hint) > 40, f"kind {kind!r} hint is too terse to act on: {hint!r}"


# --------------------------------------------------------------------------
# Real execution: failures produced by an actual kernel, not a fixture.
# --------------------------------------------------------------------------


def test_refused_connection_is_classified_and_hinted_as_a_dead_broker():
    """REAL socket: nothing is listening, and this loopback family sends RST."""
    found = _loopback_that("refuse")
    if found is None:
        pytest.skip("no loopback family on this host refuses connections to closed ports")
    host, port = found

    client = JobdClient(base_url=_url(host, port), timeout=(2.0, 2.0))
    with pytest.raises(BrokerUnreachable) as ei:
        client.get("/health")

    assert ei.value.kind == "refused", f"got kind={ei.value.kind!r} for a refused connection"
    assert "not running" in ei.value.hint
    # It must NOT send the operator after the network — that is the whole point.
    assert "firewall" not in ei.value.hint.lower()


def test_dropped_connection_is_classified_as_a_path_problem():
    """REAL socket: the incident itself — a SYN that goes nowhere.

    Skipped on hosts that refuse instead of dropping (normal Linux); runs for
    real on any host whose loopback drops, which is precisely how the 2026-09-02
    outage presented.
    """
    found = _loopback_that("drop")
    if found is None:
        pytest.skip("no loopback family on this host drops connections to closed ports")
    host, port = found

    client = JobdClient(base_url=_url(host, port), timeout=(1.0, 1.0))
    with pytest.raises(BrokerUnreachable) as ei:
        client.get("/health")

    assert ei.value.kind == "timeout", f"got kind={ei.value.kind!r} for a dropped connection"
    assert "firewall" in ei.value.hint or "ACL" in ei.value.hint
    assert "broker" in ei.value.hint  # steers away from blaming the broker


def test_unresolvable_host_is_classified_as_dns():
    """REAL resolver: `.invalid` is reserved by RFC 2606 and cannot resolve."""
    client = JobdClient(base_url="http://broker.invalid:8765", timeout=(3.0, 3.0))
    with pytest.raises(BrokerUnreachable) as ei:
        client.get("/health")

    assert ei.value.kind == "dns", f"got kind={ei.value.kind!r} for an unresolvable host"
    assert "resolve" in ei.value.hint


def test_server_that_accepts_then_goes_quiet_is_a_read_timeout_not_a_dead_broker():
    """REAL socket: accept the connection, then never answer.

    The wedged/overloaded broker. Reporting it as "unreachable" would send an
    operator to check whether the process is running — it is, and that is the
    misleading answer this test exists to prevent.
    """
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    accepted: list[socket.socket] = []
    stop = threading.Event()

    def _accept_and_stall() -> None:
        srv.settimeout(5.0)
        try:
            conn, _ = srv.accept()
            accepted.append(conn)
            stop.wait(5.0)  # hold it open, send nothing
        except OSError:
            pass

    t = threading.Thread(target=_accept_and_stall, daemon=True)
    t.start()
    try:
        client = JobdClient(base_url=f"http://127.0.0.1:{port}", timeout=(2.0, 0.5))
        with pytest.raises(BrokerUnreachable) as ei:
            client.get("/health")
        assert ei.value.kind == "read_timeout", f"got kind={ei.value.kind!r} for a stalled server"
        assert "running" in ei.value.hint
    finally:
        stop.set()
        for c in accepted:
            c.close()
        srv.close()
        t.join(timeout=2.0)


def test_connect_timeout_type_classifies_without_a_network():
    """Type-level companion to the dropped-connection test, so the timeout branch
    stays covered on hosts where no loopback family drops."""
    assert classify_connect_failure(httpx.ConnectTimeout("timed out")) == "timeout"


# --------------------------------------------------------------------------
# The CLI surface: a broker that answered is not "unreachable".
# --------------------------------------------------------------------------


def _patch_client(monkeypatch, exc):
    import job_cli.cli as cli_mod

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def get(self, path, params=None):
            raise exc

    monkeypatch.setattr(cli_mod, "JobdClient", FakeClient)
    return cli_mod


def test_ping_does_not_call_an_answering_broker_unreachable(monkeypatch):
    """A 401 means the broker received the request, parsed it, and rejected the
    credential. Calling that "unreachable" is what sends an operator to the
    network when the answer is a missing token.
    """
    import json as _json

    from jobd.client import BrokerRefusal

    cli_mod = _patch_client(
        monkeypatch,
        BrokerRefusal(
            "broker 401", status_code=401, detail="missing Authorization: Bearer <token> header"
        ),
    )
    r = CliRunner().invoke(cli_mod.app, ["ping", "--json"])

    assert r.exit_code == 2  # still unhealthy — we just refuse to lie about why
    payload = _json.loads(r.stdout)
    assert payload["reachable"] is True, "the broker answered; reachable must not be False"
    assert payload["healthy"] is False
    assert payload["kind"] == "refusal"
    assert "token" in (payload["hint"] or "").lower()


def test_ping_surfaces_the_hint_in_human_output(monkeypatch):
    """The human path is the one an operator actually reads at 3am."""
    cli_mod = _patch_client(
        monkeypatch,
        BrokerUnreachable(
            "ConnectTimeout: timed out (JOBD_URL=http://x)",
            kind="timeout",
            hint=_UNREACHABLE_HINTS["timeout"],
        ),
    )
    r = CliRunner().invoke(cli_mod.app, ["ping"])

    assert r.exit_code == 2
    assert "unreachable (timeout)" in r.output
    assert "hint:" in r.output
    assert "firewall" in r.output or "ACL" in r.output
