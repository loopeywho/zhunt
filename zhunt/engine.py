# Parts adapted from FreeLLMAPI (MIT, (c) 2026 Tashfeen Ahmed); see THIRD_PARTY_NOTICES.md
"""Free-tier engine: wires the catalog, quota tracker and selector into the daemon."""

from __future__ import annotations

import asyncio
import os
import random
import re
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from fastapi import HTTPException

from zhunt import __version__, licence, llm
from zhunt.adapters.base import (
    AdapterError,
    strip_thinking_blocks,
    system_first,
    text_content,
)
from zhunt.brain import Tier
from zhunt.catalog import Catalog, CatalogModel, load_catalog
from zhunt.catalog_feed import ActiveCatalog, CatalogFeed, load_active
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
from zhunt.spend import SpendLedger, cap_from_env, cost_of, month_of
from zhunt.state import StateStore
from zhunt.telemetry import TelemetryLogger, recent_requests

# Original attempt plus up to this many failovers per client request.
MAX_REPLAYS = 3
DECISION_LOG_SIZE = 200
# Output budget reserved against a model's token quota for a compare request.
_COMPARE_OUTPUT_TOKENS = 512
_COMPARE_TIMEOUT_SECONDS = 60
_ERROR_TEXT_LIMIT = 300
_TRUTHY = {"1", "true", "yes", "on"}

# Client-supplied fields that could redirect a call, pick another route or send
# the provider key somewhere else. The engine alone decides these.
_CLIENT_ROUTING_FIELDS = (
    "api_base",
    "base_url",
    "api_version",
    "custom_llm_provider",
    "fallbacks",
    "context_window_fallbacks",
    "content_policy_fallbacks",
    "models",
    "deployment_id",
    "user_config",
    "extra_headers",
    "num_retries",
    "max_retries",
)


# The endpoint, credential and observability names from LiteLLM 1.94's own
# request blocklist (``_BANNED_REQUEST_BODY_PARAMS``), kept now that zhunt no
# longer uses LiteLLM: a client must never set where a call goes or what it carries.
_LITELLM_BANNED_FIELDS = (
    "arize_api_key", "arize_space_id", "arize_space_key", "aws_bedrock_project_id",
    "aws_bedrock_runtime_endpoint", "aws_role_name", "aws_sts_endpoint",
    "aws_web_identity_token", "azure_ad_token", "bedrock_tags", "braintrust_api_key",
    "braintrust_host", "braintrust_project", "dd_agent_host", "dd_agent_port", "dd_api_key",
    "dd_site", "deployment_url", "gcs_bucket_name", "gcs_path_service_account",
    "humanloop_api_key", "langfuse_host", "langfuse_public_key", "langfuse_secret",
    "langfuse_secret_key", "langsmith_api_key", "langsmith_base_url", "langsmith_project",
    "langsmith_tenant_id", "lunary_public_key", "model_list", "nvcf_function_id",
    "phoenix_project_name", "phoenix_project_name_override", "posthog_api_key",
    "posthog_api_url", "posthog_host", "s3_endpoint_url", "sagemaker_base_url",
    "slack_webhook_url", "turn_off_message_logging", "use_ssl",
    "user_api_key_auth_metadata", "vertex_credentials", "wandb_api_key",
    "weave_project_id",
)


def _banned_request_params() -> frozenset[str]:
    """Every field a client must not control: ours plus LiteLLM's former blocklist
    (endpoints, credentials, observability hosts)."""

    return frozenset(_CLIENT_ROUTING_FIELDS) | frozenset(_LITELLM_BANNED_FIELDS)


