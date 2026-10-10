"""Free-tier engine: wires the catalog, quota tracker and selector into LiteLLM."""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from threading import RLock
from typing import Any

import litellm
from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

from zhunt import __version__
from zhunt.adapters.base import (
    AdapterError,
    strip_thinking_blocks,
    system_first,
    text_content,
)
from zhunt.brain import Tier
from zhunt.catalog import Catalog, CatalogModel, load_catalog
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
    _response_mapping,
    _telemetry_wire_dialect,
    _usage_tokens,
)
from zhunt.telemetry import TelemetryLogger

# Original attempt plus up to this many failovers per client request.
MAX_REPLAYS = 3
DECISION_LOG_SIZE = 200
# Output budget reserved against a model's token quota for a compare request.
_COMPARE_OUTPUT_TOKENS = 512
_COMPARE_TIMEOUT_SECONDS = 60
_ERROR_TEXT_LIMIT = 300
_TRUTHY = {"1", "true", "yes", "on"}


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
    session: str


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
        self._decisions: deque[dict[str, Any]] = deque(maxlen=DECISION_LOG_SIZE)
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
                session=_header(headers, "x-session-id") or decision.session_key,
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
        self._record_decision(
            pending, response, failure.value if failure is not None else None
        )
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
        self._record_decision(
            pending, None, kind.value if kind is not None else "client-error"
        )
        if kind is None:
            return
        candidate = pending.candidate
        for slot in self._benched_slots(candidate.model, candidate.key_id, error):
            self.quota.record_failure(slot, kind, retry_after=retry_after)
        self.reliability.record(pending.candidate.model.id, success=False)
        self._queue_retry(pending, pending.decision, escalated=pending.escalated)

    def _benched_slots(
        self, model: CatalogModel, key_id: str, error: BaseException
    ) -> list[Slot]:
        """A rejected key (401) or empty account (402) fails for every model on
        that provider; any other failure is specific to the model that raised it."""

        if getattr(error, "status_code", None) not in (401, 402):
            return [(model.id, key_id)]
        return [
            (other.id, key_id)
            for other in self.catalog.models
            if other.platform == model.platform
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
        input_tokens, output_tokens = _attempt_tokens(pending, response)
        self.telemetry.record_request(
            wire_dialect=pending.wire_dialect,
            decision=pending.decision,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            success=success,
            failure=failure.value if failure is not None else None,
        )


    # -- workspace API ------------------------------------------------------

    def _record_decision(
        self, pending: _Pending, response: Any, failure: str | None
    ) -> None:
        input_tokens, output_tokens = _attempt_tokens(pending, response)
        record = {
            "ts": time.time(),
            "session": pending.session,
            "requested_alias": pending.decision.requested_alias,
            "tier": pending.candidate.tier.value,
            "model": pending.candidate.model.id,
            "degraded": pending.candidate.degraded,
            "attempt": pending.attempts,
            "success": failure is None,
            "failure": failure,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": round((time.perf_counter() - pending.started_at) * 1000),
        }
        with self._lock:
            self._decisions.append(record)

    def decisions(self, *, limit: int = 50, session: str | None = None) -> list[dict[str, Any]]:
        """Newest first, optionally only one session's attempts."""

        with self._lock:
            newest_first = reversed(self._decisions)
            matching = (d for d in newest_first if session is None or d["session"] == session)
            return [dict(d) for _, d in zip(range(limit), matching)]

    def status(self) -> dict[str, Any]:
        """Catalog, policy and live quota state. Reports whether a key is set, never it."""

        policy = self.selector.policy
        providers = []
        for provider in self.catalog.providers.values():
            key_ids = self.selector.key_ids(provider.platform)
            key_id = key_ids[0] if key_ids else "default"
            models = []
            for model in self.catalog.models:
                if model.platform != provider.platform:
                    continue
                slot = (model.id, key_id)
                reason = self.selector.blocked_reason(model)
                models.append(
                    {
                        "id": model.id,
                        "tiers": [t.value for t in Tier if t in model.tiers],
                        "free": model.free,
                        "may_train_on_prompts": model.may_train_on_prompts,
                        "allowed": reason is None,
                        "blocked_reason": reason,
                        "headroom": round(self.quota.headroom(slot, model.limits), 3),
                        "cooldown_remaining": round(self.quota.cooldown_remaining(slot), 1),
                        "reliability": round(self.reliability.score(model.id), 3),
                        "limits": asdict(model.limits),
                    }
                )
            providers.append(
                {
                    "platform": provider.platform,
                    "name": provider.name,
                    "key_set": bool(key_ids),
                    "key_env": provider.key_env,
                    "models": models,
                }
            )
        return {
            "version": __version__,
            "policy": {
                "allow_paid": policy.allow_paid,
                "allow_training": policy.allow_training,
            },
            "providers": providers,
        }

    async def compare(
        self, messages: Sequence[Mapping[str, Any]], model_ids: Sequence[str]
    ) -> list[dict[str, Any]]:
        """One prompt on several chosen models at once, bypassing routing but not
        the key, policy, quota or cooldown checks. Ids must exist in the catalog."""

        by_id = {model.id: model for model in self.catalog.models}
        estimate = (
            sum(len(text_content(m.get("content"))) for m in messages) // 4
            + 1
            + _COMPARE_OUTPUT_TOKENS
        )
        return list(
            await asyncio.gather(
                *(self._compare_one(by_id[mid], messages, estimate) for mid in model_ids)
            )
        )

    async def _compare_one(
        self, model: CatalogModel, messages: Sequence[Mapping[str, Any]], tokens: int
    ) -> dict[str, Any]:
        def blocked(reason: str) -> dict[str, Any]:
            return _compare_result(model.id, error=f"blocked: {reason}")

        reason = self.selector.blocked_reason(model)
        provider = self.catalog.providers[model.platform]
        api_key = os.environ.get(provider.key_env)
        if reason is None and not api_key:
            reason = "no key"
        if reason is not None:
            return blocked(reason)
        key_id = self.selector.key_ids(model.platform)[0]
        slot = (model.id, key_id)
        cooldown = self.quota.cooldown_remaining(slot)
        if cooldown > 0:
            return blocked(f"cooling down for {round(cooldown)}s")
        lease = self.quota.try_acquire(slot, model.limits, tokens=tokens)
        if lease is None:
            return blocked("over quota")

        started = time.perf_counter()
        try:
            response = await litellm.acompletion(
                model=f"{provider.litellm_provider}/{model.model_id}",
                messages=list(messages),
                api_key=api_key,
                timeout=_COMPARE_TIMEOUT_SECONDS,
            )
        except Exception as error:
            kind, retry_after = classify_failure(error)
            if kind is not None:
                for bench in self._benched_slots(model, key_id, error):
                    self.quota.record_failure(bench, kind, retry_after=retry_after)
                self.reliability.record(model.id, success=False)
            return _compare_result(model.id, error=_error_text(error, api_key))
        latency_ms = round((time.perf_counter() - started) * 1000)
        self.quota.record_success(slot)
        self.reliability.record(model.id, success=True)
        input_tokens, output_tokens = _usage_tokens(response)
        if input_tokens is not None and output_tokens is not None:
            self.quota.settle(lease, input_tokens + output_tokens)
        return _compare_result(
            model.id,
            text=_reply_text(response),
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


def _attempt_tokens(pending: _Pending, response: Any) -> tuple[int, int]:
    """Reported usage, else the routing estimate (as telemetry has always done)."""

    input_tokens, output_tokens = _usage_tokens(response)
    if input_tokens is None:
        input_tokens = pending.request.estimated_input_tokens or 1
    if output_tokens is None:
        output_tokens = pending.request.estimated_output_tokens
    return input_tokens, output_tokens


def _compare_result(
    model_id: str,
    *,
    text: str | None = None,
    error: str | None = None,
    latency_ms: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> dict[str, Any]:
    return {
        "model": model_id,
        "ok": error is None,
        "text": text,
        "error": error,
        "latency_ms": latency_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _reply_text(response: Any) -> str:
    choices = _response_mapping(response).get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        message = choices[0].get("message")
        if isinstance(message, Mapping):
            return text_content(message.get("content"))
    return ""


def _error_text(error: BaseException, secret: str | None) -> str:
    """A provider error is shown to the user, so it must not echo our key."""

    text = str(error) or type(error).__name__
    if secret:
        text = text.replace(secret, "***")
    status = getattr(error, "status_code", None)
    if status is not None:
        text = f"{status}: {text}"
    return text[:_ERROR_TEXT_LIMIT]


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
