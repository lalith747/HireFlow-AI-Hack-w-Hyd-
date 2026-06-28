"""
setup_hindsight.py
Run once to load all campaign data into the Hindsight memory bank.

Prerequisites:
    pip install hindsight-client
    hindsight-api must be running (or use Hindsight Cloud URL)

Usage:
    HINDSIGHT_BASE_URL=http://localhost:8888 python setup_hindsight.py
"""

import json
import os
from hindsight_client import Hindsight

BANK_ID = "hireflow-campaigns"
CAMPAIGNS_FILE = "campaigns.json"
# from hindsight_client import Hindsight

# BANK_ID = "hireflow-campaigns"
# BASE_URL = os.environ.get("HINDSIGHT_BASE_URL", "http://localhost:8888")
from hindsight_client import Hindsight

client = Hindsight(
    base_url="https://api.hindsight.vectorize.io",
    api_key="hsk_api" )

with open(CAMPAIGNS_FILE, "r") as f:
    campaigns = json.load(f)

print(f"Loading {len(campaigns)} campaigns into Hindsight bank: '{BANK_ID}' ...")

for c in campaigns:
    content = (
        f"Campaign ID {c['campaign_id']}: Role '{c['role']}' was posted on {c['platform']}. "
        f"Budget was ${c['budget']}. It received {c['applications']} applications, "
        f"{c['interviews']} interviews, and resulted in {c['hires']} hires."
    )
    client.retain(bank_id=BANK_ID, content=content)
    print(f"  Retained campaign {c['campaign_id']}: {c['role']} / {c['platform']}")

print("\nDone. All campaigns loaded into Hindsight.")