def _enforce_engine_routing(data: dict[str, Any], call_type: str) -> None:
    """Scrub every client-controlled routing field before the engine sets its own.

    Without this a caller holding the master key could send ``api_base`` (or a
    ``fallbacks`` entry) and receive the provider key at a host of its choosing;
    ``zhunt.llm`` forwards only standard fields, so this is the first of two
    fences. ``data`` also carries the daemon's own metadata at this point, so
    offending names are dropped rather than rejected.
    """

    banned = _banned_request_params()
    for field in banned:
        data.pop(field, None)
    for field in ("extra_body", "litellm_embedding_config"):
        data.pop(field, None)
    for key in ("metadata", "litellm_metadata"):
        nested = data.get(key)
        if isinstance(nested, dict):
            for field in banned:
                nested.pop(field, None)
    if call_type in ("completion", "acompletion"):
        # One engine lease must be one upstream request; failover is the engine's job.
        data["max_retries"] = 0
        data["num_retries"] = 0


def keys_from_env(catalog: Catalog, env: Mapping[str, str]) -> dict[str, list[str]]:
    """Platforms whose API key is set. One key per platform; ids are not secrets."""

    return {
        provider.platform: ["default"]
        for provider in catalog.providers.values()
        if provider.keyless or env.get(provider.key_env)
    }


# Stands in for the key of providers that need none, so they count as usable; it is
# never sent (see async_pre_call_hook).
_ANONYMOUS_KEY = "anonymous"


def provider_key(provider: Any) -> str | None:
    """The user's key for ``provider``, or the anonymous placeholder when none is needed."""

    return os.environ.get(provider.key_env) or (_ANONYMOUS_KEY if provider.keyless else None)


def policy_from_env(env: Mapping[str, str]) -> SelectionPolicy:
    return SelectionPolicy(
        allow_paid=env.get("ZHUNT_ALLOW_PAID", "").lower() in _TRUTHY,
        allow_training=env.get("ZHUNT_ALLOW_TRAINING", "").lower() in _TRUTHY,
    )


def registry_from_catalog(catalog: Catalog) -> ModelRegistry:
    """A registry view of the catalog so v1's classifier and stickiness still work.

    The engine, not this registry, picks the model that is actually called.
    """

    # Every tier must exist for the classifier, even in a catalog that lacks models
    # for it; the names in this view are never called.
    tiers = {
        tier.value: [
            {"model": m.id, "in": str(m.input_cost), "out": str(m.output_cost)}
            for m in (catalog.models_for_tier(tier) or catalog.models)
        ]
        for tier in Tier
    }
    aliases: dict[str, Any] = {"zhunt-auto": {"tier": "auto"}}
    aliases.update({f"zhunt-{name}": {"tier": name} for name in tiers})
    return ModelRegistry.from_data({"aliases": aliases, "tiers": tiers})


def classify_failure(
    error: BaseException,
) -> tuple[ProviderFailure | None, float | None]:
    """Map a provider error to a cooldown kind, or ``None`` for a client error.

    A 4xx other than those mapped here is about this request (too long,
    malformed), not about the model, so it must not bench the model.
    """

    status = getattr(error, "status_code", None)
    retry_after = _retry_after(error)
    if status == 429:
        if _is_daily_quota_exhausted(str(error)):
            return ProviderFailure.DAILY_QUOTA, retry_after
        return ProviderFailure.RATE_LIMIT, retry_after
    if status in _STATUS_FAILURES:
        return _STATUS_FAILURES[status], retry_after
    if isinstance(status, int) and 400 <= status < 500 and status != 408:
        return None, None
    return ProviderFailure.SERVER, retry_after


_STATUS_FAILURES = {
    401: ProviderFailure.AUTH,
    402: ProviderFailure.CREDIT,
    403: ProviderFailure.FORBIDDEN,
    404: ProviderFailure.NOT_FOUND,
    410: ProviderFailure.NOT_FOUND,
}
# Providers whose daily free allowance resets at a fixed local midnight rather
# than UTC (https://ai.google.dev/gemini-api/docs/rate-limits).
_DAILY_RESET_ZONES = {"google": "America/Los_Angeles"}
# Groq ends every limit error with "Upgrade to Dev Tier today": that "today" is
# sales copy, not a window, so it is cut before looking for a daily marker.
_UPGRADE_UPSELL = re.compile(r"\bupgrade to [^.?!]*?\btoday\b")
_DAILY_MARKER = re.compile(r"daily|per[ _-]?day|\btoday\b")
_QUOTA_MARKER = re.compile(r"allocation|quota|limit|exhaust|used up")


