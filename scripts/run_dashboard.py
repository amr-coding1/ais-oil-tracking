#!/usr/bin/env python3
"""Launch the Streamlit dashboard."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

DASHBOARD_PATH = Path(__file__).resolve().parent.parent / "dashboard" / "app.py"


def main():
    subprocess.run(
        [sys.executable, "-m", "streamlit", "run", str(DASHBOARD_PATH),
         "--server.headless", "true"],
        cwd=str(DASHBOARD_PATH.parent.parent),
    )


if __name__ == "__main__":
    main()
