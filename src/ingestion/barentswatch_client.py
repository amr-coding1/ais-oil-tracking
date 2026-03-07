"""REST/SSE client for BarentsWatch (Norway) AIS data."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import requests
import sseclient

logger = logging.getLogger(__name__)

TOKEN_URL = "https://id.barentswatch.no/connect/token"
SSE_URL = "https://live.ais.barentswatch.no/v1/sse/combined"
LATEST_URL = "https://live.ais.barentswatch.no/v1/latest/combined"
HISTORIC_URL = "https://historic.ais.barentswatch.no/v1/historic/trackslast24hours"


class BarentsWatchClient:
    """Connects to BarentsWatch SSE stream for Norwegian AIS data."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        on_position: Callable[..., Any] | None = None,
        on_metadata: Callable[..., Any] | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.on_position = on_position
        self.on_metadata = on_metadata
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._running = False

    def _get_token(self) -> str:
        """Obtain or refresh OAuth2 access token."""
        if self._token and time.time() < self._token_expiry - 60:
            return self._token

        logger.info("Requesting BarentsWatch access token...")
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "ais",
                "grant_type": "client_credentials",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + data.get("expires_in", 3600)
        logger.info("BarentsWatch token acquired")
        return self._token  # type: ignore[return-value]

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Accept": "text/event-stream",
        }

    def start(self) -> None:
        """Start the SSE stream (blocking — call from a thread)."""
        self._running = True
        reconnect_delay = 1.0

        while self._running:
            try:
                self._stream()
            except (requests.RequestException, OSError, StopIteration) as exc:
                if not self._running:
                    break
                logger.warning(
                    "BarentsWatch connection lost: %s. Reconnecting in %.0fs",
                    exc, reconnect_delay,
                )
                time.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60.0)

    def stop(self) -> None:
        self._running = False

    def _stream(self) -> None:
        logger.info("Connecting to BarentsWatch SSE stream...")
        resp = requests.get(
            SSE_URL,
            headers=self._headers(),
            stream=True,
            timeout=(10, None),
        )
        resp.raise_for_status()
        logger.info("BarentsWatch SSE stream connected")

        client = sseclient.SSEClient(resp)
        for event in client.events():
            if not self._running:
                break
            if not event.data:
                continue
            try:
                data = json.loads(event.data)
            except json.JSONDecodeError:
                continue
            self._handle_message(data)

    def fetch_latest(self) -> list[dict[str, Any]]:
        """Fetch snapshot of all latest vessel positions (REST)."""
        headers = {
            "Authorization": f"Bearer {self._get_token()}",
            "Accept": "application/json",
        }
        resp = requests.get(LATEST_URL, headers=headers, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def fetch_historic_track(self, mmsi: int) -> list[dict[str, Any]]:
        """Fetch last 24h track for a vessel."""
        headers = {
            "Authorization": f"Bearer {self._get_token()}",
            "Accept": "application/json",
        }
        resp = requests.get(
            f"{HISTORIC_URL}/{mmsi}",
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def _handle_message(self, data: dict[str, Any]) -> None:
        mmsi = data.get("mmsi")
        if not mmsi:
            return

        # BarentsWatch combined endpoint provides both position and metadata
        timestamp = data.get("msgtime")
        if not timestamp:
            timestamp = datetime.now(timezone.utc).isoformat()

        if self.on_position:
            lat = data.get("latitude")
            lon = data.get("longitude")
            if lat is not None and lon is not None:
                self.on_position(
                    mmsi=mmsi,
                    timestamp=timestamp,
                    latitude=lat,
                    longitude=lon,
                    sog=data.get("speedOverGround"),
                    cog=data.get("courseOverGround"),
                    heading=data.get("trueHeading"),
                    nav_status=data.get("navigationalStatus"),
                    source="barentswatch",
                )

        if self.on_metadata:
            draft_m = data.get("draught")
            # BarentsWatch reports draught in metres directly
            self.on_metadata(
                mmsi=mmsi,
                imo=data.get("imo"),
                name=(data.get("name") or "").strip(),
                callsign=(data.get("callSign") or "").strip(),
                ship_type=data.get("shipType"),
                length_m=data.get("shipLength"),
                beam_m=data.get("shipWidth"),
                draft_m=draft_m,
                destination=(data.get("destination") or "").strip(),
                eta=data.get("eta"),
                source="barentswatch",
            )
