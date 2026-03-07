"""Streamlit dashboard for North Sea & Baltic Crude Flow Monitor."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf

from src.database import (
    get_all_vessel_states,
    get_active_floating_storage,
    get_connection,
    get_recent_loading_events,
    get_tanker_count,
    load_terminals,
)
from src.tracking.export_tracker import ExportTracker
from src.tracking.floating_storage import FloatingStorageDetector
from src.analysis.trade_implications import TradeImplicationsAnalyser

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="North Sea & Baltic Crude Flow Monitor",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Auto-refresh every 60 seconds
st.markdown(
    '<meta http-equiv="refresh" content="60">',
    unsafe_allow_html=True,
)


@st.cache_resource
def get_db():
    return get_connection()


conn = get_db()

# ---------------------------------------------------------------------------
# Live oil prices (cached 60s)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60)
def get_live_prices() -> dict:
    """Fetch live Brent and WTI spot prices."""
    prices = {}
    for ticker, label in [("BZ=F", "Brent"), ("CL=F", "WTI")]:
        try:
            t = yf.Ticker(ticker)
            data = t.fast_info
            price = data.get("lastPrice") or data.get("last_price")
            prev = data.get("previousClose") or data.get("previous_close")
            if price:
                prices[label] = {
                    "price": price,
                    "change": price - prev if prev else None,
                    "pct": ((price - prev) / prev * 100) if prev else None,
                }
        except Exception:
            pass
    return prices


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

st.sidebar.title("Crude Flow Monitor")
st.sidebar.markdown("North Sea & Baltic Region")

# Live prices in sidebar
prices = get_live_prices()
if prices:
    st.sidebar.markdown("### Live Crude Prices")
    for label, data in prices.items():
        delta_str = f"{data['change']:+.2f} ({data['pct']:+.1f}%)" if data["change"] is not None else None
        st.sidebar.metric(
            label,
            f"${data['price']:.2f}",
            delta=delta_str,
        )
    st.sidebar.markdown("---")

page = st.sidebar.radio(
    "View",
    ["Live Map", "Brent Loading", "ARA Storage", "Baltic Exports", "Trade Implications"],
)

st.sidebar.markdown("---")
tanker_count = get_tanker_count(conn)
st.sidebar.metric("Tankers Tracked", tanker_count)

# ---------------------------------------------------------------------------
# Status colour mapping
# ---------------------------------------------------------------------------

STATUS_COLOURS = {
    "moving_ballast": "#2ecc71",
    "moving_laden": "#e74c3c",
    "at_anchor": "#f39c12",
    "floating_storage": "#c0392b",
    "at_terminal": "#3498db",
    "stationary": "#95a5a6",
    "moving_uncertain": "#bdc3c7",
}

STATUS_LABELS = {
    "moving_ballast": "Moving (Ballast)",
    "moving_laden": "Moving (Laden)",
    "at_anchor": "At Anchorage",
    "floating_storage": "Floating Storage",
    "at_terminal": "At Terminal",
    "stationary": "Stationary",
    "moving_uncertain": "Moving (Unknown Load)",
}

# Marker sizes by vessel class for visual hierarchy
CLASS_SIZES = {
    "VLCC": 12,
    "Suezmax": 10,
    "Aframax": 8,
    "MR": 6,
    "Small": 5,
    "Unknown": 4,
}

# ---------------------------------------------------------------------------
# Page: Live Map  (tankers only, fully interactive)
# ---------------------------------------------------------------------------

def render_live_map():
    st.title("Live Tanker Map")

    # Live prices banner at top
    if prices:
        cols = st.columns(len(prices) + 2)
        for i, (label, data) in enumerate(prices.items()):
            delta = f"{data['change']:+.2f} ({data['pct']:+.1f}%)" if data["change"] is not None else None
            cols[i].metric(label, f"${data['price']:.2f}", delta=delta)

    st.caption("Oil tankers only (AIS type 80-89) -- scroll to zoom, drag to pan, click vessels for details")

    # Query ONLY tankers from the database
    vessels = conn.execute(
        """
        SELECT vs.*, v.name, v.imo, v.vessel_class, v.max_draft_m, v.ship_type
        FROM vessel_state vs
        JOIN vessels v ON vs.mmsi = v.mmsi
        WHERE v.ship_type BETWEEN 80 AND 89
          AND vs.latitude IS NOT NULL
          AND vs.longitude IS NOT NULL
        """
    ).fetchall()

    if not vessels:
        st.info("No tanker data yet. Start the collector and wait a few minutes.")
        return

    # Build dataframe
    rows = []
    for v in vessels:
        status = v["status"] or "moving_uncertain"
        vc = v["vessel_class"] or "Unknown"
        rows.append({
            "lat": v["latitude"],
            "lon": v["longitude"],
            "name": v["name"] or f"MMSI {v['mmsi']}",
            "mmsi": v["mmsi"],
            "imo": v["imo"] or "N/A",
            "vessel_class": vc,
            "sog": round(v["sog"], 1) if v["sog"] is not None else None,
            "sog_str": f"{v['sog']:.1f} kn" if v["sog"] is not None else "N/A",
            "draft": v["draft_m"],
            "draft_str": f"{v['draft_m']:.1f}m" if v["draft_m"] is not None else "N/A",
            "destination": v["destination"] or "N/A",
            "status_key": status,
            "status": STATUS_LABELS.get(status, status),
            "colour": STATUS_COLOURS.get(status, "#bdc3c7"),
            "size": CLASS_SIZES.get(vc, 5),
            "region": v["region"] or "N/A",
        })

    df = pd.DataFrame(rows)

    # Summary stats
    col1, col2, col3, col4, col5 = st.columns(5)
    status_counts = df["status"].value_counts()
    col1.metric("Tankers on Map", len(df))
    col2.metric("Moving (Laden)", status_counts.get("Moving (Laden)", 0))
    col3.metric("Moving (Ballast)", status_counts.get("Moving (Ballast)", 0))
    col4.metric("At Terminal", status_counts.get("At Terminal", 0))
    col5.metric("At Anchorage", status_counts.get("At Anchorage", 0))

    # Legend
    legend_parts = [
        f'<span style="color:{c};">&#11044;</span> {STATUS_LABELS[s]}'
        for s, c in STATUS_COLOURS.items()
    ]
    st.markdown(" &nbsp;&nbsp; ".join(legend_parts), unsafe_allow_html=True)

    # Terminal markers
    terminals = load_terminals().get("terminals", {})
    terminal_rows = [
        {"lat": t["latitude"], "lon": t["longitude"],
         "name": t["name"], "grade": t["grade"]}
        for t in terminals.values()
    ]
    terminal_df = pd.DataFrame(terminal_rows)

    # Build interactive map — one trace per status for proper legend
    fig = go.Figure()

    for status_key, colour in STATUS_COLOURS.items():
        label = STATUS_LABELS[status_key]
        subset = df[df["status_key"] == status_key]
        if subset.empty:
            continue
        fig.add_trace(go.Scattermapbox(
            lat=subset["lat"],
            lon=subset["lon"],
            mode="markers",
            marker=dict(
                size=subset["size"],
                color=colour,
                opacity=0.9,
            ),
            text=subset.apply(
                lambda r: (
                    f"<b>{r['name']}</b><br>"
                    f"MMSI: {r['mmsi']}<br>"
                    f"IMO: {r['imo']}<br>"
                    f"Class: {r['vessel_class']}<br>"
                    f"Speed: {r['sog_str']}<br>"
                    f"Draft: {r['draft_str']}<br>"
                    f"Dest: {r['destination']}<br>"
                    f"Status: {r['status']}<br>"
                    f"Region: {r['region']}"
                ),
                axis=1,
            ),
            hoverinfo="text",
            name=f"{label} ({len(subset)})",
        ))

    # Terminal markers (diamonds)
    if not terminal_df.empty:
        fig.add_trace(go.Scattermapbox(
            lat=terminal_df["lat"],
            lon=terminal_df["lon"],
            mode="markers+text",
            marker=dict(size=14, color="#1a1a2e", symbol="square"),
            text=terminal_df["name"],
            textposition="top center",
            textfont=dict(size=10, color="#1a1a2e"),
            hovertext=terminal_df.apply(
                lambda r: f"<b>{r['name']}</b><br>Grade: {r['grade']}", axis=1
            ),
            hoverinfo="text",
            name="Terminals",
        ))

    fig.update_layout(
        mapbox=dict(
            style="carto-positron",
            center=dict(lat=58.0, lon=8.0),
            zoom=4,
        ),
        margin=dict(l=0, r=0, t=0, b=0),
        height=700,
        legend=dict(
            x=0.01, y=0.99,
            bgcolor="rgba(255,255,255,0.85)",
            bordercolor="#ccc",
            borderwidth=1,
            font=dict(size=11),
        ),
    )

    st.plotly_chart(fig, use_container_width=True, config={
        "scrollZoom": True,
        "displayModeBar": True,
        "modeBarButtonsToAdd": ["zoom2d", "pan2d", "resetScale2d"],
    })

    # Vessel class breakdown
    col1, col2 = st.columns(2)
    with col1:
        class_counts = df["vessel_class"].value_counts().reset_index()
        class_counts.columns = ["Class", "Count"]
        fig = px.bar(class_counts, x="Class", y="Count", color="Class",
                     title="Tanker Fleet by Class")
        fig.update_layout(height=300, showlegend=False)
        st.plotly_chart(fig, use_container_width=True)
    with col2:
        region_counts = df["region"].value_counts().head(10).reset_index()
        region_counts.columns = ["Region", "Count"]
        fig = px.bar(region_counts, x="Region", y="Count", color="Region",
                     title="Tankers by Region (Top 10)")
        fig.update_layout(height=300, showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

    # Vessel table
    with st.expander("Full Vessel Details Table"):
        st.dataframe(
            df[["name", "mmsi", "imo", "vessel_class", "sog_str", "draft_str",
                "destination", "status", "region"]].rename(columns={
                    "sog_str": "speed", "draft_str": "draft",
                }),
            use_container_width=True,
            height=400,
        )


# ---------------------------------------------------------------------------
# Page: Brent Loading
# ---------------------------------------------------------------------------

def render_brent_loading():
    st.title("Brent Basket Loading Monitor")
    st.caption("BFOET grades -- Sullom Voe, Hound Point, Sture, Mongstad, Ekofisk/Teesside")

    days = st.slider("Look-back period (days)", 7, 90, 30)
    events = get_recent_loading_events(conn, days=days)

    if not events:
        st.info("No loading events detected yet. Data will appear once the collector has been running and vessels depart terminals.")
        return

    df = pd.DataFrame([dict(r) for r in events])

    if "grade" in df.columns:
        summary = df.groupby("grade").agg(
            cargoes=("grade", "count"),
            total_bbl=("estimated_cargo_bbl", "sum"),
        ).reset_index()
        summary["total_kbbl"] = summary["total_bbl"].fillna(0) / 1000

        col1, col2 = st.columns(2)
        with col1:
            fig = px.bar(summary, x="grade", y="cargoes", color="grade",
                         title="Cargo Count by BFOET Grade")
            st.plotly_chart(fig, use_container_width=True)
        with col2:
            fig = px.pie(summary, values="total_kbbl", names="grade",
                         title="Estimated Volume (kbbl)")
            st.plotly_chart(fig, use_container_width=True)

    if "arrival_time" in df.columns:
        df["week"] = pd.to_datetime(df["arrival_time"]).dt.isocalendar().week
        weekly = df.groupby(["week", "grade"]).size().reset_index(name="count")
        fig = px.bar(weekly, x="week", y="count", color="grade",
                     title="Weekly Cargo Count by Grade", barmode="stack")
        st.plotly_chart(fig, use_container_width=True)

    st.subheader("Recent Loading Events")
    display_cols = ["name", "terminal", "grade", "arrival_time", "departure_time",
                    "draft_arrival_m", "draft_departure_m", "estimated_cargo_bbl",
                    "destination_reported"]
    available = [c for c in display_cols if c in df.columns]
    st.dataframe(df[available].head(20), use_container_width=True)


# ---------------------------------------------------------------------------
# Page: ARA Storage
# ---------------------------------------------------------------------------

def render_ara_storage():
    st.title("ARA Floating Storage & Congestion")
    st.caption("Amsterdam-Rotterdam-Antwerp approaches")

    detector = FloatingStorageDetector(conn)
    summary = detector.get_storage_summary()

    ara = summary.get("ARA", {"total_bbl": 0, "vessel_count": 0})
    if not isinstance(ara, dict):
        ara = {"total_bbl": 0, "vessel_count": 0}
    total_all = sum(v["total_bbl"] for v in summary.values() if isinstance(v, dict))

    col1, col2, col3 = st.columns(3)
    col1.metric("ARA Floating Storage", f"{ara.get('total_bbl', 0) / 1_000_000:.1f} MMbbl")
    col2.metric("ARA Vessels", ara.get("vessel_count", 0))
    col3.metric("Total All Regions", f"{total_all / 1_000_000:.1f} MMbbl")

    # Time series
    metrics_df = pd.read_sql_query(
        """SELECT date, floating_storage_ara_bbl, brent_m1_m2_spread, brent_m1
           FROM daily_metrics ORDER BY date""",
        conn,
    )
    if not metrics_df.empty and metrics_df["brent_m1"].notna().any():
        st.subheader("Brent Front-Month Price")
        fig = px.line(metrics_df.dropna(subset=["brent_m1"]),
                      x="date", y="brent_m1", title="Brent M1 ($/bbl)")
        st.plotly_chart(fig, use_container_width=True)

    if not metrics_df.empty and metrics_df["floating_storage_ara_bbl"].notna().any():
        st.subheader("ARA Floating Storage vs Brent M1-M2 Spread")
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=metrics_df["date"],
            y=metrics_df["floating_storage_ara_bbl"] / 1_000_000,
            name="ARA Storage (MMbbl)", yaxis="y",
        ))
        fig.add_trace(go.Scatter(
            x=metrics_df["date"],
            y=metrics_df["brent_m1_m2_spread"],
            name="Brent M1-M2 ($/bbl)", yaxis="y2",
            line=dict(dash="dash"),
        ))
        fig.update_layout(
            yaxis=dict(title="Floating Storage (MMbbl)"),
            yaxis2=dict(title="M1-M2 Spread ($/bbl)", overlaying="y", side="right"),
            legend=dict(x=0, y=1.1, orientation="h"),
        )
        st.plotly_chart(fig, use_container_width=True)

    active = get_active_floating_storage(conn, region="ARA")
    if active:
        st.subheader("Active Floating Storage Vessels")
        fs_df = pd.DataFrame([dict(r) for r in active])
        display = ["name", "vessel_class", "estimated_cargo_bbl", "start_time", "region"]
        available = [c for c in display if c in fs_df.columns]
        st.dataframe(fs_df[available], use_container_width=True)
    else:
        st.info("No active floating storage events detected at ARA. "
                "Detection requires vessels stationary and laden for >7 days.")


# ---------------------------------------------------------------------------
# Page: Baltic Exports
# ---------------------------------------------------------------------------

def render_baltic_exports():
    st.title("Baltic Crude Export Flows")
    st.caption("Primorsk, Ust-Luga, Gdansk, Butinge")

    tracker = ExportTracker(conn)
    days = st.slider("Look-back period (days)", 7, 90, 30, key="baltic_days")

    volumes = tracker.get_export_volume_by_terminal(days=days)
    destinations = tracker.get_destination_breakdown(days=days)

    total_dep = sum(v["departures"] for v in volumes.values())
    total_bbl = sum(v["total_bbl"] for v in volumes.values())

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Departures", total_dep)
    col2.metric("Est. Volume", f"{total_bbl / 1_000_000:.1f} MMbbl")
    col3.metric("Avg Daily (mb/d)", f"{total_bbl / max(days, 1) / 1_000_000:.2f}")

    if volumes:
        vol_df = pd.DataFrame([
            {"Terminal": k, "Departures": v["departures"],
             "Volume (MMbbl)": v["total_bbl"] / 1_000_000}
            for k, v in volumes.items()
        ])
        col1, col2 = st.columns(2)
        with col1:
            fig = px.bar(vol_df, x="Terminal", y="Volume (MMbbl)",
                         title="Export Volume by Terminal", color="Terminal")
            st.plotly_chart(fig, use_container_width=True)
        with col2:
            dest_data = [{"Dest": k, "Count": v} for k, v in destinations.items() if v > 0]
            if dest_data:
                fig = px.pie(pd.DataFrame(dest_data),
                             values="Count", names="Dest",
                             title="Destination Breakdown")
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("No destination data yet")
    else:
        st.info("No Baltic export departures detected yet.")

    st.subheader("Shadow Fleet / AIS Gap Analysis")
    gaps = tracker.get_ais_gap_vessels(hours_threshold=48)
    if gaps:
        gap_df = pd.DataFrame(gaps)
        st.warning(f"{len(gaps)} vessel(s) went dark after loading at Russian terminals")
        st.dataframe(gap_df, use_container_width=True)
    else:
        st.success("No AIS gaps detected (>48h) for vessels departing Russian terminals")


# ---------------------------------------------------------------------------
# Page: Trade Implications
# ---------------------------------------------------------------------------

def render_trade_implications():
    st.title("Trade Implications Analysis")
    st.caption("Linking physical AIS flows to Brent timespreads")

    # Show live prices at top
    if prices:
        cols = st.columns(len(prices))
        for i, (label, data) in enumerate(prices.items()):
            delta = f"{data['change']:+.2f} ({data['pct']:+.1f}%)" if data["change"] is not None else None
            cols[i].metric(label, f"${data['price']:.2f}", delta=delta)

    # Show data availability status
    metrics_count = conn.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0]
    ara_count = conn.execute(
        "SELECT COUNT(*) FROM daily_metrics WHERE floating_storage_ara_bbl IS NOT NULL"
    ).fetchone()[0]
    baltic_count = conn.execute(
        "SELECT COUNT(*) FROM daily_metrics WHERE baltic_export_volume_bbl IS NOT NULL"
    ).fetchone()[0]

    with st.expander("Data Collection Status", expanded=(metrics_count < 14)):
        col1, col2, col3 = st.columns(3)
        col1.metric("Daily Metrics Rows", metrics_count, help="Need 14+ for analysis")
        col2.metric("Days with ARA Storage Data", ara_count, help="Needs 7+ days of stationary laden tankers")
        col3.metric("Days with Baltic Export Data", baltic_count, help="Needs tanker departures from Baltic terminals")
        if metrics_count < 14:
            st.info(
                "The collector needs to run for **at least 2 weeks** to accumulate enough data. "
                "AIS-derived metrics (floating storage, exports, loadings) populate gradually as "
                "vessel events are detected — storage needs 7+ days, loading cycles take 12-48 hours."
            )

    analyser = TradeImplicationsAnalyser(conn)
    results = analyser.run_all()

    if "error" in results:
        st.warning(results["error"])
        return

    signal = results.get("current_signal", {})
    if signal and "commentary" in signal:
        st.subheader("Current Market Signal")
        st.info(signal["commentary"])
        cols = st.columns(3)
        if signal.get("brent_m1"):
            cols[0].metric("Brent M1", f"${signal['brent_m1']:.2f}")
        if signal.get("brent_m1_m2_spread") is not None:
            cols[1].metric("M1-M2 Spread", f"${signal['brent_m1_m2_spread']:.2f}")
        if signal.get("ara_storage_percentile") is not None:
            cols[2].metric("ARA Storage %ile", f"{signal['ara_storage_percentile']:.0f}th")

    for i, (key, title) in enumerate([
        ("ara_storage_vs_spread", "1. ARA Floating Storage vs Brent M1-M2 Spread"),
        ("baltic_exports_vs_price", "2. Baltic Export Volumes vs Brent Price"),
        ("brent_loading_vs_spread", "3. Brent Loading Activity vs Term Structure"),
    ], 1):
        st.subheader(title)
        result = results.get(key, {})
        if "limitation" in result:
            st.caption(f"Note: {result['limitation']}")
        _render_analysis_result(result)

    st.markdown("---")
    st.markdown(
        "**Methodology:** Pearson correlation with Fisher z-transform confidence intervals. "
        "Walk-forward validation splits data 60/40 (in-sample/out-of-sample). "
        "Regime analysis separates contango (M1-M2 < 0) from backwardation (M1-M2 > 0) periods."
    )


def _render_analysis_result(result: dict):
    if "error" in result:
        st.warning(result["error"])
        return
    if "description" in result:
        st.markdown(result["description"])

    lags = result.get("multi_lag", [])
    if lags:
        lag_df = pd.DataFrame(lags)
        st.dataframe(lag_df, use_container_width=True)

        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=[f"Lag {r['lag_days']}d" for r in lags],
            y=[r["correlation"] for r in lags],
            marker_color=["#2ecc71" if r["significant"] else "#95a5a6" for r in lags],
            text=[f"r={r['correlation']:.3f}<br>p={r['p_value']:.3f}" for r in lags],
        ))
        fig.update_layout(
            title="Correlation by Lag (green = significant at p<0.05)",
            yaxis_title="Pearson r", height=300,
        )
        st.plotly_chart(fig, use_container_width=True)

    wf = result.get("walk_forward", {})
    if wf and "error" not in wf:
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**In-sample:**")
            if wf.get("in_sample"):
                st.json(wf["in_sample"])
        with col2:
            st.markdown("**Out-of-sample:**")
            if wf.get("out_of_sample"):
                st.json(wf["out_of_sample"])

    regime = result.get("regime", {})
    if regime:
        st.markdown("**Regime analysis (contango vs backwardation):**")
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("*Contango:*")
            st.json(regime.get("contango", {}))
        with col2:
            st.markdown("*Backwardation:*")
            st.json(regime.get("backwardation", {}))


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

if page == "Live Map":
    render_live_map()
elif page == "Brent Loading":
    render_brent_loading()
elif page == "ARA Storage":
    render_ara_storage()
elif page == "Baltic Exports":
    render_baltic_exports()
elif page == "Trade Implications":
    render_trade_implications()
