"""MQTT-publisher: bot-status als Home Assistant sensoren via MQTT discovery.

Concept overgenomen uit de oude bot (mqtt_publisher), herbouwd: alleen status
publiceren, geen commando-kanaal. HA maakt de sensoren automatisch aan zodra
de Mosquitto broker add-on draait en host/user/pass geconfigureerd zijn.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

DEVICE = {"identifiers": ["tradebot"], "name": "AI Trade Platform",
          "manufacturer": "martin-trade"}

SENSORS: list[dict] = [
    {"key": "total_eur", "name": "Paper portfolio", "unit": "EUR", "icon": "mdi:wallet"},
    {"key": "cash_eur", "name": "Paper cash", "unit": "EUR", "icon": "mdi:cash"},
    {"key": "open_positions", "name": "Open posities", "unit": None,
     "icon": "mdi:format-list-numbered"},
    {"key": "closed_trades", "name": "Closed trades", "unit": None, "icon": "mdi:swap-horizontal"},
    {"key": "win_rate_pct", "name": "Win-rate", "unit": "%", "icon": "mdi:percent"},
    {"key": "net_pnl_eur", "name": "Netto P&L", "unit": "EUR", "icon": "mdi:chart-line"},
    {"key": "total_fees_eur", "name": "Totaal fees", "unit": "EUR", "icon": "mdi:cash-minus"},
    {"key": "last_decision", "name": "Laatste besluit", "unit": None, "icon": "mdi:robot"},
]


def discovery_payload(sensor: dict, prefix: str = "tradebot") -> tuple[str, str]:
    """(topic, json-payload) voor één HA discovery-sensor. Puur, testbaar."""
    key = sensor["key"]
    topic = f"homeassistant/sensor/{prefix}_{key}/config"
    payload = {
        "name": sensor["name"],
        "unique_id": f"{prefix}_{key}",
        "state_topic": f"{prefix}/{key}",
        "device": DEVICE,
        "icon": sensor["icon"],
    }
    if sensor["unit"]:
        payload["unit_of_measurement"] = sensor["unit"]
    return topic, json.dumps(payload)


class MqttPublisher:
    def __init__(self, host: str = "", port: int = 1883, user: str = "",
                 password: str = "", prefix: str = "tradebot"):
        self.host, self.port, self.user, self.password = host, int(port or 1883), user, password
        self.prefix = prefix
        self._discovery_sent = False

    @property
    def enabled(self) -> bool:
        return bool(self.host)

    def publish_status(self, status: dict) -> None:
        """Publiceert status-dict (keys uit SENSORS). Fouten loggen, nooit crashen."""
        if not self.enabled:
            return
        try:
            import paho.mqtt.client as mqtt_client
            client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
            if self.user:
                client.username_pw_set(self.user, self.password)
            client.connect(self.host, self.port, keepalive=10)
            if not self._discovery_sent:
                for sensor in SENSORS:
                    topic, payload = discovery_payload(sensor, self.prefix)
                    client.publish(topic, payload, retain=True)
                self._discovery_sent = True
            for sensor in SENSORS:
                key = sensor["key"]
                if key in status and status[key] is not None:
                    client.publish(f"{self.prefix}/{key}", str(status[key]), retain=True)
            client.disconnect()
        except Exception as exc:  # noqa: BLE001 - status-publicatie mag de bot nooit stoppen
            log.warning("MQTT publish mislukt (%s:%s): %s", self.host, self.port, exc)
