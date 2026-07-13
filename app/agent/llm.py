"""Bedrock-backed remediation-plan drafting.

The client is resolved lazily (not at import time — no network/credential
lookup just from importing this module) and can be swapped for a fake via
set_client(), which is how tests/test_agent_graph.py exercises the graph
without a real Bedrock call. No temperature/top_p/top_k or assistant prefill
— both are rejected by Claude Sonnet 5 (see PLAN.md section 0.2).
"""

from __future__ import annotations

import json
import logging

import anthropic
from anthropic import AnthropicBedrock

from app.config import settings
from app.exceptions import LLMRefusalError, LLMTimeoutError

logger = logging.getLogger(__name__)

REMEDIATION_SYSTEM_PROMPT = """\
You are a compliance-aware loan-remediation drafting assistant for RiskGuard AI. \
Given a customer's risk profile and the applicable policy clauses, draft a \
restructuring plan the customer can be offered.

Base every numeric limit strictly on the policy clauses provided — never invent a limit \
that is not supported by them. If the provided clauses don't cover something, say so in \
SUMMARY rather than guessing.

Respond with EXACTLY these five lines, each starting with the label shown, and nothing else:

RATE_REDUCTION_BPS: <integer basis points, 0 if none proposed>
FORBEARANCE_MONTHS: <integer months, 0 if none proposed>
TERM_EXTENSION_MONTHS: <integer months, 0 if none proposed>
POLICY_CITATION: <the clause heading(s) you relied on>
SUMMARY: <a short, customer-facing explanation of the plan, one or two sentences>
"""

_RETRYABLE_EXCEPTIONS = (anthropic.APITimeoutError, anthropic.RateLimitError, anthropic.APIStatusError)

_default_client: anthropic.Anthropic | None = None
_override_client: anthropic.Anthropic | None = None


def set_client(client: anthropic.Anthropic | None) -> None:
    """Test seam — inject a fake/mock client. Pass None to restore the real one."""
    global _override_client
    _override_client = client


def _active_client() -> anthropic.Anthropic:
    global _default_client
    if _override_client is not None:
        return _override_client
    if _default_client is None:
        _default_client = AnthropicBedrock(aws_region=settings.aws_region, timeout=60.0)
    return _default_client


def _build_prompt(
    *,
    risk_features: dict,
    policies: list[str],
    prior_draft: str | None,
    revision_feedback: str | None,
) -> str:
    lines = [
        "Customer risk profile:",
        json.dumps(risk_features, indent=2, default=str),
        "",
        "Applicable policy clauses (use ONLY these for numeric limits):",
    ]
    for i, policy_text in enumerate(policies, start=1):
        lines.append(f"--- Policy excerpt {i} ---")
        lines.append(policy_text)

    if prior_draft is not None:
        lines += ["", "--- Prior draft (revise this) ---", prior_draft]
    if revision_feedback is not None:
        lines += ["", "--- Feedback to address in this revision ---", revision_feedback]

    lines += ["", "Draft the remediation plan now, following the required 5-line format exactly."]
    return "\n".join(lines)


def draft_remediation_plan(
    *,
    risk_features: dict,
    policies: list[str],
    prior_draft: str | None = None,
    revision_feedback: str | None = None,
) -> str:
    """Retries once on a transient failure (timeout/rate-limit/API status
    error); raises LLMTimeoutError if the retry also fails. Raises
    LLMRefusalError immediately (no retry) if the model declines."""
    prompt = _build_prompt(
        risk_features=risk_features,
        policies=policies,
        prior_draft=prior_draft,
        revision_feedback=revision_feedback,
    )
    client = _active_client()

    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            response = client.messages.create(
                model=settings.bedrock_model_id,
                max_tokens=4096,
                system=REMEDIATION_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
        except _RETRYABLE_EXCEPTIONS as exc:
            last_exc = exc
            logger.warning("bedrock call failed (attempt %s/2): %s", attempt + 1, exc)
            continue

        if getattr(response, "stop_reason", None) == "refusal":
            category = getattr(getattr(response, "stop_details", None), "category", None)
            raise LLMRefusalError(f"Bedrock declined the request (category={category})")

        text_blocks = [block.text for block in response.content if block.type == "text"]
        return "\n".join(text_blocks).strip()

    raise LLMTimeoutError(f"Bedrock call failed after retry: {last_exc}") from last_exc
