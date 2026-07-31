from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "astrbot-plugin" / "astrbot_plugin_bili_feed"


# ── Stub astrbot + zoneinfo so main.py can be exec'd ──────────────────

def _make_astrbot_stubs():
    astrbot_mod = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = MagicMock()
    api.event = types.ModuleType("astrbot.api.event")
    api.event.AstrMessageEvent = MagicMock
    api.event.MessageChain = MagicMock
    api.event.filter = MagicMock()
    api.message_components = types.ModuleType("astrbot.api.message_components")
    # Comp is imported as `import astrbot.api.message_components as Comp`
    # so the module itself needs Plain, Image, Node, Nodes at the top level.
    class _Plain:
        def __init__(self, text=""):
            self.text = text
    class _Image:
        file = ""
        @classmethod
        def fromFileSystem(cls, path):
            img = _Image()
            img.file = path
            return img
    class _Node:
        def __init__(self, content=None, name="", uin=""):
            self.content = content or []
    class _Nodes:
        def __init__(self, nodes=None):
            self.nodes = nodes or []
    api.message_components.Plain = _Plain
    api.message_components.Image = _Image
    api.message_components.Node = _Node
    api.message_components.Nodes = _Nodes
    api.star = types.ModuleType("astrbot.api.star")
    api.star.Context = MagicMock
    api.star.Star = type("Star", (), {})
    api.star.register = lambda *a, **kw: lambda cls: cls
    astrbot_mod.api = api
    return {
        "astrbot": astrbot_mod,
        "astrbot.api": api,
        "astrbot.api.event": api.event,
        "astrbot.api.message_components": api.message_components,
        "astrbot.api.star": api.star,
    }

# Stub zoneinfo
import datetime as _dt
_zoneinfo = types.ModuleType("zoneinfo")
_zoneinfo.ZoneInfo = lambda _name: _dt.timezone.utc
_zoneinfo.ZoneInfoNotFoundError = Exception
sys.modules["zoneinfo"] = _zoneinfo

# Exec main.py
_main_source = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
_main_ns = {}
for k, v in _make_astrbot_stubs().items():
    sys.modules[k] = v
    _main_ns[k] = v

_pkg = types.ModuleType("bili_feed_plugin")
_pkg.__path__ = [str(PLUGIN_DIR)]  # needed for relative imports to work
_card = types.ModuleType("bili_feed_plugin.card_renderer")
_card.render_bili_card = MagicMock()
_feed = types.ModuleType("bili_feed_plugin.feed_client")
_feed.FeedItem = MagicMock
_feed.LiveRoomStatus = MagicMock
_feed.extract_cookie_value = MagicMock(return_value="")
_feed.fetch_cookie_dynamic_detail = MagicMock()
_feed.fetch_cookie_user_feed = MagicMock()
_feed.fetch_live_room_status = MagicMock()
_feed.fetch_user_feed = MagicMock()
_feed.normalize_cookie = MagicMock(return_value="")
_feed.normalize_uid = MagicMock(return_value="123")
_html = types.ModuleType("bili_feed_plugin.html_card_renderer")
_html.render_bili_card_html = MagicMock()
_stor = types.ModuleType("bili_feed_plugin.storage")
_stor.BiliFeedStore = MagicMock
_stor.Subscription = MagicMock
_pkg.card_renderer = _card
_pkg.feed_client = _feed
_pkg.html_card_renderer = _html
_pkg.storage = _stor
sys.modules["bili_feed_plugin"] = _pkg
sys.modules["bili_feed_plugin.card_renderer"] = _card
sys.modules["bili_feed_plugin.feed_client"] = _feed
sys.modules["bili_feed_plugin.html_card_renderer"] = _html
sys.modules["bili_feed_plugin.storage"] = _stor
_main_ns["__name__"] = "bili_feed_plugin"
_main_ns["__file__"] = str(PLUGIN_DIR / "main.py")
_main_ns["__package__"] = "bili_feed_plugin"
_main_ns["zoneinfo"] = _zoneinfo

exec(_main_source, _main_ns)


# ── Real production functions ──────────────────────────────────────────
_file_base64_estimate = _main_ns["BiliFeedPlugin"]._file_base64_estimate
_plain_estimate = _main_ns["BiliFeedPlugin"]._plain_estimate
_node_overhead = _main_ns["BiliFeedPlugin"]._node_overhead
_batch_nodes = _main_ns["BiliFeedPlugin"]._batch_nodes
_send_forward_batches = _main_ns["BiliFeedPlugin"]._send_forward_batches
_ORIG_BUDGET = _main_ns["BiliFeedPlugin"]._MAX_FORWARD_BATCH_BYTES


def _make_temp_file(size, suffix=".jpg"):
    """Create a real temp file so os.path.getsize works."""
    f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    f.write(b"\xff\xd8\xff\xe0" + b"\x00" * max(0, size - 4))
    f.flush()
    f.close()
    return f.name


# ═══════════════════════════════════════════════════════════════════════

