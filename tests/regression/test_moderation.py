import asyncio
import sys
import unittest
from types import SimpleNamespace
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[2]))
from core import App
from db import DB
from settings import UserError

class RecordingTransport:
    def __init__(self):
        self.sent = []
        self.edited = []
        self.deleted = []
    async def send_message(self, recipient, text, **kwargs):
        self.sent.append((recipient, text, kwargs))
        return SimpleNamespace(id=len(self.sent))
    async def edit_message(self, *args, **kwargs):
        self.edited.append((args, kwargs))
    async def delete_messages(self, chat, ids, revoke=True):
        self.deleted.append((chat, ids, revoke))

class ModerationTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(':memory:')
        self.app = App(self.db)
        self.db.set('admin_ids', '12345')
        self.db.set('auto_publish', '0')
        self.db.set('target_channel', '@test_newsbot_channel')
        self.app.bot = RecordingTransport()
        self.db._exec('INSERT INTO posts(channel,msg_id,date,text,link) VALUES(?,?,?,?,?)',
                      ('test_source', 7, 0, 'Test source document', 'https://t.me/test_source/7'))
        self.pid = self.db.add_proposal('Test', 'Test draft <text>', [('test_source', 7)])
    def tearDown(self):
        self.db.conn.close()
    def test_publish_and_reject_labels(self):
        rows = self.app.button_rows(self.db.get_proposal(self.pid))
        self.assertEqual(rows[0], [('✅ Опубликовать', f'pub:{self.pid}'),
                                   ('❌ Отклонить', f'skip:{self.pid}')])

    def test_sources_are_below_draft(self):
        preview = self.app.preview_html(self.db.get_proposal(self.pid))
        self.assertLess(preview.index('Test draft'), preview.index('🔗 Источники:'))
        self.assertIn('https://t.me/test_source/7', preview)
        self.assertIn('&lt;text&gt;', preview)

    def test_delivery_sends_to_owner_without_channel_publication(self):
        asyncio.run(self.app.deliver(self.pid))
        self.assertEqual(len(self.app.bot.sent), 1)
        recipient, text, options = self.app.bot.sent[0]
        self.assertEqual(recipient, 12345)
        self.assertIn('🔗 Источники:', text)
        self.assertTrue(options['buttons'])
        self.assertEqual(self.db.get_proposal(self.pid)['status'], 'new')

    def test_rejection_does_not_publish(self):
        asyncio.run(self.app.deliver(self.pid))
        asyncio.run(self.app.skip(self.pid))
        self.assertEqual(len(self.app.bot.sent), 1)
        self.assertEqual(self.db.get_proposal(self.pid)['status'], 'skipped')
        self.assertEqual(self.app.button_rows(self.db.get_proposal(self.pid)), [])
        with self.assertRaises(UserError):
            asyncio.run(self.app.approve(self.pid))
        self.assertEqual(len(self.app.bot.sent), 1)

    def test_approval_publishes_once_after_delivery(self):
        asyncio.run(self.app.deliver(self.pid))
        self.assertEqual(len(self.app.bot.sent), 1)
        asyncio.run(self.app.approve(self.pid))
        self.assertEqual(len(self.app.bot.sent), 2)
        self.assertEqual(self.app.bot.sent[-1][0], '@test_newsbot_channel')
        self.assertEqual(self.db.get_proposal(self.pid)['status'], 'published')
        with self.assertRaises(UserError):
            asyncio.run(self.app.approve(self.pid))
        self.assertEqual(len(self.app.bot.sent), 2)

if __name__ == '__main__':
    unittest.main()
