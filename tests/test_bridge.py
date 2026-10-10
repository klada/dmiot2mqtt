"""
Tests for dmiot2mqtt.

The integration tests start a real mosquitto broker on a free port and talk
to the bridge through a simulated fan. They are skipped when mosquitto is
not installed.

Run with:  python3 -m pytest tests
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import aiomqtt
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dmiot2mqtt  # noqa: E402
from dmiot2mqtt import BridgeConfig, JsonStreamParser, MqttConfig  # noqa: E402

DEVICE_ID = "ABCDEF0123456789ABCDEF01"
AUTH = {
    "action": 1, "resource_id": 2001, "version": "zeico_3.0.0", "msg_id": 0,
    "data": {"device_id": DEVICE_ID, "device_key": "secretkey0000000"},
}
HEARTBEAT = {"action": 1, "resource_id": 127, "version": "zeico_3.0.0", "data": {"signal": 90}}
STATE = {
    "action": 84, "resource_id": 9031, "version": "zeico_3.0.0", "msg_id": 1,
    "data": {"power": 1, "speed": 40, "mode": 0, "source": "manual", "temperature": 23.6},
}

HAS_MOSQUITTO = shutil.which("mosquitto") is not None
needs_broker = pytest.mark.skipif(not HAS_MOSQUITTO, reason="mosquitto not installed")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ----------------------------------------------------------------------
# Parser
# ----------------------------------------------------------------------

def test_parser_splits_concatenated_objects():
    parser = JsonStreamParser()
    assert parser.feed((json.dumps(AUTH) + json.dumps(HEARTBEAT)).encode()) == [AUTH, HEARTBEAT]


def test_parser_waits_for_rest_of_object():
    parser = JsonStreamParser()
    raw = json.dumps(STATE).encode()
    cut = raw.index(b"manual") + 3  # inside a string value
    assert parser.feed(raw[:cut]) == []
    assert parser.feed(raw[cut:]) == [STATE]


def test_parser_ignores_braces_and_quotes_inside_strings():
    parser = JsonStreamParser()
    message = {"action": 1, "data": {"ssid": 'my{wifi}"x\\'}}
    assert parser.feed(json.dumps(message).encode()) == [message]


def test_parser_handles_utf8_split_across_reads():
    parser = JsonStreamParser()
    message = {"data": {"ssid": "Łódź"}}
    raw = json.dumps(message, ensure_ascii=False).encode()
    cut = raw.index("Ł".encode()) + 1  # inside the two-byte sequence
    assert parser.feed(raw[:cut]) == []
    assert parser.feed(raw[cut:]) == [message]


def test_parser_skips_invalid_object_and_junk():
    parser = JsonStreamParser()
    data = b'garbage {"broken": tru}' + json.dumps(HEARTBEAT).encode()
    assert parser.feed(data) == [HEARTBEAT]


def test_parser_resynchronises_after_unterminated_object():
    parser = JsonStreamParser(max_size=200)
    heartbeat = json.dumps(HEARTBEAT).encode()
    messages = parser.feed(b'{"x": 1' + heartbeat * 10)
    assert messages == [HEARTBEAT] * 10


def test_parser_drops_deeply_nested_data():
    parser = JsonStreamParser()
    # Without a depth limit this would be rescanned byte by byte
    assert parser.feed(b"{" * 20000) == []
    assert len(parser._buffer) <= dmiot2mqtt.MAX_DEPTH


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

@pytest.fixture
def clean_config(monkeypatch):
    for name in ("server", "port", "base_topic", "user", "password", "use_ssl", "retain", "topic_id"):
        monkeypatch.setattr(MqttConfig, name, getattr(MqttConfig, name))
    monkeypatch.setattr(BridgeConfig, "read_timeout", BridgeConfig.read_timeout)


def test_config_reads_all_options(tmp_path, clean_config):
    ini = tmp_path / "test.ini"
    ini.write_text(
        "[mqtt]\nserver = broker\nport = 8883\nuse_ssl = 1\nretain = 1\ntopic_id = device_id\n"
        "[bridge]\nread_timeout = 30\n"
    )
    MqttConfig.read_config(str(ini))
    kwargs = MqttConfig.get_mqtt_client_kwargs()
    assert kwargs["hostname"] == "broker"
    assert kwargs["port"] == 8883
    assert kwargs["tls_context"] is not None
    assert MqttConfig.retain is True
    assert MqttConfig.topic_id == "device_id"
    assert BridgeConfig.read_timeout == 30


def test_config_defaults_and_unknown_topic_id(tmp_path, clean_config):
    ini = tmp_path / "test.ini"
    ini.write_text("[mqtt]\nserver = broker\ntopic_id = mac\n")
    MqttConfig.read_config(str(ini))
    kwargs = MqttConfig.get_mqtt_client_kwargs()
    assert kwargs["port"] == 1883
    assert "tls_context" not in kwargs
    assert MqttConfig.retain is False
    assert MqttConfig.topic_id == "ip"


# ----------------------------------------------------------------------
# Integration helpers
# ----------------------------------------------------------------------

def start_mosquitto(port):
    conf = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False)
    conf.write(f"listener {port} 127.0.0.1\nallow_anonymous true\n")
    conf.close()
    proc = subprocess.Popen(
        ["mosquitto", "-c", conf.name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    return proc, conf.name


def stop_mosquitto(proc, conf_name):
    proc.terminate()
    proc.wait()
    os.unlink(conf_name)


class FakeFan:
    """Plays the fan side of the protocol."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        # Commands from tests can be larger than anything a fan sends
        self.parser = JsonStreamParser(max_size=10 * 1024 * 1024)
        self.inbox = []

    async def send(self, *messages):
        self.writer.write(b"".join(json.dumps(m).encode() for m in messages))
        await self.writer.drain()

    async def send_raw(self, data: bytes):
        self.writer.write(data)
        await self.writer.drain()

    async def receive(self, timeout=3.0) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not self.inbox:
            data = await asyncio.wait_for(self.reader.read(65536), max(deadline - loop.time(), 0.01))
            if not data:
                raise ConnectionError("bridge closed the connection")
            self.inbox.extend(self.parser.feed(data))
        return self.inbox.pop(0)

    async def receive_ack(self, resource_id):
        message = await self.receive()
        assert message["action"] == 81 and message["resource_id"] == resource_id, message
        return message

    async def authenticate(self, auth=AUTH):
        await self.send(auth)
        await self.receive_ack(2001)

    async def is_closed_by_bridge(self, timeout=3.0) -> bool:
        try:
            return await asyncio.wait_for(self.reader.read(1024), timeout) == b""
        except asyncio.TimeoutError:
            # Checked first: since Python 3.11 this is the builtin
            # TimeoutError, which is a subclass of OSError
            return False
        except (ConnectionError, OSError):
            return True

    def abort(self):
        self.writer.transport.abort()

    async def close(self):
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass


