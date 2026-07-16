"""Config surface: /classify, /projects..., /reload, /resolve.

Stage-3 split (backlog 2026-07-15): endpoint bodies are VERBATIM from
app.py's build_app — build_router unpacks BrokerDeps into the same local
names the closures always captured, so the move is byte-identical at the
body level and the whole suite passes unchanged.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from jobd.broker.context import BrokerDeps
from jobd.broker.projects import (
    _persist_projects,
    _projects_to_jsonable,
    projects_mutation_lock,
)
from jobd.config import (
    ProjectEntry,
    load_classifier_rules,
    load_effective_projects,
    load_profiles,
    resolve_effective_config,
    resolve_profile,
)
from jobd.models import (
    ClassifyRequest,
    ClassifyResult,
    FieldResolution,
    JobSubmit,
    NudgePriorityRequest,
    ResolvedConfig,
    SetPriorityRequest,
)


def build_router(deps: BrokerDeps) -> APIRouter:
    router = APIRouter()
    state = deps.state

    @router.post("/classify", response_model=ClassifyResult)
    def classify_endpoint(req: ClassifyRequest) -> ClassifyResult:
        from jobd.classifier import classify as _classify

        return _classify(req.cmd, state["classifier"])

    @router.get("/projects")
    def list_projects():
        return _projects_to_jsonable(state["projects"])

    @router.post("/projects/{name}")
    def set_project_priority(name: str, payload: SetPriorityRequest):
        # Lock: these endpoints run on the threadpool and mutate-then-iterate
        # the shared projects dict; unsynchronized, a concurrent set/nudge can
        # blow up mid-iteration or last-write-win the overlay (F7).
        with projects_mutation_lock:
            priority = max(0, min(100, payload.priority))
            existing = state["projects"].get(name)
            if existing is None:
                state["projects"][name] = ProjectEntry(priority=priority)
            else:
                existing.priority = priority
            _persist_projects(state)
            return _projects_to_jsonable(state["projects"])

    @router.post("/projects/{name}/nudge")
    def nudge_project_priority(name: str, payload: NudgePriorityRequest):
        with projects_mutation_lock:
            delta = payload.delta
            existing = state["projects"].get(name)
            if existing is None:
                base_entry = state["projects"].get("_default")
                base = base_entry.priority if base_entry is not None else 40
                new_priority = max(0, min(100, base + delta))
                state["projects"][name] = ProjectEntry(priority=new_priority)
            else:
                existing.priority = max(0, min(100, existing.priority + delta))
            _persist_projects(state)
            return _projects_to_jsonable(state["projects"])

    @router.post("/reload")
    def reload_config():
        # Re-read the git baseline AND re-apply the runtime overrides overlay, so
        # a `git pull` of projects.yaml takes effect without discarding priorities
        # set at runtime via `job projects set/nudge` (audit 2026-07-12).
        projects, base_priorities = load_effective_projects(
            state["paths"]["projects"], state["paths"]["project_overrides"]
        )
        state["projects"] = projects
        state["base_priorities"] = base_priorities
        state["profiles"] = load_profiles(state["paths"]["profiles"])
        state["classifier"] = load_classifier_rules(state["paths"]["classifier"])
        return {"reloaded": True}

    @router.post("/resolve", response_model=ResolvedConfig)
    def resolve_job(req: JobSubmit) -> ResolvedConfig:
        """Dry-run submit: return the effective resolved config without
        enqueuing a job. Sources are tagged so callers can see *why* each
        field landed where it did.
        """
        profile_spec = None
        if req.profile:
            profile_spec = resolve_profile(state["profiles"], req.profile)
            if profile_spec is None:
                raise HTTPException(status_code=404, detail=f"unknown profile: {req.profile}")

        eff = resolve_effective_config(req, state["projects"], profile_spec)
        # requires is carried as the JobRequires OBJECT internally (so /submit
        # can serialize it); the API surfaces it model_dumped. Every other field
        # is the shared FieldResolution verbatim — same precedence /submit runs.
        requires_value = eff.requires.value
        return ResolvedConfig(
            project=req.project,
            effective_priority=eff.priority,
            effective_host_pin=eff.host_pin,
            effective_max_wall_s=eff.max_wall_s,
            effective_idle_timeout_s=eff.idle_timeout_s,
            effective_checkpoint_grace_s=eff.checkpoint_grace_s,
            effective_preemptible=eff.preemptible,
            effective_requires=FieldResolution(
                value=requires_value.model_dump() if requires_value is not None else None,
                source=eff.requires.source,
            ),
            effective_escalate_to_arc=eff.escalate_to_arc,
            submit_warning=eff.unknown_project_warning,
        )

    return router
