from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import IsolatedAsyncioTestCase


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "astrbot-plugin" / "astrbot_plugin_github_watch"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


storage = load_module("github_watch_storage_test", PLUGIN_DIR / "storage.py")
delivery = load_module("github_watch_delivery_test", PLUGIN_DIR / "delivery.py")


class DeliveryStateTests(unittest.TestCase):
    def test_delivery_is_recorded_only_when_marked(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = storage.GitHubWatchStore(Path(temporary_directory))
            event_key = "github:push:owner/repo:main:abc123"
            target_origin = "napcat:GroupMessage:test"

            self.assertFalse(
                store.is_delivered(event_key=event_key, target_origin=target_origin)
            )
            self.assertTrue(
                store.mark_delivered(event_key=event_key, target_origin=target_origin)
            )
            self.assertTrue(
                store.is_delivered(event_key=event_key, target_origin=target_origin)
            )
            self.assertFalse(
                store.mark_delivered(event_key=event_key, target_origin=target_origin)
            )


class DeliveryOrderTests(IsolatedAsyncioTestCase):
    async def test_failed_send_is_not_marked_and_can_retry(self):
        calls = []

        class Store:
            delivered = False

            def is_delivered(self, **_kwargs):
                calls.append("check")
                return self.delivered

            def mark_delivered(self, **_kwargs):
                calls.append("mark")
                self.delivered = True
                return True

        store = Store()

        async def failed_send():
            calls.append("send-failed")
            raise RuntimeError("QQ send failed")

        with self.assertRaisesRegex(RuntimeError, "QQ send failed"):
            await delivery.deliver_once(
                store=store,
                event_key="event",
                target_origin="target",
                send=failed_send,
            )
        self.assertFalse(store.delivered)
        self.assertEqual(calls, ["check", "send-failed"])

        async def successful_send():
            calls.append("send-ok")

        delivered = await delivery.deliver_once(
            store=store,
            event_key="event",
            target_origin="target",
            send=successful_send,
        )
        self.assertTrue(delivered)
        self.assertEqual(calls[-3:], ["check", "send-ok", "mark"])


if __name__ == "__main__":
    unittest.main()
