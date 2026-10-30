"""Typed FastAPI control routes behind the existing Gateway JWT preflight."""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from aws_mesouq.benchmark import validate_supported_contexts
from mangum import Mangum

from .api import APIError

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Scenario(WireModel):
    family: Literal["baseline", "higher_noise", "broader_priors"]
    scale: Annotated[float, Field(ge=1, le=3)]


class HNPE(WireModel):
    width: Literal[64, 128, 256]
    depth: Literal[1, 2, 3]
    learning_rate: Literal[0.0001, 0.0003]
    batch_size: Literal[256, 512, 1024, 2048]
    max_updates: Annotated[int, Field(ge=1, le=1000000)]
    training_budget: Annotated[int, Field(ge=120, le=14400)]


class TMCMC(WireModel):
    population_size: Literal[2048, 4096, 8192]
    target_cov: Literal[0.4, 0.6, 0.8]
    proposal_scale: Literal[0.25, 0.5, 1.0]
    burn_in: Literal[1, 7, 10]
    max_generations: Annotated[int, Field(ge=100, le=10000)]


class Controls(WireModel):
    hnpe: HNPE
    tmcmc: TMCMC


class Evaluation(WireModel):
    case_count: Annotated[int, Field(ge=1, le=256)]
    posterior_draws: Annotated[int, Field(ge=256, le=8192)]
    cohort: Literal["central_90"]


class Budget(WireModel):
    mode: Literal["fixed"]
    method_gpu_seconds: Annotated[int, Field(ge=1, le=2147483647)]


class Reference(WireModel):
    initial_power: Annotated[int, Field(ge=4, le=20)]
    maximum_power: Annotated[int, Field(ge=5, le=22)]
    scrambles: Annotated[int, Field(ge=4, le=16)]
    relative_tolerance: Annotated[float, Field(gt=0, le=0.1)]


class CreateCampaign(WireModel):
    schema_version: Literal[1]
    idempotency_key: Identifier
    scenario: Scenario
    seed: Annotated[int, Field(ge=0, le=2147483647)]
    controls: Controls
    evaluation: Evaluation
    budget: Budget
    reference: Reference


class Empty(WireModel):
    pass


class Claim(WireModel):
    project_id: Identifier
    connector_id: Identifier
    site: Literal["karolina"]
    request_id: Identifier
    signature: Annotated[str, Field(max_length=128)]
    campaign_id: Identifier | None = None
    exclude_stage_ids: list[Identifier] | None = None
    supported_contexts: Annotated[list[str], Field(min_length=1, max_length=16)] | None = None

    @field_validator("supported_contexts")
    @classmethod
    def canonical_contexts(cls, value):
        # The default is omitted for old claims. Explicit null is invalid;
        # normalizing it away would change the signed request body.
        return validate_supported_contexts(value)


class ArchiveEvent(WireModel):
    project_id: Identifier
    campaign_id: Identifier
    connector_id: Identifier
    event_id: Identifier
    type: Literal["archive_verified"]
    receipt: dict
    signature: Annotated[str, Field(max_length=128)]


class StageEvent(WireModel):
    project_id: Identifier
    connector_id: Identifier
    lease_token: Annotated[str, Field(min_length=1, max_length=256)]
    event_id: Identifier
    type: Literal["heartbeat", "submitting", "submitted", "queued", "running", "submission_unknown", "succeeded", "failed", "cancelled", "artifact_upload", "artifact_status"]
    signature: Annotated[str, Field(max_length=128)]
    slurm_job_id: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$")] | None = None
    result: dict | None = None
    failure: dict | None = None
    timing: dict | None = None
    artifacts: list[dict] | None = None
    artifact_ids: list[Identifier] | None = None
    scheduler_accounting: dict | None = None
    not_submitted: bool | None = None


def create_app(application):
    app = FastAPI(title="MesoUQ Benchmark Lab control API", docs_url=None, redoc_url=None, openapi_url=None)
    headers = {"cache-control": "no-store", "x-content-type-options": "nosniff"}

    @app.exception_handler(APIError)
    async def api_error(request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status, headers=headers)

    @app.exception_handler(Exception)
    async def unavailable(request, exc):
        return JSONResponse({"error": {"code": "service_unavailable", "message": "Service temporarily unavailable; retry idempotently"}}, status_code=503, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic errors can echo submitted secrets; only bounded field locations escape.
        fields = [".".join(str(part) for part in error["loc"]) for error in exc.errors()][:20]
        return JSONResponse({"error": {"code": "invalid_benchmark", "message": "Invalid benchmark request", "fields": fields}}, status_code=400, headers=headers)

    def dispatch(request, body=None):
        event = request.scope.get("aws.event", {})
        claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims")
        # No developer/header/body identity fallback. Gateway validation happens in handler.py.
        if not claims:
            raise APIError(401, "unauthenticated", "Verified access token required")
        service = application() if callable(application) else application
        context = request.scope.get("aws.context")
        remaining_ms = getattr(context, "get_remaining_time_in_millis", None)
        if remaining_ms is None:
            status, result = service.handle(request.method, request.url.path, claims, body or {})
        else:
            status, result = service.handle(request.method, request.url.path, claims, body or {}, remaining_ms=remaining_ms)
        return JSONResponse(result, status_code=status, headers=headers)

    @app.get("/v1/benchmark-scenarios")
    async def scenarios(request: Request):
        return dispatch(request)

    @app.get("/v1/projects/{project}/benchmarks/capabilities")
    async def capabilities(project: Identifier, request: Request):
        return dispatch(request)

    @app.get("/v1/projects/{project}/benchmarks")
    async def campaigns(project: Identifier, request: Request):
        return dispatch(request)

    @app.post("/v1/projects/{project}/benchmarks")
    async def create(project: Identifier, payload: CreateCampaign, request: Request):
        return dispatch(request, payload.model_dump(mode="json"))

    @app.get("/v1/projects/{project}/benchmarks/{campaign}")
    async def detail(project: Identifier, campaign: Identifier, request: Request):
        return dispatch(request)

    @app.post("/v1/projects/{project}/benchmarks/{campaign}/cancel")
    async def cancel(project: Identifier, campaign: Identifier, payload: Empty, request: Request):
        return dispatch(request, payload.model_dump())

    @app.get("/v1/projects/{project}/benchmarks/{campaign}/results")
    async def results(project: Identifier, campaign: Identifier, request: Request):
        return dispatch(request)

    @app.get("/v1/projects/{project}/benchmarks/{campaign}/artifacts/{artifact}")
    async def artifact(project: Identifier, campaign: Identifier, artifact: Identifier, request: Request):
        return dispatch(request)

    @app.post("/v1/projects/{project}/benchmarks/{campaign}/archive")
    async def archive(project: Identifier, campaign: Identifier, payload: Empty, request: Request):
        return dispatch(request, payload.model_dump())

    @app.post("/v1/benchmark-connector/archive")
    async def archive_receipt(payload: ArchiveEvent, request: Request):
        return dispatch(request, payload.model_dump(mode="json"))

    @app.post("/v1/benchmark-connector/claim")
    async def claim(payload: Claim, request: Request):
        return dispatch(request, payload.model_dump(mode="json", exclude_none=True))

    @app.post("/v1/benchmark-connector/campaigns/{campaign}/stages/{stage}/{action}")
    async def event(campaign: Identifier, stage: Identifier, action: Literal["events", "artifacts"], payload: StageEvent, request: Request):
        return dispatch(request, payload.model_dump(mode="json", exclude_none=True))

    return app


def lambda_adapter(application):
    return Mangum(create_app(application), lifespan="off")
