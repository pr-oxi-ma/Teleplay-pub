import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from pydantic import ValidationError

from app.config import Settings
from app import edge_cache


class PublicStreamModeSettingsTests(unittest.TestCase):
    def base_values(self):
        return {
            "JWT_SECRET": "j" * 64,
            "CACHE_MODE": "hybrid",
            "GOOGLE_DRIVE_CLIENT_ID": "client",
            "GOOGLE_DRIVE_CLIENT_SECRET": "secret",
            "GOOGLE_DRIVE_REFRESH_TOKEN": "refresh",
            "MEDIA_CACHE_MASTER_KEY_BASE64": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
            "CLOUDFLARE_WORKER_BASE_URL": "https://worker.example",
            "CLOUDFLARE_EDGE_SIGNING_SECRET": "e" * 64,
            "CLOUDFLARE_ORIGIN_SECRET": "o" * 64,
            "CLOUDFLARE_TOUCH_SECRET": "t" * 64,
        }

    def test_default_is_backward_compatible_off(self):
        settings = Settings(_env_file=None, **self.base_values())
        self.assertEqual(settings.normalized_public_stream_edge_mode, "off")

    def test_redirect_and_proxy_are_valid(self):
        for mode in ("redirect", "proxy"):
            values = self.base_values()
            values["PUBLIC_STREAM_EDGE_MODE"] = mode
            settings = Settings(_env_file=None, **values)
            self.assertEqual(settings.normalized_public_stream_edge_mode, mode)

    def test_edge_mode_requires_cloudflare_cache(self):
        values = self.base_values()
        values.update({
            "CACHE_MODE": "gdrive",
            "PUBLIC_STREAM_EDGE_MODE": "proxy",
        })
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, **values)

    def test_invalid_mode_is_rejected(self):
        values = self.base_values()
        values["PUBLIC_STREAM_EDGE_MODE"] = "magic"
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, **values)

    def test_worker_base_url_requires_absolute_origin(self):
        values = self.base_values()
        values["CLOUDFLARE_WORKER_BASE_URL"] = "l1-media.example.com"
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, **values)

    def test_worker_base_url_rejects_path(self):
        values = self.base_values()
        values["CLOUDFLARE_WORKER_BASE_URL"] = "https://l1-media.example.com/media"
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, **values)

    def test_custom_domain_origin_is_normalized(self):
        values = self.base_values()
        values["CLOUDFLARE_WORKER_BASE_URL"] = "https://l1-media.example.com/"
        settings = Settings(_env_file=None, **values)
        self.assertEqual(settings.cloudflare_worker_origin, "https://l1-media.example.com")


class PublicEdgeUrlTests(unittest.TestCase):
    def test_force_download_is_added_without_changing_signed_identity(self):
        fake_settings = SimpleNamespace(
            cloudflare_cache_enabled=True,
            cloudflare_worker_base_url="https://worker.example",
            cloudflare_worker_origin="https://worker.example",
            cloudflare_edge_signing_secret="e" * 64,
            cloudflare_edge_url_ttl_seconds=7200,
            media_cache_key_version=2,
        )
        file = SimpleNamespace(
            id=69,
            file_unique_id="unique-media-id",
            file_size=123456,
        )
        with patch.object(edge_cache, "settings", fake_settings):
            normal = edge_cache.build_edge_stream_url(file)
            download = edge_cache.build_edge_stream_url(file, force_download=True)

        self.assertTrue(normal.startswith("https://worker.example/media/69/"))
        self.assertTrue(download.startswith("https://worker.example/media/69/"))
        normal_query = parse_qs(urlparse(normal).query)
        download_query = parse_qs(urlparse(download).query)
        self.assertNotIn("download", normal_query)
        self.assertEqual(download_query["download"], ["1"])
        for key in ("v", "size"):
            self.assertEqual(normal_query[key], download_query[key])


if __name__ == "__main__":
    unittest.main()
