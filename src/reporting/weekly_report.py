"""Weekly market intelligence report generator.

Produces a Markdown report summarising:
    1. Brent loading activity (BFOET grades)
    2. ARA floating storage levels
    3. Baltic crude export flows
    4. Market context and trade implications
    5. Key watch items for the coming week
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.analysis.trade_implications import TradeImplicationsAnalyser
from src.tracking.export_tracker import ExportTracker
from src.tracking.floating_storage import FloatingStorageDetector

logger = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).resolve().parent.parent.parent / "reports"


class WeeklyReportGenerator:
    """Generates weekly Markdown market intelligence reports."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.storage_detector = FloatingStorageDetector(conn)
        self.export_tracker = ExportTracker(conn)
        self.analyser = TradeImplicationsAnalyser(conn)

    def generate(self) -> str:
        """Generate the full weekly report as Markdown."""
        now = datetime.now(timezone.utc)
        week_start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        week_end = now.strftime("%Y-%m-%d")

        sections = [
            self._header(week_start, week_end),
            self._brent_loading_section(),
            self._ara_storage_section(),
            self._baltic_exports_section(),
            self._market_context_section(),
            self._watch_items_section(),
            self._footer(),
        ]

        report = "\n\n".join(sections)

        # Save to file
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"weekly_report_{now.strftime('%Y%m%d')}.md"
        path = REPORTS_DIR / filename
        path.write_text(report)
        logger.info("Weekly report saved to %s", path)

        return report

    def _header(self, week_start: str, week_end: str) -> str:
        return (
            f"# North Sea & Baltic Crude Flow Monitor — Weekly Report\n\n"
            f"**Period:** {week_start} to {week_end}\n\n"
            f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"---"
        )

    def _brent_loading_section(self) -> str:
        """Section 1: Brent loading summary."""
        rows = self.conn.execute(
            """
            SELECT grade, terminal, COUNT(*) as cnt,
                   COALESCE(SUM(estimated_cargo_bbl), 0) as total_bbl
            FROM loading_events
            WHERE arrival_time >= datetime('now', '-7 days')
              AND terminal NOT IN ('Primorsk', 'Ust-Luga', 'Gdansk/Naftoport', 'Butinge')
            GROUP BY grade
            ORDER BY grade
            """
        ).fetchall()

        prev_rows = self.conn.execute(
            """
            SELECT COUNT(*) as cnt
            FROM loading_events
            WHERE arrival_time >= datetime('now', '-14 days')
              AND arrival_time < datetime('now', '-7 days')
              AND terminal NOT IN ('Primorsk', 'Ust-Luga', 'Gdansk/Naftoport', 'Butinge')
            """
        ).fetchone()
        prev_count = prev_rows["cnt"] if prev_rows else 0

        total_this_week = sum(r["cnt"] for r in rows)

        lines = ["## 1. Brent Basket Loading Summary\n"]
        lines.append(
            f"**{total_this_week} cargoes** loaded this week across BFOET grades "
            f"(vs {prev_count} last week).\n"
        )

        if rows:
            lines.append("| Grade | Terminal | Cargoes | Est. Volume (kbbl) |")
            lines.append("|-------|----------|---------|--------------------|")
            for r in rows:
                vol = r["total_bbl"] / 1000 if r["total_bbl"] else 0
                lines.append(
                    f"| {r['grade'] or 'Unknown'} | {r['terminal']} | "
                    f"{r['cnt']} | {vol:,.0f} |"
                )
        else:
            lines.append("*No loading events detected this week.*")

        # Recent notable departures
        recent = self.conn.execute(
            """
            SELECT v.name, le.terminal, le.grade, le.destination_reported,
                   le.estimated_cargo_bbl
            FROM loading_events le
            JOIN vessels v ON le.mmsi = v.mmsi
            WHERE le.departure_time >= datetime('now', '-7 days')
              AND le.terminal NOT IN ('Primorsk', 'Ust-Luga', 'Gdansk/Naftoport', 'Butinge')
            ORDER BY le.departure_time DESC
            LIMIT 5
            """
        ).fetchall()

        if recent:
            lines.append("\n**Recent departures:**")
            for r in recent:
                cargo = f"~{r['estimated_cargo_bbl']/1000:.0f} kbbl" if r["estimated_cargo_bbl"] else "unknown vol"
                lines.append(
                    f"- {r['name']}: {r['grade'] or '?'} from {r['terminal']} "
                    f"→ {r['destination_reported'] or 'unknown dest'} ({cargo})"
                )

        return "\n".join(lines)

    def _ara_storage_section(self) -> str:
        """Section 2: ARA floating storage."""
        summary = self.storage_detector.get_storage_summary()
        ara = summary.get("ARA", {"total_bbl": 0, "vessel_count": 0})
        total_bbl = ara.get("total_bbl", 0) if isinstance(ara, dict) else 0
        vessel_count = ara.get("vessel_count", 0) if isinstance(ara, dict) else 0
        mmbbl = total_bbl / 1_000_000

        # Week-over-week comparison from daily metrics
        wow_change = self._get_ara_storage_change()

        lines = ["## 2. ARA Floating Storage\n"]
        lines.append(
            f"**{mmbbl:.1f} MMbbl** in floating storage at ARA "
            f"({vessel_count} vessels)."
        )

        if wow_change is not None:
            direction = "up" if wow_change > 0 else "down"
            lines.append(
                f"Week-over-week: **{direction} {abs(wow_change)/1_000_000:.1f} MMbbl**."
            )

        # Current vessels at anchor
        anchored = self.conn.execute(
            """
            SELECT vs.mmsi, v.name, v.vessel_class, vs.draft_m,
                   vs.status_since, vs.status,
                   ROUND(julianday('now') - julianday(vs.status_since), 1) as days_stationary
            FROM vessel_state vs
            JOIN vessels v ON vs.mmsi = v.mmsi
            WHERE vs.region LIKE 'anchorage:%'
              AND vs.sog < 1.0
            ORDER BY days_stationary DESC
            LIMIT 10
            """
        ).fetchall()

        if anchored:
            lines.append("\n**Vessels at ARA anchorage:**\n")
            lines.append("| Vessel | Class | Days | Status |")
            lines.append("|--------|-------|------|--------|")
            for r in anchored:
                lines.append(
                    f"| {r['name'] or 'Unknown'} | {r['vessel_class'] or '?'} | "
                    f"{r['days_stationary']:.1f} | {r['status']} |"
                )

        return "\n".join(lines)

    def _baltic_exports_section(self) -> str:
        """Section 3: Baltic crude exports."""
        volumes = self.export_tracker.get_export_volume_by_terminal(days=7)
        destinations = self.export_tracker.get_destination_breakdown(days=7)
        ais_gaps = self.export_tracker.get_ais_gap_vessels(hours_threshold=48)

        total_departures = sum(v["departures"] for v in volumes.values())
        total_bbl = sum(v["total_bbl"] for v in volumes.values())

        lines = ["## 3. Baltic Crude Export Flows\n"]
        lines.append(
            f"**{total_departures} laden departures** this week, "
            f"estimated **{total_bbl/1_000_000:.1f} MMbbl** total.\n"
        )

        if volumes:
            lines.append("| Terminal | Departures | Est. Volume (MMbbl) |")
            lines.append("|----------|------------|---------------------|")
            for terminal, data in volumes.items():
                lines.append(
                    f"| {terminal} | {data['departures']} | "
                    f"{data['total_bbl']/1_000_000:.2f} |"
                )

        lines.append(f"\n**Destination breakdown:** {destinations}")

        if ais_gaps:
            lines.append(
                f"\n**AIS gap alert:** {len(ais_gaps)} vessel(s) loaded at "
                "Russian terminals and subsequently went dark (>48h no signal):"
            )
            for v in ais_gaps[:5]:
                lines.append(
                    f"- {v['name']} (MMSI {v['mmsi']}): loaded at {v['terminal']}, "
                    f"last seen {v['hours_since_last']:.0f}h ago"
                )

        return "\n".join(lines)

    def _market_context_section(self) -> str:
        """Section 4: Market context and trade implications."""
        signal = self.analyser._current_signal(
            self.analyser._load_daily_metrics(self.conn) if hasattr(self.analyser, '_load_daily_metrics') else _load_daily_metrics_standalone(self.conn)
        )

        lines = ["## 4. Market Context\n"]
        commentary = signal.get("commentary", "Insufficient data.")
        lines.append(commentary)

        brent = signal.get("brent_m1")
        spread = signal.get("brent_m1_m2_spread")
        if brent:
            lines.append(f"\n- **Brent front-month:** ${brent:.2f}/bbl")
        if spread:
            lines.append(f"- **M1-M2 spread:** ${spread:.2f}/bbl")

        return "\n".join(lines)

    def _watch_items_section(self) -> str:
        """Section 5: Key watch items for next week."""
        lines = ["## 5. Key Watch Items\n"]

        # Check for unusual patterns
        items = []

        # High floating storage
        storage = self.storage_detector.get_total_storage_bbl("ARA")
        if storage > 5_000_000:
            items.append(
                f"ARA floating storage elevated at {storage/1_000_000:.1f} MMbbl — "
                "monitor for discharge activity or further accumulation."
            )

        # Check pending terminal visits
        active_at_terminal = self.conn.execute(
            """
            SELECT COUNT(*) as cnt FROM vessel_state
            WHERE status = 'at_terminal'
            """
        ).fetchone()
        if active_at_terminal and active_at_terminal["cnt"] > 0:
            items.append(
                f"{active_at_terminal['cnt']} vessel(s) currently at terminals — "
                "watch for departure events and draft changes."
            )

        if not items:
            items.append(
                "No unusual patterns detected. Continue monitoring Brent loading "
                "cadence and ARA anchorage occupancy."
            )

        for i, item in enumerate(items, 1):
            lines.append(f"{i}. {item}")

        return "\n".join(lines)

    def _footer(self) -> str:
        return (
            "---\n\n"
            "*This report is generated from free terrestrial AIS data "
            "(aisstream.io, Digitraffic, BarentsWatch). Coverage gaps and "
            "AIS data inaccuracies mean these figures are indicative, not definitive. "
            "See project README for full methodology and limitations.*"
        )

    def _get_ara_storage_change(self) -> float | None:
        """Get week-over-week change in ARA floating storage."""
        rows = self.conn.execute(
            """
            SELECT floating_storage_ara_bbl
            FROM daily_metrics
            WHERE date >= date('now', '-14 days')
            ORDER BY date
            """
        ).fetchall()
        if len(rows) < 8:
            return None
        recent = rows[-1]["floating_storage_ara_bbl"] or 0
        prior = rows[-8]["floating_storage_ara_bbl"] or 0
        return recent - prior


def _load_daily_metrics_standalone(conn: sqlite3.Connection):
    """Standalone loader for when analyser method isn't accessible."""
    import pandas as pd
    df = pd.read_sql_query("SELECT * FROM daily_metrics ORDER BY date", conn)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
    return df
