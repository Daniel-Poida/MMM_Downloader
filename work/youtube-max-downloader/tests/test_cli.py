from __future__ import annotations

import unittest

from ytmax.cli import _parser


class CliTests(unittest.TestCase):
    def test_native_h264_is_the_default(self) -> None:
        args = _parser().parse_args(["https://youtu.be/example", "--to", "C:\\Video"])

        self.assertEqual(args.quality, "compatible")


if __name__ == "__main__":
    unittest.main()
