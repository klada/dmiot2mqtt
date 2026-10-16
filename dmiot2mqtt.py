#!/usr/bin/env python3
"""
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import argparse
import asyncio
import collections
import configparser
import copy
import json
import logging
import re
import secrets
import ssl
import sys
from typing import Dict, List, Optional, Tuple

try:
    import aiomqtt
except ImportError:
    sys.exit("aiomqtt library is missing, quitting...")
    

HOST = '0.0.0.0'
PORT = 31270

# Largest amount of data from one fan that may stay unparsed. A complete
# message is well below 1 KiB, so more than this means a broken object.
MAX_BUFFER_SIZE = 4 * 1024

# Deepest nesting accepted in a message. Fan messages are two levels deep.
MAX_DEPTH = 8

# Seconds to wait before reconnecting to the MQTT broker after a failure. The
# delay doubles with every failed attempt, up to MQTT_MAX_RECONNECT_DELAY,
# and is reset once a connection has lasted MQTT_STABLE_CONNECTION seconds.
MQTT_RECONNECT_DELAY = 5
MQTT_MAX_RECONNECT_DELAY = 60
MQTT_STABLE_CONNECTION = 30

# Seconds allowed for publishing "offline" when a fan disconnects.
AVAILABILITY_TIMEOUT = 2

# Seconds between two diagnostics updates caused only by a changed WiFi
# signal. The fans report it with every heartbeat, i.e. every 10 seconds.
DIAGNOSTICS_INTERVAL = 300

# Seconds a fan may take to accept data we send before its connection is
# treated as dead, and to close the connection at the end of a session.
WRITE_TIMEOUT = 10
CLOSE_TIMEOUT = 2

logger = logging.getLogger("main")


class JsonStreamParser:
    """
    Splits the TCP byte stream sent by a fan into JSON objects.

    The fans send JSON objects back to back without any delimiter. A single
    read() can therefore contain several objects, or only a part of one.
    Incomplete data is kept until the rest arrives.
    """

    def __init__(self, max_size: int = MAX_BUFFER_SIZE):
        self._buffer = b""
        self._max_size = max_size

    @staticmethod
    def _find_object_end(buffer: bytes) -> int:
        """
        Returns the index right after the JSON object that starts at the
        beginning of `buffer`, -1 if the object is not complete yet, or -2 if
        it nests deeper than MAX_DEPTH. Braces inside strings are ignored.
        Scanning bytes is safe because all characters checked here are ASCII
        and never occur inside multi-byte UTF-8 sequences.
        """
        depth = 0
        in_string = False
        escaped = False
        for index, char in enumerate(buffer):
            if in_string:
                if escaped:
                    escaped = False
                elif char == 0x5C:  # backslash
                    escaped = True
                elif char == 0x22:  # double quote
                    in_string = False
            elif char == 0x22:
                in_string = True
            elif char == 0x7B:  # {
                depth += 1
                if depth > MAX_DEPTH:
                    return -2
            elif char == 0x7D:  # }
                depth -= 1
                if depth == 0:
                    return index + 1
        return -1

    def feed(self, data: bytes) -> List[dict]:
        """
        Adds received data and returns all JSON objects completed by it.
        """
        self._buffer += data
        messages = []
        while self._buffer:
            start = self._buffer.find(b"{")
            if start == -1:
                if self._buffer.strip():
                    logger.warning("Discarding non-JSON data: %r", self._buffer[:100])
                self._buffer = b""
                break
            if self._buffer[:start].strip():
                logger.warning("Discarding non-JSON data: %r", self._buffer[:start][:100])
            self._buffer = self._buffer[start:]

            end = self._find_object_end(self._buffer)
            if end == -1 and len(self._buffer) <= self._max_size:
                break
            if end < 0:
                # An object that never closes would swallow every following
                # message, so give up on it and look for the next one.
                logger.warning("Broken JSON object, skipping to the next one: %r", self._buffer[:100])
                next_start = self._buffer.find(b"{", 1)
                self._buffer = self._buffer[next_start:] if next_start != -1 else b""
                continue

            raw, self._buffer = self._buffer[:end], self._buffer[end:]
            try:
                message = json.loads(raw.decode())
            except (UnicodeDecodeError, json.JSONDecodeError):
                logger.warning("Invalid JSON received, ignoring: %r", raw[:100])
                continue
            messages.append(message)
        return messages


# Active session per fan, keyed by its MQTT topic
ACTIVE_SESSIONS: Dict[str, "DreamMakerIotClient"] = {}


class DreamMakerIotClient:
    """
    Wrapper which handles the communication for one client connection.
    """
    REPLY_TEMPLATE =  {"action": 81, "resource_id": 0, "version":"zeico_3.0.0", "code": 0}
    # @attention: Data dict is missing and needs to be inserted!
    COMMAND_TEMPLATE = {"action":4, "resource_id": 9031, "version":"zeico_3.0.0", "msg_id": 0}
    
    RESOURCE_STATUS = 127
    RESOURCE_PROVISIONING = 2000
    RESOURCE_AUTH = 2001

    # Never published as state: heartbeats would spam the broker, and the
    # auth/provisioning messages carry the device key.
    UNPUBLISHED_RESOURCES = {RESOURCE_STATUS, RESOURCE_PROVISIONING, RESOURCE_AUTH}

    # Heartbeat fields published on the diagnostics topic; nothing else from
    # the heartbeat is passed on.
    DIAGNOSTIC_FIELDS = ("signal", "comm_version", "mcu_version", "rf_version")
    
    MAP_MODES = {
        0: "direct",
        1: "natural",
        2: "smart"
    }
    
    def __init__(self, stream_reader, stream_writer):
        addr = stream_writer.get_extra_info('peername')
        logger.info(f"Client {addr!r} connected.")
        self.client_ip = addr[0]
        self.stream_reader = stream_reader
        self.stream_writer = stream_writer
        self.parser = JsonStreamParser()
        self.pending_messages = collections.deque()
        self.send_lock = asyncio.Lock()
        self.device_id: Optional[str] = None
        self.session_key: Optional[str] = None
        self.mqtt_identifier: Optional[str] = None
        self.superseded = False
        self.mqtt_client: Optional[aiomqtt.Client] = None
        self.last_state: Optional[dict] = None
        self.diagnostics: Optional[dict] = None
        self.published_diagnostics: Optional[dict] = None
        self.diagnostics_published_at: Optional[float] = None

    # ------------------------------------------------------------------
    # Topics and session registry
    # ------------------------------------------------------------------

    def has_usable_device_id(self) -> bool:
        # Fans provisioned through this bridge all get the same all-zero id
        # (see async_send_provisioning_data), so it cannot tell them apart.
        return bool(self.device_id and self.device_id.strip("0"))

    def get_fan_identifier(self) -> str:
        if MqttConfig.topic_id == "device_id" and self.has_usable_device_id():
            identifier = self.device_id
        else:
            identifier = self.client_ip
        return identifier.lower()

    def get_topic_base(self) -> str:
        return f"{MqttConfig.base_topic}/{self.get_fan_identifier()}"

    def get_command_topic(self) -> str:
        return f"{self.get_topic_base()}/command"

    def get_availability_topic(self) -> str:
        return f"{self.get_topic_base()}/availability"

    def get_diagnostics_topic(self) -> str:
        return f"{self.get_topic_base()}/diagnostics"

    def get_mqtt_identifier(self) -> str:
        return self.mqtt_identifier

    def is_same_fan(self, other: "DreamMakerIotClient") -> bool:
        # Fans are told apart by device id. Without one, assume the same fan.
        if not self.device_id or not other.device_id:
            return True
        return self.device_id == other.device_id

    def register_session(self):
        """
        Makes this the active session for its fan. A fan that lost WiFi or
        power reconnects while the server may still hold the old connection,
        so the old session is closed here.
        """
        if MqttConfig.topic_id == "device_id" and not self.has_usable_device_id():
            logger.warning(
                "Fan %s has no unique device id (%r), using its IP address in the topic",
                self.client_ip, self.device_id
            )
        self.session_key = self.get_topic_base()
        # A fixed client id per fan lets the broker replace a stale session
        # (and drop its last will) instead of keeping both.
        self.mqtt_identifier = "dmiot2mqtt-" + self.session_key.replace("/", "-")
        previous = ACTIVE_SESSIONS.get(self.session_key)
        if previous is not None and previous is not self and not self.is_same_fan(previous):
            # Two fans behind one IP address (e.g. NAT): closing the other one
            # would make them kick each other out forever.
            logger.error(
                "Fans %s and %s share the topic %s; use topic_id = device_id or give them "
                "different IP addresses", previous.device_id, self.device_id, self.session_key
            )
            self.mqtt_identifier += "-" + secrets.token_hex(4)
            return
        ACTIVE_SESSIONS[self.session_key] = self
        if previous is not None and previous is not self:
            logger.info("Fan %s reconnected, closing its previous connection", self.session_key)
            previous.supersede()

    def is_active_session(self) -> bool:
        return self.session_key is not None and ACTIVE_SESSIONS.get(self.session_key) is self

    def supersede(self):
        self.superseded = True
        self.stream_writer.transport.abort()

    # ------------------------------------------------------------------
    # Fan side (TCP)
    # ------------------------------------------------------------------

    async def async_authenticate_client(self) -> bool:
        while True:
            message = await self.async_get_data()
            if message is None:
                return False
            # Provisioning request
            if message["action"] == 1 and message["resource_id"] == self.RESOURCE_PROVISIONING:
                logger.info("Received provisioning request, sending data...")
                logger.debug(message)
                await self.async_send_provisioning_data()
            # Auth request
            elif message["action"] == 1 and message["resource_id"] == self.RESOURCE_AUTH:
                logger.info("Auth handshake")
                logger.debug(message)
                data = message.get("data")
                if isinstance(data, dict) and data.get("device_id"):
                    self.device_id = str(data["device_id"])
                await self.async_ack_message(message)
                return True

    async def async_get_data(self) -> Optional[dict]:
        """
        Returns the next complete message from the fan, or None once the
        connection has been closed or has been silent for too long.
        """
        while not self.pending_messages:
            timeout = BridgeConfig.read_timeout if BridgeConfig.read_timeout > 0 else None
            try:
                data = await asyncio.wait_for(self.stream_reader.read(1024), timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "No data from %s for %d seconds, closing the connection",
                    self.client_ip, BridgeConfig.read_timeout
                )
                return None
            if not data:
                return None
            for message in self.parser.feed(data):
                # Every protocol message carries a numeric action and resource id
                if (isinstance(message, dict) and isinstance(message.get("action"), int)
                        and isinstance(message.get("resource_id"), int)):
                    self.pending_messages.append(message)
                else:
                    logger.warning("Ignoring message without action/resource_id: %r", message)
        return self.pending_messages.popleft()
    
    async def async_send_data(self, data: dict):
        message = json.dumps(data).encode()
        # ACKs from the main loop and commands from the MQTT task may write
        # at the same time
        async with self.send_lock:
            self.stream_writer.write(message)
            logger.debug("Sending: {}".format(message))
            try:
                await asyncio.wait_for(self.stream_writer.drain(), WRITE_TIMEOUT)
            except asyncio.TimeoutError:
                # The fan does not read anymore. Without this, a session
                # blocked here would never notice the read timeout.
                logger.warning("Fan %s does not accept data, closing the connection", self.client_ip)
                self.stream_writer.transport.abort()
                raise ConnectionResetError("write timeout")
        
    async def async_send_command(self, command_data: dict):
        message = copy.copy(self.COMMAND_TEMPLATE)
        message["data"] = command_data
        await self.async_send_data(message)
    
    async def async_ack_message(self, message: dict):
        """
        Each incoming message needs to be either ACK'ed or answered properly.
        """
        response = copy.copy(self.REPLY_TEMPLATE)
        response["resource_id"] = message["resource_id"]
        await self.async_send_data(response)
    
    async def async_send_provisioning_data(self):
        response = copy.copy(self.REPLY_TEMPLATE)
        response["resource_id"] = self.RESOURCE_PROVISIONING
        response["action"] = 81
        # Just add a dummy device_key/device_id. We won't need this information later.
        response["data"] = {"device_key":"0000000000000000", "device_id":"000000000000000000000000"}
        await self.async_send_data(response)
 
    async def async_handle_fan_message(self, message: dict):
        logger.debug(message)
        await self.async_ack_message(message)
        if message["resource_id"] == self.RESOURCE_STATUS:
            await self.async_handle_heartbeat(message.get("data"))
            return
        if message["resource_id"] in self.UNPUBLISHED_RESOURCES:
            return
        data = message.get("data")
        if isinstance(data, dict):
            await self.async_publish_state(data)
            
    # ------------------------------------------------------------------
    # MQTT side
    # ------------------------------------------------------------------

    async def async_publish_state(self, data: dict):
        self.last_state = data
        client = self.mqtt_client
        if client is None:
            logger.warning("MQTT not connected, state will be published after reconnect")
            return
        try:
            await client.publish(
                self.get_topic_base(), payload=json.dumps(data).encode(), retain=MqttConfig.retain
            )
        except aiomqtt.MqttError as e:
            logger.warning("Publishing state failed: %s", e)

    async def async_handle_heartbeat(self, data):
        if not isinstance(data, dict):
            return
        diagnostics = {field: data[field] for field in self.DIAGNOSTIC_FIELDS if field in data}
        if not diagnostics:
            return
        self.diagnostics = diagnostics
        await self.async_publish_diagnostics()

    async def async_publish_diagnostics(self, force: bool = False):
        """
        Publishes the latest diagnostics when they differ from the published
        ones. A change of the WiFi signal alone is published at most every
        DIAGNOSTICS_INTERVAL seconds, as it changes with almost every heartbeat.
        """
        diagnostics = self.diagnostics
        client = self.mqtt_client
        if diagnostics is None or client is None:
            return
        published = self.published_diagnostics
        now = asyncio.get_running_loop().time()
        if not force and published is not None:
            if diagnostics == published:
                return
            only_signal_changed = all(
                diagnostics.get(field) == published.get(field)
                for field in self.DIAGNOSTIC_FIELDS if field != "signal"
            )
            if only_signal_changed and now - self.diagnostics_published_at < DIAGNOSTICS_INTERVAL:
                return
        try:
            await client.publish(
                self.get_diagnostics_topic(), payload=json.dumps(diagnostics).encode(), retain=True
            )
        except aiomqtt.MqttError as e:
            logger.warning("Publishing diagnostics failed: %s", e)
            return
        self.published_diagnostics = diagnostics
        self.diagnostics_published_at = now

    async def async_publish_availability(self, payload: bytes) -> bool:
        client = self.mqtt_client
        if client is None:
            return False
        try:
            await asyncio.wait_for(
                client.publish(self.get_availability_topic(), payload=payload, qos=1, retain=True),
                AVAILABILITY_TIMEOUT
            )
        except (aiomqtt.MqttError, asyncio.TimeoutError) as e:
            logger.warning("Publishing availability failed: %r", e)
            return False
        return True

    async def async_publish_offline_once(self):
        """
        Publishes "offline" through a short-lived connection, for a session
        that ends while it has no MQTT connection (right after connecting, or
        during a broker outage). It uses its own client id, so it cannot take
        over the connection of a session the fan has just opened again.
        """
        kwargs = MqttConfig.get_mqtt_client_kwargs(identifier=self.get_mqtt_identifier() + "-offline")

        async def publish():
            async with aiomqtt.Client(**kwargs) as client:
                await client.publish(self.get_availability_topic(), payload=b"offline", qos=1, retain=True)

        try:
            await asyncio.wait_for(publish(), AVAILABILITY_TIMEOUT)
        except (aiomqtt.MqttError, asyncio.TimeoutError) as e:
            logger.warning("Could not publish offline availability: %r", e)

    async def async_handle_mqtt_command(self, payload):
        try:
            if isinstance(payload, (bytes, bytearray)):
                payload = payload.decode()
            command = json.loads(payload)
        except (UnicodeDecodeError, TypeError, json.JSONDecodeError):
            logger.warning("Ignoring invalid MQTT command: %r", payload)
            return
        if not isinstance(command, dict) or not command:
            logger.warning("Ignoring MQTT command that is not a JSON object: %r", payload)
            return
        logger.debug("Received command %s", command)
        try:
            await self.async_send_command(command)
        except (ConnectionError, OSError) as e:
            logger.warning("Could not send command to fan: %s", e)

    def get_discovery_node_id(self) -> str:
        # Home Assistant accepts only these characters in a discovery topic
        return re.sub(r"[^a-zA-Z0-9_-]", "_", self.get_topic_base())

    def build_discovery_configs(self) -> List[Tuple[str, dict]]:
        """
        Returns (topic, payload) pairs for Home Assistant MQTT discovery: the
        fan, its settings and its sensors as entities of one device. All
        topics in the payloads are this fan's topics, so they follow
        base_topic and topic_id.
        """
        node_id = self.get_discovery_node_id()
        state_topic = self.get_topic_base()
        command_topic = self.get_command_topic()
        common = {
            "availability_topic": self.get_availability_topic(),
            "device": {
                "identifiers": [node_id],
                "name": f"Dream Maker fan {self.get_fan_identifier()}",
                "manufacturer": "Dream Maker",
            },
            "origin": {"name": "dmiot2mqtt"},
        }

        def switch(field: str, name: str, icon: str) -> dict:
            return {
                "name": name,
                "icon": icon,
                "entity_category": "config",
                "state_topic": state_topic,
                "value_template": f"{{{{ value_json.{field} }}}}",
                "state_on": "1",
                "state_off": "0",
                "command_topic": command_topic,
                "payload_on": json.dumps({field: 1}),
                "payload_off": json.dumps({field: 0}),
            }

        entities = [
            ("fan", "fan", {
                # The entity takes the device name
                "name": None,
                "state_topic": state_topic,
                "state_value_template": "{{ value_json.power }}",
                "command_topic": command_topic,
                "command_template": '{"power": {{ value }}}',
                "payload_on": "1",
                "payload_off": "0",
                "percentage_state_topic": state_topic,
                "percentage_value_template": "{{ value_json.speed }}",
                "percentage_command_topic": command_topic,
                # The fan accepts speeds from 1 to 100, so 0 turns it off
                "percentage_command_template": (
                    '{% if value | int == 0 %}{"power": 0}'
                    '{% else %}{"speed": {{ value | int }}}{% endif %}'
                ),
                "speed_range_min": 1,
                "speed_range_max": 100,
                "oscillation_state_topic": state_topic,
                "oscillation_value_template": "{{ value_json.roll_enable }}",
                "oscillation_command_topic": command_topic,
                "oscillation_command_template": '{"roll_enable": {{ value }}}',
                "payload_oscillation_on": "1",
                "payload_oscillation_off": "0",
                "preset_modes": [self.MAP_MODES[mode] for mode in sorted(self.MAP_MODES)],
                "preset_mode_state_topic": state_topic,
                "preset_mode_value_template": (
                    '{% if value_json.mode == 1 %}natural'
                    '{% elif value_json.mode == 2 %}smart{% else %}direct{% endif %}'
                ),
                "preset_mode_command_topic": command_topic,
                "preset_mode_command_template": (
                    '{% if value == "natural" %}{"mode": 1}'
                    '{% elif value == "smart" %}{"mode": 2}{% else %}{"mode": 0}{% endif %}'
                ),
                "json_attributes_topic": state_topic,
            }),
            ("select", "roll_angle", {
                "name": "Oscillation angle",
                "icon": "mdi:arrow-oscillating",
                "entity_category": "config",
                "state_topic": state_topic,
                "value_template": "{{ value_json.roll_angle }}",
                "command_topic": command_topic,
                "command_template": '{"roll_angle": {{ value | int }}}',
                "options": ["30", "60", "90", "120", "140"],
            }),
            ("number", "power_delay", {
                "name": "Off timer",
                "icon": "mdi:fan-clock",
                "state_topic": state_topic,
                "value_template": "{{ value_json.power_delay }}",
                "command_topic": command_topic,
                # Home Assistant sends numbers as floats, e.g. 30.0
                "command_template": '{"power_delay": {{ value | int }}}',
                "min": 0,
                "max": 480,
                "step": 1,
                "unit_of_measurement": "min",
            }),
            ("switch", "light", switch("light", "LED indicators", "mdi:led-on")),
            ("switch", "sound", switch("sound", "Button sounds", "mdi:volume-high")),
            ("switch", "child_lock", switch("child_lock", "Child lock", "mdi:lock")),
            ("sensor", "temperature", {
                "name": "Temperature",
                "device_class": "temperature",
                "state_class": "measurement",
                "unit_of_measurement": "°C",
                "state_topic": state_topic,
                "value_template": "{{ value_json.temperature }}",
            }),
            ("sensor", "humidity", {
                "name": "Humidity",
                "device_class": "humidity",
                "state_class": "measurement",
                "unit_of_measurement": "%",
                "state_topic": state_topic,
                "value_template": "{{ value_json.humidity }}",
            }),
            ("sensor", "signal", {
                # The protocol notes give no unit for the signal value
                "name": "WiFi signal",
                "icon": "mdi:wifi",
                "entity_category": "diagnostic",
                "state_class": "measurement",
                "state_topic": self.get_diagnostics_topic(),
                "value_template": "{{ value_json.signal }}",
                "json_attributes_topic": self.get_diagnostics_topic(),
            }),
        ]

        configs = []
        for component, object_id, entity in entities:
            payload = dict(common, unique_id=f"{node_id}_{object_id}", **entity)
            topic = f"{MqttConfig.discovery_prefix}/{component}/{node_id}/{object_id}/config"
            configs.append((topic, payload))
        return configs

    def get_ha_status_topic(self) -> str:
        return f"{MqttConfig.discovery_prefix}/status"

    async def async_handle_ha_status(self, client: "aiomqtt.Client", payload):
        """
        Home Assistant publishes "online" on its status topic after it has
        started. Without the retain option it would otherwise not know the
        fan state until the fan reports its next change.
        """
        if payload != b"online":
            return
        logger.debug("Home Assistant started, publishing %s again", self.get_topic_base())
        await self.async_publish_discovery(client)
        if self.last_state is not None:
            await self.async_publish_state(self.last_state)

    async def async_publish_discovery(self, client: "aiomqtt.Client"):
        if not MqttConfig.discovery:
            return
        for topic, payload in self.build_discovery_configs():
            await client.publish(topic, payload=json.dumps(payload).encode(), qos=1, retain=True)
        logger.debug("Published Home Assistant discovery for %s", self.get_topic_base())

    async def async_mqtt_loop(self):
        """
        Keeps one MQTT connection for this fan and reconnects when the broker
        goes away, while the fan connection stays open. Runs until cancelled.
        """
        will = aiomqtt.Will(self.get_availability_topic(), b"offline", qos=1, retain=True)
        kwargs = MqttConfig.get_mqtt_client_kwargs(identifier=self.get_mqtt_identifier(), will=will)
        delay = MQTT_RECONNECT_DELAY
        loop = asyncio.get_running_loop()
        while not self.superseded:
            connected_at = None
            try:
                async with aiomqtt.Client(**kwargs) as client:
                    self.mqtt_client = client
                    connected_at = loop.time()
                    # Subscribe before announcing "online", so a command sent
                    # in reaction to "online" is not lost
                    await client.subscribe(self.get_command_topic())
                    logger.debug("MQTT subscribe to %s finished", self.get_command_topic())
                    if MqttConfig.discovery:
                        await client.subscribe(self.get_ha_status_topic())
                    # The entities must exist before "online" makes them available
                    await self.async_publish_discovery(client)
                    await self.async_publish_availability(b"online")
                    if self.last_state is not None:
                        await self.async_publish_state(self.last_state)
                    # The broker may have lost the retained message
                    await self.async_publish_diagnostics(force=True)
                    async for message in client.messages:
                        if message.topic.matches(self.get_ha_status_topic()):
                            await self.async_handle_ha_status(client, message.payload)
                        else:
                            await self.async_handle_mqtt_command(message.payload)
            except aiomqtt.MqttError as e:
                logger.warning("MQTT connection lost (%s), reconnecting in %gs", e, delay)
            except Exception:
                logger.exception("Unexpected MQTT error, reconnecting in %gs", delay)
            finally:
                self.mqtt_client = None
            # A connection that is dropped right away (e.g. another client
            # with the same id) must not reset the backoff
            if connected_at is not None and loop.time() - connected_at >= MQTT_STABLE_CONNECTION:
                delay = MQTT_RECONNECT_DELAY
            await asyncio.sleep(delay)
            delay = min(delay * 2, MQTT_MAX_RECONNECT_DELAY)

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------
        
    async def async_run(self):
        """
        Main entry point for a new client connection.
        """       
        if not await self.async_authenticate_client():
            return False
        self.register_session()
        logger.info("Client authenticated, publishing on %s", self.get_topic_base())
       
        mqtt_task = asyncio.ensure_future(self.async_mqtt_loop())
        try:
            ##
            # Main communication loop
            ##
            while True:
                message = await self.async_get_data()
                if message is None:
                    break
                await self.async_handle_fan_message(message)
        finally:
            await self.async_end_session(mqtt_task)
        return True
            
    async def async_end_session(self, mqtt_task):
        # A superseded session must not report "offline": the fan is
        # connected through its newer session.
        offline_pending = self.is_active_session()
        if offline_pending:
            del ACTIVE_SESSIONS[self.session_key]
            offline_pending = not await self.async_publish_availability(b"offline")
        mqtt_task.cancel()
        # gather() returns the task's cancellation as a result, but still
        # raises if this session itself is being cancelled
        await asyncio.gather(mqtt_task, return_exceptions=True)
        # No MQTT connection at the end: publish "offline" separately, unless
        # the fan has reconnected in the meantime.
        if offline_pending and self.session_key not in ACTIVE_SESSIONS:
            await self.async_publish_offline_once()
        
    async def async_stop(self):
        logger.info("Quit....")
        self.stream_writer.close()
        try:
            await asyncio.wait_for(self.stream_writer.wait_closed(), CLOSE_TIMEOUT)
        except asyncio.TimeoutError:
            # Data the fan never reads would keep the connection open
            self.stream_writer.transport.abort()
        except (ConnectionError, OSError):
            pass


class BridgeConfig:
    # Seconds without data before a fan connection is treated as dead. The
    # fans send a heartbeat every 10 seconds. 0 or less disables the timeout.
    read_timeout = 90


class MqttConfig:
    server = "localhost"
    port = 1883
    base_topic = "dmiot2mqtt"
    user = None
    password = None
    use_ssl = False
    retain = False
    # Which value identifies a fan in the MQTT topic: "ip" or "device_id"
    topic_id = "ip"
    # Home Assistant MQTT discovery
    discovery = True
    discovery_prefix = "homeassistant"

    @classmethod
    def get_uri(cls):
        if cls.use_ssl:
            proto = "mqtts"
        else:
            proto = "mqtt"
        
        uri = f"{proto}://"
        if cls.user:
            uri += cls.user
            if cls.password:
                uri += ":" + cls.password
            uri += "@"
        uri += f"{cls.server}:{cls.port}/"
        return uri

    @classmethod
    def read_config(cls, config_file):
        """
        Reads MQTT config values from the specified ini file.
        """
        config = configparser.ConfigParser()
        config.read(config_file)
        if "bridge" in config:
            BridgeConfig.read_timeout = config["bridge"].getint("read_timeout", BridgeConfig.read_timeout)
        if "mqtt" not in config:
            logger.warning("No MQTT config found, falling back to localhost")
            return

        cls.server = config["mqtt"].get("server", cls.server)
        cls.port = config["mqtt"].getint("port", cls.port)
        cls.base_topic = config["mqtt"].get("base_topic", cls.base_topic)
        cls.user = config["mqtt"].get("user", None)
        cls.password = config["mqtt"].get("password", None)
        cls.use_ssl = config["mqtt"].getboolean("use_ssl", False)
        cls.retain = config["mqtt"].getboolean("retain", False)
        topic_id = config["mqtt"].get("topic_id", cls.topic_id).strip().lower()
        if topic_id not in ("ip", "device_id"):
            logger.warning("Unknown topic_id %r, using 'ip'", topic_id)
            topic_id = "ip"
        cls.topic_id = topic_id
        cls.discovery = config["mqtt"].getboolean("discovery", cls.discovery)
        cls.discovery_prefix = config["mqtt"].get("discovery_prefix", cls.discovery_prefix).strip().strip("/")
    
    @classmethod
    def get_mqtt_client_kwargs(cls, identifier: Optional[str] = None, will: Optional["aiomqtt.Will"] = None) -> dict:
        kwargs = {
            'hostname': cls.server,
            'port': cls.port,
            'username': cls.user,
            'password': cls.password,
            'clean_session': True,
        }
        if identifier is not None:
            kwargs['identifier'] = identifier
        if will is not None:
            kwargs['will'] = will
        if cls.use_ssl:
            kwargs['tls_context'] = ssl.create_default_context()
        return kwargs


async def client_connected_callback(reader, writer):
    addr = writer.get_extra_info('peername')
    dreammakeriotclient = DreamMakerIotClient(reader, writer)
    try:
        await dreammakeriotclient.async_run()
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError) as e:
        logger.warning(f"Client {addr!r} disconnected: {e}")
    except Exception:
        logger.exception(f"Unexpected error handling client {addr!r}")
    finally:
        try:
            await dreammakeriotclient.async_stop()
        except Exception:
            pass
        logger.info(f"Client {addr!r} handler finished, server still listening for new connections.")


async def main():
    server = await asyncio.start_server(client_connected_callback, HOST, PORT)
    
    addr = server.sockets[0].getsockname()
    print(f'Serving on {addr[0]}:{addr[1]}')

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    
    def argparse_type_loglevel(level):
        """
        Type check for log level.
        """
        numeric_level = getattr(logging, level.upper(), None)
        if not isinstance(numeric_level, int):
            raise ValueError('Invalid log level: %s' % level)
        else:
            return numeric_level
    
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", nargs='?', default="dmiot2mqtt.ini", help="Config file location")
    parser.add_argument("-l", "--loglevel", default="info", nargs='?', help="Sets the log level (DEBUG, INFO, WARNING, ERROR). Default: INFO", type=argparse_type_loglevel)
    args = parser.parse_args()
    
    # Set up logging
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(args.loglevel)
    
    # Read config and store it in global class scope
    MqttConfig.read_config(args.config)

    # Async main loop
    asyncio.run(main())
