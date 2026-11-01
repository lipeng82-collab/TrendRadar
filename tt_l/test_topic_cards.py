"""Offline regression tests; fixtures contain synthetic public-news titles only."""
from datetime import date
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from tt_l.topic_cards import build_cards, canonical_url, export_cards, rank_relevance
from trendradar.core.frequency import load_frequency_words, matches_word_groups
from trendradar.core.loader import _load_filter_config

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 1)


def row(title="湖南国企发布算力采购合同", url="https://example.org/news/1", **changes):
    item = dict(title=title, url=url, source="合成测试来源", kind="news",
                day=DAY.isoformat(), observed_at="09:00", rank=2)
    item.update(changes)
    return item


class TopicCardsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        marker = self.repo / "config/custom/tt_l_profile.json"
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({"profile": "personal"}), encoding="utf-8")
        self.out = self.root / "candidates"

    def database(self, kind="news", title="湖南国企发布算力采购合同"):
        path = self.repo / "output" / kind / f"{DAY}.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as conn:
            schema = "schema.sql" if kind == "news" else "rss_schema.sql"
            conn.executescript((ROOT / "trendradar/storage" / schema).read_text(encoding="utf-8"))
            if kind == "news":
                conn.execute("INSERT INTO platforms(id,name) VALUES ('test','合成测试来源')")
                conn.execute("INSERT INTO news_items(title,platform_id,rank,url,first_crawl_time,last_crawl_time) VALUES (?,'test',2,'https://example.org/news/1','09:00','09:00')", (title,))
            else:
                conn.execute("INSERT INTO rss_feeds(id,name) VALUES ('test','合成RSS来源')")
                conn.execute("INSERT INTO rss_items(title,feed_id,url,published_at,first_crawl_time,last_crawl_time) VALUES (?,'test','https://example.org/rss/1','2026-11-01T08:00:00+08:00','09:00','09:00')", (title,))
            conn.commit()
        return path

    def test_news_and_rss_export_without_database_changes(self):
        databases = [self.database(), self.database("rss", "大学发布AI教育调查数据")]
        before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in databases]
        j, m, n = export_cards(self.repo, self.out, "personal", DAY, 2, 3)
        cards = json.loads(j.read_text(encoding="utf-8"))
        self.assertEqual(n, 2)
        self.assertEqual({c["source_type"] for c in cards}, {"RSS", "热榜"})
        self.assertTrue(all(not c["fact_verified"] and not c["primary_source_verified"] for c in cards))
        self.assertIn("未核实", m.read_text(encoding="utf-8"))
        self.assertEqual(before, [hashlib.sha256(p.read_bytes()).hexdigest() for p in databases])

    def test_duplicate_url_and_title(self):
        rows = [row(), row(url="https://example.org/news/1?utm_source=x"),
                row(url="https://example.org/reprint")]
        self.assertEqual(len(build_cards(rows, "personal", 3)), 1)

    def test_ascii_keyword_boundaries(self):
        self.assertEqual(rank_relevance(row(title="Chair review and retail launch"), "personal")[0], 0)
        self.assertGreater(rank_relevance(row(title="AI pricing published"), "personal")[0], 0)

    def test_reject_private_and_access_bearing_urls(self):
        for url in ("file:///a", "http://localhost/a", "http://127.0.0.1/a", "http://[::1]/a",
                    "http://10.0.0.1/a", "https://user:pass@example.org/a",
                    "http://service.internal/a", "https://example.org/a?token=secret",
                    "https://example.org/a?X-Amz-Signature=secret"):
            with self.subTest(url=url):
                self.assertEqual(canonical_url(url), "")

    def test_tracking_removed_but_article_identity_preserved(self):
        self.assertEqual(canonical_url("https://example.org/a?source=article1&utm_source=test#top"),
                         "https://example.org/a?source=article1")

    def test_no_database_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "没有可读取"):
            export_cards(self.repo, self.out, "personal", DAY, 2, 3)
        self.assertFalse(self.out.exists())

    def test_corrupt_database_is_an_error(self):
        p = self.repo / "output/news" / f"{DAY}.db"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"not sqlite")
        with self.assertRaisesRegex(ValueError, "没有可读取"):
            export_cards(self.repo, self.out, "personal", DAY, 2, 3)

    def test_real_zero_is_allowed(self):
        self.database(title="合成无关标题")
        j, _, n = export_cards(self.repo, self.out, "personal", DAY, 1, 3)
        self.assertEqual((n, json.loads(j.read_text(encoding="utf-8"))), (0, []))

    def test_profiles_are_isolated(self):
        self.database()
        with self.assertRaisesRegex(ValueError, "拒绝跨流"):
            export_cards(self.repo, self.out, "institution", DAY, 2, 2)

    def test_refuse_output_inside_repository(self):
        self.database()
        with self.assertRaisesRegex(ValueError, "仓库外"):
            export_cards(self.repo, self.repo / "cards", "personal", DAY, 2, 3)

    def test_repeated_export_does_not_overwrite(self):
        self.database()
        first = export_cards(self.repo, self.out, "personal", DAY, 2, 3)
        second = export_cards(self.repo, self.out, "personal", DAY, 2, 3)
        self.assertNotEqual(first[0], second[0])
        self.assertTrue(first[0].is_file() and second[0].is_file())

    def test_limits(self):
        for profile, count in (("personal", 0), ("personal", 4), ("institution", 3)):
            with self.subTest(profile=profile, count=count), self.assertRaises(ValueError):
                build_cards([row()], profile, count)


