"""Typed accuracy-target routes sharing the existing authenticated dispatcher."""
from __future__ import annotations

import inspect
from typing import Annotated, Any, Literal
from fastapi import Request, Query
from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from aws_mesouq import benchmark_accuracy as wire
from aws_mesouq import benchmark_accuracy_protocol as protocol
from .benchmark_http import Scenario, Controls


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Evaluation(StrictModel):
    cohort: Literal["central_90"]
    guide_case_count: Literal[8]
    confirmation_case_count: Literal[256]
    posterior_draws: Literal[2048]


class Budget(StrictModel):
    mode: Literal["accuracy_target"]
    method_gpu_seconds: Annotated[int, Field(ge=1, le=2147483647)]


class Profiles(StrictModel):
    profile_id: Literal["w2-rms0p1-central90-2048-244of256-v1"]
    effort_policy_id: Literal["bounded-effort-ladders-v1"]
    reference_profile_id: Literal["independent-resolution-relative-reference-v1"]


class CreateCampaign(StrictModel):
    schema_version: Literal[2]
    idempotency_key: Annotated[str, Field(min_length=1, max_length=128)]
    scenario: Scenario
    seed: Annotated[int, Field(ge=0, le=2147483647)]
    controls: Controls
    evaluation: Evaluation
    budget: Budget
    accuracy_target: Profiles

    @model_validator(mode="before")
    @classmethod
    def strict_raw_contract(cls, value):
        # Literal/Pydantic equality must not erase raw bool or float/integer types.
        return wire.validate_create(value)


class Empty(StrictModel):
    pass


class Claim(RootModel[dict[str, Any]]):
    @model_validator(mode="before")
    @classmethod
    def strict_raw_contract(cls, value):
        result = protocol.validate_claim(value)
        if not isinstance(result.get("signature"), str):
            raise ValueError("Signed connector claim required")
        return result


class Event(RootModel[dict[str, Any]]):
    @model_validator(mode="before")
    @classmethod
    def strict_raw_contract(cls, value):
        return protocol.validate_event(value)


class Archive(StrictModel):
    project_id: str
    campaign_id: str
    connector_id: str
    event_id: str
    receipt: dict[str, Any]
    signature: str


def attach_accuracy_routes(app, dispatch):
    async def call(request, body=None):
        result = dispatch(request, {} if body is None else body)
        return await result if inspect.isawaitable(result) else result

    base = "/v1/projects/{project}/benchmark-accuracy"

    @app.get(base + "/capabilities")
    async def accuracy_capabilities(project: str, request: Request):
        return await call(request)

    @app.get(base + "/campaigns")
    async def accuracy_campaigns(project: str, request: Request,
        archived: Literal["all", "true", "false"] = "all",
        limit: Annotated[int, Query(ge=1, le=50)] = 50, cursor: str | None = None):
        return await call(request, {"archived": archived, "limit": limit, "cursor": cursor})

    @app.post(base + "/campaigns")
    async def accuracy_create(project: str, payload: CreateCampaign, request: Request):
        return await call(request, payload.model_dump(mode="json"))

    @app.get(base + "/campaigns/{campaign}")
    async def accuracy_detail(project: str, campaign: str, request: Request):
        return await call(request)

    @app.get(base + "/campaigns/{campaign}/tasks")
    async def accuracy_tasks(project: str, campaign: str, request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 50, cursor: str | None = None):
        return await call(request, {"limit": limit, "cursor": cursor})

    @app.get(base + "/campaigns/{campaign}/tasks/{task}")
    async def accuracy_task(project: str, campaign: str, task: str, request: Request):
        return await call(request)

    @app.post(base + "/campaigns/{campaign}/cancel")
    async def accuracy_cancel(project: str, campaign: str, payload: Empty, request: Request):
        return await call(request, payload.model_dump())

    @app.post(base + "/campaigns/{campaign}/archive")
    async def accuracy_archive(project: str, campaign: str, payload: Empty, request: Request):
        return await call(request, payload.model_dump())

    @app.get(base + "/campaigns/{campaign}/accounting")
    async def accuracy_accounting(project: str, campaign: str, request: Request):
        return await call(request)

    @app.get(base + "/campaigns/{campaign}/results")
    async def accuracy_results(project: str, campaign: str, request: Request):
        return await call(request)

    @app.get(base + "/campaigns/{campaign}/artifacts/{artifact}")
    async def accuracy_artifact(project: str, campaign: str, artifact: str, request: Request):
        return await call(request)

    @app.post("/v1/benchmark-connector/accuracy/claim")
    async def accuracy_claim(payload: Claim, request: Request):
        return await call(request, payload.model_dump())

    @app.post("/v1/benchmark-connector/accuracy/campaigns/{campaign}/tasks/{task}/events")
    async def accuracy_event(campaign: str, task: str, payload: Event, request: Request):
        return await call(request, payload.model_dump())

    @app.post("/v1/benchmark-connector/accuracy/campaigns/{campaign}/archive")
    async def accuracy_archive_verified(campaign: str, payload: Archive, request: Request):
        return await call(request, payload.model_dump())
