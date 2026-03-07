# North Sea & Baltic Crude Flow Monitor

**Real-time AIS pipeline tracking crude oil tankers in the Dated Brent pricing window and Baltic export corridor — the most commercially significant crude region for European physical traders.**

## Motivation

Physical commodity traders at firms like Glencore, Vitol, and Trafigura use vessel tracking data to inform cargo pricing, storage decisions, and timespread positioning. Commercial providers (Kpler, Vortexa) charge significant fees for global satellite-augmented AIS coverage. This project builds an open-source monitor covering the North Sea and Baltic using three complementary free AIS data sources, focused specifically on the flows that determine European crude pricing — where terrestrial AIS coverage is strongest and commercial relevance is highest.

## Key Features

- **Multi-source AIS fusion** from aisstream.io (global WebSocket), Digitraffic (Finland MQTT), and BarentsWatch (Norway SSE) — three independent data feeds deduplicated on MMSI into a unified vessel state
- **Brent basket (BFOET) loading monitor** tracking cargo activity at Sullom Voe (Brent), Hound Point (Forties), Sture (Oseberg), Mongstad (Troll/Johan Sverdrup), and Ekofisk/Teesside
- **ARA floating storage detection** with volume estimation — identifies tankers stationary and laden for >7 days in the Amsterdam-Rotterdam-Antwerp approaches
- **Baltic crude export flow tracking** from Primorsk, Ust-Luga, Gdansk, and Butinge with destination analysis and shadow fleet indicators
- **Trade implications analysis** linking AIS-derived physical flow metrics to Brent timespreads with walk-forward validation and regime-dependent correlation analysis
- **Automated weekly market intelligence reports** with commercial commentary
- **Interactive Streamlit dashboard** with live map, loading tables, storage charts, and statistical analysis views

## Architecture

```
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│ aisstream.io │  │ Digitraffic  │  │ BarentsWatch │
│  (WebSocket) │  │   (MQTT)     │  │  (SSE/REST)  │
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                 │                 │
       └─────────────────┼─────────────────┘
                         │
              ┌──────────▼──────────┐
              │    Data Fusion      │
              │  (Dedup on MMSI,    │
              │   tanker filter)    │
              └──────────┬──────────┘
                         │
              ┌──────────▼──────────┐
              │   SQLite Database   │
              │  (positions, state, │
              │   events, metrics)  │
              └──────────┬──────────┘
                         │
        ┌────────────────┼────────────────┐
        │                │                │
  ┌─────▼─────┐  ┌──────▼──────┐  ┌─────▼──────┐
  │ Tracking  │  │  Analysis   │  │ Reporting  │
  │ Modules   │  │  Engine     │  │ Generator  │
  │           │  │             │  │            │
  │• Loading  │  │• Market     │  │• Weekly    │
  │  detector │  │  data fetch │  │  markdown  │
  │• Floating │  │• Spread     │  │  reports   │
  │  storage  │  │  analysis   │  │            │
  │• Export   │  │• Walk-fwd   │  │            │
  │  tracker  │  │  backtest   │  │            │
  └─────┬─────┘  └──────┬──────┘  └─────┬──────┘
        │                │                │
        └────────────────┼────────────────┘
                         │
              ┌──────────▼──────────┐
              │  Streamlit Dashboard │
              │  (Map, Tables,      │
              │   Charts, Analysis) │
              └─────────────────────┘
```

## Combined Coverage

| Source | Coverage | Protocol | Auth |
|--------|----------|----------|------|
| **aisstream.io** | Global terrestrial — UK North Sea, English Channel, ARA approaches, Danish Straits | WebSocket | Free API key |
| **Digitraffic** | Gulf of Finland — Primorsk/Ust-Luga approaches, eastern Baltic | MQTT over WebSocket | None (open) |
| **BarentsWatch** | Norwegian EEZ — all BFOET loading terminals (Sture, Mongstad, Ekofisk), Norwegian Sea | REST + SSE | OAuth2 client credentials |

The three sources provide overlapping coverage where it matters most: the Dated Brent loading terminals and Baltic export chokepoints.

## Methodology

### Vessel Classification

AIS ship type codes 80-89 identify tankers. Vessel class is estimated from AIS-reported dimensions (length = refA + refB):