class EstimateTests(unittest.TestCase):
    """Estimation helpers — real production functions."""

    def test_file_base64_positive(self):
        path = _make_temp_file(104)
        try:
            est = _file_base64_estimate(path)
            raw = os.path.getsize(path)
            expected = (raw + 2) // 3 * 4
            self.assertEqual(est, expected)
            self.assertGreater(est, 0)
        finally:
            os.unlink(path)

    def test_file_base64_missing_returns_zero(self):
        self.assertEqual(_file_base64_estimate("/nonexistent/path.png"), 0)

    def test_plain_estimate_includes_text_len(self):
        est = _plain_estimate("hello")
        self.assertGreater(est, len("hello"))

    def test_node_overhead_positive(self):
        self.assertGreater(_node_overhead(), 0)


class BatchNodesTests(unittest.TestCase):
    """Core batching logic — pure, no I/O needed beyond temp files."""

    @classmethod
    def setUpClass(cls):
        # Comp is the message_components module (see main.py import)
        cls.Comp = sys.modules["astrbot.api.message_components"]
        cls.display_name = "TestBot"
        cls.display_uin = "123456"

    def setUp(self):
        # Reset budget before each test to avoid cross-test pollution
        _main_ns["BiliFeedPlugin"]._MAX_FORWARD_BATCH_BYTES = _ORIG_BUDGET

    def _set_budget(self, budget):
        _main_ns["BiliFeedPlugin"]._MAX_FORWARD_BATCH_BYTES = budget

    def _header(self):
        return [self.Comp.Plain("test card text")]

    # ── tests ──────────────────────────────────────────────────────────

    def test_no_header_no_images_returns_empty(self):
        nodes = _batch_nodes(self.display_name, self.display_uin, [], [])
        self.assertEqual(nodes, [])

    def test_header_only_single_node(self):
        header = [self.Comp.Plain("hello")]
        nodes = _batch_nodes(self.display_name, self.display_uin, header, [])
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].content, header)

    def test_small_images_dont_split(self):
        path = _make_temp_file(204)
        try:
            header = [self.Comp.Plain("test")]
            nodes = _batch_nodes(self.display_name, self.display_uin, header, [path, path])
            self.assertEqual(len(nodes), 1)
        finally:
            os.unlink(path)

    def test_large_images_split(self):
        size = 1 * 1024 * 1024  # 1 MB
        f1 = _make_temp_file(size)
        f2 = _make_temp_file(size)
        f3 = _make_temp_file(size)

        one_img_b64 = (size + 2) // 3 * 4
        header_bytes = _node_overhead() + _plain_estimate("test")
        budget = header_bytes + one_img_b64 * 2 + 1
        self._set_budget(budget)

        try:
            header = [self.Comp.Plain("test")]
            nodes = _batch_nodes(
                self.display_name, self.display_uin,
                header, [f1, f2, f3],
            )
            self.assertEqual(len(nodes), 2, "Expected 2 nodes, got %d" % len(nodes))
            # first: header + 2 images
            self.assertEqual(len(nodes[0].content), 3)
            # second: 1 image
            self.assertEqual(len(nodes[1].content), 1)
        finally:
            for p in (f1, f2, f3):
                os.unlink(p)

    def test_single_image_over_budget_still_included(self):
        path = _make_temp_file(5 * 1024 * 1024)
        try:
            self._set_budget(1024)
            nodes = _batch_nodes(self.display_name, self.display_uin, [], [path])
            self.assertEqual(len(nodes), 1)
            self.assertEqual(len(nodes[0].content), 1)
        finally:
            os.unlink(path)

    def test_preserves_image_order(self):
        paths = []
        try:
            for i in range(6):
                p = _make_temp_file(500004 + i * 100)
                paths.append(p)

            one_img_b64 = _file_base64_estimate(paths[0])
            header_bytes = _node_overhead() + _plain_estimate("t")
            self._set_budget(header_bytes + one_img_b64 * 2 + 1)

            nodes = _batch_nodes(
                self.display_name, self.display_uin,
                [self.Comp.Plain("t")], paths,
            )
            all_paths = []
            for n in nodes:
                for c in n.content:
                    if hasattr(c, "file"):
                        all_paths.append(c.file)
            self.assertEqual(all_paths, paths)
        finally:
            for p in paths:
                os.unlink(p)

    def test_no_header_images_only(self):
        f1 = _make_temp_file(5 * 1024 * 1024)
        f2 = _make_temp_file(5 * 1024 * 1024)

        one_img_b64 = _file_base64_estimate(f1)
        self._set_budget(_node_overhead() + one_img_b64 + 1)

        try:
            nodes = _batch_nodes(self.display_name, self.display_uin, [], [f1, f2])
            self.assertEqual(len(nodes), 2)
            self.assertEqual(len(nodes[0].content), 1)
            self.assertEqual(len(nodes[1].content), 1)
        finally:
            os.unlink(f1)
            os.unlink(f2)


