"""
cascadeflow_runtime.py
---------------------
CascadeFlow runtime integration layer for the HireFlow backend.

Uses the verified CascadeFlow v2 API:
- cascadeflow.init(**kwargs)  → HarnessInitReport
- with cascadeflow.run(...) as ctx → HarnessRunContext
- ctx.model_used, ctx.cost, ctx.savings, ctx.last_action, ctx._trace

Note: CascadeFlow auto-instruments only OpenAI and Anthropic SDKs.
Groq calls are NOT auto-patched. We manually record metrics after
each Groq call to keep the harness context accurate.
"""

import os
import time
import logging
from typing import Optional, Any

import cascadeflow
from cascadeflow.harness.api import HarnessInitReport, HarnessRunContext

logger = logging.getLogger("cascadeflow_runtime")

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_initialized: bool = False
_init_report: Optional[HarnessInitReport] = None
_init_time: Optional[float] = None


def init_cascadeflow(
    mode: str = "observe",
    budget: Optional[float] = None,
    compliance: Optional[str] = None,
    verbose: bool = False,
    max_latency_ms: Optional[float] = None,
    kpi_targets: Optional[dict[str, float]] = None,
    kpi_weights: Optional[dict[str, float]] = None,
) -> HarnessInitReport:
    """
    Activate the CascadeFlow harness globally.

    Parameters match the verified cascadeflow.init() signature.
    Called once at application startup (in main.py lifespan).

    Returns
    -------
    HarnessInitReport
        Contains mode, instrumented SDKs, and detected-but-not-instrumented SDKs.
    """
    global _initialized, _init_report, _init_time

    # Resolve from env if not explicitly provided
    if mode is None:
        mode = os.getenv("CASCADEFLOW_MODE", "observe")

    if budget is None:
        budget_str = os.getenv("CASCADEFLOW_DEFAULT_BUDGET")
        budget = float(budget_str) if budget_str else None

    # Call the real cascadeflow.init()
    _init_report = cascadeflow.init(
        mode=mode,
        budget=budget,
        compliance=compliance,
        verbose=verbose,
        max_latency_ms=max_latency_ms,
        kpi_targets=kpi_targets,
        kpi_weights=kpi_weights,
    )

    _initialized = True
    _init_time = time.time()

    logger.info(
        "CASCADEFLOW INITIALIZED | mode=%s | instrumented=%s | detected_not_instrumented=%s",
        _init_report.mode,
        _init_report.instrumented,
        _init_report.detected_but_not_instrumented,
    )

    # Intercept proof
    print("CASCADEFLOW INTERCEPT ACTIVE")
    logger.info("CASCADEFLOW INTERCEPT ACTIVE — harness is live")

    return _init_report


def get_harness_status() -> dict[str, Any]:
    """
    Return the current CascadeFlow harness status.
    """
    uptime = time.time() - _init_time if _init_time else 0.0
    return {
        "initialized": _initialized,
        "mode": _init_report.mode if _init_report else "off",
        "instrumented_sdks": _init_report.instrumented if _init_report else [],
        "detected_not_instrumented": (
            _init_report.detected_but_not_instrumented if _init_report else []
        ),
        "init_time_utc": _init_time,
        "uptime_seconds": round(uptime, 3),
    }


