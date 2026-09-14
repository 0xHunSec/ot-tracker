import unittest

from ot_tracker.http import strip_xssi_prefix


class XssiTest(unittest.TestCase):
    def test_strips_google_prefix(self) -> None:
        self.assertEqual(b'{"ok":true}', strip_xssi_prefix(b')]}\'\n{"ok":true}'))

    def test_leaves_plain_json(self) -> None:
        self.assertEqual(b'{"ok":true}', strip_xssi_prefix(b'{"ok":true}'))


if __name__ == "__main__":
    unittest.main()
