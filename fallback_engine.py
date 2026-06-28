"""
fallback_engine.py
Same Hindsight Engine logic but reads directly from campaigns.json.
Use this to test locally without needing Hindsight server running.

Usage:
    python fallback_engine.py
"""

import json
from collections import defaultdict

CAMPAIGNS_FILE = "campaigns.json"


def load_campaigns() -> list[dict]:
    with open(CAMPAIGNS_FILE, "r") as f:
        return json.load(f)


def find_similar_campaigns(role: str, platforms: list[str], all_campaigns: list[dict]) -> dict:
    role_lower = role.lower()
    platform_campaigns = defaultdict(list)

    for c in all_campaigns:
        if role_lower in c["role"].lower() and c["platform"] in platforms:
            platform_campaigns[c["platform"]].append(c)

    return dict(platform_campaigns)


def calculate_metrics(campaigns: list[dict]) -> dict:
    if not campaigns:
        return {}

    total_budget = sum(c["budget"] for c in campaigns)
    total_applications = sum(c["applications"] for c in campaigns)
    total_interviews = sum(c["interviews"] for c in campaigns)
    total_hires = sum(c["hires"] for c in campaigns)

    hire_rate = total_hires / total_applications if total_applications > 0 else 0.0
    interview_rate = total_interviews / total_applications if total_applications > 0 else 0.0
    cost_per_hire = total_budget / total_hires if total_hires > 0 else None

    return {
        "total_campaigns": len(campaigns),
        "total_budget": total_budget,
        "total_applications": total_applications,
        "total_interviews": total_interviews,
        "total_hires": total_hires,
        "hire_rate": round(hire_rate, 4),
        "interview_rate": round(interview_rate, 4),
        "cost_per_hire": round(cost_per_hire, 2) if cost_per_hire is not None else None,
    }


def rate_platform(metrics: dict) -> tuple[str, str]:
    if not metrics:
        return ("No Data", "No historical campaigns found for this platform and role.")

    hire_rate = metrics["hire_rate"]
    cost_per_hire = metrics["cost_per_hire"]
    total_hires = metrics["total_hires"]
    interview_rate = metrics["interview_rate"]

    if total_hires == 0:
        rating = "Poor"
        reason = (
            f"Generated {metrics['total_applications']} applications across "
            f"{metrics['total_campaigns']} campaign(s) but resulted in zero hires."
        )
    elif hire_rate >= 0.03:
        rating = "Excellent"
        reason = (
            f"Hire rate of {hire_rate*100:.1f}% is above 3%. "
            f"Cost per hire: ${cost_per_hire}. "
            f"Strong interview conversion at {interview_rate*100:.1f}%."
        )
    elif hire_rate >= 0.02:
        rating = "Good"
        reason = (
            f"Hire rate of {hire_rate*100:.1f}% is between 2-3%. "
            f"Consistent performance with cost per hire of ${cost_per_hire}."
        )
    elif hire_rate >= 0.01:
        rating = "Average"
        reason = (
            f"Hire rate of {hire_rate*100:.1f}% is between 1-2%. "
            f"Below average conversion. Cost per hire: ${cost_per_hire}."
        )
    else:
        rating = "Poor"
        reason = (
            f"Hire rate of {hire_rate*100:.1f}% is below 1%. "
            f"Very low conversion despite applications coming in."
        )

    return (rating, reason)


def build_recommendations(platform_analysis: dict) -> tuple[list, list]:
    recommended = []
    avoid = []
    for platform, data in platform_analysis.items():
        if data["rating"] in ("Excellent", "Good"):
            recommended.append(platform)
        elif data["rating"] == "Poor":
            avoid.append(platform)
    return recommended, avoid


def analyze_campaign(role: str, budget: int, platforms: list[str]) -> dict:
    all_campaigns = load_campaigns()
    platform_campaigns = find_similar_campaigns(role, platforms, all_campaigns)

    total_history_found = sum(len(v) for v in platform_campaigns.values())

    platform_analysis = {}
    for platform in platforms:
        campaigns = platform_campaigns.get(platform, [])
        metrics = calculate_metrics(campaigns)
        rating, reason = rate_platform(metrics)

        platform_analysis[platform] = {
            "rating": rating,
            "hire_rate": metrics.get("hire_rate"),
            "interview_rate": metrics.get("interview_rate"),
            "cost_per_hire": metrics.get("cost_per_hire"),
            "campaigns_found": metrics.get("total_campaigns", 0),
            "reason": reason,
        }

    recommended, avoid = build_recommendations(platform_analysis)

    return {
        "role": role,
        "budget": budget,
        "history_found": total_history_found,
        "platform_analysis": platform_analysis,
        "recommended": recommended,
        "avoid": avoid,
    }


if __name__ == "__main__":
    result = analyze_campaign(
        role="Python Backend Engineer",
        budget=500,
        platforms=["LinkedIn", "Naukri", "Indeed", "Wellfound"]
    )
    print(json.dumps(result, indent=2))
