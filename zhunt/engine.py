"""Free-tier engine: wires the catalog, quota tracker and selector into LiteLLM."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from threading import RLock
from typing import Any

import yaml
from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

from zhunt.adapters.base import AdapterError, strip_thinking_blocks, system_first
from zhunt.brain import Tier
from zhunt.catalog import Catalog
from zhunt.quota import FailureKind as ProviderFailure
from zhunt.quota import Lease, QuotaTracker, Slot
from zhunt.registry import ModelRegistry
from zhunt.router import (
    FailureKind,
    RoutingCoordinator,
    RoutingDecision,
    RoutingRequest,
)
from zhunt.selection import (
    Candidate,
    NoCandidateError,
    Reliability,
    SelectionPolicy,
    Selector,
)
from zhunt.server import (
    _ADAPTERS,
    _ATTEMPT_HEADER,
    _call_id,
    _header,
    _original_request,
    _response_failure,
    _telemetry_wire_dialect,
    _usage_tokens,
)
from zhunt.telemetry import TelemetryLogger

# Original attempt plus up to this many failovers per client request.
MAX_REPLAYS = 3
_TRUTHY = {"1", "true", "yes", "on"}


def load_catalog(path: Path | None = None) -> Catalog:
    if path is not None:
        return Catalog.from_path(path)
    packaged = resources.files("zhunt").joinpath("catalog.yaml")
    with packaged.open(encoding="utf-8") as catalog_file:
        return Catalog.from_data(yaml.safe_load(catalog_file))


def keys_from_env(catalog: Catalog, env: Mapping[str, str]) -> dict[str, list[str]]:
    """Platforms whose API key is set. One key per platform; ids are not secrets."""

    return {
        provider.platform: ["default"]
        for provider in catalog.providers.values()
        if env.get(provider.key_env)
    }


def policy_from_env(env: Mapping[str, str]) -> SelectionPolicy:
    return SelectionPolicy(
        allow_paid=env.get("ZHUNT_ALLOW_PAID", "").lower() in _TRUTHY,
        allow_training=env.get("ZHUNT_ALLOW_TRAINING", "").lower() in _TRUTHY,
    )


def registry_from_catalog(catalog: Catalog) -> ModelRegistry:
    """A registry view of the catalog so v1's classifier and stickiness still work.

    The engine, not this registry, picks the model that is actually called.
    """

    tiers = {
        tier.value: [
            {"model": m.id, "in": str(m.input_cost), "out": str(m.output_cost)}
            for m in catalog.models_for_tier(tier)
        ]
        for tier in Tier
        if catalog.models_for_tier(tier)
    }
    aliases: dict[str, Any] = {"zhunt-auto": {"tier": "auto"}}
    aliases.update({f"zhunt-{name}": {"tier": name} for name in tiers})
    return ModelRegistry.from_data({"aliases": aliases, "tiers": tiers})


def classify_failure(
    error: BaseException,
) -> tuple[ProviderFailure | None, float | None]:
    """Map a provider error to a cooldown kind, or ``None`` for a client error.

    A 4xx other than auth/credit/rate-limit is about this request (too long,
    malformed), not about the model, so it must not bench the model.
    """

    status = getattr(error, "status_code", None)
    retry_after = _retry_after(error)
    if status == 429:
        return ProviderFailure.RATE_LIMIT, retry_after
    if status == 402:
        return ProviderFailure.CREDIT, retry_after
    if status in (401, 403, 404):
        return ProviderFailure.AUTH, retry_after
    if isinstance(status, int) and 400 <= status < 500 and status != 408:
        return None, None
    return ProviderFailure.SERVER, retry_after


def _retry_after(error: BaseException) -> float | None:
    headers = getattr(getattr(error, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        value = float(headers.get("retry-after"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


@dataclass(frozen=True)
class _Pending:
    candidate: Candidate
    lease: Lease
    decision: RoutingDecision
    request: RoutingRequest
    attempt_id: str | None
    attempts: int
    escalated: bool
    started_at: float
    wire_dialect: str


@dataclass(frozen=True)
class _Retry:
    decision: RoutingDecision
    request: RoutingRequest
    attempts: int
    escalated: bool


class EngineProxyHook(CustomLogger):
    """Route supported LiteLLM proxy calls through the free-tier engine."""

    max_replays = MAX_REPLAYS

    def __init__(
        self,
        coordinator: RoutingCoordinator,
        selector: Selector,
        quota: QuotaTracker,
        reliability: Reliability,
        catalog: Catalog,
        *,
        telemetry: TelemetryLogger | None = None,
        summary: tuple[str, ...] = (),
    ) -> None:
        self.summary = summary
        self.coordinator = coordinator
        self.selector = selector
        self.quota = quota
        self.reliability = reliability
        self.catalog = catalog
        self.telemetry = telemetry
        self._pending: dict[str, _Pending] = {}
        self._retries: dict[str, _Retry] = {}
        self._lock = RLock()

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict[str, Any],
        call_type: str,
    ) -> dict[str, Any]:
        adapter = _ADAPTERS.get(call_type)
        if adapter is None:
            return data

        payload, headers = _original_request(data)
        if isinstance(payload.get("input"), list):
            data["input"] = strip_thinking_blocks(system_first(payload["input"]))
        if isinstance(payload.get("messages"), list):
            data["messages"] = strip_thinking_blocks(system_first(payload["messages"]))
        attempt_id = _header(headers, _ATTEMPT_HEADER.decode())
        retry = self._take_retry(attempt_id)
        if retry is None:
            try:
                routed = adapter.route(payload, self.coordinator, headers=headers)
            except AdapterError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
            decision, request = routed.decision, routed.request
            attempts, escalated = 1, False
        else:
            decision, request = retry.decision, retry.request
            attempts, escalated = retry.attempts + 1, retry.escalated

        tokens = (request.estimated_input_tokens or 1) + request.estimated_output_tokens
        try:
            candidate, lease = self.selector.acquire(
                decision.tier,
                tokens=tokens,
                requires_tools=bool(payload.get("tools")),
            )
        except NoCandidateError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        provider = self.catalog.providers[candidate.model.platform]
        api_key = os.environ.get(provider.key_env)
        if not api_key:
            raise HTTPException(
                status_code=503, detail=f"{provider.key_env} is not configured"
            )
        data["model"] = f"{provider.litellm_provider}/{candidate.model.model_id}"
        data["api_key"] = api_key
        decision = replace(decision, model=candidate.model.id)

        call_id = _call_id(data)
        with self._lock:
            self._pending[call_id] = _Pending(
                candidate=candidate,
                lease=lease,
                decision=decision,
                request=request,
                attempt_id=attempt_id,
                attempts=attempts,
                escalated=escalated,
                started_at=time.perf_counter(),
                wire_dialect=_telemetry_wire_dialect(call_type),
            )
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            data["metadata"] = metadata
        metadata["zhunt"] = {
            "requested_alias": decision.requested_alias,
            "session_key": decision.session_key,
            "tier": candidate.tier.value,
            "model": decision.model,
            "upstream_model": data["model"],
            "degraded": candidate.degraded,
            "attempt": attempts,
            "reused_session_route": decision.reused_session_route,
            "escalation_count": decision.escalation_count,
        }
        return data

    async def async_post_call_success_hook(
        self,
        data: dict[str, Any],
        user_api_key_dict: Any,
        response: Any,
    ) -> None:
        pending = self._pop_pending(data)
        if pending is not None:
            self._finish(pending, response, _response_failure(response, request_data=data))

    async def async_post_call_failure_hook(
        self,
        request_data: dict[str, Any],
        original_exception: Exception,
        user_api_key_dict: Any,
        traceback_str: str | None = None,
    ) -> None:
        pending = self._pop_pending(request_data)
        if pending is not None:
            self._handle_error(pending, original_exception)

    async def async_post_call_streaming_iterator_hook(
        self,
        user_api_key_dict: Any,
        response: Any,
        request_data: dict[str, Any],
    ) -> Any:
        failure: FailureKind | None = None
        last_chunk: Any = None
        try:
            async for chunk in response:
                last_chunk = chunk
                failure = failure or _response_failure(chunk, request_data=request_data)
                yield chunk
        except (asyncio.CancelledError, GeneratorExit):
            self._pop_pending(request_data)
            raise
        except Exception as error:
            pending = self._pop_pending(request_data)
            if pending is not None:
                self._handle_error(pending, error)
            raise
        else:
            pending = self._pop_pending(request_data)
            if pending is not None:
                self._finish(pending, last_chunk, failure)

    def has_retry(self, attempt_id: str) -> bool:
        with self._lock:
            return attempt_id in self._retries

    def discard_retry(self, attempt_id: str) -> None:
        with self._lock:
            self._retries.pop(attempt_id, None)

    def _finish(
        self,
        pending: _Pending,
        response: Any,
        failure: FailureKind | None,
    ) -> None:
        """The provider answered; truncation or refusal may still need a better tier."""

        self._record_telemetry(pending, response, failure is None, failure)
        slot = pending.candidate.slot
        self.quota.record_success(slot)
        self.reliability.record(pending.candidate.model.id, success=True)
        input_tokens, output_tokens = _usage_tokens(response)
        if input_tokens is not None and output_tokens is not None:
            self.quota.settle(pending.lease, input_tokens + output_tokens)
        if failure is None:
            self.coordinator.record_success(pending.decision)
            return
        if pending.escalated or pending.attempt_id is None:
            return  # one promotion per client request
        promoted = self.coordinator.escalate(pending.decision, failure)
        self._queue_retry(pending, promoted, escalated=True)

    def _handle_error(self, pending: _Pending, error: BaseException) -> None:
        self._record_telemetry(pending, None, False, FailureKind.PROVIDER_ERROR)
        kind, retry_after = classify_failure(error)
        if kind is None:
            return
        for slot in self._benched_slots(pending, error):
            self.quota.record_failure(slot, kind, retry_after=retry_after)
        self.reliability.record(pending.candidate.model.id, success=False)
        self._queue_retry(pending, pending.decision, escalated=pending.escalated)

    def _benched_slots(self, pending: _Pending, error: BaseException) -> list[Slot]:
        """A rejected key (401) or empty account (402) fails for every model on
        that provider; any other failure is specific to the model that raised it."""

        if getattr(error, "status_code", None) not in (401, 402):
            return [pending.candidate.slot]
        candidate = pending.candidate
        return [
            (model.id, candidate.key_id)
            for model in self.catalog.models
            if model.platform == candidate.model.platform
        ]

    def _queue_retry(
        self,
        pending: _Pending,
        decision: RoutingDecision,
        *,
        escalated: bool,
    ) -> None:
        if pending.attempt_id is None or pending.attempts > self.max_replays:
            return
        with self._lock:
            self._retries[pending.attempt_id] = _Retry(
                decision=decision,
                request=pending.request,
                attempts=pending.attempts,
                escalated=escalated,
            )

    def _take_retry(self, attempt_id: str | None) -> _Retry | None:
        if attempt_id is None:
            return None
        with self._lock:
            return self._retries.pop(attempt_id, None)

    def _pop_pending(self, data: Mapping[str, Any]) -> _Pending | None:
        call_id = data.get("litellm_call_id")
        if not isinstance(call_id, str):
            return None
        with self._lock:
            return self._pending.pop(call_id, None)

    def _record_telemetry(
        self,
        pending: _Pending,
        response: Any,
        success: bool,
        failure: FailureKind | None,
    ) -> None:
        if self.telemetry is None:
            return
        input_tokens, output_tokens = _usage_tokens(response)
        if input_tokens is None:
            input_tokens = pending.request.estimated_input_tokens or 1
        if output_tokens is None:
            output_tokens = pending.request.estimated_output_tokens
        self.telemetry.record_request(
            wire_dialect=pending.wire_dialect,
            decision=pending.decision,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            success=success,
            failure=failure.value if failure is not None else None,
        )


def build_engine_hook(
    *,
    catalog_path: Path | None = None,
    telemetry_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> EngineProxyHook:
    env = os.environ if env is None else env
    catalog = load_catalog(catalog_path)
    quota = QuotaTracker()
    reliability = Reliability()
    keys = keys_from_env(catalog, env)
    policy = policy_from_env(env)
    selector = Selector(catalog, quota, reliability, keys, policy=policy)
    registry = registry_from_catalog(catalog)
    return EngineProxyHook(
        RoutingCoordinator(registry=registry),
        selector,
        quota,
        reliability,
        catalog,
        telemetry=(
            TelemetryLogger(telemetry_path, registry) if telemetry_path else None
        ),
        summary=describe(catalog, keys, policy),
    )


def describe(
    catalog: Catalog,
    keys: Mapping[str, list[str]],
    policy: SelectionPolicy,
) -> tuple[str, ...]:
    ready = [p.name for p in catalog.providers.values() if p.platform in keys]
    missing = [
        f"{p.name} ({p.key_env})"
        for p in catalog.providers.values()
        if p.platform not in keys
    ]
    lines = [f"Free-tier engine: ready providers: {', '.join(ready) or 'none'}"]
    if missing:
        lines.append(f"Free-tier engine: no key set for: {', '.join(missing)}")
    if not policy.allow_training:
        lines.append(
            "Free-tier engine: providers that may train on prompts are off "
            "(set ZHUNT_ALLOW_TRAINING=1 to allow them)"
        )
    if not policy.allow_paid:
        lines.append("Free-tier engine: paid models are off (ZHUNT_ALLOW_PAID=1 to allow)")
    return tuple(lines)
