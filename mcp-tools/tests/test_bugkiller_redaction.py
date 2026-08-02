"""Redaction and canonical-hash tests for Bugkiller evidence."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bugkiller.redaction import canonical_json, canonical_sha256, redact_text


class BugkillerRedactionTests(unittest.TestCase):
    def test_canonical_json_and_hash_are_order_independent(self) -> None:
        left = {"b": [2, 1], "a": {"z": True}}
        right = {"a": {"z": True}, "b": [2, 1]}

        self.assertEqual(canonical_json(left), canonical_json(right))
        self.assertEqual(canonical_sha256(left), canonical_sha256(right))

    def test_redacts_tokens_headers_pem_and_url_credentials_with_bound(self) -> None:
        evidence = (
            "Authorization: Bearer abc.def.ghi\n"
            "X-Api-Key: secret-key\n"
            "https://alice:password@example.test/path\n"
            "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----"
        )

        redacted = redact_text(evidence, max_chars=120)

        self.assertNotIn("abc.def.ghi", redacted)
        self.assertNotIn("secret-key", redacted)
        self.assertNotIn("alice:password", redacted)
        self.assertNotIn("\nsecret\n", redacted)
        self.assertIn("[REDACTED", redacted)
        self.assertLessEqual(len(redacted), 120)


if __name__ == "__main__":
    unittest.main()
