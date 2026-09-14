"""Проверки минимального Stage 0 scaffold без внешних сервисов."""

from __future__ import annotations

import contextlib
import io
import unittest

from mail_organizer import __version__
from mail_organizer.cli import build_parser, main


class ScaffoldTests(unittest.TestCase):
    def test_version_is_defined(self) -> None:
        self.assertEqual(__version__, "0.0.0")

    def test_cli_parser_name(self) -> None:
        self.assertEqual(build_parser().prog, "mailctl")

    def test_cli_without_arguments_is_noop(self) -> None:
        self.assertEqual(main([]), 0)

    def test_cli_version(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as raised:
                main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stdout.getvalue().strip(), "mailctl 0.0.0")


if __name__ == "__main__":
    unittest.main()
