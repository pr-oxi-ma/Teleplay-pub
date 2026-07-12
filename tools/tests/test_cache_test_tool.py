import importlib.util
import time
import unittest
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

MODULE_PATH = Path(__file__).resolve().parents[1] / "test_production_cache.py"
spec = importlib.util.spec_from_file_location("test_production_cache", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
assert spec.loader is not None
spec.loader.exec_module(module)


class CacheTestToolTests(unittest.TestCase):
    def valid_url(self):
        return (
            "https://worker.example/media/69/" + "a" * 64
            + "?v=2&size=2883744&expires=" + str(int(time.time()) + 3600)
            + "&token=" + "b" * 16 + "&sig=" + "c" * 64
        )

    def test_parse_valid_edge_url(self):
        parsed = module.parse_edge_url(self.valid_url())
        self.assertEqual(parsed["file_id"], 69)
        self.assertEqual(parsed["size"], 2883744)
        self.assertEqual(parsed["cache_key"], "a" * 64)

    def test_reject_expired_url(self):
        expired = self.valid_url().replace(
            str(int(time.time()) + 3600), str(int(time.time()) - 10)
        )
        with self.assertRaises(ValueError):
            module.parse_edge_url(expired)

    def test_signature_mutation_changes_only_signature(self):
        original = self.valid_url()
        mutated = module.mutate_signature(original)
        original_query = parse_qs(urlsplit(original).query)
        mutated_query = parse_qs(urlsplit(mutated).query)
        self.assertNotEqual(original_query["sig"], mutated_query["sig"])
        for key in {"v", "size", "expires", "token"}:
            self.assertEqual(original_query[key], mutated_query[key])

    def test_redaction_removes_query(self):
        redacted = module.redact_url(self.valid_url())
        self.assertEqual(urlsplit(redacted).query, "")
        self.assertIn("/media/69/", redacted)

    def test_content_range(self):
        self.assertEqual(module.expected_content_range(0, 1023, 10000), "bytes 0-1023/10000")


if __name__ == "__main__":
    unittest.main()