| Class | Length | Typical DWT | Est. Full Load |
|-------|--------|-------------|----------------|
| VLCC | >300m | ~300,000 | 2.0 MMbbl |
| Suezmax | 250-300m | ~150,000 | 1.0 MMbbl |
| Aframax | 220-250m | ~100,000 | 600 kbbl |
| MR | 170-220m | ~50,000 | 350 kbbl |
| Small | <170m | ~20,000 | 140 kbbl |

Laden/ballast status is inferred from the ratio of current draft to maximum observed draft:
- **Laden:** draft > 70% of max (conservative threshold accounting for AIS draft inaccuracies)
- **Ballast:** draft < 50% of max
- **Uncertain:** 50-70% (partially laden or unreliable draft data)

### Floating Storage Detection

A vessel is classified as floating storage when **all** conditions are met:

1. **Speed < 1 knot** — standard maritime definition of "at anchor" or stationary
2. **Stationary > 7 days** — filters out STS transfers (1-3 days), berth waiting (<5 days), and bunkering operations
3. **Draft > 70% of max** — vessel is laden, not empty
4. **Not at a terminal berth** — excludes vessels alongside loading/discharging

Volume is estimated as: `vessel_class_full_load_bbl * (current_draft / max_draft)`

### Loading Event Detection

Terminal loading is detected when a tanker:
1. Enters a terminal geofence (5nm radius around terminal coordinates)
2. Slows below 1 knot (alongside or at single buoy mooring)
3. Remains for >12 hours (minimum crude loading duration)
4. Departs with higher draft than arrival (confirms cargo loaded)

### Trade Implications Analysis

Three analyses with documented statistical methodology:

| Analysis | X Variable | Y Variable | Hypothesis |
|----------|-----------|-----------|------------|
| ARA Storage vs Spread | Floating storage (bbl) | Brent M1-M2 | Rising storage -> contango widens |
| Baltic Exports vs Price | Export volume (bbl) | Brent front-month | High exports -> weaker Brent |
| BFOET Loading vs Spread | Loading deviation from mean | Brent M1-M2 | Below-average loading -> backwardation |

All correlations reported with:
- **Confidence intervals** via Fisher z-transform
- **p-values** for significance testing
- **Multi-lag analysis** at 0, 1, 2, and 4 weeks
- **Walk-forward validation** (60/40 in-sample/out-of-sample split)
- **Regime analysis** (separate behaviour in contango vs backwardation)

## Trade Implications — Honest Assessment

This monitor uses free terrestrial AIS data from three complementary sources covering the North Sea and Baltic — the most commercially significant crude oil region for European physical traders. The deliberate regional focus on the Dated Brent pricing window and Baltic export corridor reflects where these data sources have strongest coverage and where the commercial relevance is highest.

The forward predictive power of AIS-derived metrics for crude timespreads is modest, which is itself an informative finding — it suggests the market prices publicly available vessel tracking information relatively quickly. Commercial providers (Kpler, Vortexa) offer global satellite-augmented AIS with proprietary algorithms and are widely used by trading desks, meaning much of this information is already reflected in prices.

**The value of this tool for a physical trading desk is not as a standalone trading signal.** It is one input alongside inventory data, term structure analysis, and fundamental S/D balances. Specifically:

1. **Brent loading monitor** provides a cross-check against published loading programmes, flagging operational delays that affect near-term physical supply
2. **ARA floating storage** tracks European crude oversupply/undersupply in near real-time — commercially actionable when it confirms or contradicts the prevailing term structure
3. **Baltic export flows** provide visibility on post-sanctions Russian crude volumes and destinations, including shadow fleet activity

A desk would combine these with proprietary data sources for a more complete picture.

## Dashboard

The Streamlit dashboard provides five views:

- **Live Map** — Interactive folium map with tankers colour-coded by status (laden/ballast/anchor/storage/terminal), terminal geofences, and anchorage zones
- **Brent Loading** — Cargo counts and volumes by BFOET grade, weekly trend charts, recent departure table
- **ARA Storage** — Floating storage time series with M1-M2 spread overlay, current anchorage occupancy, dwell time trends
- **Baltic Exports** — Export volumes by terminal, destination breakdown (Atlantic vs intra-Baltic), AIS gap/shadow fleet analysis
- **Trade Implications** — Correlation results with lag analysis, walk-forward validation, regime breakdown, current market signal

## How to Run

