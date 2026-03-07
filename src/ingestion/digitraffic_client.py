"""MQTT client for Digitraffic (Finland) AIS data."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

DIGITRAFFIC_HOST = "meri.digitraffic.fi"
DIGITRAFFIC_PORT = 443
APP_NAME = "NorthSeaCrudeMonitor/1.0"


class DigitrafficClient:
    """Connects to Digitraffic MQTT WebSocket for Finnish/Baltic AIS data."""

    def __init__(
        self,
        on_position: Callable[..., Any] | None = None,
        on_metadata: Callable[..., Any] | None = None,
    ) -> None:
        self.on_position = on_position
        self.on_metadata = on_metadata
        self._client: mqtt.Client | None = None

    def start(self) -> None:
        """Start the MQTT connection (blocking — call from a thread)."""
        client_id = f"{APP_NAME}; {uuid.uuid4()}"
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            transport="websockets",
            client_id=client_id,
        )
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._client.tls_set()
        self._client.ws_set_options(path="/mqtt")

        logger.info("Connecting to Digitraffic MQTT...")
        self._client.connect(DIGITRAFFIC_HOST, DIGITRAFFIC_PORT)
        self._client.loop_forever()

    def stop(self) -> None:
        if self._client:
            self._client.disconnect()

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        if reason_code == 0:
            logger.info("Connected to Digitraffic MQTT")
            client.subscribe("vessels-v2/#")
        else:
            logger.error("Digitraffic MQTT connect failed: %s", reason_code)

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any = None,
        reason_code: Any = None,
        properties: Any = None,
    ) -> None:
        logger.warning("Digitraffic MQTT disconnected (rc=%s), will auto-reconnect", reason_code)

    def _on_message(
        self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage
    ) -> None:
        try:
            payload = json.loads(msg.payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        topic_parts = msg.topic.split("/")
        # Expected: vessels-v2/<mmsi>/location or vessels-v2/<mmsi>/metadata
        if len(topic_parts) < 3:
            return

        try:
            mmsi = int(topic_parts[1])
        except (ValueError, IndexError):
            return

        msg_kind = topic_parts[2] if len(topic_parts) > 2 else ""

        if msg_kind == "location" and self.on_position:
            ts_epoch = payload.get("time")
            if ts_epoch:
                ts = datetime.fromtimestamp(ts_epoch, tz=timezone.utc).isoformat()
            else:
                ts = datetime.now(timezone.utc).isoformat()

            self.on_position(
                mmsi=mmsi,
                timestamp=ts,
                latitude=payload.get("lat"),
                longitude=payload.get("lon"),
                sog=payload.get("sog"),
                cog=payload.get("cog"),
                heading=payload.get("heading"),
                nav_status=payload.get("navStat"),
                source="digitraffic",
            )

        elif msg_kind == "metadata" and self.on_metadata:
            # Digitraffic draught is in 1/10 metres
            raw_draft = payload.get("draught")
            draft_m = raw_draft / 10.0 if raw_draft and raw_draft > 0 else None

            ref_a = payload.get("refA", 0) or 0
            ref_b = payload.get("refB", 0) or 0
            ref_c = payload.get("refC", 0) or 0
            ref_d = payload.get("refD", 0) or 0
            length = ref_a + ref_b if (ref_a + ref_b) > 0 else None
            beam = ref_c + ref_d if (ref_c + ref_d) > 0 else None

            self.on_metadata(
                mmsi=mmsi,
                imo=payload.get("imo"),
                name=(payload.get("name") or "").strip(),
                callsign=(payload.get("callSign") or "").strip(),
                ship_type=payload.get("type"),
                length_m=length,
                beam_m=beam,
                draft_m=draft_m,
                destination=(payload.get("destination") or "").strip(),
                eta=payload.get("eta"),
                source="digitraffic",
            )