class FilterIntegrationTests(unittest.TestCase):
    def test_fallback_keywords_match_and_exclude(self):
        groups, filters, globals_ = load_frequency_words(str(ROOT / "config/custom/keyword/tt_l_personal.txt"))
        self.assertEqual(len(groups), 5)
        self.assertTrue(matches_word_groups("湖南国企发布算力采购合同", groups, filters, globals_))
        self.assertFalse(matches_word_groups("明星八卦", groups, filters, globals_))
        self.assertFalse(matches_word_groups("震惊！湖南国企采购", groups, filters, globals_))

    def test_global_filters_accept_legacy_and_regex(self):
        self.assertFalse(matches_word_groups("震惊消息", [], [], ["震惊"]))
        self.assertFalse(matches_word_groups("震惊消息", [], [], [{"word": "震惊", "is_regex": False}]))

    def test_frequency_config_preserves_default(self):
        self.assertIsNone(_load_filter_config({})["FREQUENCY_FILE"])
        self.assertEqual(_load_filter_config({"filter": {"frequency_file": "tt_l_personal.txt"}})["FREQUENCY_FILE"], "tt_l_personal.txt")

    def test_ai_failure_uses_keyword_result(self):
        from trendradar.__main__ import NewsAnalyzer
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.filter_method = "ai"
        analyzer.interests_file = None
        analyzer.ctx = SimpleNamespace(config={"STORAGE": {"FORMATS": {"HTML": False}}},
            display_mode="keyword", run_ai_filter=Mock(return_value=SimpleNamespace(success=False, error="offline fixture")),
            count_frequency=Mock(return_value=([{"count": 1}], 1)))
        result = analyzer._run_analysis_pipeline({}, "current", {}, {}, [], [], {})
        self.assertEqual(result[0], [{"count": 1}])
        analyzer.ctx.count_frequency.assert_called_once()

    def test_successful_empty_ai_result_does_not_fallback(self):
        from trendradar.__main__ import NewsAnalyzer
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.filter_method = "ai"
        analyzer.interests_file = None
        analyzer.ctx = SimpleNamespace(config={"STORAGE": {"FORMATS": {"HTML": False}}},
            display_mode="keyword", run_ai_filter=Mock(return_value=SimpleNamespace(success=True, total_matched=0, tags=[])),
            count_frequency=Mock(), convert_ai_filter_to_report_data=Mock(return_value=([], [], [])))
        result = analyzer._run_analysis_pipeline({}, "current", {}, {}, [], [], {})
        self.assertEqual(result[0], [])
        analyzer.ctx.count_frequency.assert_not_called()


if __name__ == "__main__":
    unittest.main()