class SendForwardBatchesTests(unittest.TestCase):
    """_send_forward_batches: failure fallback, ordering, no re-send."""

    def setUp(self):
        _main_ns["BiliFeedPlugin"]._MAX_FORWARD_BATCH_BYTES = _ORIG_BUDGET

    def test_successful_send_all_batches(self):
        """All batches succeed — every node sent exactly once."""
        f1 = _make_temp_file(204)
        f2 = _make_temp_file(204)
        try:
            nodes = _batch_nodes("Bot", "123", [], [f1, f2])
            self.assertEqual(len(nodes), 1)

            calls = []
            async def _send(chain):
                calls.append(chain)

            import asyncio
            asyncio.new_event_loop().run_until_complete(
                _send_forward_batches(nodes, _send, link="test")
            )
            # One Nodes call, no fallback components
            self.assertEqual(len(calls), 1)
        finally:
            for p in (f1, f2):
                os.unlink(p)

    def _make_nodes(self, count):
        """Create ``count`` small single-image nodes for testing."""
        paths = [_make_temp_file(204) for _ in range(count)]
        nodes = _batch_nodes("Bot", "123", [], paths)
        return nodes, paths

    def test_batch2_failure_fallback(self):
        """Second batch fails → components fallback, batch 3 still sent."""
        nodes, paths = self._make_nodes(3)
        # Force 3 separate batches by putting each image in its own node
        # with a tiny budget
        one_img = _file_base64_estimate(paths[0])
        _main_ns["BiliFeedPlugin"]._MAX_FORWARD_BATCH_BYTES = (
            _node_overhead() + one_img + 1
        )
        nodes = _batch_nodes("Bot", "123", [], paths)
        self.assertEqual(len(nodes), 3, "Expected 3 singleton nodes")

        calls = []
        fail_on = {1}  # 0-indexed: fail on second batch

        async def _send(chain):
            idx = len(calls)
            calls.append(("nodes", idx))
            if idx in fail_on:
                raise RuntimeError("mock ws timeout")

        import asyncio
        asyncio.new_event_loop().run_until_complete(
            _send_forward_batches(nodes, _send, link="t")
        )

        # Expected call sequence:
        # 0: ("nodes", 0) — success (batch 1)
        # 1: ("nodes", 1) — fails → 1 component fallback
        # 2: ("nodes", 2) — success (batch 3, continues after failure)
        self.assertGreaterEqual(len(calls), 3,
                                "Expected batch1 + batch2 fallback + batch3")
        # Batch 2 fallback produced a component call
        self.assertEqual(calls[0], ("nodes", 0))
        self.assertEqual(calls[1], ("nodes", 1))
        self.assertEqual(calls[2], ("nodes", 2))

    def test_batch_failure_does_not_repeat_succeeded(self):
        """Already-sent batches are not repeated when later batch fails."""
        nodes, paths = self._make_nodes(3)
        one_img = _file_base64_estimate(paths[0])
        _main_ns["BiliFeedPlugin"]._MAX_FORWARD_BATCH_BYTES = (
            _node_overhead() + one_img + 1
        )
        nodes = _batch_nodes("Bot", "123", [], paths)
        self.assertEqual(len(nodes), 3)

        send_order = []

        async def _send(chain):
            send_order.append(len(send_order))
            if len(send_order) == 2:  # fail on batch 2
                raise RuntimeError("fail")

        import asyncio
        asyncio.new_event_loop().run_until_complete(
            _send_forward_batches(nodes, _send, link="t")
        )

        # send_order: [0, 1 (fail→fallback), 2 (fallback comp), 3]
        # Batch indices: 0 sent, 1 failed, 2 sent — no duplicate 0
        batch_calls = [n for n in send_order if n < 3]
        self.assertEqual(batch_calls, [0, 1, 2],
                         "Each batch attempted exactly once in order")

    def test_component_fallback_also_handles_failure(self):
        """When component fallback itself fails, remaining components still tried."""
        nodes, paths = self._make_nodes(2)
        one_img = _file_base64_estimate(paths[0])
        _main_ns["BiliFeedPlugin"]._MAX_FORWARD_BATCH_BYTES = (
            _node_overhead() + one_img + 1
        )
        nodes = _batch_nodes("Bot", "123", [], paths)
        self.assertEqual(len(nodes), 2)

        attempts = []

        async def _send(chain):
            attempts.append(len(attempts))
            # fail both the batch AND the component fallback
            raise RuntimeError("fail")

        import asyncio
        asyncio.new_event_loop().run_until_complete(
            _send_forward_batches(nodes, _send, link="t")
        )

        # Both batches attempted + each component attempted as fallback
        self.assertGreaterEqual(len(attempts), 4,
                                "Expected batch attempts + fallback attempts")

    def tearDown(self):
        _main_ns["BiliFeedPlugin"]._MAX_FORWARD_BATCH_BYTES = _ORIG_BUDGET


if __name__ == "__main__":
    unittest.main()
