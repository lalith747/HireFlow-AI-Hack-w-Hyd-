"""
hindsight_engine.py
Hindsight Engine for HireFlow.

Strategy:
- Use Hindsight recall() to find which platforms are relevant for a role
- Load actual numbers directly from campaigns.json (reliable, no regex parsing)
- Calculate metrics and return structured JSON for Person 2
"""

import json
import os
import re
from collections import defaultdict
from hindsight_client import Hindsight

BANK_ID       = "hireflow-campaigns"
CAMPAIGNS_FILE = os.path.join(os.path.dirname(__file__), "campaigns.json")

client = Hindsight(
    base_url="https://api.hindsight.vectorize.io",
    api_key="hsk_508e408ad972eb1a8018850b01bd9454_ccd180e5622164b3"
)


# ─────────────────────────────────────────────
# Load JSON data
# ─────────────────────────────────────────────

def load_campaigns() -> list:
    with open(CAMPAIGNS_FILE, "r") as f:
        return json.load(f)


# ─────────────────────────────────────────────
# Component 2 — Campaign Matcher
# Two-pronged approach:
#   1. Hindsight recall tells us which platforms are relevant
#   2. We load exact numbers from JSON (no regex fragility)
# ─────────────────────────────────────────────

def find_similar_campaigns(role: str, platforms: list, all_campaigns: list) -> dict:
    """
    Primary: match directly from JSON by role name (exact + partial match).
    Secondary: use Hindsight recall to find any additional platform signals.
    Returns { platform -> list of campaign dicts }
    """
    role_lower = role.lower()
    platform_campaigns = defaultdict(list)

    # --- Primary: direct JSON match (reliable) ---
    for c in all_campaigns:
        if role_lower in c["role"].lower() and c["platform"] in platforms:
            platform_campaigns[c["platform"]].append(c)

    # --- Secondary: Hindsight recall for semantic boost ---
    # If JSON match found nothing (new role not in JSON), try Hindsight recall
    total_json_found = sum(len(v) for v in platform_campaigns.values())

    if total_json_found == 0:
        print("[Hindsight] No exact JSON match — using semantic recall from Hindsight memory...")
        hindsight_signals = _recall_from_hindsight(role, platforms)

        for platform, texts in hindsight_signals.items():
            # Extract campaign IDs mentioned in recalled text and look them up in JSON
            for text in texts:
                ids = re.findall(r"Campaign ID (\d+)", text)
                for cid in ids:
                    match = next((c for c in all_campaigns if c["campaign_id"] == int(cid)), None)
                    if match and match["platform"] in platforms:
                        platform_campaigns[match["platform"]].append(match)

    return dict(platform_campaigns)


def _recall_from_hindsight(role: str, platforms: list) -> dict:
    """
    Calls Hindsight recall and returns raw memory texts per platform.
    """
    platform_texts = defaultdict(list)
    for platform in platforms:
        query    = f"{role} hiring campaign on {platform}"
        response = client.recall(bank_id=BANK_ID, query=query)
        for memory in response.results:
            platform_texts[platform].append(memory.text)
    return dict(platform_texts)


# ─────────────────────────────────────────────
# Component 3 — ROI Calculator
# ─────────────────────────────────────────────

def calculate_metrics(campaigns: list) -> dict:
    if not campaigns:
        return {}

    # Deduplicate by campaign_id to avoid double counting
    seen = set()
    unique = []
    for c in campaigns:
        cid = c.get("campaign_id", id(c))
        if cid not in seen:
            seen.add(cid)
            unique.append(c)

    total_budget       = sum(c["budget"]       for c in unique)
    total_applications = sum(c["applications"] for c in unique)
    total_interviews   = sum(c["interviews"]   for c in unique)
    total_hires        = sum(c["hires"]        for c in unique)

    hire_rate      = total_hires      / total_applications if total_applications > 0 else 0.0
    interview_rate = total_interviews / total_applications if total_applications > 0 else 0.0
    cost_per_hire  = total_budget     / total_hires        if total_hires        > 0 else None

    return {
        "total_campaigns":    len(unique),
        "total_budget":       total_budget,
        "total_applications": total_applications,
        "total_interviews":   total_interviews,
        "total_hires":        total_hires,
        "hire_rate":          round(hire_rate, 4),
        "interview_rate":     round(interview_rate, 4),
        "cost_per_hire":      round(cost_per_hire, 2) if cost_per_hire is not None else None,
    }


