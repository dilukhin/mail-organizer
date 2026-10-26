"""CLI: только локальные искусственные данные и dry-run; без провайдеров."""
from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from . import __version__
from .offline import classify, load_rules, parse_messages
from .state import open_memory_state, record_decision


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mailctl", description="Административный CLI mail-organizer.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")
    sync = sub.add_parser("sync", help="Только синтетическая фикстура и dry-run")
    sync.add_argument("--dry-run", action="store_true", required=True)
    sync.add_argument("--fixture", type=Path, required=True)
    sync.add_argument("--policy-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "sync":
        if args.command is None:
            parser.print_help()
        return 0
    if not args.dry_run:
        parser.error("Для sync необходим --dry-run")
    try:
        messages = parse_messages(args.fixture)
        rules = load_rules(args.policy_dir)
        stats: Counter[str] = Counter()
        with closing(open_memory_state()) as conn:
            for msg in messages:
                decision = classify(msg, rules)
                record_decision(conn, msg, decision)
                stats[decision.status] += 1
            count = conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
        print("Локальный dry-run: без доступа к Gmail/Outlook, без изменения писем и файлов SQLite.")
        print(f"Всего: {len(messages)}; предложено: {count}; защищено: {stats['PROTECTED']}; "
              f"требуют проверки: {stats['REVIEW_REQUIRED']}")
        return 0
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Ошибка локального dry-run: {exc}\n")
