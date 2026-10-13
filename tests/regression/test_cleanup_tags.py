"""Offline feature gates: real scoped App/DB; recording Telegram only."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from core import App
from db import DB
from settings import UserError

class Transport:
    def __init__(self, db):
        self.db = db
        self.sent, self.deleted, self.edited, self.files = [], [], [], []
        self.fail_delete = set()
        self.fail_send = False
    async def send_message(self, chat, text, **kw):
        if self.fail_send: raise RuntimeError('send unavailable')
        self.sent.append((chat, text, kw))
        return SimpleNamespace(id=len(self.sent))
    async def edit_message(self, *a, **kw): self.edited.append((a, kw))
    async def delete_messages(self, chat, ids, revoke=True):
        self.deleted.append((chat, ids, revoke, self.db.get_proposal(self.pid)['status']))
        if chat in self.fail_delete: raise RuntimeError('SECRET exception payload')
    async def send_file(self, chat, file, **kw): self.files.append((chat, kw))

class Features(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = DB(':memory:'); self.app = App(self.db)
        self.db.set('target_channel', 'original_out'); self.db.set('admin_ids', '123,456')
        self.pid = self.db.add_proposal('News', 'Draft <safe>', [])
        self.bot = self.app.bot = Transport(self.db); self.bot.pid = self.pid
    async def asyncTearDown(self): self.db.conn.close()
    async def test_rejection_deletes_only_recorded_cards_after_status_persistence(self):
        await self.app.deliver(self.pid)
        refs = self.db.get_proposal(self.pid)['tg_msgs']
        await self.app.skip(self.pid)
        self.assertEqual(self.bot.deleted, [(chat,[mid],True,'skipped') for chat,mid in refs])
        self.assertEqual(self.db.get_proposal(self.pid)['tg_msgs'], [])
        self.assertEqual(self.bot.edited, [])
        self.assertEqual(len(self.bot.sent), 2)

    async def test_queue_approval_deletes_cards_without_republishing_on_sync(self):
        await self.app.deliver(self.pid)
        self.app.modules['queue'].save({'enabled':'1'})
        answer = await self.app.approve(self.pid)
        self.assertIn('В очереди', answer)
        self.assertEqual(self.db.get_proposal(self.pid)['status'], 'queued')
        self.assertEqual(len(self.bot.deleted), 2)
        await self.app.set_text(self.pid, 'Queued edit')
        await self.app.send_proposal(self.pid)
        self.assertEqual(self.bot.edited, [])
        self.assertEqual(len(self.bot.sent), 2)
        self.assertEqual(self.db.get_proposal(self.pid)['tg_msgs'], [])

    async def test_skip_waits_for_inflight_publish_and_cannot_overwrite_success(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.bot.send_message
        async def slow(*a, **kw):
            entered.set(); await release.wait(); return await original(*a, **kw)
        with patch.object(self.bot, 'send_message', slow):
            publication = asyncio.create_task(self.app.publish(self.pid))
            await entered.wait()
            rejection = asyncio.create_task(self.app.skip(self.pid))
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(publication, rejection, return_exceptions=True)
        self.assertIsInstance(results[1], UserError)
        self.assertEqual(self.db.get_proposal(self.pid)['status'], 'published')
        self.assertEqual(len(self.bot.sent), 1)

    async def test_cleanup_failure_preserves_only_failed_refs_without_failing_publication(self):
        await self.app.deliver(self.pid)
        refs = self.db.get_proposal(self.pid)['tg_msgs']
        self.bot.fail_delete = {123}
        with self.assertLogs('core', level='WARNING') as captured:
            await self.app.approve(self.pid)
        self.assertNotIn('SECRET', ''.join(captured.output))
        self.assertEqual(self.db.get_proposal(self.pid)['status'], 'published')
        self.assertEqual(self.db.get_proposal(self.pid)['tg_msgs'], [r for r in refs if r[0] == 123])
        self.bot.fail_delete.clear()
        await self.app.sync_tg(self.pid)
        self.assertEqual(self.db.get_proposal(self.pid)['tg_msgs'], [])
        self.assertEqual(len(self.bot.sent), 3)
        with self.assertRaises(UserError): await self.app.approve(self.pid)

    async def test_failed_publication_preserves_cards_and_edit_updates_pending(self):
        await self.app.deliver(self.pid)
        refs = self.db.get_proposal(self.pid)['tg_msgs']
        await self.app.set_text(self.pid, 'Owner edit')
        self.assertEqual(len(self.bot.edited), 2)
        self.bot.fail_send = True
        with self.assertRaises(RuntimeError): await self.app.approve(self.pid)
        self.assertEqual(self.db.get_proposal(self.pid)['status'], 'new')
        self.assertEqual(self.db.get_proposal(self.pid)['tg_msgs'], refs)
        self.assertEqual(self.bot.deleted, [])

    async def test_inflight_delivery_cannot_leave_cards_after_rejection(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.bot.send_message
        async def slow(*a, **kw):
            entered.set(); await release.wait(); return await original(*a, **kw)
        with patch.object(self.bot, 'send_message', slow):
            delivery = asyncio.create_task(self.app.send_proposal(self.pid))
            await entered.wait()
            rejection = asyncio.create_task(self.app.skip(self.pid))
            await asyncio.sleep(0)
            release.set(); await asyncio.gather(delivery, rejection)
        self.assertEqual(self.db.get_proposal(self.pid)['tg_msgs'], [])
        self.assertEqual(len(self.bot.deleted), len(self.bot.sent))

    async def test_default_tags_follow_originating_workspace_only_in_management(self):
        wid = self.db.create_workspace('АйТи / Технологии!')
        space = self.app.workspace(wid); space.db.set('target_channel','it_out')
        pid = space.db.add_proposal('IT', 'IT <news>', [])
        self.assertIn('#АйТи_Технологии', space.preview_html(space.db.get_proposal(pid)))
        self.assertIn('#Основная', self.app.preview_html(self.db.get_proposal(self.pid)))
        await space.approve(pid)
        self.assertEqual(self.bot.sent[0][0], 'it_out')
        self.assertNotIn('#АйТи_Технологии', self.bot.sent[0][1])
        self.assertIn('IT &lt;news&gt;', self.bot.sent[0][1])
        self.assertEqual(space.state()['settings']['publication_hashtags'], '')
        self.assertEqual(space.state()['settings']['publication_hashtags_enabled'], '1')

    async def test_editable_tags_validate_deduplicate_and_remain_workspace_local(self):
        wid = self.db.create_workspace('Games'); space = self.app.workspace(wid)
        try:
            await space.save_settings({'publication_hashtags':'#АйТи, #Технологии #айти #AI_2'})
        except UserError as e:
            self.fail('Workspace publication hashtags are not editable: ' + str(e))
        self.assertEqual(space.db.get('publication_hashtags'), '#АйТи #Технологии #AI_2')
        self.assertEqual(self.db.get('publication_hashtags'), '')
        space.db.set('signature', 'Owner signature')
        pid = space.db.add_proposal('Tags', '#Технологии_новости #технологии!', [])
        out = space.channel_html(space.db.get_proposal(pid))
        self.assertTrue(space.preview_html(space.db.get_proposal(pid)).endswith('#AI_2'))
        self.assertNotIn('#AI_2', out)
        self.assertIn('#технологии!', out)
        self.assertNotIn('\n\n#АйТи', out)
        for bad in ('AI', '#two-words', '#123', '#___', '#a<script>', '#'+ 'a'*65,
                    ' '.join('#t'+str(i) for i in range(9)), ['#AI']):
            with self.subTest(value=bad):
                with self.assertRaises(UserError): await space.save_settings({'publication_hashtags': bad})
        self.assertEqual(space.db.get('publication_hashtags'), '#АйТи #Технологии #AI_2')
        await space.save_settings({'publication_hashtags_enabled': '0'})
        self.assertFalse(space.preview_html(space.db.get_proposal(pid)).endswith('#AI_2'))
        self.assertEqual(space.channel_html(space.db.get_proposal(pid)), out)
        self.assertEqual(space.db.get('auto_publish'), '0')
        self.assertEqual(space.db.get('mod.breaking.auto_publish'), '0')
        self.assertEqual(space.db.operation_cost_state()['history'], [])
        await space.save_settings({'publication_hashtags':'', 'publication_hashtags_enabled':'1'})
        self.assertEqual(space.publication_tags(), ['#Games'])
        self.db.rename_workspace(wid, ' 123 / 🤖 !!! ')
        self.assertEqual(space.publication_tags(), ['#Новости'])

    async def test_long_publication_keeps_complete_links_signature_tags_and_html(self):
        from telethon.extensions.html import parse
        self.db.add_post('source', 7, 0, 'Source', 'https://t.me/source/7?x=1&y=2')
        pid = self.db.add_proposal('Long', ('😀<&' * 2000) + '\n\n#Тематический', [('source',7)])
        self.db.set('signature', 'Signature <&>')
        text, entities = parse(self.app.channel_html(self.db.get_proposal(pid)))
        self.assertIn('#Тематический', text)
        self.assertNotIn('#Основная', text)
        self.assertIn('Signature <&>', text)
        self.assertIn('Источники: [1]', text)
        self.assertEqual([e.url for e in entities if hasattr(e, 'url')], ['https://t.me/source/7?x=1&y=2'])
        self.assertLessEqual(len(text.encode('utf-16-le'))//2, 4096)
        self.assertNotIn('&lt;', text)
        self.db.set('signature', 'x'*4100)
        with self.assertRaises(UserError): await self.app.publish(pid)
        self.assertEqual(self.db.get_proposal(pid)['status'], 'new')
        self.assertEqual(self.bot.sent, [])

    async def test_media_emoji_caption_limit_uses_utf16_and_preserves_tagged_fallback(self):
        media = self.app.modules['media']; media.save({'enabled':'1'})
        self.db.set_proposal_media(self.pid, ('source',7))
        self.db.set_proposal_text(self.pid, '😀'*600+'\n\n#Медиа')
        with patch.object(media, 'fetch', return_value=b'offline-image'):
            await self.app.approve(self.pid)
        self.assertEqual(self.bot.files, [])
        self.assertEqual(len(self.bot.sent), 1)
        self.assertTrue(self.bot.sent[0][1].endswith('#Медиа'))
        self.assertNotIn('#Основная', self.bot.sent[0][1])

    async def test_repeated_delivery_retains_all_bot_generated_refs_for_cleanup(self):
        await self.app.send_proposal(self.pid)
        await self.app.send_proposal(self.pid)
        await self.app.skip(self.pid)
        self.assertEqual(len(self.bot.deleted), len(self.bot.sent))
        self.assertEqual({(c, ids[0]) for c,ids,_,_ in self.bot.deleted},
                         {(c, i+1) for i,(c,_,_) in enumerate(self.bot.sent)})

    async def test_media_short_caption_and_long_photo_then_text_have_final_tags(self):
        media = self.app.modules['media']; media.save({'enabled':'1','long_mode':'photo_then_text'})
        self.db.set_proposal_media(self.pid, ('source',7))
        self.db.set_proposal_text(self.pid, 'Draft <safe>\n\n#Фото')
        with patch.object(media, 'fetch', return_value=b'offline-image'):
            await self.app.approve(self.pid)
            self.assertTrue(self.bot.files[0][1]['caption'].endswith('#Фото'))
            self.assertNotIn('#Основная', self.bot.files[0][1]['caption'])
            pid = self.db.add_proposal('Long', 'x'*1200+'\n\n#Фото', [])
            self.db.set_proposal_media(pid, ('source',7))
            await self.app.approve(pid)
        self.assertEqual(len(self.bot.files),2)
        self.assertEqual(self.bot.files[1][1],{})
        self.assertTrue(self.bot.sent[0][1].endswith('#Фото'))
        self.assertNotIn('#Основная', self.bot.sent[0][1])

    async def test_queued_fire_uses_topic_tags_and_target_snapshot(self):
        self.db.set_proposal_text(self.pid, 'Queued news\n\n#Очередь #Новость')
        await self.app.deliver(self.pid)
        queue = self.app.modules['queue']; queue.save({'enabled':'1','notify':'0'})
        await self.app.approve(self.pid)
        self.db.set('target_channel','changed_out')
        await queue.fire()
        self.assertEqual(self.bot.sent[-1][0],'original_out')
        self.assertTrue(self.bot.sent[-1][1].endswith('#Очередь #Новость'))
        self.assertNotIn('#Основная', self.bot.sent[-1][1])
        self.assertEqual(self.db.get_proposal(self.pid)['status'],'published')
        self.assertEqual(len(self.bot.deleted),2)

    async def test_breaking_manual_moderation_and_direct_publish_use_owner_tags(self):
        from unittest.mock import AsyncMock
        wid=self.db.create_workspace('Срочные / IT')
        space=self.app.workspace(wid); space.db.set('target_channel','breaking_out')
        breaking=space.modules['breaking']; breaking.save({'enabled':'1'})
        space.db.add_post('source',7,1,'Breaking source','https://t.me/source/7')
        posts=space.db.posts_by_keys([('source',7)])
        model=SimpleNamespace(propose=AsyncMock(return_value=[{'title':'Urgent','text':'Urgent text\n\n#Разработка #Технологии','sources':[1]}]))
        with patch('modules.breaking.LLM',return_value=model): await breaking.handle(posts)
        pid=space.db.proposals()[0]['id']
        self.assertIn('#Срочные_IT', self.bot.sent[0][1])
        self.assertEqual(space.db.get_proposal(pid)['status'],'new')
        await space.publish(pid)
        self.assertEqual(self.bot.sent[-1][0],'breaking_out')
        self.assertIn('#Разработка #Технологии', self.bot.sent[-1][1])
        self.assertNotIn('#Срочные_IT', self.bot.sent[-1][1])
        self.assertEqual(space.db.get('mod.breaking.auto_publish'),'0')
        self.assertEqual(self.db.proposals()[0]['status'],'new')

if __name__ == '__main__': unittest.main()
