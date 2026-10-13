"""Separate workspace-management labels from model-selected topic hashtags."""
import unittest
from db import DB
from core import App
from telethon.extensions.html import parse

class HashtagScope(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db=DB(':memory:'); self.app=App(self.db)
        self.db.rename_workspace(1,'IT'); self.db.set('target_channel','offline_out')
    async def asyncTearDown(self): self.db.conn.close()
    def draft(self,text):
        return self.db.get_proposal(self.db.add_proposal('Heading',text,[]))
    async def test_workspace_label_is_only_in_management_card(self):
        p=self.draft('**Heading**\n\nВажный **факт**.\n\n#Нейросети #Технологии')
        preview=self.app.preview_html(p)
        channel=self.app.channel_html(p)
        self.assertIn('#IT',preview)
        self.assertNotIn('Хэштеги публикации:',preview)
        self.assertNotIn('#IT',channel)
        self.assertIn('#Нейросети #Технологии',channel)
        self.assertEqual(self.db.get_proposal(p['id'])['text'],p['text'])
        other=self.app.workspace(self.db.create_workspace('Наука'))
        q=other.db.get_proposal(other.db.add_proposal('Other','Other news',[]))
        self.assertIn('#Наука',other.preview_html(q))
        self.assertNotIn('#Наука',other.channel_html(q))
        self.assertNotIn('#IT',other.preview_html(q))

    async def test_propose_and_rewrite_require_relevant_channel_hashtags(self):
        import prompts
        self.db.set('prompt','Старое пожелание: никаких хэштегов.')
        self.db.set('publication_hashtags','#СлужебнаяМетка')
        for prompt in (prompts.build_propose(self.db.get,1),prompts.build_rewrite(self.db.get,'Без хэштегов.')):
            self.assertIn('Хэштеги канала',prompt)
            final=prompt.split('Хэштеги канала')[-1]
            self.assertIn('3–5',final)
            self.assertIn('конкретной новости',final)
            self.assertIn('название вкладки',final)
            self.assertNotIn('#СлужебнаяМетка',final)
        self.assertEqual(self.db.get('prompt'),'Старое пожелание: никаких хэштегов.')

    async def test_model_topic_tags_survive_long_body_and_service_footer(self):
        self.db.set('signature','Owner signature')
        self.db.add_post('source',1,0,'Original','https://t.me/source/1')
        pid=self.db.add_proposal('Heading','**Heading**\n\n'+'x '*3000+'\n\n#Нейросети #AI #ai #LLM',[('source',1)])
        p=self.db.get_proposal(pid)
        text,entities=parse(self.app.channel_html(p))
        self.assertIn('#Нейросети #AI #LLM',text)
        self.assertEqual(text.casefold().count('#ai'),1)
        self.assertNotIn('#IT',text)
        self.assertIn('Owner signature',text)
        self.assertIn('Источники: [1]',text)
        self.assertLessEqual(len(text.encode('utf-16-le'))//2,4096)
        self.assertEqual([e.url for e in entities if hasattr(e,'url')],['https://t.me/source/1'])
        self.assertEqual(self.db.get_proposal(pid)['text'],p['text'])

    async def test_ui_explains_service_labels_and_model_selected_channel_tags(self):
        from pathlib import Path
        html=(Path(__file__).resolve().parents[2]/'static/index.html').read_text()
        self.assertIn('Хэштеги карточек управления',html)
        self.assertIn('В канале нейросеть добавляет',html)
        self.assertIn('Метка только в боте управления',html)
        self.assertNotIn('<span>Хэштеги публикации</span>',html)

if __name__=='__main__':unittest.main()
