"""Exercise the installed SDK's real blocking start() without network access."""
import asyncio
import threading
import unittest
from unittest.mock import patch

from scripts.tests import support  # Isolate config before importing app services.
from app.services import feishu_ws
import lark_oapi.ws.client as sdk_ws


class FakeConnection:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FeishuWebsocketLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.original_loop = sdk_ws.loop
        self.connected = threading.Event()
        self.connection_changed = threading.Condition()
        self.clients = []
        self.loops = []
        self.connections = []
        self.connect_gate = None

        async def connect(client):
            with self.connection_changed:
                self.clients.append(client)
                self.loops.append(asyncio.get_running_loop())
                connection = FakeConnection()
                self.connections.append(connection)
                client._conn = connection
                self.connected.set()
                self.connection_changed.notify_all()
            if self.connect_gate is not None:
                # Mimic the SDK's synchronous endpoint-discovery HTTP call.
                self.connect_gate.wait(timeout=3)

        async def ping(client):
            await asyncio.Event().wait()

        for target, replacement in (
            ("app.services.feishu_ws.receive_mode", lambda: "sdk"),
            ("app.services.feishu_ws._credentials", lambda: ("cli_test", "test-secret")),
            ("lark_oapi.ws.client.Client._connect", connect),
            ("lark_oapi.ws.client.Client._ping_loop", ping),
        ):
            patcher = patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target in ("requests.sessions.Session.request", "websockets.connect"):
            patcher = patch(target, side_effect=AssertionError("Network disabled"))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.cleanup_worker)

    def cleanup_worker(self):
        if self.connect_gate is not None:
            self.connect_gate.set()
        feishu_ws.stop()
        sdk_ws.loop = self.original_loop

    def start(self):
        self.connected.clear()
        feishu_ws.restart(object())
        self.assertTrue(self.connected.wait(timeout=3), feishu_ws._status)
        self.assertTrue(feishu_ws._thread.is_alive())
        self.assertEqual(feishu_ws._status["error"], "")

    def test_repeated_save_restarts_real_sdk_and_closes_old_connections(self):
        for _ in range(4):
            self.start()
        self.assertEqual(len({id(loop) for loop in self.loops}), 4)
        self.assertTrue(all(loop.is_closed() for loop in self.loops[:-1]))
        self.assertTrue(all(connection.closed for connection in self.connections[:-1]))
        self.assertFalse(self.connections[-1].closed)
        # SDK caches and websocket coroutines must share the worker's loop.
        self.assertIs(self.clients[-1]._cache._cron.get_loop(), self.loops[-1])

        feishu_ws.stop()
        self.assertIsNone(feishu_ws._thread)
        self.assertIsNone(feishu_ws._client)
        self.assertFalse(feishu_ws._status["running"])
        self.assertTrue(self.loops[-1].is_closed())
        self.assertTrue(self.connections[-1].closed)

    def test_switching_to_callback_closes_sdk_connection(self):
        self.start()
        with patch.object(feishu_ws, "receive_mode", return_value="callback"):
            feishu_ws.restart(object())
        self.assertTrue(self.connections[0].closed)
        self.assertTrue(self.loops[0].is_closed())
        self.assertIsNone(feishu_ws._thread)
        self.assertEqual(feishu_ws._status["error"], "")

    def test_blocked_old_connection_prevents_a_second_sdk_thread(self):
        self.connect_gate = threading.Event()
        self.start()
        original_thread = feishu_ws._thread
        with patch.object(feishu_ws, "_STOP_TIMEOUT", 0.01):
            feishu_ws.restart(object())
        self.assertIs(feishu_ws._thread, original_thread)
        self.assertEqual(len(self.clients), 1)
        self.assertIn("旧飞书连接尚未退出", feishu_ws._status["error"])
        self.connect_gate.set()
        original_thread.join(timeout=3)
        self.assertFalse(original_thread.is_alive())
        self.assertTrue(self.connections[0].closed)
        self.start()
        self.assertEqual(len(self.clients), 2)

    def test_simultaneous_saves_never_leave_multiple_sdk_workers(self):
        errors = []

        def save():
            try:
                feishu_ws.restart(object())
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=save) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        # An earlier worker may have set connected before being replaced. Wait
        # for the current client itself; restart() only launches its thread.
        with self.connection_changed:
            self.assertTrue(self.connection_changed.wait_for(
                lambda: feishu_ws._client is not None and feishu_ws._client in self.clients,
                timeout=3,
            ))
        self.assertEqual(feishu_ws._status["error"], "")
        self.assertEqual(sum(not loop.is_closed() for loop in self.loops), 1)
        feishu_ws.stop()
        self.assertTrue(all(connection.closed for connection in self.connections))


if __name__ == "__main__":
    unittest.main()
