"""MQTT client for state topics on the Cerbo's local broker.

Every value published here is retained and remembered, and all of them are
republished whenever paho (re)connects, so a broker restart or a reconnect
after a dropped socket never leaves stale retained state (such as the last
will's "offline") in place. connect_async + loop_start lets paho retry until
the broker is up instead of crashing at boot.
"""

import logging

import paho.mqtt.client as mqtt

log = logging.getLogger("venus_ble")


class RetainedMqtt:
    def __init__(self, client_id, base, host="127.0.0.1", port=1883, keepalive=30,
                 will_suffix="bridge"):
        self.base = base
        self.retained = {}
        # paho 2.x requires an explicit callback API version; 1.x has no such argument.
        if hasattr(mqtt, "CallbackAPIVersion"):
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                      client_id=client_id, clean_session=True)
        else:
            self.client = mqtt.Client(client_id=client_id, clean_session=True)
        self.will_suffix = will_suffix
        self.client.will_set(f"{base}/{will_suffix}", "offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.retained[will_suffix] = "online"
        self.client.connect_async(host, port, keepalive=keepalive)
        self.client.loop_start()

    def publish(self, suffix, value):
        self.retained[suffix] = value
        self.client.publish(f"{self.base}/{suffix}", str(value), qos=1, retain=True)

    def _on_connect(self, client, userdata, *args):
        log.info("mqtt connected; publishing %d retained topics", len(self.retained))
        for suffix, value in list(self.retained.items()):
            client.publish(f"{self.base}/{suffix}", str(value), qos=1, retain=True)

    def close(self, final=None):
        """Publish final retained values (plus will_suffix=offline), wait for
        them briefly, and disconnect cleanly."""
        final = dict(final or {})
        final[self.will_suffix] = "offline"
        infos = []
        for suffix, value in final.items():
            self.retained[suffix] = value
            infos.append(self.client.publish(f"{self.base}/{suffix}", str(value),
                                             qos=1, retain=True))
        for info in infos:
            try:
                info.wait_for_publish(timeout=2)
            except (RuntimeError, ValueError, TypeError):
                pass
        self.client.disconnect()
        self.client.loop_stop()
