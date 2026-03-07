"""WebSocket client for aisstream.io AIS data."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import websockets
import websockets.exceptions

logger = logging.getLogger(__name__)

AISSTREAM_WS_URL = "wss://stream.aisstream.io/v0/stream"

# North Sea + Baltic bounding boxes (split into smaller regions to stay
# within aisstream.io size limits — a single large box silently fails).
DEFAULT_BOUNDING_BOXES = [
    [[49.0, -5.0], [54.0, 5.0]],    # Southern North Sea / English Channel
    [[54.0, -2.0], [58.0, 8.0]],    # Central North Sea
    [[58.0, -5.0], [62.0, 8.0]],    # Norwegian Sea
    [[54.0, 8.0], [58.0, 16.0]],    # Danish Straits / SW Baltic
    [[58.0, 8.0], [62.0, 20.0]],    # Skagerrak / Central Baltic
    [[62.0, 10.0], [66.0, 30.0]],   # Gulf of Bothnia / Finland
    [[56.0, 16.0], [60.0, 24.0]],   # Central Baltic
    [[54.0, 16.0], [56.0, 22.0]],   # SE Baltic (Poland / Kaliningrad)
]


class AISStreamClient:
    """Connects to aisstream.io WebSocket and yields parsed AIS messages."""

    def __init__(
        self,
        api_key: str,
        on_position: Callable[..., Any] | None = None,
        on_metadata: Callable[..., Any] | None = None,
        bounding_boxes: list[list[list[float]]] | None = None,
    ) -> None:
        self.api_key = api_key
        self.on_position = on_position
        self.on_metadata = on_metadata
        self.bounding_boxes = bounding_boxes or DEFAULT_BOUNDING_BOXES
        self._running = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0

    async def start(self) -> None:
        """Start the WebSocket connection with automatic reconnection."""
        self._running = True
        while self._running:
            try:
                await self._connect()
            except (
                websockets.exceptions.ConnectionClosed,
                websockets.exceptions.WebSocketException,
                OSError,
                asyncio.TimeoutError,
            ) as exc:
                if not self._running:
                    break
                logger.warning(
                    "aisstream connection lost: %s. Reconnecting in %.0fs",
                    exc, self._reconnect_delay,
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self._max_reconnect_delay
                )

    async def stop(self) -> None:
        self._running = False

    async def _connect(self) -> None:
        logger.info("Connecting to aisstream.io...")
        async with websockets.connect(
            AISSTREAM_WS_URL, ping_interval=20, ping_timeout=20,
            open_timeout=30, close_timeout=10,
        ) as ws:
            subscribe_msg = {
                "APIKey": self.api_key,
                "BoundingBoxes": self.bounding_boxes,
                "FiltersShipMMSI": [],
                "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
            }
            await ws.send(json.dumps(subscribe_msg))
            logger.info("aisstream subscription sent, listening for messages")
            self._reconnect_delay = 1.0  # reset on successful connect

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    self._handle_message(msg)
                except json.JSONDecodeError:
                    logger.debug("aisstream: malformed JSON, skipping")

    def _handle_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("MessageType")
        meta = msg.get("MetaData", {})
        mmsi = meta.get("MMSI")
        if not mmsi:
            return

        if msg_type == "PositionReport" and self.on_position:
            pos = msg.get("Message", {}).get("PositionReport", {})
            if not pos:
                return
            self.on_position(
                mmsi=mmsi,
                timestamp=meta.get("time_utc", datetime.now(timezone.utc).isoformat()),
                latitude=pos.get("Latitude"),
                longitude=pos.get("Longitude"),
                sog=pos.get("Sog"),
                cog=pos.get("Cog"),
                heading=pos.get("TrueHeading"),
                nav_status=pos.get("NavigationalStatus"),
                source="aisstream",
            )

        elif msg_type == "ShipStaticData" and self.on_metadata:
            static = msg.get("Message", {}).get("ShipStaticData", {})
            if not static:
                return
            dim = static.get("Dimension", {})
            length = None
            beam = None
            if dim:
                a = dim.get("A", 0) or 0
                b = dim.get("B", 0) or 0
                c = dim.get("C", 0) or 0
                d = dim.get("D", 0) or 0
                length = a + b if (a + b) > 0 else None
                beam = c + d if (c + d) > 0 else None

            draft_raw = static.get("MaximumStaticDraught")
            # aisstream.io already decodes draught to metres (not raw AIS tenths)
            draft_m = draft_raw if draft_raw and draft_raw > 0 else None

            self.on_metadata(
                mmsi=mmsi,
                imo=static.get("ImoNumber"),
                name=static.get("Name", "").strip(),
                callsign=static.get("CallSign", "").strip(),
                ship_type=static.get("Type"),
                length_m=length,
                beam_m=beam,
                draft_m=draft_m,
                destination=static.get("Destination", "").strip(),
                eta=static.get("Eta"),
                source="aisstream",
            )
