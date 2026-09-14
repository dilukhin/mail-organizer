"""Минимальная точка входа административного CLI."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    """Создать парсер CLI без каких-либо внешних действий."""
    parser = argparse.ArgumentParser(
        prog="mailctl",
        description="Административный CLI mail-organizer.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Разобрать аргументы и завершиться без изменения внешнего состояния."""
    parser = build_parser()
    parser.parse_args(argv)
    return 0