### 1. Prerequisites

- Python 3.10+
- API keys (free registration):
  - **aisstream.io** — register at [aisstream.io](https://aisstream.io)
  - **BarentsWatch** — register at [barentswatch.no](https://barentswatch.no), create an API client on MyPage
  - **Digitraffic** — no API key required (open access)

### 2. Setup

```bash
git clone https://github.com/amr-coding1/ais-oil-tracking.git
cd ais-oil-tracking

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure API keys
cp .env.example .env
# Edit .env with your API keys

# Copy settings template
cp config/settings.json.example config/settings.json
```

### 3. Start Data Collection

```bash
python scripts/run_collector.py
```

This starts all three AIS clients concurrently with automatic reconnection, plus background tasks for floating storage detection and metrics aggregation. The system is designed to run unattended — let it collect data for at least a few days before running analysis.

### 4. Launch Dashboard

```bash
python scripts/run_dashboard.py
# Or directly: streamlit run dashboard/app.py
```

### 5. Run Analysis

```bash
# Trade implications analysis (requires accumulated data)
python scripts/run_analysis.py

# Generate weekly report
python scripts/run_report.py
```

## Data Sources

- **aisstream.io** — Free terrestrial AIS data via WebSocket. [aisstream.io](https://aisstream.io). License: free for non-commercial use.
- **Digitraffic** — Finnish Transport Infrastructure Agency open AIS data. [digitraffic.fi](https://www.digitraffic.fi/en/marine/). License: CC BY 4.0.
- **BarentsWatch** — Norwegian Coastal Administration AIS data. [barentswatch.no](https://www.barentswatch.no/). License: free for registered API clients.

## Limitations & Future Work

- **Terrestrial AIS coverage** has lower update frequency than commercial satellite-augmented providers, with potential gaps during congestion or adverse weather
- **AIS draft data is manually entered** by crew and sometimes inaccurate or outdated — this affects laden/ballast classification and cargo volume estimates
- **Short observation period** limits statistical power of trade implications analysis — longer collection periods would improve confidence
- **No access to Platts loading programmes** for direct comparison of observed vs scheduled loadings
- **Brent M1-M2 spread proxy** — Yahoo Finance does not provide individual Brent contract months; the spread proxy uses rolling price changes. A production system would use ICE data feeds
- **Urals-Dated Brent differential** is not freely available; flat Brent price is used as proxy in Baltic export analysis
- A commercial version would integrate satellite AIS, refinery run data, pipeline flow nominations, and proprietary loading programme data

## Project Structure

```
├── config/                     # Terminal geofences, anchorage zones, vessel classes
├── src/
│   ├── ingestion/              # AIS data clients (aisstream, digitraffic, barentswatch)
│   │   └── data_fusion.py      # Multi-source deduplication and unified state
│   ├── tracking/               # Detection algorithms
│   │   ├── vessel_tracker.py   # Geofencing, region assignment, status determination
│   │   ├── floating_storage.py # Floating storage detection (>7d, laden, stationary)
│   │   ├── loading_detector.py # Terminal loading event detection (draft change)
│   │   └── export_tracker.py   # Baltic export flow tracking
│   ├── analysis/               # Market data and trade implications
│   │   ├── market_data.py      # Brent futures via yfinance
│   │   ├── statistics.py       # Correlation, walk-forward, regime analysis
│   │   └── trade_implications.py
│   ├── reporting/              # Automated report generation
│   └── database.py             # SQLite schema, queries, vessel classification
├── dashboard/app.py            # Streamlit dashboard
├── scripts/                    # Entry points (collector, analysis, report, dashboard)
├── tests/                      # Unit tests (57 tests)
└── requirements.txt
```

## Tests

```bash
python -m pytest tests/ -v
```

57 unit tests covering:
- Vessel classification (VLCC/Suezmax/Aframax/MR by length)
- Tanker detection (AIS ship type filtering)
- Laden/ballast estimation from draft ratios
- Floating storage detection algorithm (qualifying, duration filter, ballast exclusion, terminal exclusion)
- Loading event detection (arrival/departure, duration threshold, draft change)
- Data fusion deduplication (within-window filtering, cross-source merging)
- Geofencing (terminal matching, anchorage zones, regional assignment)
- Statistical methods (Pearson with CI, lagged correlation, walk-forward, regime analysis)
