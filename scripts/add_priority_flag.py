"""Add priority: true to markets.json entries that serve Cherry Road cities."""

import json

CR_PATH = "config/cherry_road_markets.json"
MARKETS_PATH = "config/markets.json"

with open(CR_PATH) as f:
    cr_markets = json.load(f)

# Get unique site_ids from CR manifest
cr_site_ids = {m["site_id"] for m in cr_markets}
print(f"Cherry Road manifest has {len(cr_markets)} city entries -> {len(cr_site_ids)} distinct site_ids")

with open(MARKETS_PATH) as f:
    markets = json.load(f)

flagged = 0
already_flagged = 0
not_in_config = []

# Build a set for fast lookup
config_sids = {m["site_id"] for m in markets}
for cr_sid in cr_site_ids:
    if cr_sid not in config_sids:
        not_in_config.append(cr_sid)

if not_in_config:
    print(f"\n[WARNING] {len(not_in_config)} CR site_ids not in markets.json:")
    for sid in sorted(not_in_config):
        print(f"  {sid}")

for m in markets:
    if m["site_id"] in cr_site_ids:
        if m.get("priority"):
            already_flagged += 1
        else:
            m["priority"] = True
            flagged += 1

print(f"\nFlagged {flagged} new markets as priority")
print(f"Already flagged: {already_flagged}")

with open(MARKETS_PATH, "w", encoding="utf-8") as f:
    json.dump(markets, f, indent=2, ensure_ascii=False)

# Verify
with open(MARKETS_PATH) as f:
    markets = json.load(f)
priority_count = sum(1 for m in markets if m.get("priority"))
print(f"\nTotal priority markets in config: {priority_count}")
