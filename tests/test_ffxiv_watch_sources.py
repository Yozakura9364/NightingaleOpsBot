from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "astrbot-plugin" / "astrbot_plugin_ffxiv_watch"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sources = load_module("ffxiv_watch_sources_test", PLUGIN_DIR / "sources.py")
storage = load_module("ffxiv_watch_storage_test", PLUGIN_DIR / "storage.py")


class LodestoneNewsSourceTests(unittest.TestCase):
    SAMPLE_PAYLOAD = {
        "topics": [
            {
                "id": "topic-new",
                "title": "Newest topic",
                "url": "https://na.finalfantasyxiv.com/lodestone/topics/detail/topic-new",
                "time": "2026-07-24T05:10:00Z",
                "description": "<p>Topic summary.</p>",
                "image": "https://img.finalfantasyxiv.com/t/topic-new.jpg?1",
            }
        ],
        "maintenance": [
            {
                "id": "news-old",
                "title": "[Maintenance] Older notice",
                "url": "https://na.finalfantasyxiv.com/lodestone/news/detail/news-old",
                "time": "2026-07-24T03:40:00Z",
            }
        ],
    }

    def test_lodestone_parser_extracts_json_feed_in_time_order(self):
        source = sources.SOURCES["na-news"]
        items = sources._parse_lodestone_news(self.SAMPLE_PAYLOAD, source)

        self.assertEqual([item.item_id for item in items], ["topic-new", "news-old"])
        self.assertEqual(items[0].title, "Newest topic")
        self.assertEqual(items[0].image, "https://img.finalfantasyxiv.com/t/topic-new.jpg?1")
        self.assertIn("Topic summary.", items[0].summary)
        self.assertEqual(items[1].title, "[Maintenance] Older notice")
        self.assertEqual(
            int(sources.datetime.fromisoformat(items[0].published_at).timestamp()),
            1784869800,
        )

    def test_jp_and_na_news_use_direct_lodestone_with_new_baseline(self):
        for source_id in ("jp-news", "na-news"):
            source = sources.SOURCES[source_id]
            self.assertEqual(source.baseline_version, 2)
            self.assertTrue(source.url.startswith("https://"))
            with self.subTest(source_id=source_id), mock.patch.object(
                sources,
                "_http_get",
                return_value=sources.json.dumps(self.SAMPLE_PAYLOAD),
            ) as http_get:
                items = sources.fetch_source(
                    source_id,
                    timeout_seconds=7,
                    rsshub_base_url="http://rsshub.invalid:1200",
                )
                self.assertEqual(len(items), 2)
                http_get.assert_called_once_with(source.url, timeout_seconds=7)

    def test_cn_news_still_uses_rsshub(self):
        rss = """<?xml version="1.0"?><rss><channel><item>
        <guid>cn-1</guid><title>CN news</title><link>https://example.test/cn-1</link>
        </item></channel></rss>"""
        with mock.patch.object(sources, "_http_get", return_value=rss) as http_get:
            items = sources.fetch_source(
                "cn-news",
                timeout_seconds=9,
                rsshub_base_url="http://rsshub.test:1200",
            )
        self.assertEqual(len(items), 1)
        http_get.assert_called_once_with(
            "http://rsshub.test:1200/ff14/zh/news",
            timeout_seconds=9,
        )


class StoreSourceTests(unittest.TestCase):
    def test_http_body_decoder_handles_gzip_json(self):
        raw = '{"name":"压缩响应"}'.encode("utf-8")
        compressed = sources.gzip.compress(raw)
        decoded = sources._decode_http_body(
            compressed,
            content_type="application/json; charset=utf-8",
            content_encoding="gzip",
        )
        self.assertEqual(decoded, raw.decode("utf-8"))

    def test_store_sources_use_list_baseline_version(self):
        for source_id in ("cn-store", "tw-store", "jp-store"):
            self.assertEqual(sources.SOURCES[source_id].baseline_version, 2)

    def test_cn_store_parser_uses_stable_sku_event_key(self):
        source = sources.SOURCES["cn-store"]
        payload = {
            "data": {
                "productList": [
                    {
                        "sku": {"skuId": "CN-100", "netPrice": 1200},
                        "product": {
                            "productName": "测试商品",
                            "picUrl": "https://example.test/cn.jpg",
                        },
                        "currency": {"shortName": "点券"},
                    }
                ]
            }
        }
        item = sources._parse_cn_store_items(source, payload)[0]
        self.assertEqual(item.stable_key(), "store-new:cn-store:CN-100")
        self.assertEqual(item.url, "https://qu.sdo.com/product-detail/CN-100")
        self.assertEqual(item.price, "1200")
        self.assertEqual(item.currency, "点券")

        payload["data"]["productList"][0]["sku"]["netPrice"] = 999
        changed_price_item = sources._parse_cn_store_items(source, payload)[0]
        self.assertEqual(changed_price_item.stable_key(), item.stable_key())

    def test_tw_store_html_parser_extracts_product(self):
        source = sources.SOURCES["tw-store"]
        parser = sources._TWStoreListParser(source)
        parser.feed(
            """
            <div class="item"><a href="product_detail.aspx?id=TW-200">
              <img src="https://example.test/tw.jpg">
              <div class="name"><h3> 台服测试商品 </h3></div>
              <div class="price"><span> 150 </span></div>
            </a></div>
            """
        )
        item = parser.items[0]
        self.assertEqual(item.stable_key(), "store-new:tw-store:TW-200")
        self.assertEqual(item.title, "台服测试商品")
        self.assertEqual(item.price, "150")
        self.assertEqual(item.currency, "水晶")

    def test_jp_store_parser_prefers_sale_price(self):
        source = sources.SOURCES["jp-store"]
        payload = {
            "products": [
                {
                    "id": 300,
                    "skuId": "MOG-00300",
                    "name": "日本测试商品",
                    "thumbnailUrl": "https://example.test/jp.jpg",
                    "priceText": "1,100 円",
                    "salePriceText": "990 円",
                }
            ]
        }
        item = sources._parse_jp_store_items(source, payload)[0]
        self.assertEqual(item.stable_key(), "store-new:jp-store:MOG-00300")
        self.assertEqual(item.price, "990 円")
        self.assertEqual(item.summary, "原价：1,100 円")
        self.assertTrue(item.url.endswith("/product/300"))


class StoreStateMigrationTests(unittest.TestCase):
    def test_legacy_database_adds_and_updates_baseline_version(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "ffxiv_watch.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE source_state (
                        source_id TEXT PRIMARY KEY,
                        baseline_done INTEGER NOT NULL DEFAULT 0,
                        last_keys_json TEXT NOT NULL DEFAULT '[]',
                        last_checked_at TEXT NOT NULL DEFAULT '',
                        last_success_at TEXT NOT NULL DEFAULT '',
                        failure_count INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO source_state (
                        source_id, baseline_done, last_keys_json,
                        last_checked_at, last_success_at, failure_count, last_error
                    ) VALUES ('cn-store', 1, '["store-change:cn-store:legacy"]', '', '', 0, '')
                    """
                )
            connection.close()

            store = storage.FFXIVWatchStore(Path(temporary_directory))
            self.assertEqual(store.get_source_state("cn-store").baseline_version, 1)
            store.record_source_success(
                source_id="cn-store",
                keys=["store-new:cn-store:CN-100"],
                baseline_version=2,
            )
            state = store.get_source_state("cn-store")
            self.assertEqual(state.baseline_version, 2)
            self.assertEqual(state.last_keys, ["store-new:cn-store:CN-100"])


if __name__ == "__main__":
    unittest.main()
