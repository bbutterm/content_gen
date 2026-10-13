"""Synthetic-only news outlook acceptance, real SDK with offline HTTP boundary."""
import json
import unittest
from unittest.mock import patch

import httpx2
import openai
from telethon.extensions.html import parse

import llm
import prompts
from cycle_cost import CURRENT_CYCLE, CycleReport
from db import DB
from post_format import render_post

FACTS = '**Обновление сервиса**\n\nКомпания объявила о тестировании функции.\n\nПроверка пока ограничена пилотной группой.'
TAGS = '#Сервис #Тестирование #Технологии'
CONSEQUENCE = 'Если пилот подтвердит результаты, в ближайшие недели возможно расширение тестирования.'
ASSUMPTION = 'Условие — успешное завершение пилота.'
UNCERTAINTY = 'Сроки не подтверждены; результат может измениться.'
GENERIC_ECHO = ('Редакционный сценарий: на ближайшие дни/недели это может повлиять на обсуждение новости. '
            'При новых подтверждённых деталях оценку следует пересмотреть; последствия пока неизвестны.')


class OutlookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = DB(':memory:')
        self.db.set('llm_api_key', 'offline-fixture-key')
        self.db.set('llm_model', 'offline-fixture-model')
        self.db.set('llm_provider', 'openrouter')
        self.db.set('llm_input_usd_per_million', '2.5')
        self.db.set('llm_output_usd_per_million', '10')
        self.posts = [{'channel': 'fixture', 'date': 1, 'text': 'Компания тестирует функцию в пилотной группе.'}]
        self.calls, self.clients = [], []
        self.content = ''
        self.statuses = []
        def respond(request):
            self.calls.append(json.loads(request.content))
            if self.statuses:
                status = self.statuses.pop(0)
                if status != 200:
                    return httpx2.Response(status, json={'error': {'message': 'offline JSON mode unsupported', 'type': 'fixture'}})
            return httpx2.Response(200, json={'id': 'fixture', 'object': 'chat.completion', 'created': 1,
                'model': 'offline-fixture-model', 'choices': [{'index': 0, 'finish_reason': 'stop',
                    'message': {'role': 'assistant', 'content': self.content}}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120, 'cost': 0.001}})
        def sdk(**kwargs):
            client = openai.AsyncOpenAI(**kwargs, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)))
            self.clients.append(client)
            return client
        self.boundary = patch.object(llm, 'AsyncOpenAI', sdk)
        self.boundary.start()

    async def asyncTearDown(self):
        self.boundary.stop()
        for client in self.clients:
            await client.close()
        self.db.conn.close()

    def proposal(self, outlook):
        self.content = json.dumps({'proposals': [{'title': 'Обновление сервиса', 'text': FACTS + '\n\n' + TAGS,
            'sources': [1], 'outlook': outlook}]}, ensure_ascii=False)

    def valid_outlook(self):
        return {'consequences': [CONSEQUENCE], 'assumptions': ASSUMPTION, 'uncertainty': UNCERTAINTY}

    async def generate(self):
        report = CycleReport(self.db, True)
        token = CURRENT_CYCLE.set(report)
        try:
            result = await llm.LLM(self.db).propose(self.posts, 1, [], 'Не добавляй прогноз.')
            report.finish('completed')
        finally:
            CURRENT_CYCLE.reset(token)
        self.assertEqual(len(self.calls), 1, 'Outlook must use the existing completion only')
        self.assertEqual(report.report['calls'], 1)
        self.assertEqual(report.report['cost_usd'], '0.001')
        self.assertEqual(report.report['attempts'][0]['stage'], 'propose')
        return result[0]

    async def test_generation_labels_conditional_outlook_before_topic_tags_in_one_request(self):
        self.proposal(self.valid_outlook())
        result = await self.generate()
        expected = FACTS + '\n\n🤖 **ИИ-прогноз**\n' + ' '.join((CONSEQUENCE, ASSUMPTION, UNCERTAINTY)) + '\n\n' + TAGS
        self.assertEqual(result['text'], expected)
        self.assertEqual(result['sources'], [1])
        body, tags = render_post(result['text']).split_topic_hashtags()
        visible, entities = parse(body.html())
        self.assertEqual(tags, TAGS.split())
        encoded = visible.encode('utf-16-le')
        self.assertIn('ИИ-прогноз', [encoded[e.offset*2:(e.offset+e.length)*2].decode('utf-16-le') for e in entities])
        system = self.calls[0]['messages'][0]['content']
        self.assertIn('"outlook"', system)
        self.assertGreater(system.rfind('Новостной ИИ-прогноз'), system.rfind('Не добавляй прогноз.'))
        self.assertTrue(system.endswith(prompts.NEWS_OUTLOOK_RULES))
        self.assertNotIn('ИИ-прогноз', prompts.FORMAT_RULES)
        self.assertEqual(self.calls[0]['response_format'], {'type': 'json_object'})


    async def test_missing_outlook_keeps_only_facts_and_tags_without_another_request(self):
        self.content = json.dumps({'proposals': [{'title': 'Обновление сервиса', 'text': FACTS + '\n\n' + TAGS, 'sources': [1]}]})
        result = await self.generate()
        self.assertEqual(result['text'], FACTS + '\n\n' + TAGS)
        public = render_post(result['text'], emoji=False)
        self.assertNotIn('ИИ-прогноз', public.plain)
        self.assertNotIn('Редакционный сценарий', public.html())
        self.assertTrue(public.plain.endswith(TAGS))


    async def test_provider_generic_editorial_echo_is_omitted_in_all_news_paths(self):
        echoes = (GENERIC_ECHO, 'Это может повлиять на обсуждение новости.',
                  'ЭТО МОЖЕТ  ПОВЛИЯТЬ НА ОБСУЖДЕНИЕ ЭТОЙ НОВОСТИ!')
        old = FACTS + '\n\n**ИИ-прогноз**\n' + GENERIC_ECHO + '\n\n' + TAGS
        for enabled in ('1', '0'):
            self.db.set('news_outlook_enabled', enabled)
            for echo in echoes:
                for operation in ('propose', 'rewrite'):
                    with self.subTest(enabled=enabled, echo=echo, operation=operation):
                        self.calls.clear()
                        outlook = {**self.valid_outlook(), 'consequences': [echo]}
                        if operation == 'propose':
                            self.proposal(outlook)
                            result = (await self.generate())['text']
                        else:
                            self.content = FACTS + '\n\n**ИИ-прогноз**\n' + ' '.join((echo, ASSUMPTION, UNCERTAINTY)) + '\n\n' + TAGS
                            report = CycleReport(self.db, True, operation_type='rewrite')
                            token = CURRENT_CYCLE.set(report)
                            try:
                                result = await llm.LLM(self.db).rewrite(self.posts, old)
                                report.finish('completed')
                            finally:
                                CURRENT_CYCLE.reset(token)
                            self.assertEqual(len(self.calls), 1)
                            self.assertEqual(report.report['calls'], 1)
                            self.assertEqual(report.report['cost_usd'], '0.001')
                            self.assertNotIn(GENERIC_ECHO, self.calls[0]['messages'][1]['content'])
                        self.assertEqual(result, FACTS + '\n\n' + TAGS)
                        public = render_post(result, emoji=False)
                        for output in (result, public.plain, public.html()):
                            self.assertNotIn('ИИ-прогноз', output)
                            self.assertNotIn('Редакционный сценарий', output)
                            self.assertNotIn('обсуждение новости', output)
                        self.assertTrue(public.plain.endswith(TAGS))

    async def test_unusable_forecasts_keep_one_sdk_attempt_in_all_four_news_paths(self):
        invalid_cases = ('missing', 'null', 'malformed', 'unsafe', 'overlong', 'no_evidence', 'empty_label')
        old = FACTS + '\n\n**ИИ-прогноз**\n' + GENERIC_ECHO + '\n\n' + TAGS
        pid = self.db.add_proposal('Manual unchanged', old, [])
        source_posts = list(self.posts)
        for enabled in ('1', '0'):
            self.db.set('news_outlook_enabled', enabled)
            for case in invalid_cases:
                self.posts = [] if case == 'no_evidence' else source_posts
                self.db.set('news_outlook_max_len', '200' if case == 'overlong' else '600')
                outlook = self.valid_outlook()
                if case in ('missing', 'null', 'empty_label'):
                    outlook = None
                elif case == 'malformed':
                    outlook = {'consequences': [12], 'assumptions': [], 'uncertainty': None}
                elif case == 'unsafe':
                    outlook['consequences'] = ['Это может привести к росту на 99%.']
                elif case == 'overlong':
                    outlook['consequences'] = ['Это может привести к расширению тестирования' + ' сервиса' * 35 + '.']
                for operation in ('propose', 'rewrite'):
                    with self.subTest(enabled=enabled, case=case, operation=operation):
                        self.calls.clear()
                        if operation == 'propose':
                            self.proposal(outlook)
                            payload = json.loads(self.content)
                            payload['proposals'][0]['text'] = old
                            if case == 'missing':
                                del payload['proposals'][0]['outlook']
                            self.content = json.dumps(payload, ensure_ascii=False)
                        else:
                            prediction = ''
                            if isinstance(outlook, dict) and case != 'malformed':
                                prediction = ' '.join(outlook['consequences'] + [ASSUMPTION, UNCERTAINTY])
                            elif case == 'malformed':
                                prediction = 'Некорректный вывод.'
                            block = '\n\n**ИИ-прогноз**\n' + prediction if case not in ('missing', 'null') else ''
                            self.content = FACTS + block + '\n\n' + TAGS
                        report = CycleReport(self.db, True, operation_type='rewrite')
                        token = CURRENT_CYCLE.set(report)
                        try:
                            model = llm.LLM(self.db)
                            if operation == 'propose':
                                result = (await model.propose(self.posts, 1, []))[0]['text']
                            else:
                                result = await model.rewrite(self.posts, old)
                            report.finish('completed')
                        finally:
                            CURRENT_CYCLE.reset(token)
                        self.assertEqual(result, FACTS + '\n\n' + TAGS)
                        self.assertEqual(len(self.calls), 1, 'Never repair an unusable forecast with another completion')
                        self.assertEqual(report.report['calls'], 1)
                        self.assertEqual(len(report.report['attempts']), 1)
                        self.assertEqual(report.report['attempts'][0]['status'], 'completed')
                        self.assertEqual(report.report['cost_usd'], '0.001')
                        public = render_post(result, emoji=False)
                        for output in (result, public.plain, public.html()):
                            self.assertNotIn('ИИ-прогноз', output)
                            self.assertNotIn('Редакционный сценарий', output)
                        self.assertTrue(public.plain.endswith(TAGS))
                        self.assertEqual(self.db.get_proposal(pid)['text'], old)
        self.assertEqual(self.db.get('auto_publish'), '0')

    async def test_grounded_city_service_consequence_is_retained_not_fabricated_locally(self):
        facts = '**Наблюдение грунта**\n\nСпутниковые оценки указывают на проседание грунта и риск повреждения городской инфраструктуры.'
        tags = '#Города #Инфраструктура #Мониторинг'
        consequence = 'Городские службы могут пересмотреть приоритеты осмотра зданий и канализации.'
        assumption = 'Условие — местные измерения подтвердят спутниковые оценки.'
        uncertainty = 'Масштаб риска неизвестен; оценка может измениться.'
        outlook = {'consequences': [consequence], 'assumptions': assumption, 'uncertainty': uncertainty}
        self.posts = [{'channel': 'fixture', 'date': 1, 'text': 'Спутниковая оценка проседания грунта указывает на риск для инфраструктуры.'}]
        self.content = json.dumps({'proposals': [{'title': 'Наблюдение грунта', 'text': facts + '\n\n' + tags,
            'sources': [1], 'outlook': outlook}]}, ensure_ascii=False)
        generated = (await self.generate())['text']
        expected = facts + '\n\n🤖 **ИИ-прогноз**\n' + ' '.join((consequence, assumption, uncertainty)) + '\n\n' + tags
        self.assertEqual(generated, expected)
        self.calls.clear()
        self.content = expected
        rewritten = await llm.LLM(self.db).rewrite(self.posts, facts)
        self.assertEqual(rewritten, expected)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('Редакционный сценарий', render_post(rewritten).plain)

    async def test_specific_regulation_discussion_is_not_a_generic_editorial_echo(self):
        consequence = 'Это может повлиять на обсуждение правил отчётности участниками пилотной группы.'
        self.proposal({**self.valid_outlook(), 'consequences': [consequence]})
        generated = await self.generate()
        self.assertIn(consequence, generated['text'])
        self.calls.clear()
        self.content = generated['text']
        rewritten = await llm.LLM(self.db).rewrite(self.posts, FACTS)
        self.assertEqual(rewritten, generated['text'])
        self.assertEqual(len(self.calls), 1)

    async def test_invalid_or_certain_outlook_is_rejected_not_repaired_or_regenerated(self):
        invalid = [None, '', [], {}, {'consequences': 'not a list'},
            {**self.valid_outlook(), 'consequences': []},
            {**self.valid_outlook(), 'consequences': [CONSEQUENCE] * 3},
            {**self.valid_outlook(), 'assumptions': ''},
            {**self.valid_outlook(), 'uncertainty': ''},
            {**self.valid_outlook(), 'consequences': ['На следующей неделе пилот точно будет расширен.']},
            {**self.valid_outlook(), 'consequences': ['Если пилот успешен, расширение может произойти с вероятностью 80%.']},
            {**self.valid_outlook(), 'uncertainty': '<script>unsupported</script>'}]
        for outlook in invalid:
            with self.subTest(outlook=outlook):
                self.calls.clear()
                self.proposal(outlook)
                result = await self.generate()
                self.assertEqual(result['text'], FACTS + '\n\n' + TAGS)


    async def test_generation_accepts_modal_consequence_without_literal_time_token(self):
        consequence = 'Если это так, возможно улучшение.'
        self.proposal({**self.valid_outlook(), 'consequences': [consequence]})
        result = await self.generate()
        self.assertEqual(result['text'], FACTS + '\n\n🤖 **ИИ-прогноз**\n' +
                         ' '.join((consequence, ASSUMPTION, UNCERTAINTY)) + '\n\n' + TAGS)
        self.assertNotIn(GENERIC_ECHO, result['text'])

    async def test_rewrite_replaces_old_outlook_preserves_facts_and_tags_with_one_cost_attempt(self):
        old = FACTS + '\n\n🤖 **ИИ-прогноз**\nСтарый необоснованный вывод.\n\n' + TAGS
        prediction = ' '.join((CONSEQUENCE, ASSUMPTION, UNCERTAINTY))
        # A provider may echo the stale block; the final newly authored block wins.
        self.content = old.removesuffix(TAGS) + '**ИИ-прогноз**\n' + prediction + '\n\n' + TAGS
        report = CycleReport(self.db, True, operation_type='rewrite')
        token = CURRENT_CYCLE.set(report)
        try:
            text = await llm.LLM(self.db).rewrite(self.posts, old, 'Без прогноза.')
            report.finish('completed')
        finally:
            CURRENT_CYCLE.reset(token)
        self.assertEqual(text, FACTS + '\n\n🤖 **ИИ-прогноз**\n' + prediction + '\n\n' + TAGS)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(report.report['calls'], 1)
        self.assertEqual(report.report['cost_usd'], '0.001')
        self.assertNotIn('response_format', self.calls[0], 'Rewrite stays a plain-text completion')
        self.assertNotIn('Старый необоснованный вывод', self.calls[0]['messages'][1]['content'])
        self.assertTrue(self.calls[0]['messages'][0]['content'].endswith(prompts.NEWS_OUTLOOK_RULES))
        self.assertIn('Не обещай исход', self.calls[0]['messages'][0]['content'])
        self.assertEqual(old.count('Старый необоснованный вывод'), 1, 'Input not mutated')


    async def test_generation_without_usable_source_references_cannot_claim_an_outlook(self):
        for sources in ([], [0], [99]):
            with self.subTest(sources=sources):
                self.calls.clear()
                self.content = json.dumps({'proposals': [{'title': 'Обновление сервиса', 'text': FACTS + '\n\n' + TAGS,
                    'sources': sources, 'outlook': self.valid_outlook()}]})
                result = await self.generate()
                self.assertEqual(result['text'], FACTS + '\n\n' + TAGS)
                self.assertNotIn(CONSEQUENCE, result['text'])

    async def test_rewrite_without_source_evidence_omits_outlook_not_old_inference(self):
        self.content = FACTS + '\n\n**ИИ-прогноз**\n' + ' '.join((CONSEQUENCE, ASSUMPTION, UNCERTAINTY)) + '\n\n' + TAGS
        self.posts = []
        text = await llm.LLM(self.db).rewrite(self.posts, FACTS)
        self.assertEqual(text, FACTS + '\n\n' + TAGS)
        self.assertEqual(len(self.calls), 1)


    async def test_rewrite_rejects_missing_assumptions_and_excess_consequences(self):
        invalid = [CONSEQUENCE + ' ' + UNCERTAINTY,
            ' '.join((CONSEQUENCE, CONSEQUENCE, CONSEQUENCE, ASSUMPTION, UNCERTAINTY)),
            CONSEQUENCE + ' Пилот гарантированно расширят. ' + ASSUMPTION + ' ' + UNCERTAINTY]
        for prediction in invalid:
            with self.subTest(prediction=prediction):
                self.calls.clear()
                self.content = FACTS + '\n\n**ИИ-прогноз**\n' + prediction + '\n\n' + TAGS
                text = await llm.LLM(self.db).rewrite(self.posts, FACTS)
                self.assertEqual(text, FACTS + '\n\n' + TAGS)
                self.assertEqual(len(self.calls), 1)


    async def test_generation_removes_separate_paragraph_old_blocks_even_with_bold_robot(self):
        self.content = json.dumps({'proposals': [{'title': 'Обновление сервиса',
            'text': FACTS + '\n\n**🤖 ИИ-прогноз**\n\nСтарый прогноз.\n\nИИ-прогноз: Второй старый прогноз.\n\n' + TAGS,
            'sources': [1], 'outlook': self.valid_outlook()}]})
        result = await self.generate()
        self.assertEqual(result['text'], FACTS + '\n\n🤖 **ИИ-прогноз**\n' + ' '.join((CONSEQUENCE, ASSUMPTION, UNCERTAINTY)) + '\n\n' + TAGS)


    async def test_structured_outlook_rejects_certainty_hidden_after_conditional_sentence(self):
        outlook = self.valid_outlook()
        outlook['consequences'] = [CONSEQUENCE + ' Расширение гарантированно состоится.']
        self.proposal(outlook)
        result = await self.generate()
        self.assertEqual(result['text'], FACTS + '\n\n' + TAGS)
        self.assertNotIn('гарантированно', result['text'])


    async def test_emoji_off_keeps_label_conditional_outlook_and_tags(self):
        self.db.set('emoji', '0')
        self.proposal(self.valid_outlook())
        result = await self.generate()
        self.assertEqual(result['text'], FACTS + '\n\n**ИИ-прогноз**\n' + ' '.join((CONSEQUENCE, ASSUMPTION, UNCERTAINTY)) + '\n\n' + TAGS)
        self.assertNotIn('🤖', render_post(result['text'], emoji=False).plain)
        self.calls.clear()
        self.content = FACTS + '\n\n**ИИ-прогноз**\n' + ' '.join((CONSEQUENCE, ASSUMPTION, UNCERTAINTY)) + '\n\n' + TAGS
        rewritten = await llm.LLM(self.db).rewrite(self.posts, FACTS)
        self.assertEqual(rewritten, result['text'])
        self.assertEqual(len(self.calls), 1)

    async def test_rewrite_missing_or_certain_prediction_keeps_emoji_off_facts_only(self):
        self.db.set('emoji', '0')
        for forecast in ('', '\n\n**ИИ-прогноз**\nНа следующей неделе всё точно улучшится.',
                         '\n\n**ИИ-прогноз**\nЕсли пилот успешен, в ближайшие дни рост может составить 99%. Условие — пилот. Срок неизвестен.'):
            with self.subTest(forecast=forecast):
                self.calls.clear()
                self.content = FACTS + forecast + '\n\n' + TAGS
                text = await llm.LLM(self.db).rewrite(self.posts, FACTS)
                self.assertEqual(text, FACTS + '\n\n' + TAGS)
                self.assertEqual(len(self.calls), 1)

    async def test_json_mode_fallback_still_records_only_existing_two_attempts(self):
        self.statuses = [400, 200]
        self.proposal(self.valid_outlook())
        report = CycleReport(self.db, True)
        token = CURRENT_CYCLE.set(report)
        try:
            result = await llm.LLM(self.db).propose(self.posts, 1, [])
            report.finish('completed')
        finally:
            CURRENT_CYCLE.reset(token)
        self.assertIn(CONSEQUENCE, result[0]['text'])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(report.report['calls'], 2)
        self.assertEqual([a['status'] for a in report.report['attempts']], ['failed', 'completed'])
        self.assertEqual(report.report['known_cost_usd'], '0.001')
        self.assertIsNone(report.report['cost_usd'])
        self.assertFalse(report.report['cost_complete'])
        self.assertNotIn('response_format', self.calls[-1])

    async def test_generic_chat_for_plan_or_connection_does_not_append_a_news_outlook(self):
        self.content = 'Обычный учебный материал без прогноза.'
        result = await llm.LLM(self.db).chat(prompts.FORMAT_RULES, 'Educational post')
        self.assertEqual(result, self.content)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('ИИ-прогноз', self.calls[0]['messages'][0]['content'])

    async def test_rendering_and_manual_owner_edits_are_not_automatically_changed(self):
        raw = FACTS + '\n\nМой ручной вывод без метки ИИ.\n\n' + TAGS
        pid = self.db.add_proposal('Обновление сервиса', raw, [])
        draft = self.db.get_proposal(pid)
        self.assertNotIn('ИИ-прогноз', render_post(draft['text']).plain)
        self.assertEqual(self.db.get_proposal(pid)['text'], raw)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.db.cycle_cost_state()['history'], [])
        self.assertEqual(self.db.operation_cost_state()['history'], [])


    async def test_empty_rewrite_does_not_create_a_publishable_forecast_only_article(self):
        self.content = ''
        text = await llm.LLM(self.db).rewrite(self.posts, FACTS)
        self.assertEqual(text, '')
        self.assertEqual(len(self.calls), 1)

    async def test_fact_heading_about_forecasting_is_not_removed_as_a_label(self):
        raw = '**ИИ-прогнозирование обсуждают**\n\nКомпания тестирует новую функцию.'
        self.content = json.dumps({'proposals': [{'title': 'ИИ-прогнозирование обсуждают', 'text': raw,
            'sources': [1], 'outlook': None}]})
        result = await self.generate()
        self.assertEqual(result['text'], raw)


    async def test_unbalanced_forecast_markup_is_omitted_without_breaking_fact_bold(self):
        outlook = self.valid_outlook()
        outlook['uncertainty'] = UNCERTAINTY + ' **незакрыто'
        self.proposal(outlook)
        result = await self.generate()
        self.assertEqual(result['text'], FACTS + '\n\n' + TAGS)
        html = render_post(result['text'], emoji=False).html()
        self.assertIn('<b>Обновление сервиса</b>', html)
        self.assertNotIn('<b>ИИ-прогноз</b>', html)


    async def test_two_conditional_consequences_are_kept_without_inventing_topic_tags(self):
        second = 'Если проверка выявит проблемы, в ближайшие дни возможна задержка тестирования.'
        outlook = self.valid_outlook()
        outlook['consequences'].append(second)
        self.content = json.dumps({'proposals': [{'title': 'Обновление сервиса', 'text': FACTS,
            'sources': [1], 'outlook': outlook}]})
        result = await self.generate()
        self.assertEqual(result['text'], FACTS + '\n\n🤖 **ИИ-прогноз**\n' + ' '.join((CONSEQUENCE, second, ASSUMPTION, UNCERTAINTY)))
        self.assertNotIn('#', result['text'], 'Assembly never fabricates tags for noncompliant provider output')


if __name__ == '__main__':
    unittest.main()