# A provider-stated ceiling in a 429/413 body, e.g. Groq's "on tokens per minute
# (TPM): Limit 6000, Used 5000, Requested 2000". Per-day axes are checked before
# per-minute and tokens before requests so the more specific one wins.
_LIMIT_VALUE = re.compile(r"\blimit[:\s]+([\d,]+)", re.IGNORECASE)
_LIMIT_AXIS_PATTERNS = (
    ("tpd", re.compile(r"tokens?\s*per\s*day|\btpd\b", re.IGNORECASE)),
    ("tpm", re.compile(r"tokens?\s*per\s*min(?:ute)?|\btpm\b", re.IGNORECASE)),
    ("rpd", re.compile(r"requests?\s*per\s*day|\brpd\b", re.IGNORECASE)),
    ("rpm", re.compile(r"requests?\s*per\s*min(?:ute)?|\brpm\b", re.IGNORECASE)),
)
# Rate-limit headers by provider, as (header, axis). Groq's request header is a
# daily count and Cerebras names its windows; anything else follows OpenAI's
# per-minute convention, which errs high (harmless) if a provider means per day.
_HEADER_LIMITS = {
    "groq": (("x-ratelimit-limit-requests", "rpd"), ("x-ratelimit-limit-tokens", "tpm")),
    "cerebras": (
        ("x-ratelimit-limit-requests-day", "rpd"),
        ("x-ratelimit-limit-tokens-minute", "tpm"),
    ),
}
_DEFAULT_HEADER_LIMITS = (
    ("x-ratelimit-limit-requests", "rpm"),
    ("x-ratelimit-limit-tokens", "tpm"),
)


def parse_limit_from_error(message: str) -> tuple[str, int] | None:
    """``(axis, cap)`` when a body states both a "Limit N" and a clear axis.
    Guessing the axis would cap the wrong window, so anything vaguer is None."""

    match = _LIMIT_VALUE.search(message)
    if match is None:
        return None
    value = int(match.group(1).replace(",", "") or 0)
    if value <= 0:
        return None
    for axis, pattern in _LIMIT_AXIS_PATTERNS:
        if pattern.search(message):
            return axis, value
    return None


def limits_from_headers(platform: str, headers: Any) -> list[tuple[str, int]]:
    """``(axis, cap)`` pairs from a provider's rate-limit response headers."""

    if headers is None:
        return []
    found = []
    for name, axis in _HEADER_LIMITS.get(platform, _DEFAULT_HEADER_LIMITS):
        try:
            value = int(float(headers.get(name)))
        except (AttributeError, TypeError, ValueError):
            continue
        if value > 0:
            found.append((axis, value))
    return found


def _error_headers(error: BaseException) -> Any:
    headers = getattr(error, "response_headers", None)
    if headers is None:
        headers = getattr(getattr(error, "response", None), "headers", None)
    return headers


def _response_headers(response: Any) -> Any:
    """The raw headers ``zhunt.llm`` keeps on every response and stream chunk."""

    return getattr(response, "headers", None)


def _is_daily_quota_exhausted(message: str) -> bool:
    """A 429 body that says the day's allowance is spent. Needs both a daily and
    a quota marker so an ordinary per-minute 429 never matches."""

    text = _UPGRADE_UPSELL.sub("", message.lower())
    return bool(_DAILY_MARKER.search(text) and _QUOTA_MARKER.search(text))


_MAX_RETRY_AFTER_SECONDS = 86_400.0


def _retry_after(error: BaseException) -> float | None:
    """Provider ``retry-after`` in seconds, clamped to a day.

    ``zhunt.llm`` errors carry the provider's headers on ``response_headers``;
    an httpx error carries them on ``.response``.
    """

    for headers in (
        getattr(error, "response_headers", None),
        getattr(getattr(error, "response", None), "headers", None),
    ):
        if headers is None:
            continue
        try:
            value = float(headers.get("retry-after"))
        except (TypeError, ValueError):
            continue
        if value == value and value > 0:  # not NaN, positive
            return min(value, _MAX_RETRY_AFTER_SECONDS)
    return None


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
    exclude: frozenset[str] = frozenset()


