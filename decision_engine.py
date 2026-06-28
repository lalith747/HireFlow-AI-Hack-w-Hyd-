"""
decision_engine.py
------------------
CascadeFlowEngine — the core campaign decision engine.

Refactored to run inside a CascadeflowSession so that every LLM call
is tracked, measured, and (in enforce mode) governed by the CascadeFlow harness.

Note: CascadeFlow auto-instruments OpenAI and Anthropic SDKs only.
Groq calls are tracked manually via session.record_model_call().
"""

import os
import time
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, Any

from groq import Groq

from cascadeflow_runtime import CascadeflowSession, init_cascadeflow

logger = logging.getLogger("decision_engine")

# ---------------------------------------------------------------------------
# Model registry for multi-model routing
# ---------------------------------------------------------------------------

MODEL_SIMPLE = os.getenv("GROQ_MODEL_SIMPLE", "llama-3.1-8b-instant")
MODEL_COMPLEX = os.getenv("GROQ_MODEL_COMPLEX", "llama-3.3-70b-versatile")

# Approximate costs per 1K tokens (adjust based on Groq pricing)
COST_PER_1K_SIMPLE = 0.00004   # llama-3.1-8b-instant ~ $0.04/M tokens
COST_PER_1K_COMPLEX = 0.00059  # llama-3.3-70b ~ $0.59/M tokens

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

PLATFORM_ANALYSIS_PROMPT = """You are a marketing strategist. Given the following campaign brief, recommend the top 3 platforms (from: Google Ads, Meta Ads, TikTok, LinkedIn, Twitter/X, Pinterest, Snapchat, YouTube) and allocate a percentage of the total budget to each.

Campaign Brief:
{user_input}

Total Budget: ${total_budget}

Respond ONLY with valid JSON in this exact format:
{{
  "platforms": [
    {{"name": "Platform Name", "allocation_pct": 25, "rationale": "One sentence reason."}},
    ...
  ]
}}
"""

