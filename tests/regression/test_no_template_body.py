"""A model echo in the article body must not revive the removed local template."""
import unittest
from news_outlook import assemble_outlook, OUTLOOK_HORIZONS
from post_format import render_post

FACTS = '**Измерения грунта**\n\nГородские службы изучают спутниковые измерения.'
TAGS = '#Мониторинг #Инфраструктура'

class BodyTemplateTests(unittest.TestCase):
    def test_standalone_legacy_template_echo_in_body_is_removed_without_fact_loss(self):
        for enabled in (False, True):
            for horizon in OUTLOOK_HORIZONS.values():
                paragraph = ('Редакционный сценарий: на ' + horizon +
                    ' это может повлиять на обсуждение новости. При новых подтверждённых деталях '
                    'оценку следует пересмотреть; последствия пока неизвестны.')
                for body in (paragraph, '**' + paragraph + '**', 'Это может повлиять на обсуждение новости.'):
                    with self.subTest(enabled=enabled, horizon=horizon, body=body):
                        result = assemble_outlook(FACTS + '\n\n' + body + '\n\n' + TAGS, None, enabled=enabled)
                        self.assertEqual(result, FACTS + '\n\n' + TAGS)
                        self.assertNotIn('Редакционный сценарий', render_post(result).plain)
                        self.assertNotIn('ИИ-прогноз', result)

    def test_attributed_prose_and_additional_facts_are_not_removed_as_a_template(self):
        prose = ['Источник назвал это «Редакционный сценарий» и сообщил о новых измерениях.',
                 'Это может повлиять на обсуждение новости. В отчёте указаны районы измерений.',
                 'Редакционный сценарий: местные службы обсуждают мониторинг грунта.']
        for paragraph in prose:
            with self.subTest(paragraph=paragraph):
                text = FACTS + '\n\n' + paragraph + '\n\n' + TAGS
                self.assertEqual(assemble_outlook(text, None), text)
