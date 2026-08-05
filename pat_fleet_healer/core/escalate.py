"""Escalation: the log line is the record of truth; MQTT publish to central is
best-effort (core.mqtt carries its own client - paho is absent on the PISN nodes)."""
import json
import time
from .log import log
from . import mqtt


def escalate(cfg, healer, verdict, ev=None):
    ev = ev or {}
    log(cfg, "ESCALATE [%s] %s | %s" % (healer, verdict, json.dumps(ev, ensure_ascii=False)))
    payload = json.dumps({"device_id": cfg.device_id, "healer": healer, "verdict": verdict,
                          "evidence": ev, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ")},
                         ensure_ascii=False)
    if not mqtt.publish(cfg.mqtt_host, cfg.mqtt_port,
                        "healer/%s/escalate" % cfg.device_id, payload, cfg.node_id):
        log(cfg, "escalate-publish-failed (%s:%s) - logged only"
                 % (cfg.mqtt_host, cfg.mqtt_port))
