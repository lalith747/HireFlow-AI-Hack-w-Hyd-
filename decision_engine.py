"""
decision_engine.py
------------------
CascadeFlowEngine — the core campaign decision engine.

Refactored to run inside a CascadeflowSession so that every LLM call
is tracked, measured, and (in enforce mode) governed by the CascadeFlow harness.
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

# These models must be available in your Groq account.
# CascadeFlow will switch between them based on complexity in enforce mode.

MODEL_SIMPLE = os.getenv("GROQ_MODEL_SIMPLE", "llama-3.1-8b-instant")
MODEL_COMPLEX = os.getenv("GROQ_MODEL_COMPLEX", "llama-3.3-70b-versatile")

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

        # Default budget per session (can be overridden per request)
        self.default_budget = float(os.getenv("CASCADEFLOW_DEFAULT_BUDGET", "1.00"))

        # Routing mode
        self.semantic_routing = (
            os.getenv("CASCADEFLOW_ENABLE_SEMANTIC_ROUTING", "true").lower() == "true"
        )

        # Ensure harness is initialized (idempotent if already called in main.py)
        init_cascadeflow(
            mode=os.getenv("CASCADEFLOW_MODE", "observe"),
            budget=self.default_budget,
            enable_semantic_routing=self.semantic_routing,
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

        Parameters
        ----------
        user_input : str
            The user's campaign brief / requirements.
        total_budget : float
            Total campaign budget in USD.
        campaign_name : str
            Human-readable name for tracing.
        budget_cap : float or None
            Maximum cumulative LLM spend for this execution.
        labels : dict or None
            Metadata attached to the session trace.

        Returns
        -------
        dict with keys:
            campaign_name, run_id, timestamp, total_budget,
            complexity_score, model_used, platforms_selected,
            budget_saved, cascadeflow_session_summary, metadata
        """
        run_id = str(uuid.uuid4())[:8]
        start_time = time.time()

        if budget_cap is None:
            budget_cap = self.default_budget

        if labels is None:
            labels = {}
        labels.update({"run_id": run_id, "campaign": campaign_name})

        # ── Enter the CascadeFlow session ──────────────────────────
        async with CascadeflowSession(budget=budget_cap, labels=labels) as session:
            # Phase 1: Complexity detection
            complexity_score = await self._detect_complexity(user_input)

            # Phase 2: Select model based on complexity (multi-model routing)
            selected_model = self._select_model(complexity_score)

            # Phase 3: Platform analysis with the selected model
            analysis = await self._analyze_platforms(
                user_input=user_input,
                total_budget=total_budget,
                model=selected_model,
            )

            # Phase 4: Budget redistribution from analysis
            platforms = self._parse_platforms(analysis, total_budget)

            # Phase 5: Compute savings
            budget_saved = self._compute_savings(platforms, total_budget)

            # Collect session summary from CascadeFlow
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
                },
            }

            self._log(result, session_summary)
            return result

    # ------------------------------------------------------------------
    # Private: Complexity detection
    # ------------------------------------------------------------------

    async def _detect_complexity(self, user_input: str) -> int:
        """
        Call the LLM to rate input complexity on a 1-10 scale.

        Uses the simple/fast model regardless of routing mode
        to keep the complexity check cheap.
        """
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

        This is the routing decision that proves multi-model behavior.
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
        """
        Call the selected Groq model to produce platform recommendations.
        """
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
    # Private: Parsing and computation
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_platforms(raw: str, total_budget: float) -> list[dict[str, Any]]:
        """
        Parse JSON from the LLM response and attach dollar amounts.
        """
        import json

        try:
            # Strip markdown fences if present
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
        """
        Compute unallocated budget as savings.
        """
        allocated = sum(p.get("allocated_budget", 0.0) for p in platforms)
        return max(0.0, total_budget - allocated)

    # ------------------------------------------------------------------
    # Logging (audit trail)
    # ------------------------------------------------------------------

    @staticmethod
    def _log(result: dict[str, Any], session_summary: Any) -> None:
        """
        Emit structured audit log for every execution.
        """
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
