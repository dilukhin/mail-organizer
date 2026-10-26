"""Граничные тесты безопасности на вымышленных сообщениях."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from mail_organizer.cli import main
from mail_organizer.offline import (MessageFeatures, PolicyRule, classify, is_protected,
                                    load_rules, parse_messages)
from mail_organizer.state import open_memory_state, record_decision

ROOT = Path(__file__).resolve().parents[1]


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.rules = load_rules(ROOT / 'policy')
        self.messages = parse_messages(ROOT / 'tests/fixtures/offline_messages.json')

    def test_protected_wins_over_sender(self):
        result = classify(self.messages[2], self.rules)
        self.assertEqual((result.status, result.action_hint), ('PROTECTED', 'none'))

    def test_protected_subtree_and_unrelated_names(self):
        msg = self.messages[0]
        self.assertTrue(is_protected(msg.__class__(**{**vars(msg), 'labels':('ChatGPT-Control/Responses',)})))
        self.assertFalse(is_protected(msg.__class__(**{**vars(msg), 'labels':('ChatGPT-Controlled',)})))

    def test_known_and_unknown(self):
        self.assertEqual(classify(self.messages[0], self.rules).category, 'receipts')
        self.assertEqual(classify(self.messages[3], self.rules).status, 'REVIEW_REQUIRED')

    def test_conflict_requires_review(self):
        conflicting = self.rules + [PolicyRule('z', 'receipts.invalid', '', 'notifications', 'managed')]
        self.assertEqual(classify(self.messages[0], conflicting).reason_code, 'conflicting_rules')

    def test_untrusted_subject_never_command(self):
        msg = self.messages[3]
        self.assertEqual(classify(msg, self.rules).action_hint, 'none')

    def test_body_and_secrets_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'fixture.json'
            path.write_text(json.dumps([{**vars(self.messages[0]), 'labels': [], 'body':'secret'}]))
            with self.assertRaises(ValueError):
                parse_messages(path)

    def test_external_rule_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'rules.yaml'
            path.write_text(json.dumps({'schema_version':1, 'rules':[
                {'id':'outside','sender_domain':'receipts.invalid','subject_contains':'',
                 'category':'receipts', 'ownership':'external'}]}))
            with self.assertRaises(ValueError):
                load_rules(Path(tmp))

    def test_sqlite_contains_no_subject_or_body(self):
        with open_memory_state() as db:
            for msg in self.messages:
                record_decision(db, msg, classify(msg, self.rules))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0], 4)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM actions').fetchone()[0], 2)
            dump = '\n'.join(db.iterdump()).lower()
            self.assertNotIn('ignore instructions', dump)
            self.assertNotIn('receipt\'', dump)
            for tab in ('provider_cursors','message_metadata','decisions','actions','incidents'):
                self.assertTrue(db.execute('SELECT name FROM sqlite_master WHERE name=?',(tab,)).fetchone())

    def test_sqlite_duplicate_is_transactionally_rejected(self):
        with open_memory_state() as db:
            msg=self.messages[0]
            record_decision(db,msg,classify(msg,self.rules))
            with self.assertRaises(sqlite3.IntegrityError):
                record_decision(db,msg,classify(msg,self.rules))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0],1)

    def test_no_network_or_filesystem_writes_cli(self):
        import contextlib, io
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(['sync','--dry-run','--fixture',str(ROOT/'tests/fixtures/offline_messages.json'),
                                   '--policy-dir',str(ROOT/'policy')]),0)
        self.assertIn('предложено: 2',output.getvalue())


if __name__ == '__main__':
    unittest.main()