class EngineProxyHook:
    """Route each inference call through the free-tier engine.

    ``zhunt.server`` calls the hooks in the order the LiteLLM proxy did (pre-call,
    then success, failure or the streaming iterator), so they keep its names.
    """

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
        spend: SpendLedger | None = None,
        plan: Callable[[], licence.Plan] | None = None,
        monthly_cap: float | None = None,
    ) -> None:
        self.summary = summary
        # Premium (paid) models need Pro, the user's opt-in (ZHUNT_ALLOW_PAID) and
        # headroom under the monthly cap; refresh_policy() combines them per request.
        self._base_policy = selector.policy
        self.spend = spend or SpendLedger()
        self._plan = plan or (lambda: licence.current_plan())
        self.monthly_cap = cap_from_env() if monthly_cap is None else monthly_cap
        self._plan_cache: tuple[float, licence.Plan] | None = None
        self.premium_reason: str | None = None
        self.coordinator = coordinator
        self.selector = selector
        self.quota = quota
        self.reliability = reliability
        self.catalog = catalog
        self.telemetry = telemetry
        self._pending: dict[str, _Pending] = {}
        self._retries: dict[str, _Retry] = {}
        self._decisions: deque[dict[str, Any]] = deque(maxlen=DECISION_LOG_SIZE)
        if telemetry is not None:  # so the day's log outlives a restart
            self._decisions.extend(_decisions_from_telemetry(telemetry))
        self._lock = RLock()
        self.catalog_feed: CatalogFeed | None = None

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
        _enforce_engine_routing(data, call_type)
        self.refresh_policy()
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
            attempts, escalated, exclude = 1, False, frozenset()
        else:
            decision, request = retry.decision, retry.request
            attempts, escalated = retry.attempts + 1, retry.escalated
            exclude = retry.exclude

        tokens = (request.estimated_input_tokens or 1) + request.estimated_output_tokens
        try:
            candidate, lease = self.selector.acquire(
                decision.tier,
                tokens=tokens,
                requires_tools=bool(payload.get("tools")),
                exclude=exclude,
            )
        except NoCandidateError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        provider = self.catalog.providers.get(candidate.model.platform)
        if provider is None:  # the catalog was replaced between ranking and here
            raise HTTPException(status_code=503, detail="the model catalog just changed; retry")
        api_key = provider_key(provider)
        if not api_key:
            raise HTTPException(
                status_code=503, detail=f"{provider.key_env} is not configured"
            )
        data["model"] = f"{provider.litellm_provider}/{candidate.model.model_id}"
        # no-key providers get no Authorization header: OVHcloud rejects a made-up key
        data["api_key"] = None if api_key == _ANONYMOUS_KEY else api_key
        if provider.api_base:
            data["api_base"] = provider.api_base  # set after the client copy was scrubbed
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
            cancelled = self._pop_pending(request_data)
            if cancelled is not None:
                self.quota.settle(cancelled.lease, 0)
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
        model = pending.candidate.model
        self._learn_limits(
            model, limits_from_headers(model.platform, _response_headers(response))
        )
        self.quota.record_success(slot)
        self.reliability.record(pending.candidate.model.id, success=True)
        self.selector.penalties.record_success(pending.candidate.model.id)
        input_tokens, output_tokens = _usage_tokens(response)
        if input_tokens is not None and output_tokens is not None:
            self.quota.settle(pending.lease, input_tokens + output_tokens)
            self._record_spend(model, input_tokens, output_tokens)
        if failure is None:
            self.coordinator.record_success(pending.decision)
            return
        if pending.escalated or pending.attempt_id is None:
            return  # one promotion per client request
        promoted = self.coordinator.escalate(pending.decision, failure)
        if promoted.tier == pending.decision.tier:
            return  # already at the top tier: a replay would only repeat the call
        self._queue_retry(
            pending,
            promoted,
            escalated=True,
            exclude=frozenset({pending.candidate.model.id}),
        )

    def _handle_error(self, pending: _Pending, error: BaseException) -> None:
        # Nothing was generated, so the reserved tokens must not count against the
        # per-minute/day budget (otherwise a few failures lock a model out locally).
        self.quota.settle(pending.lease, 0)
        self._record_telemetry(pending, None, False, FailureKind.PROVIDER_ERROR)
        kind, retry_after = classify_failure(error)
        self._record_decision(
            pending, None, kind.value if kind is not None else "client-error"
        )
        if kind is None:
            return
        candidate = pending.candidate
        self._learn_from_error(candidate.model, error)
        self._bench(candidate.model, candidate.key_id, kind, retry_after)
        self._queue_retry(pending, pending.decision, escalated=pending.escalated)

    def _bench(
        self,
        model: CatalogModel,
        key_id: str,
        kind: ProviderFailure,
        retry_after: float | None,
    ) -> None:
        """Cool down whatever the failure condemns and count it against the model."""

        for slot in self._benched_slots(model, key_id, kind):
            # Provider-wide kinds have fixed durations, so the failing model's
            # limits are only consulted for its own slot.
            self.quota.record_failure(
                slot,
                kind,
                limits=model.limits,
                retry_after=retry_after,
                daily_reset_zone=_DAILY_RESET_ZONES.get(model.platform),
            )
        every_key = [(model.id, key) for key in self.selector.key_ids(model.platform)]
        self.quota.record_model_failure(model.id, every_key or [(model.id, key_id)])
        self.reliability.record(model.id, success=False)
        self.selector.penalties.record_failure(
            model.id,
            rate_limited=kind in (ProviderFailure.RATE_LIMIT, ProviderFailure.DAILY_QUOTA),
        )

    def _learn_from_error(self, model: CatalogModel, error: BaseException) -> None:
        learned = limits_from_headers(model.platform, _error_headers(error))
        if getattr(error, "status_code", None) in (413, 429):
            stated = parse_limit_from_error(str(error))
            if stated is not None:
                learned.append(stated)
        self._learn_limits(model, learned)

    def _learn_limits(
        self, model: CatalogModel, learned: Sequence[tuple[str, int]]
    ) -> None:
        for axis, value in learned:
            self.quota.learn_limit(model.id, model.limits, axis, value)

    def _benched_slots(
        self, model: CatalogModel, key_id: str, kind: ProviderFailure
    ) -> list[Slot]:
        """A rejected key (401) or empty account (402) fails for every model on
        that provider; a model the provider no longer serves (404) fails on every
        key; anything else (a 403 tier gate included) is this model and key only."""

        if kind is ProviderFailure.NOT_FOUND:
            keys = self.selector.key_ids(model.platform) or (key_id,)
            return [(model.id, key) for key in keys]
        if kind not in (ProviderFailure.AUTH, ProviderFailure.CREDIT):
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
        exclude: frozenset[str] = frozenset(),
    ) -> None:
        if pending.attempt_id is None or pending.attempts > self.max_replays:
            return
        with self._lock:
            self._retries[pending.attempt_id] = _Retry(
                decision=decision,
                request=pending.request,
                attempts=pending.attempts,
                escalated=escalated,
                exclude=exclude,
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
            attempt=pending.attempts,
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

    def _free_today(self) -> dict[str, Any]:
        """What the free models can still take today, over every key: requests and tokens left
        under the daily caps, and whether some ready model has no daily cap at all."""

        out: dict[str, Any] = {"models_ready": 0, "requests_left": 0, "tokens_left": 0,
                               "requests_uncapped": False, "tokens_uncapped": False}
        for model in self.catalog.models:
            if not model.free or self.selector.blocked_reason(model) is not None:
                continue
            ready = False
            for key_id in self.selector.key_ids(model.platform):
                slot = (model.id, key_id)
                if self.quota.cooldown_remaining(slot) > 0:
                    continue
                usage = self.quota.usage(slot, model.limits)
                ready = True
                for axis, total in (("rpd", "requests"), ("tpd", "tokens")):
                    cap = usage[axis]["cap"]
                    if cap is None:
                        out[f"{total}_uncapped"] = True
                    else:
                        out[f"{total}_left"] += max(cap - int(usage[axis]["used"] or 0), 0)
            out["models_ready"] += ready
        return out

    def decisions(self, *, limit: int = 50, session: str | None = None) -> list[dict[str, Any]]:
        """Newest first, optionally only one session's attempts."""

        with self._lock:
            newest_first = reversed(self._decisions)
            matching = (d for d in newest_first if session is None or d["session"] == session)
            return [dict(d) for _, d in zip(range(limit), matching)]

    def current_plan(self) -> licence.Plan:
        """The licence plan, re-read at most every 15 s (activation needs no restart)."""

        now = time.monotonic()
        if self._plan_cache is None or now - self._plan_cache[0] > 15:
            self._plan_cache = (now, self._plan())
        return self._plan_cache[1]

    def set_allow_training(self, allow: bool) -> None:
        """Turn the privacy gate on or off (the Connect window's switch)."""

        self._base_policy = replace(self._base_policy, allow_training=allow)
        self.refresh_policy()

    def apply_catalog(self, active: ActiveCatalog) -> None:
        """Route with a new catalog from now on, without a restart. Quota, cooldown,
        reliability and learned limits are keyed by model id, so they carry over."""

        catalog = active.catalog
        registry = registry_from_catalog(catalog)
        with self._lock:
            self.selector.set_catalog(catalog)
            self.catalog = catalog
            self.selector.set_keys(keys_from_env(catalog, os.environ))
            self.coordinator.registry = registry
            if self.telemetry is not None:
                self.telemetry.registry = registry

    def reload_keys(self) -> None:
        """Pick up provider keys added or removed since start-up (the Connect window)."""

        self.selector.set_keys(keys_from_env(self.catalog, os.environ))

    def refresh_policy(self) -> None:
        """Recompute whether premium models may be used for the next request."""

        base = self._base_policy
        if not self.current_plan().has("premium_fallback"):
            reason: str | None = "pro only"
        elif not base.allow_paid:
            reason = "premium off"
        elif self.spend.spent() >= self.monthly_cap:
            reason = "monthly cap reached"
        else:
            reason = None
        self.premium_reason = reason
        self.selector.set_policy(
            replace(base, allow_paid=reason is None, paid_block_reason=reason or "paid")
        )

    def _record_spend(self, model: CatalogModel, input_tokens: int, output_tokens: int) -> None:
        if not model.free:
            self.spend.add(model.id, cost_of(input_tokens, output_tokens, model.input_cost, model.output_cost))

    def status(self) -> dict[str, Any]:
        """Catalog, policy and live quota state. Reports whether a key is set, never it."""

        self.refresh_policy()
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
                        "usage": self.quota.usage(slot, model.limits),
                        "cooldown_remaining": round(self.quota.cooldown_remaining(slot), 1),
                        "reliability": round(self.reliability.mean(model.id), 3),
                        "limits": asdict(
                            self.quota.effective_limits(model.id, model.limits)
                        ),
                    }
                )
            providers.append(
                {
                    "platform": provider.platform,
                    "name": provider.name,
                    "key_set": bool(key_ids),
                    "key_env": provider.key_env,
                    "key_url": provider.key_url,
                    "free_note": provider.free_note,
                    "keyless": provider.keyless,
                    "models": models,
                }
            )
        return {
            "version": __version__,
            "free_today": self._free_today(),
            "policy": {
                "allow_paid": policy.allow_paid,
                "allow_training": policy.allow_training,
                "premium_reason": self.premium_reason,
            },
            "spend": {
                "month": month_of(time.time()),
                "spent": round(self.spend.spent(), 4),
                "cap": self.monthly_cap,
            },
            "providers": providers,
            "catalog": (
                self.catalog_feed.active.to_api()
                if self.catalog_feed is not None
                else {"source": "file", "version": 0}
            ),
        }

    async def compare(
        self, messages: Sequence[Mapping[str, Any]], model_ids: Sequence[str]
    ) -> list[dict[str, Any]]:
        """One prompt on several chosen models at once, bypassing routing but not
        the key, policy, quota or cooldown checks. Ids must exist in the catalog."""

        self.refresh_policy()
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
        api_key = provider_key(provider)
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
            response = await llm.acompletion(
                model=f"{provider.litellm_provider}/{model.model_id}",
                messages=list(messages),
                api_key=api_key,
                api_base=provider.api_base,
                timeout=_COMPARE_TIMEOUT_SECONDS,
                max_retries=0,
            )
        except Exception as error:
            self.quota.settle(lease, 0)
            kind, retry_after = classify_failure(error)
            if kind is not None:
                self._learn_from_error(model, error)
                self._bench(model, key_id, kind, retry_after)
            return _compare_result(model.id, error=_error_text(error, api_key))
        latency_ms = round((time.perf_counter() - started) * 1000)
        self.quota.record_success(slot)
        self.reliability.record(model.id, success=True)
        self.selector.penalties.record_success(model.id)
        input_tokens, output_tokens = _usage_tokens(response)
        if input_tokens is not None and output_tokens is not None:
            self.quota.settle(lease, input_tokens + output_tokens)
            self._record_spend(model, input_tokens, output_tokens)
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