class Subscriber:
    """Collects everything published below a topic filter."""

    def __init__(self, broker_port, topic_filter):
        self.broker_port = broker_port
        self.topic_filter = topic_filter
        self.messages = []
        self._event = asyncio.Event()

    async def __aenter__(self):
        self._client = aiomqtt.Client("127.0.0.1", port=self.broker_port)
        await self._client.__aenter__()
        await self._client.subscribe(self.topic_filter)
        self._task = asyncio.ensure_future(self._collect())
        return self

    async def __aexit__(self, *exc):
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        await self._client.__aexit__(*exc)

    async def _collect(self):
        async for message in self._client.messages:
            self.messages.append(message)
            self._event.set()

    async def wait_for(self, topic, payload=None, timeout=3.0):
        """Waits for a message on `topic` (and with `payload`, if given)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for message in self.messages:
                if message.topic.matches(topic) and (payload is None or message.payload == payload):
                    return message
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AssertionError(f"no message on {topic} with payload {payload!r}: {self.payloads(topic)}")
            self._event.clear()
            try:
                await asyncio.wait_for(self._event.wait(), remaining)
            except asyncio.TimeoutError:
                pass

    def payloads(self, topic):
        return [m.payload for m in self.messages if m.topic.matches(topic)]


async def retained_payload(broker_port, topic, timeout=0.5):
    """Returns the retained payload of `topic`, or None."""
    async with Subscriber(broker_port, topic) as subscriber:
        try:
            message = await subscriber.wait_for(topic, timeout=timeout)
        except AssertionError:
            return None
        return message.payload if message.retain else None


async def wait_until(condition, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


# ----------------------------------------------------------------------
# Integration fixtures
# ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def broker():
    if not HAS_MOSQUITTO:
        pytest.skip("mosquitto not installed")
    port = free_port()
    proc, conf_name = start_mosquitto(port)
    yield port
    stop_mosquitto(proc, conf_name)


@pytest.fixture
def bridge_config(monkeypatch, request, clean_config):
    """Points the bridge at a broker port; each test gets its own topics."""
    def configure(broker_port, **options):
        monkeypatch.setattr(MqttConfig, "server", "127.0.0.1")
        monkeypatch.setattr(MqttConfig, "port", broker_port)
        monkeypatch.setattr(MqttConfig, "base_topic", f"t{abs(hash(request.node.nodeid)) % 10**8}")
        monkeypatch.setattr(MqttConfig, "retain", options.get("retain", True))
        monkeypatch.setattr(MqttConfig, "topic_id", options.get("topic_id", "ip"))
        monkeypatch.setattr(BridgeConfig, "read_timeout", options.get("read_timeout", 90))
    monkeypatch.setattr(dmiot2mqtt, "MQTT_RECONNECT_DELAY", 0.2)
    monkeypatch.setattr(dmiot2mqtt, "MQTT_MAX_RECONNECT_DELAY", 1)
    monkeypatch.setattr(dmiot2mqtt, "ACTIVE_SESSIONS", {})
    return configure


@pytest.fixture
async def bridge(bridge_config):
    """Starts the bridge server. Yields a factory for connected fans."""
    server = await asyncio.start_server(dmiot2mqtt.client_connected_callback, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    fans = []

    async def connect_fan(receive_buffer=None):
        sock = socket.socket()
        if receive_buffer is not None:
            # Must be set before connecting to limit the TCP window
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
        sock.connect(("127.0.0.1", port))
        sock.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=sock)
        fan = FakeFan(reader, writer)
        fans.append(fan)
        return fan

    yield connect_fan

    # Abort fan connections first: since Python 3.12 wait_closed() waits
    # for open connections, so a failed test would otherwise hang here.
    for fan in fans:
        fan.abort()
    try:
        await wait_until(lambda: not dmiot2mqtt.ACTIVE_SESSIONS, timeout=5)
    except AssertionError:
        pass
    server.close()
    try:
        await asyncio.wait_for(server.wait_closed(), 5)
    except asyncio.TimeoutError:
        pass


def topic(*parts, fan_id="127.0.0.1"):
    return "/".join([MqttConfig.base_topic, fan_id.lower(), *parts])


async def connect_online(connect_fan, subscriber, auth=AUTH, fan_id="127.0.0.1", **options):
    """Connects and authenticates a fan, waits until the bridge is online on MQTT."""
    fan = await connect_fan(**options)
    await fan.authenticate(auth)
    await subscriber.wait_for(topic("availability", fan_id=fan_id), b"online")
    return fan


# ----------------------------------------------------------------------
# Fan side
# ----------------------------------------------------------------------

@needs_broker
async def test_concatenated_auth_and_heartbeat_are_both_acked(broker, bridge, bridge_config):
    bridge_config(broker)
    fan = await bridge()
    await fan.send(AUTH, HEARTBEAT)
    await fan.receive_ack(2001)
    await fan.receive_ack(127)


@needs_broker
async def test_message_split_across_writes_is_acked(broker, bridge, bridge_config):
    bridge_config(broker)
    fan = await bridge()
    await fan.authenticate()
    raw = json.dumps(STATE).encode()
    await fan.send_raw(raw[:20])
    await asyncio.sleep(0.1)
    await fan.send_raw(raw[20:])
    await fan.receive_ack(9031)


@needs_broker
async def test_empty_object_does_not_end_session(broker, bridge, bridge_config):
    bridge_config(broker)
    fan = await bridge()
    await fan.send_raw(b"{}")
    await fan.authenticate()
    await fan.send_raw(b'{}{"foo": 1}')
    await fan.send(HEARTBEAT)
    await fan.receive_ack(127)


@needs_broker
async def test_message_with_non_numeric_resource_id_is_ignored(broker, bridge, bridge_config):
    bridge_config(broker)
    fan = await bridge()
    await fan.authenticate()
    await fan.send_raw(b'{"action": 1, "resource_id": [1]}{"action": "1", "resource_id": 127}')
    await fan.send(HEARTBEAT)
    await fan.receive_ack(127)


@needs_broker
async def test_negative_read_timeout_disables_it(broker, bridge, bridge_config):
    bridge_config(broker, read_timeout=-1)
    fan = await bridge()
    await fan.authenticate()
    await asyncio.sleep(0.3)
    await fan.send(HEARTBEAT)
    await fan.receive_ack(127)


@needs_broker
async def test_ack_and_command_written_while_fan_does_not_read(broker, bridge, bridge_config):
    """
    The MQTT task blocks writing a large command while the main loop has to
    ACK a heartbeat. On Python 3.9 two concurrent drain() calls raise
    AssertionError, so the writes have to be serialised.
    """
    bridge_config(broker)
    async with Subscriber(broker, f"{MqttConfig.base_topic}/#") as subscriber:
        fan = await connect_online(bridge, subscriber, receive_buffer=2048)
        session = next(iter(dmiot2mqtt.ACTIVE_SESSIONS.values()))
        session.stream_writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2048)
        fan.writer.transport.pause_reading()
        async with aiomqtt.Client("127.0.0.1", port=broker) as client:
            await client.publish(topic("command"), json.dumps({"x": "y" * 150000}).encode())
        await asyncio.sleep(0.5)
        await fan.send(HEARTBEAT)
        await asyncio.sleep(0.5)
        fan.writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        fan.writer.transport.resume_reading()
        actions = []
        while 81 not in actions:
            actions.append((await fan.receive(timeout=10))["action"])
        assert actions == [4, 81]
        # The session survived both writes
        await fan.send(HEARTBEAT)
        await fan.receive_ack(127)
        assert dmiot2mqtt.ACTIVE_SESSIONS


@needs_broker
async def test_fan_that_stops_reading_is_disconnected(broker, bridge, bridge_config, monkeypatch):
    bridge_config(broker)
    monkeypatch.setattr(dmiot2mqtt, "WRITE_TIMEOUT", 0.5)
    async with Subscriber(broker, f"{MqttConfig.base_topic}/#") as subscriber:
        fan = await connect_online(bridge, subscriber, receive_buffer=2048)
        session = next(iter(dmiot2mqtt.ACTIVE_SESSIONS.values()))
        session.stream_writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2048)
        fan.writer.transport.pause_reading()
        async with aiomqtt.Client("127.0.0.1", port=broker) as client:
            await client.publish(topic("command"), json.dumps({"x": "y" * 200000}).encode())
        await subscriber.wait_for(topic("availability"), b"offline", timeout=5)
    await wait_until(lambda: not dmiot2mqtt.ACTIVE_SESSIONS)


@needs_broker
async def test_session_ends_although_fan_never_reads_its_data(broker, bridge, bridge_config, monkeypatch):
    """Closing the connection must not wait for data the fan never reads."""
    bridge_config(broker, read_timeout=1)
    monkeypatch.setattr(dmiot2mqtt, "WRITE_TIMEOUT", 30)
    monkeypatch.setattr(dmiot2mqtt, "CLOSE_TIMEOUT", 0.5)
    async with Subscriber(broker, f"{MqttConfig.base_topic}/#") as subscriber:
        fan = await connect_online(bridge, subscriber, receive_buffer=2048)
        session = next(iter(dmiot2mqtt.ACTIVE_SESSIONS.values()))
        session.stream_writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2048)
        fan.writer.transport.pause_reading()
        async with aiomqtt.Client("127.0.0.1", port=broker) as client:
            await client.publish(topic("command"), json.dumps({"x": "y" * 150000}).encode())
        # The fan stays silent, so the read timeout ends the session
        await subscriber.wait_for(topic("availability"), b"offline", timeout=5)

    def session_tasks():
        return [t for t in asyncio.all_tasks() if "client_connected_callback" in repr(t.get_coro())]

    await wait_until(lambda: not session_tasks(), timeout=3)
    # The unread data was dropped instead of keeping the connection open
    assert session.stream_writer.transport.get_write_buffer_size() == 0


@needs_broker
async def test_silent_connection_is_closed_after_read_timeout(broker, bridge, bridge_config):
    bridge_config(broker, read_timeout=1)
    async with Subscriber(broker, f"{MqttConfig.base_topic}/#") as subscriber:
        fan = await connect_online(bridge, subscriber)
        assert await fan.is_closed_by_bridge(timeout=3)
        await subscriber.wait_for(topic("availability"), b"offline")


# ----------------------------------------------------------------------
# MQTT side
# ----------------------------------------------------------------------

@needs_broker
async def test_state_is_published_retained(broker, bridge, bridge_config):
    bridge_config(broker, retain=True)
    async with Subscriber(broker, f"{MqttConfig.base_topic}/#") as subscriber:
        fan = await connect_online(bridge, subscriber)
        await fan.send(STATE)
        await fan.receive_ack(9031)
        await subscriber.wait_for(topic())
    assert json.loads(await retained_payload(broker, topic())) == STATE["data"]
    assert await retained_payload(broker, topic("availability")) == b"online"


@needs_broker
async def test_state_is_not_retained_by_default(broker, bridge, bridge_config):
    bridge_config(broker, retain=False)
    async with Subscriber(broker, f"{MqttConfig.base_topic}/#") as subscriber:
        fan = await connect_online(bridge, subscriber)
        await fan.send(STATE)
        await fan.receive_ack(9031)
        message = await subscriber.wait_for(topic())
        assert json.loads(message.payload) == STATE["data"]
    assert await retained_payload(broker, topic()) is None


@needs_broker
async def test_device_key_is_never_published(broker, bridge, bridge_config):
    bridge_config(broker)
    async with Subscriber(broker, f"{MqttConfig.base_topic}/#") as subscriber:
        fan = await connect_online(bridge, subscriber)
        await fan.send(AUTH)  # re-auth during a session
        await fan.receive_ack(2001)
        await fan.send(STATE)
        await fan.receive_ack(9031)
        await subscriber.wait_for(topic())
        assert all(b"secretkey" not in m.payload for m in subscriber.messages)


@needs_broker
async def test_command_reaches_fan_and_invalid_payloads_are_ignored(broker, bridge, bridge_config):
    bridge_config(broker)
    async with Subscriber(broker, f"{MqttConfig.base_topic}/#") as subscriber:
        fan = await connect_online(bridge, subscriber)
        async with aiomqtt.Client("127.0.0.1", port=broker) as client:
            await client.publish(topic("command"), b"not json")
            await client.publish(topic("command"), b"[1, 2]")
            await client.publish(topic("command"), b"{}")
            await client.publish(topic("command"), b'{"power": 1}')
        command = await fan.receive()
    assert command["action"] == 4
    assert command["resource_id"] == 9031
    assert command["data"] == {"power": 1}


@needs_broker
async def test_disconnect_publishes_offline_and_leaves_no_tasks(broker, bridge, bridge_config):
    bridge_config(broker)
    async with Subscriber(broker, f"{MqttConfig.base_topic}/#") as subscriber:
        fan = await connect_online(bridge, subscriber)
        await fan.close()
        await subscriber.wait_for(topic("availability"), b"offline")
    await wait_until(lambda: not dmiot2mqtt.ACTIVE_SESSIONS)
    await asyncio.sleep(0.2)
    leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert leftover == []


# ----------------------------------------------------------------------
# Reconnecting fan
# ----------------------------------------------------------------------

@needs_broker
@pytest.mark.parametrize("old_ends", ["clean_close", "reset"])
async def test_reconnecting_fan_replaces_old_session(broker, bridge, bridge_config, old_ends):
    bridge_config(broker)
    async with Subscriber(broker, f"{MqttConfig.base_topic}/#") as subscriber:
        old = await connect_online(bridge, subscriber)
        new = await bridge()
        await new.authenticate()

        # The bridge closes the old connection itself
        assert await old.is_closed_by_bridge()
        if old_ends == "clean_close":
            await old.close()
        else:
            old.abort()
        await asyncio.sleep(0.5)

        # The fan is connected through the new session, so it stays online
        assert await retained_payload(broker, topic("availability")) == b"online"
        assert len(dmiot2mqtt.ACTIVE_SESSIONS) == 1

        # Commands go to the new connection only
        async with aiomqtt.Client("127.0.0.1", port=broker) as client:
            await client.publish(topic("command"), b'{"power": 0}')
        assert (await new.receive())["data"] == {"power": 0}
        await new.send(HEARTBEAT)
        await new.receive_ack(127)


@needs_broker
async def test_mqtt_identifier_is_stable_per_fan(broker, bridge, bridge_config, monkeypatch):
    bridge_config(broker)
    identifiers = []

    class RecordingClient(aiomqtt.Client):
        def __init__(self, *args, **kwargs):
            identifiers.append(kwargs.get("identifier"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(dmiot2mqtt.aiomqtt, "Client", RecordingClient)
    async with Subscriber(broker, f"{MqttConfig.base_topic}/#") as subscriber:
        first = await connect_online(bridge, subscriber)
        await first.close()
        await subscriber.wait_for(topic("availability"), b"offline")
        subscriber.messages.clear()
        await connect_online(bridge, subscriber)
    bridge_ids = [i for i in identifiers if i is not None]
    assert len(bridge_ids) >= 2 and len(set(bridge_ids)) == 1, identifiers


@needs_broker
async def test_subscribes_before_announcing_online(broker, bridge, bridge_config, monkeypatch):
    """A client that sends a command as soon as it sees "online" must not lose it."""
    bridge_config(broker)
    events = []

    class RecordingClient(aiomqtt.Client):
        # Only the bridge passes a client id; the test's own clients do not
        def __init__(self, *args, **kwargs):
            self.is_bridge = kwargs.get("identifier") is not None
            super().__init__(*args, **kwargs)

        async def subscribe(self, topic_filter, *args, **kwargs):
            if self.is_bridge:
                events.append("subscribe")
            return await super().subscribe(topic_filter, *args, **kwargs)

        async def publish(self, topic_name, payload=None, *args, **kwargs):
            if self.is_bridge and str(topic_name).endswith("/availability"):
                events.append(payload)
            return await super().publish(topic_name, payload, *args, **kwargs)

    monkeypatch.setattr(dmiot2mqtt.aiomqtt, "Client", RecordingClient)
    async with Subscriber(broker, f"{MqttConfig.base_topic}/#") as subscriber:
        await connect_online(bridge, subscriber)
    assert events[:2] == ["subscribe", b"online"], events


@needs_broker
async def test_stale_broker_session_cannot_override_online(broker, bridge, bridge_config):
    """
    The bridge loses its broker connection, but the broker notices only later
    and then publishes the old last will. With a fixed client id the broker
    replaces the old session when the bridge reconnects, so no late "offline".
    """
    links = []

    async def handle(client_reader, client_writer):
        broker_reader, broker_writer = await asyncio.open_connection("127.0.0.1", broker)
        links.append((client_writer, broker_writer))

        async def pump(reader, writer):
            try:
                while True:
                    data = await reader.read(4096)
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
            except (ConnectionError, OSError):
                pass

        await asyncio.gather(pump(client_reader, broker_writer), pump(broker_reader, client_writer))

    proxy = await asyncio.start_server(handle, "127.0.0.1", 0)
    bridge_config(proxy.sockets[0].getsockname()[1])
    try:
        async with Subscriber(broker, f"{MqttConfig.base_topic}/#") as subscriber:
            fan = await bridge()
            await fan.authenticate()
            await subscriber.wait_for(topic("availability"), b"online")
            client_side, broker_side = links[0]
            client_side.transport.abort()   # the bridge sees the link drop ...
            await asyncio.sleep(1.0)        # ... and reconnects through the proxy
            broker_side.transport.abort()   # the broker sees it only now
            await asyncio.sleep(0.5)
        assert await retained_payload(broker, topic("availability")) == b"online"
        await fan.send(HEARTBEAT)
        await fan.receive_ack(127)
    finally:
        for client_writer, broker_writer in links:
            client_writer.transport.abort()
            broker_writer.transport.abort()
        proxy.close()


@needs_broker
async def test_offline_when_new_session_ends_before_mqtt_connects(broker, bridge, bridge_config):
    bridge_config(broker)
    async with Subscriber(broker, f"{MqttConfig.base_topic}/#") as subscriber:
        await connect_online(bridge, subscriber)
        flaky = await bridge()
        await flaky.authenticate()
        flaky.abort()  # WiFi drops again right after reconnecting
        await wait_until(lambda: not dmiot2mqtt.ACTIVE_SESSIONS)
        await subscriber.wait_for(topic("availability"), b"offline")
    assert await retained_payload(broker, topic("availability")) == b"offline"


@needs_broker
async def test_two_fans_behind_one_ip_do_not_kick_each_other(broker, bridge, bridge_config, caplog):
    bridge_config(broker)
    first_auth = json.loads(json.dumps(AUTH))
    second_auth = json.loads(json.dumps(AUTH))
    second_auth["data"]["device_id"] = "2222222222222222222222BB"
    async with Subscriber(broker, f"{MqttConfig.base_topic}/#") as subscriber:
        first = await connect_online(bridge, subscriber, auth=first_auth)
        second = await bridge()
        await second.authenticate(second_auth)
        await asyncio.sleep(1.0)
        # Both connections stay open and keep working
        assert not await first.is_closed_by_bridge(timeout=0.1)
        assert not await second.is_closed_by_bridge(timeout=0.1)
        for fan in (first, second):
            await fan.send(HEARTBEAT)
            await fan.receive_ack(127)
    assert "share the topic" in caplog.text
    assert "MQTT connection lost" not in caplog.text


# ----------------------------------------------------------------------
# Broker outages
# ----------------------------------------------------------------------

@needs_broker
async def test_broker_down_when_fan_connects(bridge, bridge_config):
    port = free_port()
    bridge_config(port)
    fan = await bridge()
    await fan.authenticate()
    await fan.send(STATE)
    await fan.receive_ack(9031)

    proc, conf_name = start_mosquitto(port)
    try:
        async with Subscriber(port, f"{MqttConfig.base_topic}/#") as subscriber:
            await subscriber.wait_for(topic("availability"), b"online", timeout=5)
            message = await subscriber.wait_for(topic(), timeout=5)
            assert json.loads(message.payload) == STATE["data"]
    finally:
        fan.abort()
        stop_mosquitto(proc, conf_name)


@needs_broker
async def test_broker_restart_keeps_fan_connected(bridge, bridge_config):
    port = free_port()
    bridge_config(port)
    proc, conf_name = start_mosquitto(port)
    try:
        async with Subscriber(port, f"{MqttConfig.base_topic}/#") as subscriber:
            fan = await connect_online(bridge, subscriber)

        stop_mosquitto(proc, conf_name)
        proc = None
        await asyncio.sleep(0.5)
        # The fan session survives the outage, the state is kept for later
        await fan.send(HEARTBEAT, STATE)
        await fan.receive_ack(127)
        await fan.receive_ack(9031)

        proc, conf_name = start_mosquitto(port)
        async with Subscriber(port, f"{MqttConfig.base_topic}/#") as subscriber:
            await subscriber.wait_for(topic("availability"), b"online", timeout=5)
            message = await subscriber.wait_for(topic(), timeout=5)
            assert json.loads(message.payload) == STATE["data"]
            # The command subscription is restored as well
            async with aiomqtt.Client("127.0.0.1", port=port) as client:
                await client.publish(topic("command"), b'{"power": 1}')
            assert (await fan.receive())["data"] == {"power": 1}
    finally:
        if proc is not None:
            stop_mosquitto(proc, conf_name)


# ----------------------------------------------------------------------
# topic_id
# ----------------------------------------------------------------------

@needs_broker
async def test_topic_by_device_id(broker, bridge, bridge_config):
    bridge_config(broker, topic_id="device_id")
    async with Subscriber(broker, f"{MqttConfig.base_topic}/#") as subscriber:
        fan = await connect_online(bridge, subscriber, fan_id=DEVICE_ID)
        await fan.send(STATE)
        await fan.receive_ack(9031)
        message = await subscriber.wait_for(topic(fan_id=DEVICE_ID))
        assert json.loads(message.payload) == STATE["data"]


@needs_broker
async def test_all_zero_device_id_falls_back_to_ip(broker, bridge, bridge_config):
    bridge_config(broker, topic_id="device_id")
    zero_auth = json.loads(json.dumps(AUTH))
    zero_auth["data"]["device_id"] = "000000000000000000000000"
    async with Subscriber(broker, f"{MqttConfig.base_topic}/#") as subscriber:
        fan = await connect_online(bridge, subscriber, auth=zero_auth)
        await fan.send(STATE)
        await fan.receive_ack(9031)
        await subscriber.wait_for(topic())
        assert not any("0000000000" in str(m.topic) for m in subscriber.messages)
