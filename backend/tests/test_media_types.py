import unittest

from app.media_types import (
    resolve_media_type,
    sniff_media_type,
    supports_inline_display,
)


class MediaTypeResolutionTests(unittest.TestCase):
    def test_jpeg_never_uses_broken_text_plain_metadata(self):
        self.assertEqual(
            resolve_media_type("prof.jpg", "text/plain", "image"),
            "image/jpeg",
        )

    def test_binary_image_document_uses_extension(self):
        self.assertEqual(
            resolve_media_type("poster.png", "application/octet-stream", "document"),
            "image/png",
        )

    def test_video_category_repairs_cross_family_metadata(self):
        self.assertEqual(
            resolve_media_type("clip.mp4", "text/plain; charset=utf-8", "video"),
            "video/mp4",
        )

    def test_specific_same_family_metadata_survives_rename(self):
        self.assertEqual(
            resolve_media_type("renamed.jpg", "image/webp", "image"),
            "image/webp",
        )

    def test_magic_bytes_repair_legacy_rows(self):
        jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"x" * 16
        self.assertEqual(sniff_media_type(jpeg, "text/plain"), "image/jpeg")

    def test_active_content_is_not_inline(self):
        self.assertTrue(supports_inline_display("image/jpeg"))
        self.assertTrue(supports_inline_display("video/mp4"))
        self.assertTrue(supports_inline_display("application/pdf"))
        self.assertFalse(supports_inline_display("image/svg+xml"))
        self.assertFalse(supports_inline_display("text/html"))


if __name__ == "__main__":
    unittest.main()
