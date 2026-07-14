"""Every broker route is either exposed on MCP or explicitly, reasonedly not.

The recurring theme of the 2026-07-12 audit was **surfaces drift, and fixes land on one
of them**: the broker grows a route or a field, the CLI gets it, and MCP quietly doesn't
(or vice versa). That is how `scheduling_timeout_s` was silently dropped by MCP submit for
several releases while a test named "round trips every JobSubmit field" stayed green.

The fix there was to DERIVE the field list from the model and force every exclusion to be
named. This is the same shape one level up, for routes: the route table is read from the
live FastAPI app, so a route added tomorrow must be either mapped to a tool or listed in
_NOT_ON_MCP with a reason. **Forgetting is what fails.**

This deliberately does NOT demand that every route become a tool. MCP tool budget is real
— every tool competes for the model's attention, and a duplicate `jobd_job_get` was
deleted in v0.5.16 precisely because a redundant tool with a misleading description
misdirects tool selection. The point is that the choice is *made*, not defaulted into.
"""

from __future__ import annotations

import pytest

from jobd.app import build_app
from jobd.mcp.server import _TOOLS

# Broker routes deliberately absent from MCP, and why. A route here is a decision;
# a route missing from BOTH this map and the tool list is a bug.
_NOT_ON_MCP: dict[str, str] = {
    # --- worker-plane: the worker daemon calls these, never an agent. ---
    "POST /next-job": "worker-plane: the dispatch long-poll. An agent claiming jobs would steal work.",
    "POST /heartbeat": "worker-plane: capacity ad from the worker daemon.",
    "POST /jobs/{job_id}/log": "worker-plane: the worker streams captured output here.",
    "POST /jobs/{job_id}/started": "worker-plane: worker-side state transition.",
    "POST /jobs/{job_id}/complete": "worker-plane: worker-side state transition.",
    "POST /jobs/{job_id}/refuse-admission": "worker-plane: worker refuses a job at dispatch.",
    "POST /jobs/{job_id}/checkpoint-complete": "worker-plane: preemption handshake.",
    "GET /jobs/{job_id}/signal": "worker-plane: the worker polls this for cancel/preempt.",
    "POST /events": "worker-plane: event ingest. Agents READ events (jobd_events), never write them.",
    # --- covered by an existing tool's arguments, so a separate tool would be a duplicate. ---
    "GET /wait/{job_id}": "covered: jobd_submit(wait=true) and jobd_status(wait=true) block on this.",
    # --- operator-plane: rare, stateful, or better done deliberately by a human. ---
    "POST /reload": "operator-plane: re-reads config from disk. Not an agent action.",
    "POST /projects/{name}": "operator-plane: sets a project's scheduling priority fleet-wide.",
    "POST /projects/{name}/nudge": "operator-plane: as above.",
    "GET /projects": "operator-plane: read-only, but only meaningful next to the setters.",
    "POST /jobs/{job_id}/preempt-blockers": "operator-plane: force-preempts OTHER users' jobs.",
    # --- diagnostics an agent has no way to act on. ---
    "GET /gpu-holders": "diagnostic: host-level GPU process inventory; jobd_workers carries the capacity an agent needs.",
    "POST /classify": "internal: the submit path already classifies; exposing it invites divergence.",
    "POST /resolve": "internal: effective-config preview. jobd_submit(dry_run) surfaces the same answer.",
    "GET /health": "infra: liveness. The container healthcheck uses it; an agent gets nothing from it.",
    "GET /livez": "infra: unauthenticated liveness probe for external monitors (Uptime Kuma). Deliberately mute — an agent learns nothing from 'alive'.",
    "GET /readyz": "infra: unauthenticated readiness probe. Same — jobd_workers tells an agent whether the fleet can actually take work.",
}

# route -> the MCP tool that covers it
_ON_MCP: dict[str, str] = {
    "POST /submit": "jobd_submit",
    "GET /jobs": "jobd_list",
    "POST /jobs/{job_id}/cancel": "jobd_cancel",
    "POST /jobs/{job_id}/preempt": "jobd_preempt",
    "GET /events": "jobd_events",
    "GET /workers": "jobd_workers",
    "DELETE /workers/{host}": "jobd_worker_delete",
    "GET /jobs/{job_id}": "jobd_status",
    "GET /jobs/{job_id}/output": "jobd_logs",
}


def _broker_routes(tmp_path, sample_projects_yaml, sample_profiles_yaml, sample_classifier_yaml):
    app = build_app(
        db_url=f"sqlite:///{tmp_path}/parity.db",
        projects_path=sample_projects_yaml,
        profiles_path=sample_profiles_yaml,
        classifier_path=sample_classifier_yaml,
        logs_path=tmp_path / "logs",
    )
    out = set()
    for r in app.routes:
        path = getattr(r, "path", None)
        methods = getattr(r, "methods", None) or set()
        if not path or path.startswith("/openapi") or path in ("/docs", "/redoc"):
            continue
        for m in methods:
            if m in ("HEAD", "OPTIONS"):
                continue
            out.add(f"{m} {path}")
    return out


@pytest.fixture
def broker_routes(tmp_path, sample_projects_yaml, sample_profiles_yaml, sample_classifier_yaml):
    return _broker_routes(
        tmp_path, sample_projects_yaml, sample_profiles_yaml, sample_classifier_yaml
    )


def test_every_broker_route_is_exposed_or_explicitly_excluded(broker_routes):
    """The guard. A new route must be mapped to a tool or excused by name."""
    accounted = set(_ON_MCP) | set(_NOT_ON_MCP)
    unaccounted = broker_routes - accounted
    assert not unaccounted, (
        "these broker routes are on NEITHER the MCP tool map nor the documented "
        f"exclusion list: {sorted(unaccounted)}.\n\n"
        "This is the drift the 2026-07-12 audit kept finding: a route lands, one surface "
        "gets it, the other silently doesn't. Decide: add an MCP tool and map it in "
        "_ON_MCP, or add it to _NOT_ON_MCP with the reason it should not be an agent "
        "action. Either is fine — defaulting into the gap is not."
    )


def test_no_stale_entries_in_the_parity_map(broker_routes):
    """Routes that no longer exist must not linger in either map, or the map is fiction."""
    stale = (set(_ON_MCP) | set(_NOT_ON_MCP)) - broker_routes
    assert not stale, (
        f"these routes are listed in the MCP parity map but no longer exist on the "
        f"broker: {sorted(stale)}. Remove them — a map that describes a broker that "
        "isn't there stops being a guard and becomes decoration."
    )


def test_every_mapped_tool_actually_exists():
    """_ON_MCP must name real tools — otherwise the coverage it claims is imaginary."""
    registered = {name for name, _, _, _ in _TOOLS}
    claimed = set(_ON_MCP.values())
    missing = claimed - registered
    assert not missing, (
        f"_ON_MCP claims these tools cover a route, but they are not registered in "
        f"_TOOLS: {sorted(missing)}"
    )


def test_every_registered_tool_covers_a_route():
    """And the converse: a tool that maps to nothing is a tool nobody can justify."""
    registered = {name for name, _, _, _ in _TOOLS}
    orphans = registered - set(_ON_MCP.values())
    assert not orphans, (
        f"these MCP tools are registered but cover no broker route in _ON_MCP: "
        f"{sorted(orphans)}. Every tool costs model attention — a duplicate "
        "jobd_job_get was deleted in v0.5.16 for exactly this reason. Map it or drop it."
    )
