"""Minimal MQTT 3.1.1 QoS-0 publisher (no third-party dependency).

WHY WE CARRY OUR OWN INSTEAD OF USING paho
------------------------------------------
The fleet is not uniform. 56 nodes (pit/pir) run the healer from a pipx venv on
python 3.13 where paho is installed; the 6 PISN signage IRIVs run it on the SYSTEM
python 3.9, which has no paho and cannot get one without a field visit. The old
code did `import paho.mqtt.publish` INSIDE a bare `except: pass`, so on those nodes
every push failed silently - and the failure looked identical to "nothing worth
reporting happened". That is a large part of why nobody noticed the centre had
never received a single healer event.

A QoS-0 publish is CONNECT / CONNACK / PUBLISH / DISCONNECT and nothing more, so
carrying ~50 lines removes a dependency the fleet cannot satisfy uniformly, and
leaves ONE code path to reason about instead of two.

MEASURED BROKER FACTS (pit003 -> mqtt.pattaya-smart-sanitary.com, 2026-08-05):
  port 1883  -> timeout (closed).  This is what the old code was hardcoded to.
  port 8883  -> PLAIN MQTT, accepted, no credentials required.
  port 8883 + TLS -> connection reset (the broker does NOT speak TLS).
So 8883 here is the *cleartext* port despite 8883 being the IANA "secure-mqtt"
number. No TLS is attempted; adding one would break every publish.

This module MUST NOT raise: its caller is an event emitter, not a transport.
"""
import socket
import struct

CONNECT, CONNACK, PUBLISH, DISCONNECT = 0x10, 0x20, 0x30, 0xE0
MAX_CLIENT_ID = 23                       # MQTT 3.1 cap; brokers may reject longer


def _remaining_length(n):
    """MQTT variable-length integer (7 bits per byte, high bit = continue)."""
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            return bytes(out)


def _mqtt_str(s):
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b


def _recv_exactly(sock, n):
    """Read exactly n bytes, or None if the peer closed early."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def publish(host, port, topic, payload, client_id, keepalive=10, timeout=5.0):
    """Publish ONE QoS-0 message.

    Returns True only if the broker sent CONNACK with return code 0 AND the
    PUBLISH was written to the socket. QoS 0 has no publish acknowledgement, so
    True means "handed to a broker that accepted us", never "stored". Anything
    else - DNS failure, refused connection, timeout, rejected client id, protocol
    surprise - returns False. It never raises.
    """
    sock = None
    try:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        sock = socket.create_connection((host, int(port)), timeout)
        sock.settimeout(timeout)

        # CONNECT: protocol "MQTT" level 4, flags 0x02 (clean session), no auth.
        var = _mqtt_str("MQTT") + bytes(bytearray([0x04, 0x02])) + struct.pack(">H", keepalive)
        body = var + _mqtt_str(str(client_id)[:MAX_CLIENT_ID])
        sock.sendall(bytes(bytearray([CONNECT])) + _remaining_length(len(body)) + body)

        head = _recv_exactly(sock, 2)
        if not head or head[0] != CONNACK:
            return False
        rest = _recv_exactly(sock, head[1])
        if not rest or len(rest) < 2 or rest[1] != 0:
            return False                     # broker refused us; do NOT call it sent

        body = _mqtt_str(topic) + payload
        sock.sendall(bytes(bytearray([PUBLISH])) + _remaining_length(len(body)) + body)
        sock.sendall(bytes(bytearray([DISCONNECT, 0x00])))
        return True
    except Exception:
        return False
    finally:
        try:
            if sock is not None:
                sock.close()
        except Exception:
            pass
