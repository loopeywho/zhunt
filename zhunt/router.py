"""Coordinator composing classification, registry selection, and stickiness."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, replace
from enum import Enum
from threading import RLock

from zhunt.brain import (
    Classification,
    ClassificationInput,
    HeuristicClassifier,
    SessionStickiness,
    Tier,
    session_key,
)
from zhunt.registry import ModelRegistry


class FailureKind(str, Enum):
    PROVIDER_ERROR = "provider-error"
    TRUNCATION = "truncation"
    REFUSAL = "refusal"


@dataclass(frozen=True)
class RoutingRequest:
    """Protocol-neutral request emitted by each wire adapter."""

    model_alias: str
    user_text: str
    first_user_message: str
    system_prompt: str = ""
    session_id: str | None = None
    estimated_input_tokens: int | None = None
    estimated_output_tokens: int = 1
    has_tool_calls: bool = False

    def classification_input(self) -> ClassificationInput:
        return ClassificationInput(
            user_text=self.user_text,
            system_prompt=self.system_prompt,
            estimated_tokens=self.estimated_input_tokens,
            has_tool_calls=self.has_tool_calls,
        )


@dataclass(frozen=True)
class RoutingDecision:
    session_key: str
    requested_alias: str
    tier: Tier
    model: str
    classification: Classification | None
    explicit_override: bool
    reused_session_route: bool
    escalation_count: int = 0
    failure: FailureKind | None = None


class RoutingCoordinator:
    """The shared routing contract used by all inbound dialects."""

    def __init__(
        self,
        *,
        registry: ModelRegistry,
        classifier: HeuristicClassifier | None = None,
        stickiness: SessionStickiness | None = None,
    ) -> None:
        self.registry = registry
        self.classifier = classifier or HeuristicClassifier()
        self.stickiness = stickiness or SessionStickiness()
        self._failure_counts: dict[str, int] = {}
        self._lock = RLock()

    def route(
        self,
        request: RoutingRequest,
        *,
        healthy_models: Collection[str] | None = None,
    ) -> RoutingDecision:
        key = session_key(
            session_id=request.session_id,
            system_prompt=request.system_prompt,
            first_user_message=request.first_user_message,
        )
        explicit_tier = self.registry.resolve_alias(request.model_alias)
        classification: Classification | None = None

        if explicit_tier is not None:
            selected = self._select_model(
                explicit_tier,
                request=request,
                healthy_models=healthy_models,
            )
            return RoutingDecision(
                session_key=key,
                requested_alias=request.model_alias,
                tier=explicit_tier,
                model=selected,
                classification=None,
                explicit_override=True,
                reused_session_route=False,
            )

        classification = self.classifier.classify(request.classification_input())
        sticky = self.stickiness.choose(
            session_key=key,
            classified_tier=classification.tier,
            select_model=lambda tier: self._select_model(
                tier,
                request=request,
                healthy_models=healthy_models,
            ),
        )
        return RoutingDecision(
            session_key=key,
            requested_alias=request.model_alias,
            tier=sticky.route.tier,
            model=sticky.route.model,
            classification=classification,
            explicit_override=False,
            reused_session_route=not sticky.changed,
            escalation_count=self._failure_count(key),
        )

    def escalate(
        self,
        decision: RoutingDecision,
        failure: FailureKind,
        *,
        estimated_input_tokens: int | None = None,
        estimated_output_tokens: int = 1,
        healthy_models: Collection[str] | None = None,
    ) -> RoutingDecision:
        with self._lock:
            strikes = self._failure_counts.get(decision.session_key, 0) + 1
            self._failure_counts[decision.session_key] = strikes

        target_tier = (
            Tier.highest() if strikes >= 2 else decision.tier.next_higher()
        )
        request = RoutingRequest(
            model_alias=decision.requested_alias,
            user_text="",
            first_user_message="",
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
        )
        promoted = self.stickiness.choose(
            session_key=decision.session_key,
            classified_tier=target_tier,
            select_model=lambda tier: self._select_model(
                tier,
                request=request,
                healthy_models=healthy_models,
            ),
        )
        return replace(
            decision,
            tier=promoted.route.tier,
            model=promoted.route.model,
            reused_session_route=not promoted.changed,
            escalation_count=strikes,
            failure=failure,
        )

    def record_success(self, decision: RoutingDecision) -> None:
        """Reset failure telemetry after an upstream request completes cleanly."""

        with self._lock:
            self._failure_counts.pop(decision.session_key, None)

    def _select_model(
        self,
        tier: Tier,
        *,
        request: RoutingRequest,
        healthy_models: Collection[str] | None,
    ) -> str:
        input_tokens = (
            request.estimated_input_tokens
            if request.estimated_input_tokens is not None
            else 1
        )
        return self.registry.select_model(
            tier,
            input_tokens=input_tokens,
            output_tokens=request.estimated_output_tokens,
            healthy_models=healthy_models,
        ).model

    def _failure_count(self, key: str) -> int:
        with self._lock:
            return self._failure_counts.get(key, 0)
