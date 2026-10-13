"""Offline readable-post contract; real scoped App/DB and Telethon parser."""
import unittest
from db import DB
from core import App
import prompts
from types import SimpleNamespace
from unittest.mock import patch
import json
import httpx2
import openai
import llm
from telethon.extensions.html import parse
from telethon.tl.types import MessageEntityBold

def bold_text(html_text):
    text, entities = parse(html_text)
    utf16 = text.encode('utf-16-le')
    return text, [(e.offset, e.length, utf16[e.offset*2:(e.offset+e.length)*2].decode('utf-16-le'))
                  for e in entities if isinstance(e, MessageEntityBold)]

class RecordingTelegram:
    def __init__(self): self.sent=[]; self.files=[]; self.edited=[]; self.deleted=[]
    async def send_message(self, target, text, **kw):
        self.sent.append((target, text, kw)); return SimpleNamespace(id=len(self.sent))
    async def send_file(self, target, file, **kw): self.files.append((target, kw))
    async def edit_message(self, *args, **kw): self.edited.append((args, kw))
    async def delete_messages(self, target, ids, **kw): self.deleted.append((target, ids))

class PostRendering(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = DB(':memory:'); self.app = App(self.db)
        self.db.set('target_channel', 'offline_out')
    async def asyncTearDown(self): self.db.conn.close()
    def draft(self, text, title='Headline'):
        pid = self.db.add_proposal(title, text, [])
        return self.db.get_proposal(pid)
    async def test_controlled_bold_roundtrip_and_literal_html_in_shared_preview(self):
        raw = '**📰 Heading 😀**\n\nЦена **3.14 & 5**. <script>alert(1)</script>\n\nURL https://example.test/?x=1&y=2 и <b>не HTML</b>.'
        p = self.draft(raw)
        out = self.app.channel_html(p)
        text, spans = bold_text(out)
        self.assertEqual([s[2] for s in spans], ['📰 Heading 😀', '3.14 & 5'])
        self.assertEqual(spans[0][:2], (0, len('📰 Heading 😀'.encode('utf-16-le'))//2))
        self.assertEqual(spans[1][0], len('📰 Heading 😀\n\nЦена '.encode('utf-16-le'))//2)
        self.assertIn('<script>alert(1)</script>', text)
        self.assertIn('https://example.test/?x=1&y=2', text)
        self.assertIn('<b>не HTML</b>', text)
        self.assertNotIn('<script>', out)
        self.assertNotIn('**', out)
        preview = self.app.preview_html(p)
        self.assertIn('<b>📰 Heading 😀</b>', preview)
        self.assertIn('<b>3.14 &amp; 5</b>', preview)
        self.assertEqual(self.db.get_proposal(p['id'])['text'], raw)

    async def test_legacy_wall_gets_short_heading_emoji_and_conservative_paragraphs(self):
        fact = 'И. И. Иванов сообщил рост на 3.14 процента. Адрес https://example.test/a.b?x=1&y=2 остаётся прежним. '
        raw = (fact * 7).strip()
        p = self.draft(raw, 'Короткий заголовок')
        text, spans = bold_text(self.app.channel_html(p))
        self.assertTrue(spans, 'Legacy draft has no actual bold headline')
        self.assertEqual(spans[0][2], '📰 Короткий заголовок')
        body = text.split('\n\n', 1)[1].rsplit('\n\n#', 1)[0]
        self.assertEqual(' '.join(body.split()), raw)
        self.assertGreaterEqual(body.count('\n\n'), 2)
        self.assertNotIn('И.\n\n', body)
        self.assertNotIn('3.\n\n14', body)
        self.assertNotIn('https://example.test/a.\n\nb', body)
        self.assertEqual(self.db.get_proposal(p['id'])['text'], raw)
        p = self.draft('Уже заголовок\n\nТекст новости. Ещё один факт.', 'Не дублировать')
        text, spans = bold_text(self.app.channel_html(p))
        self.assertEqual(spans[0][2], '📰 Уже заголовок')
        self.assertNotIn('Не дублировать', text)
        self.assertEqual(text.count('Уже заголовок'), 1)
        p = self.draft('Заголовок\n\nВажный факт ✅. Следующий факт.', 'Другая строка')
        text, _ = bold_text(self.app.channel_html(p))
        self.assertNotIn('📰', text)
        self.assertEqual(text.count('✅'), 1)

    async def test_emoji_off_removes_generated_clusters_but_keeps_raw_and_factual_symbols(self):
        self.db.set('emoji', '0')
        raw = '**📰 Heading**\n\nРост **5% 😀**; семья 👨‍👩‍👧, флаг 🇷🇺. 1️⃣ факт, © 2026, x × 2 ≥ 1.'
        p = self.draft(raw)
        text, spans = bold_text(self.app.channel_html(p))
        self.assertNotIn('📰', text); self.assertNotIn('😀', text)
        self.assertNotIn('👨', text); self.assertNotIn('🇷', text)
        self.assertNotIn('\u200d', text); self.assertNotIn('\ufe0f', text)
        self.assertIn('1 факт, © 2026, x × 2 ≥ 1.', text)
        self.assertEqual([s[2] for s in spans], ['Heading', '5%'])
        self.assertEqual(self.db.get_proposal(p['id'])['text'], raw)
        plain = self.draft('Нет эмодзи. Только факты.')
        self.assertNotIn('📰', self.app.channel_html(plain))

    async def test_proposals_api_exposes_sanitized_preview_without_changing_raw_editor(self):
        from aiohttp.test_utils import TestClient, TestServer
        from web import build_web
        raw = '**Heading**\n\n**Important** & <script>window.pwned=1</script>.'
        p = self.draft(raw)
        async with TestClient(TestServer(build_web(self.app, 'offline-password'))) as c:
            self.assertEqual((await c.get('/api/proposals')).status, 401)
            await c.post('/api/login', json={'password': 'offline-password'})
            item = (await (await c.get('/api/proposals')).json())[0]
            self.assertIn('rendered_html', item, 'No safe rendered preview supplied')
            self.assertEqual(item['text'], raw)
            self.assertEqual(item['rendered_html'], self.app.post_body(p).html())
            self.assertIn('<b>Important</b>', item['rendered_html'])
            self.assertNotIn('<script>', item['rendered_html'])
            self.assertEqual((await c.post(f"/api/proposals/{p['id']}/text", json={'text':'x'})).status, 403)

    async def test_preview_reserves_utf16_footer_before_atomic_body_truncation(self):
        sources = []
        for i in range(12):
            link = 'https://t.me/source/' + str(i) + '?q=' + 'x'*100
            self.db.add_post('source', i, 0, 'original', link)
            sources.append(('source', i))
        pid = self.db.add_proposal('Heading', '**Heading**\n\n' + '😀<& '*1100 + '**atomic tail**', sources)
        p = self.db.get_proposal(pid)
        out = self.app.preview_html(p)
        text, spans = bold_text(out)
        self.assertLessEqual(len(text.encode('utf-16-le'))//2, 4096)
        self.assertIn('https://t.me/source/11?q=' + 'x'*100, text)
        self.assertIn('#Основная', text)
        self.assertNotIn('**', out)
        self.assertEqual(out.count('<b>'), out.count('</b>'))
        self.assertIn('Heading', [s[2] for s in spans])

    async def test_nullable_legacy_title_does_not_break_publication_or_preview(self):
        raw = 'Факт без заголовка. Второй факт.'
        p = self.draft(raw, None)
        channel, spans = bold_text(self.app.channel_html(p))
        self.assertIn(raw, channel)
        self.assertIn('📰', channel)
        self.assertIn(raw, bold_text(self.app.preview_html(p))[0])
        self.assertIsNone(self.db.get_proposal(p['id'])['title'])

    async def test_a_title_only_legacy_draft_is_not_duplicated(self):
        p = self.draft('Same headline', 'Same headline')
        text, spans = bold_text(self.app.channel_html(p))
        self.assertEqual(text.count('Same headline'), 1)
        self.assertEqual(spans[0][2], '📰 Same headline')

    async def test_bold_entities_survive_direct_media_queue_and_manual_edit_transports(self):
        bot = self.app.bot = RecordingTelegram()
        raw = '**📰 Transport heading**\n\nImportant **3.14 & 5**.\n\nSecond fact.'
        def assert_transport(out, kw):
            self.assertEqual(kw['parse_mode'], 'html')
            self.assertEqual([s[2] for s in bold_text(out)[1]], ['📰 Transport heading', '3.14 & 5'])
            self.assertNotIn('**', out)
        p = self.draft(raw); await self.app.publish(p['id'])
        assert_transport(bot.sent[-1][1], bot.sent[-1][2])
        media = self.app.modules['media']; media.save({'enabled':'1'})
        p = self.draft(raw); self.db.set_proposal_media(p['id'], ('source', 7))
        with patch.object(media, 'fetch', return_value=b'offline-image'):
            await self.app.approve(p['id'])
        assert_transport(bot.files[-1][1]['caption'], bot.files[-1][1])
        self.assertEqual(self.db.get_proposal(p['id'])['text'], raw)
        # The visible caption is under1024 despite hundreds of raw bold markers.
        many = '**📰 Caption**\n\n' + ('**x** ' * 180)
        self.assertGreater(len(many), 1024)
        p = self.draft(many); self.db.set_proposal_media(p['id'], ('source', 7))
        with patch.object(media, 'fetch', return_value=b'offline-image'):
            await self.app.approve(p['id'])
        caption = bot.files[-1][1]['caption']
        self.assertLessEqual(len(bold_text(caption)[0].encode('utf-16-le'))//2, 1024)
        self.assertEqual(len(bold_text(caption)[1]), many.count('**x**') + 1)
        media.save({'long_mode':'photo_then_text'})
        p = self.draft(raw + '\n\n' + 'additional fact '*100)
        self.db.set_proposal_media(p['id'], ('source', 7))
        with patch.object(media, 'fetch', return_value=b'offline-image'):
            await self.app.approve(p['id'])
        self.assertEqual(bot.files[-1][1], {})
        assert_transport(bot.sent[-1][1], bot.sent[-1][2])
        queue = self.app.modules['queue']; queue.save({'enabled':'1','notify':'0'})
        p = self.draft('Before edit'); await self.app.set_text(p['id'], raw)
        await self.app.approve(p['id']); self.assertEqual(self.db.get_proposal(p['id'])['status'], 'queued')
        await queue.fire(); assert_transport(bot.sent[-1][1], bot.sent[-1][2])
        self.assertEqual(self.db.get_proposal(p['id'])['text'], raw)

    async def test_real_llm_rewrite_and_breaking_keep_raw_contract_and_bold_without_extra_calls(self):
        self.app.bot = RecordingTelegram(); self.db.set('admin_ids','123')
        self.db.set('llm_api_key','offline-key'); self.db.set('llm_model','offline-model')
        raw = '**📰 Provider heading**\n\nImportant **5%**.\n\nSecond fact.'
        expected = raw + ('\n\n🤖 **ИИ-прогноз**\nРедакционный сценарий: на ближайшие дни/недели это может повлиять на обсуждение новости. '
                          'При новых подтверждённых деталях оценку следует пересмотреть; последствия пока неизвестны.')
        calls, clients = [], []
        def factory(**kw):
            def respond(request):
                body = json.loads(request.content); calls.append(body)
                content = json.dumps({'proposals':[{'title':'Provider heading','text':raw,'sources':[1]}]}) if 'response_format' in body else raw
                return httpx2.Response(200, json={'id':'offline','object':'chat.completion','created':1,
                    'model':body['model'],'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':content}}],
                    'usage':{'prompt_tokens':10,'completion_tokens':10}})
            client=openai.AsyncOpenAI(**kw, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)))
            clients.append(client); return client
        try:
            with patch.object(llm, 'AsyncOpenAI', factory):
                p = self.draft('old'); await self.app.deliver(p['id'])
                await self.app.rewrite(p['id'])
                self.assertEqual(self.db.get_proposal(p['id'])['text'], expected)
                edited = self.app.bot.edited[-1][0][2]
                self.assertIn('5%', [s[2] for s in bold_text(edited)[1]])
                await self.app.publish(p['id'])
                self.assertEqual([s[2] for s in bold_text(self.app.bot.sent[-1][1])[1]], ['📰 Provider heading', '5%', 'ИИ-прогноз'])
                self.db.add_post('source', 7, 1, 'source facts', 'https://t.me/source/7')
                breaking = self.app.modules['breaking']; breaking.save({'enabled':'1'})
                await breaking.handle(self.db.posts_by_keys([('source',7)]))
                proposal = self.db.proposals(kind='breaking')[0]
                self.assertEqual(proposal['text'], expected)
                self.assertIn('<b>5%</b>', self.app.bot.sent[-1][1])
                await self.app.publish(proposal['id'])
                self.assertEqual([s[2] for s in bold_text(self.app.bot.sent[-1][1])[1]], ['📰 Provider heading', '5%', 'ИИ-прогноз'])
                self.assertEqual(len(calls), 2, 'No additional formatting LLM pass')
                for call in calls:
                    final = call['messages'][0]['content'].split('Обязательное оформление')[-1]
                    self.assertIn('**', final); self.assertIn('2–4', final)
        finally:
            for client in clients: await client.close()

    async def test_unbalanced_markers_are_literal_plain_and_factual_asterisks_survive(self):
        self.db.set('emoji','0')
        for raw in ('Fact **unfinished & <script>x</script>', '2 * 3 = 6; **ok** but **unfinished',
                    'a ***bad*** b', 'a **two\nlines** b'):
            with self.subTest(raw=raw):
                p = self.draft(raw)
                out = self.app.channel_html(p)
                text, spans = bold_text(out)
                self.assertIn(raw, text)
                self.assertEqual([s[2] for s in spans], ['Headline'])
                self.assertNotIn('<script>', out)
                self.assertEqual(out.count('<b>'), out.count('</b>'))

    async def test_channel_atomic_bold_utf16_budget_and_rendered_hashtag_dedup(self):
        self.db.set('signature','Signed <&>'); self.db.set('emoji','0')
        self.db.add_post('source',7,0,'source','https://t.me/source/7?x=1&y=2')
        pid=self.db.add_proposal('Heading','**Heading**\n\n' + '😀<& '*1200 + '**ATOMIC SPAN '+('x'*1000)+'**',[('source',7)])
        out=self.app.channel_html(self.db.get_proposal(pid)); text, spans=bold_text(out)
        self.assertLessEqual(len(text.encode('utf-16-le'))//2,4096)
        self.assertNotIn('ATOMIC',text); self.assertIn('Signed <&>',text)
        self.assertNotIn('#Основная',text)
        self.assertEqual(out.count('<b>'),out.count('</b>'))
        self.assertEqual([e.url for e in parse(out)[1] if hasattr(e,'url')],['https://t.me/source/7?x=1&y=2'])
        p=self.draft('**Heading**\n\nFact **#Основная**.', 'Heading')
        self.assertEqual(bold_text(self.app.channel_html(p))[0].count('#Основная'),1)

class PromptContract(unittest.TestCase):
    def test_final_contract_overrides_saved_plain_text_and_module_rewrite_rules(self):
        db = DB(':memory:')
        try:
            custom = 'Старое правило: простой текст без Markdown и HTML. {emoji}'
            db.set('prompt', custom)
            for build in (lambda: prompts.build_propose(db.get, 2, 'Только plain text'),
                          lambda: prompts.build_rewrite(db.get, 'Только plain text')):
                out = build()
                final = out[out.rfind('Обязательное оформление'):]
                self.assertIn('**', final)
                self.assertIn('2–4', final)
                self.assertIn('1–3', final)
                self.assertIn('1–2 уместных эмодзи', final)
                self.assertIn('трагед', final)
                self.assertNotIn('Можно 1–2', out)
                self.assertGreater(out.rfind('Обязательное оформление'), out.rfind('Только plain text'))
            self.assertEqual(db.get('prompt'), custom)
            db.set('emoji', '0')
            self.assertIn('Без эмодзи', prompts.build_rewrite(db.get).split('Обязательное оформление')[-1])
        finally:
            db.conn.close()
