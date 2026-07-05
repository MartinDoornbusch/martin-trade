import json

from tradebot.mqtt import SENSORS, MqttPublisher, discovery_payload


def test_discovery_payload_structure():
    topic, payload = discovery_payload({"key": "total_eur", "name": "Paper portfolio",
                                        "unit": "EUR", "icon": "mdi:wallet"})
    assert topic == "homeassistant/sensor/tradebot_total_eur/config"
    data = json.loads(payload)
    assert data["state_topic"] == "tradebot/total_eur"
    assert data["unique_id"] == "tradebot_total_eur"
    assert data["unit_of_measurement"] == "EUR"
    assert data["device"]["identifiers"] == ["tradebot"]


def test_discovery_payload_without_unit():
    _, payload = discovery_payload({"key": "last_decision", "name": "Laatste besluit",
                                    "unit": None, "icon": "mdi:robot"})
    assert "unit_of_measurement" not in json.loads(payload)


def test_all_sensors_have_valid_payloads():
    for sensor in SENSORS:
        topic, payload = discovery_payload(sensor)
        assert topic.startswith("homeassistant/sensor/tradebot_")
        json.loads(payload)


def test_publisher_disabled_without_host():
    pub = MqttPublisher()
    assert not pub.enabled
    pub.publish_status({"total_eur": 1000})  # mag geen exception geven


def test_publisher_survives_unreachable_broker():
    pub = MqttPublisher(host="127.0.0.1", port=1)  # niets luistert hier
    pub.publish_status({"total_eur": 1000})  # fout wordt gelogd, niet geraised