# ─────────────────────────────────────────────
# Component 4 — Recommendation Engine
# ─────────────────────────────────────────────

def rate_platform(metrics: dict) -> tuple:
    if not metrics:
        return ("No Data", "No historical campaigns found for this platform and role.")

    hire_rate      = metrics["hire_rate"]
    cost_per_hire  = metrics["cost_per_hire"]
    total_hires    = metrics["total_hires"]
    interview_rate = metrics["interview_rate"]

    if total_hires == 0:
        return (
            "Poor",
            f"Generated {metrics['total_applications']} applications across "
            f"{metrics['total_campaigns']} campaign(s) but resulted in zero hires."
        )
    elif hire_rate >= 0.03:
        return (
            "Excellent",
            f"Hire rate of {hire_rate*100:.1f}% is above 3%. "
            f"Cost per hire: ${cost_per_hire}. "
            f"Interview conversion: {interview_rate*100:.1f}%."
        )
    elif hire_rate >= 0.02:
        return (
            "Good",
            f"Hire rate of {hire_rate*100:.1f}% is between 2-3%. "
            f"Consistent performance. Cost per hire: ${cost_per_hire}."
        )
    elif hire_rate >= 0.01:
        return (
            "Average",
            f"Hire rate of {hire_rate*100:.1f}% is between 1-2%. "
            f"Below average conversion. Cost per hire: ${cost_per_hire}."
        )
    else:
        return (
            "Poor",
            f"Hire rate of {hire_rate*100:.1f}% is below 1%. "
            f"Very low conversion despite receiving applications."
        )


def build_recommendations(platform_analysis: dict) -> tuple:
    recommended = []
    avoid       = []
    for platform, data in platform_analysis.items():
        if data["rating"] in ("Excellent", "Good"):
            recommended.append(platform)
        elif data["rating"] == "Poor":
            avoid.append(platform)
    return recommended, avoid


# ─────────────────────────────────────────────
# Main Entry Point
# ─────────────────────────────────────────────

def analyze_campaign(role: str, budget: int, platforms: list) -> dict:
    """
    Main function — hand output to Person 2.
    """
    all_campaigns   = load_campaigns()
    platform_campaigns = find_similar_campaigns(role, platforms, all_campaigns)
    total_history_found = sum(len(v) for v in platform_campaigns.values())

    platform_analysis = {}
    for platform in platforms:
        campaigns      = platform_campaigns.get(platform, [])
        metrics        = calculate_metrics(campaigns)
        rating, reason = rate_platform(metrics)

        platform_analysis[platform] = {
            "rating":          rating,
            "hire_rate":       metrics.get("hire_rate"),
            "interview_rate":  metrics.get("interview_rate"),
            "cost_per_hire":   metrics.get("cost_per_hire"),
            "campaigns_found": metrics.get("total_campaigns", 0),
            "reason":          reason,
        }

    recommended, avoid = build_recommendations(platform_analysis)

    return {
        "role":              role,
        "budget":            budget,
        "history_found":     total_history_found,
        "platform_analysis": platform_analysis,
        "recommended":       recommended,
        "avoid":             avoid,
    }


# ─────────────────────────────────────────────
# Quick Test
# ─────────────────────────────────────────────

if __name__ == "__main__":
    result = analyze_campaign(
        role="Python Backend Engineer",
        budget=500,
        platforms=["LinkedIn", "Naukri", "Indeed", "Wellfound"]
    )
    print(json.dumps(result, indent=2))