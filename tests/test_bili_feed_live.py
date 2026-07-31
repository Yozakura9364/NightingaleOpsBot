from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "astrbot-plugin" / "astrbot_plugin_bili_feed"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


feed_client = load_module("bili_feed_live_client_test", PLUGIN_DIR / "feed_client.py")
storage = load_module("bili_feed_live_storage_test", PLUGIN_DIR / "storage.py")


class LiveRoomStatusTests(unittest.TestCase):
    def test_parse_live_room_status(self):
        status = feed_client._parse_live_room_status(
            "6655514",
            {
                "code": 0,
                "data": {
                    "roomid": 5225,
                    "liveStatus": 1,
                    "liveTime": "2026-07-25 09:30:00",
                    "title": "测试直播",
                    "url": "//live.bilibili.com/5225",
                    "cover": "//example.test/live.jpg",
                },
            },
        )

        self.assertEqual(status.uid, "6655514")
        self.assertEqual(status.room_id, "5225")
        self.assertTrue(status.is_live)
        self.assertEqual(status.link, "https://live.bilibili.com/5225")
        self.assertEqual(status.cover_url, "https://example.test/live.jpg")
        self.assertEqual(status.live_started_at, "2026-07-25 09:30:00")

    def test_parse_live_room_status_rejects_api_error(self):
        with self.assertRaisesRegex(RuntimeError, "直播接口返回错误"):
            feed_client._parse_live_room_status(
                "6655514",
                {"code": -400, "message": "请求错误"},
            )


class LiveStateTests(unittest.TestCase):
    def test_live_state_persists_event_and_clears_failure(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = storage.BiliFeedStore(Path(temporary_directory))
            self.assertIsNone(store.get_live_state("6655514"))

            self.assertEqual(store.record_live_failure(uid="6655514", error="timeout"), 1)
            failed = store.get_live_state("6655514")
            self.assertIsNotNone(failed)
            self.assertEqual(failed.failure_count, 1)

            store.record_live_success(
                uid="6655514",
                room_id="5225",
                is_live=True,
                live_started_at="2026-07-25 09:30:00",
                event_key="bili-live:6655514:5225:2026-07-25 09:30:00",
                title="测试直播",
            )
            state = store.get_live_state("6655514")
            self.assertTrue(state.is_live)
            self.assertEqual(state.room_id, "5225")
            self.assertEqual(state.event_key, "bili-live:6655514:5225:2026-07-25 09:30:00")
            self.assertEqual(state.failure_count, 0)
            self.assertEqual(state.last_error, "")


if __name__ == "__main__":
    unittest.main()