def selection_rng() -> random.Random | None:
    """The sampler for reliability scores; ``None`` means the posterior mean.
    The test suite patches this to keep rankings deterministic."""

    return random.Random()


def build_engine_hook(
    *,
    catalog_path: Path | None = None,
    telemetry_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    state_path: Path | None = None,
    rng: random.Random | None = None,
) -> EngineProxyHook:
    env = os.environ if env is None else env
    if catalog_path is None:
        # The newest verified feed in ~/.zhunt/catalog.json, else the bundled catalog.
        active = load_active()
    else:
        active = ActiveCatalog(load_catalog(catalog_path), 0, "file")
    catalog = active.catalog
    # Defaults to ~/.zhunt/state.db (or $ZHUNT_STATE_DB); memory-only if unusable.
    store = StateStore.open(state_path)
    quota = QuotaTracker(store=store)
    # Thompson sampling: each pick draws from the posterior so a model with few
    # samples still gets tried. Pass a seeded ``rng`` for reproducible tests.
    reliability = Reliability(rng=rng or selection_rng())
    keys = keys_from_env(catalog, env)
    policy = policy_from_env(env)
    selector = Selector(catalog, quota, reliability, keys, policy=policy)
    registry = registry_from_catalog(catalog)
    hook = EngineProxyHook(
        RoutingCoordinator(registry=registry),
        selector,
        quota,
        reliability,
        catalog,
        telemetry=(
            TelemetryLogger(telemetry_path, registry) if telemetry_path else None
        ),
        summary=describe(catalog, keys, policy),
        spend=SpendLedger(store),
        monthly_cap=cap_from_env(dict(env)),
    )
    # A --catalog file is used as given; otherwise the feed can replace the catalog live.
    if active.source != "file":
        hook.catalog_feed = CatalogFeed(active, hook.apply_catalog, plan=hook.current_plan)
        if active.note:
            hook.summary += (f"Catalog: {active.note}",)
    return hook


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
        lines.append(
            "Free-tier engine: premium models are off (zhunt Pro, then `zhunt premium on`)"
        )
    return tuple(lines)


def _decisions_from_telemetry(telemetry: TelemetryLogger) -> list[dict[str, Any]]:
    """The last day's decision records, rebuilt from the telemetry file."""

    events = recent_requests(telemetry.path, since=time.time() - 86_400, limit=DECISION_LOG_SIZE)
    return [
        {
            "ts": event["ts"],
            "session": event.get("session"),
            "requested_alias": event.get("requested_alias"),
            "tier": event.get("tier"),
            "model": event.get("model"),
            "degraded": False,
            "attempt": event.get("attempt", 1),
            "success": bool(event.get("success")),
            "failure": event.get("failure"),
            "input_tokens": event.get("input_tokens"),
            "output_tokens": event.get("output_tokens"),
            "latency_ms": None,
        }
        for event in events
    ]
