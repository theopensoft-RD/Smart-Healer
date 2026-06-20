"""Escalation: the log line is the record of truth; MQTT publish to central is
best-effort (paho ships in the pat-smart venv; mosquitto_pub is not installed)."""
import json
import time
from .log import log


def escalate(cfg, healer, verdict, ev=None):
    ev = ev or {}
    log(cfg, "ESCALATE [%s] %s | %s" % (healer, verdict, json.dumps(ev, ensure_ascii=False)))
    payload = json.dumps({"device_id": cfg.device_id, "healer": healer, "verdict": verdict,
                          "evidence": ev, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ")},
                         ensure_ascii=False)
    try:
        import paho.mqtt.publish as publish
        publish.single("healer/%s/escalate" % cfg.device_id, payload=payload,
                       hostname=cfg.mqtt_host, port=1883, keepalive=10)
    except Exception as e:
        log(cfg, "escalate-publish-skip (%r) - logged only" % e)
