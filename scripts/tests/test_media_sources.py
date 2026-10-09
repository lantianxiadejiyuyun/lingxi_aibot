"""Resource discovery contract tests: no external requests or actual downloads."""
import base64
import hashlib
import unittest
from unittest.mock import Mock, patch

from scripts.tests import support  # isolate Config before importing app modules
from app.services import media_sources as sources


HASH = "0123456789abcdef0123456789abcdef01234567"
MAGNET = "magnet:?xt=urn:btih:" + HASH
TORRENT_INFO = b"d6:lengthi5e4:name5:a.mkv12:piece lengthi16384e6:pieces20:01234567890123456789e"
TORRENT = b"d4:info" + TORRENT_INFO + b"e"


def response(body, mime="text/html", url="https://example.org/releases"):
    return Mock(status_code=200, content=body.encode() if isinstance(body, str) else body,
                text=body if isinstance(body, str) else "", headers={"Content-Type": mime}, url=url)


class MediaSourcesTest(unittest.TestCase):
    def setUp(self):
        blocker = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected external HTTP"))
        blocker.start()
        self.addCleanup(blocker.stop)
        dns = patch.object(sources, "validate_public_url", side_effect=lambda value: value)
        dns.start()
        self.addCleanup(dns.stop)

    def test_magnet_base32_and_hex_share_identity(self):
        encoded = base64.b32encode(bytes.fromhex(HASH)).decode()
        a = sources.make_candidate(MAGNET + "&dn=first")
        b = sources.make_candidate("magnet:?xt=urn:btih:" + encoded + "&dn=other")
        self.assertEqual(a["fingerprint"], b["fingerprint"])
        self.assertEqual(a["fingerprint"], "bt:" + HASH)
        self.assertEqual(sources.magnet_infohash("magnet:?xt=urn:btmh:1220" + "a" * 64), "a" * 64)

    def test_torrent_hash_uses_exact_info_bytes_and_rejects_malformed(self):
        self.assertEqual(sources.torrent_infohash(TORRENT), hashlib.sha1(TORRENT_INFO).hexdigest())
        for invalid in (b"", b"de", b"d4:infolee", TORRENT + b"junk", b"d4:info99999999999:ae"):
            with self.subTest(invalid=invalid[:30]):
                with self.assertRaises(sources.SourceError):
                    sources.torrent_infohash(invalid)

    def test_torrent_rejects_path_escape_and_symlink_metadata(self):
        escaping = TORRENT_INFO.replace(b"4:name5:a.mkv", b"4:name9:../secret")
        symlink = TORRENT_INFO[:-1] + b"4:attr1:le"
        for info in (escaping, symlink):
            with self.assertRaises(sources.SourceError):
                sources.torrent_infohash(b"d4:info" + info + b"e")

    def test_page_extracts_links_deduplicates_and_ignores_scripts_and_navigation(self):
        page = '<a href="' + MAGNET + '">A</a><a href="' + MAGNET + '&amp;dn=other">B</a>'
        page += '<a href="/episode.mkv">C</a><a href="/privacy">D</a>'
        with patch.object(sources, "safe_get", return_value=response(page)) as fetch:
            results = sources.resolve_resources("https://example.org/releases", "Test anime")
        self.assertEqual([r["kind"] for r in results], ["magnet", "http"])
        self.assertEqual(results[1]["url"], "https://example.org/episode.mkv")
        self.assertEqual(fetch.call_args.kwargs["max_response_bytes"], sources.MAX_DOCUMENT)

    def test_torrent_and_magnet_deduplicate_content_across_sources(self):
        digest = hashlib.sha1(TORRENT_INFO).hexdigest()
        with patch.object(sources, "safe_get", return_value=response(TORRENT, "application/x-bittorrent")):
            torrent = sources.make_candidate("https://example.org/a.torrent")
        magnet = sources.make_candidate("magnet:?xt=urn:btih:" + digest)
        self.assertEqual(torrent["fingerprint"], magnet["fingerprint"])

    def test_public_safety_failure_never_returns_internal_address_or_credentials(self):
        with patch.object(sources, "safe_get", side_effect=ValueError("internal secret address")):
            with self.assertRaises(sources.SourceError) as ctx:
                sources.resolve_resources("https://example.org/feed?apikey=private-token")
        self.assertNotIn("secret", str(ctx.exception))
        self.assertNotIn("private-token", str(ctx.exception))
        with self.assertRaises(sources.SourceError):
            sources.make_candidate("https://user:password@example.org/test.mkv")

    def test_search_uses_aliases_constraints_and_excludes_all_failed_hashes(self):
        requirements = {"title": "测试番剧", "aliases": ["Test anime"], "season": 2, "episode": 3,
                        "quality": "1080p", "subtitle": "简中"}
        queries = sources.build_search_queries(requirements)
        self.assertIn("测试番剧 2季 3集 1080p 简中", queries[0])
        self.assertIn("Test anime", queries[1])
        fresh = sources.make_candidate("magnet:?xt=urn:btih:" + "f" * 40)
        failed = sources.make_candidate(MAGNET)
        with patch.object(sources.web_search_service, "search", return_value=[{"url": "https://example.org/releases"}]), \
                patch.object(sources, "resolve_resources", return_value=[failed, fresh]):
            results = sources.search_resources(requirements, exclude_fingerprints=[failed["fingerprint"]])
        self.assertEqual(results, [fresh])

    def test_rss_and_web_search_can_be_controlled_separately(self):
        feed = f'<rss><channel><item><title>测试番剧 03</title><enclosure url="{MAGNET}" /></item>' \
               f'<item><title>无关作品</title><enclosure url="magnet:?xt=urn:btih:{"f" * 40}" /></item></channel></rss>'
        configs = [{"kind": "rss", "url": "https://example.org/rss?apikey=hidden"}, {"kind": "search", "enabled": False}]
        with patch.object(sources, "safe_get", return_value=response(feed, "application/rss+xml")), \
                patch.object(sources.web_search_service, "search") as search:
            results = sources.search_resources({"title": "测试番剧"}, configs)
        search.assert_not_called()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source_url"], "https://example.org/rss")
        self.assertNotIn("hidden", repr(results))

    def test_rss_rejects_entity_expansion_and_source_validation(self):
        with patch.object(sources, "safe_get", return_value=response('<!DOCTYPE rss [<!ENTITY x "test">]><rss/>')):
            with self.assertRaises(sources.SourceError):
                sources._rss_candidates({"url": "https://example.org/rss"}, {"title": "test"}, 10)
        with self.assertRaises(sources.SourceError):
            sources.validate_source_config({"kind": "shell"})
        self.assertEqual(sources.validate_source_config({"kind": "search", "enabled": False})["enabled"], False)


if __name__ == "__main__":
    unittest.main()