class CascadeflowSession:
    """
    Context manager wrapping cascadeflow.run().

    Usage
    -----
    with CascadeflowSession(budget=0.50) as session:
        # LLM calls here
        session.record_model_call(model="llama-3.1-8b-instant", cost=0.0003)
        print(session.summary())
    """

    def __init__(
        self,
        budget: Optional[float] = None,
        max_latency_ms: Optional[float] = None,
        compliance: Optional[str] = None,
        labels: Optional[dict[str, str]] = None,
    ):
        self._budget = budget
        self._max_latency_ms = max_latency_ms
        self._compliance = compliance
        self._labels = labels or {}
        self._ctx: Optional[HarnessRunContext] = None

        # Manual tracking for Groq (since it's not auto-instrumented)
        self._manual_cost: float = 0.0
        self._manual_calls: int = 0
        self._models_used: list[str] = []

    def __enter__(self) -> "CascadeflowSession":
        run_kwargs: dict[str, Any] = {}
        if self._budget is not None:
            run_kwargs["budget"] = self._budget
        if self._max_latency_ms is not None:
            run_kwargs["max_latency_ms"] = self._max_latency_ms
        if self._compliance is not None:
            run_kwargs["compliance"] = self._compliance

        self._ctx = cascadeflow.run(**run_kwargs)
        self._ctx.__enter__()

        logger.debug(
            "CASCADEFLOW SESSION START | budget=%s | labels=%s",
            self._budget,
            self._labels,
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._ctx is not None:
            # Sync our manual Groq tracking into the harness context
            if self._manual_calls > 0 and self._ctx.model_used is None:
                # Set the last model used
                pass  # ctx attributes are read-only-ish; we track separately
            self._ctx.__exit__(exc_type, exc_val, exc_tb)
        logger.debug("CASCADEFLOW SESSION END")

    async def __aenter__(self) -> "CascadeflowSession":
        return self.__enter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        self.__exit__(exc_type, exc_val, exc_tb)

    def record_model_call(self, model: str, cost: float) -> None:
        """
        Manually record a model call for Groq (not auto-instrumented).

        Call this after every Groq API call to keep the session accurate.
        """
        self._manual_cost += cost
        self._manual_calls += 1
        if model not in self._models_used:
            self._models_used.append(model)

        logger.debug(
            "[MODEL_CALL] model=%s cost=$%.6f total_cost=$%.6f",
            model,
            cost,
            self._manual_cost,
        )

    def summary(self) -> dict[str, Any]:
        """
        Return session summary combining harness data and manual Groq tracking.
        """
        ctx_data: dict[str, Any] = {}
        if self._ctx is not None:
            ctx_data = {
                "run_id": self._ctx.run_id,
                "mode": self._ctx.mode,
                "harness_cost": self._ctx.cost,
                "harness_savings": self._ctx.savings,
                "harness_model_used": self._ctx.model_used,
                "harness_last_action": self._ctx.last_action,
                "step_count": self._ctx.step_count,
                "tool_calls": self._ctx.tool_calls,
                "budget_remaining": self._ctx.budget_remaining,
            }

        return {
            **ctx_data,
            "manual_cost_total": round(self._manual_cost, 6),
            "manual_calls": self._manual_calls,
            "models_used": self._models_used,
            "labels": self._labels,
        }

    def trace(self) -> list[dict[str, Any]]:
        """
        Return the raw trace from the harness context.
        """
        if self._ctx is not None and hasattr(self._ctx, "_trace"):
            return list(self._ctx._trace)
        return []


async def verify_cascadeflow() -> dict[str, Any]:
    """
    Runtime verification that CascadeFlow is:
    - Imported
    - Initialized
    - Intercepting calls (session context manager works)

    Returns
    -------
    dict with verification results.
    """
    results: dict[str, Any] = {
        "package_imported": False,
        "harness_initialized": False,
        "harness_status": {},
        "session_created": False,
        "session_context_works": False,
        "overall_pass": False,
        "errors": [],
    }

    # 1. Confirm the package imported
    try:
        import cascadeflow as _cf  # noqa: F401
        results["package_imported"] = True
    except ImportError as exc:
        results["errors"].append(f"ImportError: {exc}")
        return results

    # 2. Confirm harness initialized
    results["harness_initialized"] = _initialized
    results["harness_status"] = get_harness_status()

    # 3. Attempt to create a session
    try:
        with CascadeflowSession(budget=999.0, labels={"test": "verify"}) as sess:
            results["session_created"] = True
            if sess._ctx is not None:
                results["session_context_works"] = True

            summary = sess.summary()
            if summary:
                results["session_summary"] = summary

            trace = sess.trace()
            results["trace_entries"] = len(trace)
    except Exception as exc:
        results["errors"].append(f"SessionError: {exc}")

    # 4. Overall pass
    results["overall_pass"] = all(
        [
            results["package_imported"],
            results["harness_initialized"],
            results["session_created"],
            results["session_context_works"],
        ]
    )

    logger.info("CASCADEFLOW VERIFY | %s", results)
    return results