COMPLEXITY_CHECK_PROMPT = """Rate the complexity of the following marketing request on a scale of 1-10, where:
1-3 = simple (straightforward targeting, single product)
4-6 = moderate (multi-product, multi-region, or compliance requirements)
7-10 = complex (multi-channel orchestration, heavy regulation, enterprise scale)

Request: {user_input}

Respond ONLY with a single integer between 1 and 10. No explanation.
"""

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class CascadeFlowEngine:
    """
    Campaign decision engine wrapped inside CascadeFlow governance.

    Every call to execute() is tracked by the CascadeFlow harness.
    In observe mode: all metrics logged, no enforcement.
    In enforce mode: budget caps, model switching, and stop actions are active.
    """

    def __init__(self):
        # Groq client — key loaded from environment only
        self.groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])

        # Default budget per session
        self.default_budget = float(os.getenv("CASCADEFLOW_DEFAULT_BUDGET", "1.00"))

        # Semantic routing toggle (our own logic, not CascadeFlow's)
        self.semantic_routing = (
            os.getenv("CASCADEFLOW_ENABLE_SEMANTIC_ROUTING", "true").lower() == "true"
        )

        # Ensure harness is initialized (idempotent)
        init_cascadeflow(
            mode=os.getenv("CASCADEFLOW_MODE", "observe"),
            budget=self.default_budget,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(
        self,
        user_input: str,
        total_budget: float,
        campaign_name: str = "",
        budget_cap: Optional[float] = None,
        labels: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """
        Execute the campaign decision pipeline inside a CascadeFlow session.
        """
        run_id = str(uuid.uuid4())[:8]
        start_time = time.time()

        if budget_cap is None:
            budget_cap = self.default_budget

        if labels is None:
            labels = {}
        labels.update({"run_id": run_id, "campaign": campaign_name})

        # ── Enter the CascadeFlow session ──────────────────────────
        with CascadeflowSession(
            budget=budget_cap,
            labels=labels,
        ) as session:

            # Phase 1: Complexity detection
            complexity_score = await self._detect_complexity(user_input)
            # Record the complexity check call
            session.record_model_call(
                model=MODEL_SIMPLE,
                cost=self._estimate_cost(MODEL_SIMPLE, prompt_tokens=50, completion_tokens=3),
            )

            # Phase 2: Select model based on complexity (multi-model routing)
            selected_model = self._select_model(complexity_score)

            # Phase 3: Platform analysis with the selected model
            analysis = await self._analyze_platforms(
                user_input=user_input,
                total_budget=total_budget,
                model=selected_model,
            )
            # Record the platform analysis call
            session.record_model_call(
                model=selected_model,
                cost=self._estimate_cost(selected_model, prompt_tokens=200, completion_tokens=300),
            )

            # Phase 4: Parse and compute
            platforms = self._parse_platforms(analysis, total_budget)
            budget_saved = self._compute_savings(platforms, total_budget)

            # Collect session summary
            session_summary = session.summary()

            # Build response
            elapsed_ms = round((time.time() - start_time) * 1000, 2)

            result: dict[str, Any] = {
                "campaign_name": campaign_name or "unnamed",
                "run_id": run_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "total_budget": total_budget,
                "complexity_score": complexity_score,
                "model_used": selected_model,
                "platforms_selected": platforms,
                "budget_saved": round(budget_saved, 2),
                "cascadeflow_session_summary": session_summary,
                "metadata": {
                    "elapsed_ms": elapsed_ms,
                    "semantic_routing_enabled": self.semantic_routing,
                    "cascadeflow_mode": os.getenv("CASCADEFLOW_MODE", "observe"),
                    "total_cost_estimate": round(session._manual_cost, 6),
                },
            }

            self._log(result, session_summary)
            return result

    # ------------------------------------------------------------------
    # Private: Complexity detection
    # ------------------------------------------------------------------

    async def _detect_complexity(self, user_input: str) -> int:
        """Rate input complexity on a 1-10 scale using the cheap model."""
        prompt = COMPLEXITY_CHECK_PROMPT.format(user_input=user_input)

        try:
            response = self.groq_client.chat.completions.create(
                model=MODEL_SIMPLE,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=5,
            )
            raw = response.choices[0].message.content.strip()
            score = int(raw)
            return max(1, min(10, score))
        except Exception:
            logger.warning("Complexity detection failed; defaulting to 5")
            return 5

    # ------------------------------------------------------------------
    # Private: Model selection (multi-model routing)
    # ------------------------------------------------------------------

    def _select_model(self, complexity_score: int) -> str:
        """
        Route to the appropriate model based on complexity.

        Simple (1-5)  → MODEL_SIMPLE  (llama-3.1-8b-instant)
        Complex (6-10) → MODEL_COMPLEX (llama-3.3-70b-versatile)
        """
        if self.semantic_routing and complexity_score >= 6:
            selected = MODEL_COMPLEX
        else:
            selected = MODEL_SIMPLE

        logger.info(
            "[MODEL_SELECTION] complexity=%d → model=%s",
            complexity_score,
            selected,
        )
        print(
            f"\n[MODEL_SELECTION]\n"
            f"  Input Complexity: {'HIGH' if complexity_score >= 6 else 'LOW'} (score={complexity_score})\n"
            f"  Selected Model: {selected}\n"
            f"  Reason: {'Escalated After Quality Check' if complexity_score >= 6 else 'Cost Optimization'}\n"
            f"  Estimated Cost: ${self._estimate_cost(selected, 200, 300):.4f}"
        )
        return selected

    # ------------------------------------------------------------------
    # Private: Platform analysis
    # ------------------------------------------------------------------

    async def _analyze_platforms(
        self,
        user_input: str,
        total_budget: float,
        model: str,
    ) -> str:
        """Call the selected Groq model to produce platform recommendations."""
        prompt = PLATFORM_ANALYSIS_PROMPT.format(
            user_input=user_input,
            total_budget=total_budget,
        )

        response = self.groq_client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=800,
        )

        return response.choices[0].message.content

    # ------------------------------------------------------------------
    # Private: Cost estimation
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
        """Estimate cost based on model and token counts."""
        if model == MODEL_COMPLEX:
            rate = COST_PER_1K_COMPLEX
        else:
            rate = COST_PER_1K_SIMPLE

        total_tokens = prompt_tokens + completion_tokens
        return (total_tokens / 1000) * rate

    # ------------------------------------------------------------------
    # Private: Parsing and computation
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_platforms(raw: str, total_budget: float) -> list[dict[str, Any]]:
        """Parse JSON from the LLM response and attach dollar amounts."""
        import json

        try:
            clean = raw.strip()
            if clean.startswith("```"):
                lines = clean.split("\n")
                clean = "\n".join(lines[1:-1])
            data = json.loads(clean)
            platforms = data.get("platforms", [])
        except (json.JSONDecodeError, KeyError):
            logger.error("Failed to parse platform JSON; returning empty list")
            return []

        for p in platforms:
            pct = p.get("allocation_pct", 0)
            p["allocated_budget"] = round(total_budget * pct / 100.0, 2)

        return platforms

    @staticmethod
    def _compute_savings(
        platforms: list[dict[str, Any]],
        total_budget: float,
    ) -> float:
        """Compute unallocated budget as savings."""
        allocated = sum(p.get("allocated_budget", 0.0) for p in platforms)
        return max(0.0, total_budget - allocated)

    # ------------------------------------------------------------------
    # Logging (audit trail)
    # ------------------------------------------------------------------

    @staticmethod
    def _log(result: dict[str, Any], session_summary: Any) -> None:
        """Emit structured audit log for every execution."""
        logger.info(
            "CASCADEFLOW RUNTIME: EXECUTION COMPLETE | "
            "run_id=%s | campaign=%s | complexity=%s | model=%s | "
            "budget_total=%.2f | budget_saved=%.2f | elapsed=%sms | "
            "session=%s",
            result["run_id"],
            result["campaign_name"],
            result["complexity_score"],
            result["model_used"],
            result["total_budget"],
            result["budget_saved"],
            result["metadata"]["elapsed_ms"],
            session_summary,
        )
