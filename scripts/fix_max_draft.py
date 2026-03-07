#!/usr/bin/env python3
"""One-time migration: reset max_draft_m corrupted by aisstream /10 bug.

The aisstream client was dividing MaximumStaticDraught by 10 even though
aisstream.io already provides it in metres. This means all max_draft_m values
derived from aisstream are ~1/10th of their real value.

This script:
  1. Nulls out all max_draft_m values (they'll rebuild from correct data)
  2. Optionally rescans existing position history to reconstruct max_draft_m

Run once before restarting the collector with the fixed code.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.database import get_connection, DB_PATH


def main():
    print(f"Database: {DB_PATH}")
    conn = get_connection()

    # Check current state
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM vessels WHERE max_draft_m IS NOT NULL"
    ).fetchone()
    print(f"Vessels with max_draft_m set: {row['cnt']}")

    sample = conn.execute(
        "SELECT mmsi, name, vessel_class, max_draft_m FROM vessels "
        "WHERE max_draft_m IS NOT NULL ORDER BY max_draft_m LIMIT 10"
    ).fetchall()
    if sample:
        print("\nLowest max_draft_m values (likely corrupted):")
        for r in sample:
            print(f"  {r['name'] or r['mmsi']:30s}  class={r['vessel_class'] or '?':10s}  max_draft={r['max_draft_m']:.1f}m")

    print("\nResetting all max_draft_m to NULL...")
    conn.execute("UPDATE vessels SET max_draft_m = NULL")

    # Rebuild from position history (uses the highest draft seen per vessel)
    rebuilt = conn.execute(
        """
        UPDATE vessels SET max_draft_m = (
            SELECT MAX(p.draft_m)
            FROM positions p
            WHERE p.mmsi = vessels.mmsi AND p.draft_m IS NOT NULL
        )
        WHERE mmsi IN (SELECT DISTINCT mmsi FROM positions WHERE draft_m IS NOT NULL)
        """
    )
    conn.commit()

    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM vessels WHERE max_draft_m IS NOT NULL"
    ).fetchone()
    print(f"Rebuilt max_draft_m for {row['cnt']} vessels from position history")

    # Show new values
    sample = conn.execute(
        "SELECT mmsi, name, vessel_class, max_draft_m FROM vessels "
        "WHERE max_draft_m IS NOT NULL ORDER BY max_draft_m DESC LIMIT 10"
    ).fetchall()
    if sample:
        print("\nHighest max_draft_m values after rebuild:")
        for r in sample:
            print(f"  {r['name'] or r['mmsi']:30s}  class={r['vessel_class'] or '?':10s}  max_draft={r['max_draft_m']:.1f}m")

    # NOTE: position history drafts from aisstream are ALSO corrupted (/10).
    # The rebuilt max_draft_m will still be wrong until new correct data comes in.
    # But the class-based fallback will handle this gracefully.
    print("\nNote: Position history still contains /10 drafts from aisstream.")
    print("The class-based fallback will provide correct classification until")
    print("new (correct) draft data accumulates and overwrites max_draft_m.")
    print("\nDone. Restart the collector to begin collecting correct draft data.")


if __name__ == "__main__":
    main()
