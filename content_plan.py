"""Independent workspace content plan. Paid work starts only via explicit jobs."""
from datetime import datetime, timedelta
import re
import time

import asyncio
import json
import os
from pathlib import Path
from cycle_cost import CURRENT_CYCLE, CycleReport
import html
from post_format import utf16_len
from settings import to_peer
from telethon.tl.types import DocumentAttributeVideo
from plan_store import PlanStore
from settings import UserError


class PlanEngine:
    def __init__(self, root):
        self.root = root
        self.semaphore = asyncio.Semaphore(1)
        self.locks = {}
        self.jobs = {}

    async def render(self, storyboard, path):
        from motion_renderer import render_motion
        return await render_motion(storyboard, path)

    def artifact(self, app, iid, create=False):
        PlanStore(app.db).get(iid)
        base = Path(app.db.path).absolute().parent.resolve()
        parts = ('content-plan', f'workspace-{app.db.workspace_id}', f'item-{iid}')
        directory = base
        for part in parts:
            directory = directory / part
            if directory.is_symlink(): raise UserError('Небезопасный путь видео')
            if create and not directory.exists(): directory.mkdir(mode=0o700)
            if directory.exists() and not directory.is_dir(): raise UserError('Небезопасный путь видео')
            if create: os.chmod(directory,0o700)
        path = directory / 'motion.mp4'
        if path.is_symlink() or (path.exists() and not path.is_file()): raise UserError('Небезопасный путь видео')
        return path

    def video(self, app, iid):
        item = PlanStore(app.db).get(iid)
        if not item['video_meta']: return None
        try: path = self.artifact(app,iid)
        except UserError: return None
        return path if path.is_file() else None

    def cost(self, app, item):
        if not item['operation_id']: return None
        row = app.db._raw.execute('SELECT report FROM llm_operations WHERE workspace_id=? AND id=?', (app.db.workspace_id,item['operation_id'])).fetchone()
        if not row: return None
        report = json.loads(row['report'])
        return {key:report.get(key) for key in ('id','status','calls','cost_usd','known_cost_usd','cost_complete','tokens_complete','input_tokens','output_tokens','provenance')}

    async def tick(self):
        now = int(time.time())
        for workspace in self.root.db.workspaces():
            app = self.root.workspace(workspace['id'])
            if app.db.get('plan_paused') == '1': continue
            store = PlanStore(app.db)
            # Mark missed slots in one scoped write, not via a paginated UI list.
            with store.conn:
                store.conn.execute("UPDATE content_plan SET status='missed' WHERE workspace_id=? AND status IN ('scheduled','ready') AND scheduled_at<?", (store.wid, now-900))
            rows = store.conn.execute("SELECT id,status FROM content_plan WHERE workspace_id=? AND status IN ('scheduled','ready') AND scheduled_at BETWEEN ? AND ? ORDER BY scheduled_at,id LIMIT 16", (store.wid,now-900,now)).fetchall()
            for row in rows:
                if len(self.jobs) >= 16: break
                iid = row['id']
                if iid in self.jobs: continue
                if row['status'] == 'scheduled': self.start_job(app,iid,automatic=True)
                else:
                    task = self.root.spawn(self.auto_dispatch(app,iid))
                    self.jobs[iid] = task
                    task.add_done_callback(lambda done, key=iid: self.jobs.pop(key,None))

    async def auto_dispatch(self, app, iid):
        store = PlanStore(app.db)
        async with self.semaphore:
            item = store.get(iid)
            if item['status'] != 'ready' or not item['proposal_id']: return
            async with self.root._publish_locks.setdefault(item['proposal_id'],asyncio.Lock()):
                item = store.get(iid)
                if item['status'] != 'ready' or app.db.get('plan_paused') == '1': return
                now = time.time()
                if item['scheduled_at'] > now: return
                if item['scheduled_at'] < now-900:
                    store.update(iid,status='missed')
                    return
                try: await self.dispatch_locked(app,item['proposal_id'])
                except UserError:
                    # Preflight failures must stop repeated scheduler attempts.
                    if store.get(iid)['status'] == 'ready':
                        store.update(iid,status='error',error='Публикация не началась; проверьте подключение и видеоролик.')
                    app.event('error',f'content_plan #{iid}: автоматическая публикация не завершена')

    def start_job(self, app, iid, retry=False, automatic=False):
        store = PlanStore(app.db)
        item = store.get(iid)
        allowed = ('error','interrupted','missed') if retry else ('scheduled',)
        if item['status'] not in allowed or iid in self.jobs:
            raise UserError('Задание уже готовится или обработано')
        if len(self.jobs) >= 16: raise UserError('Очередь подготовки заполнена; попробуйте позже')
        store.update(iid,status='rendering' if item['payload'] else 'generating', error=None)
        task = self.root.spawn(self.prepare(app, iid, automatic))
        self.jobs[iid] = task
        task.add_done_callback(lambda done: self.jobs.pop(iid, None))
        return self.public(app,store.get(iid))

    async def prepare(self, app, iid, automatic=False):
        store = PlanStore(app.db)
        async with self.locks.setdefault(iid,asyncio.Lock()), self.semaphore:
            cycle = token = None
            try:
                item = store.get(iid)
                if item['status'] == 'cancelled': return
                if automatic and app.db.get('plan_paused') == '1':
                    store.update(iid,status='scheduled')
                    return
                if automatic and item['scheduled_at'] < time.time()-900:
                    store.update(iid,status='missed')
                    return
                payload = item['payload']
                if payload is None:
                    from plan_llm import generate
                    cycle = CycleReport(app.db, not automatic, 'content_plan')
                    token = CURRENT_CYCLE.set(cycle)
                    store.update(iid,operation_id=cycle.id)
                    async with asyncio.timeout(240):
                        payload = await generate(app.db,item)
                    cycle.finish('completed')
                    if store.get(iid)['status'] == 'cancelled': return
                    item = store.cache_payload(iid,payload)
                if item['with_video'] and self.video(app,iid) is None:
                    store.update(iid,status='rendering')
                    path = self.artifact(app,iid,create=True)
                    meta = await asyncio.wait_for(self.render({'scenes':payload['scenes']},path),300)
                    path = self.artifact(app,iid)  # revalidate after the renderer boundary
                    if not path.is_file(): raise UserError('Видеофайл не создан')
                    os.chmod(path,0o600)
                    if store.get(iid)['status'] == 'cancelled': return
                    meta = {k:meta[k] for k in ('duration','width','height','fps','bytes') if k in meta}
                    store.update(iid,video_meta=meta)
                if store.get(iid)['status'] == 'cancelled': return
                store.update(iid,status='ready',error=None)
            except BaseException as error:
                if cycle and cycle.report['status'] == 'running':
                    cycle.finish('cancelled' if isinstance(error,asyncio.CancelledError) else 'failed')
                if store.get(iid)['status'] != 'cancelled':
                    store.update(iid,status='interrupted' if isinstance(error,asyncio.CancelledError) else 'error',
                                 error='Подготовка не завершена; проверьте настройки и повторите вручную.')
                app.event('error',f'content_plan #{iid}: подготовка не завершена')
                if isinstance(error,asyncio.CancelledError): raise
            finally:
                if token is not None: CURRENT_CYCLE.reset(token)
        # Finish the due publication in this bounded job, independent of a
        # potentially slow ordinary-news cycle delaying the next scheduler tick.
        if automatic and store.get(iid)['status'] == 'ready':
            await self.auto_dispatch(app,iid)

    async def publish(self, app, iid, confirmation):
        item = PlanStore(app.db).get(iid)
        if confirmation != 'ОПУБЛИКОВАТЬ': raise UserError('Подтвердите раннюю публикацию: ОПУБЛИКОВАТЬ')
        if item['status'] != 'ready' or not item['proposal_id']: raise UserError('Публикуется только готовое задание')
        await app.publish(item['proposal_id'])
        return self.public(app,PlanStore(app.db).get(iid))

    async def dispatch_locked(self, app, pid):
        # Caller holds the existing root per-proposal publish/decision lock.
        store = PlanStore(app.db)
        item = store.for_proposal(pid)
        iid = item['id']
        if item['status'] != 'ready': raise UserError('Задание не готово или уже обработано')
        if not app.bot: raise UserError('Бот не подключён')
        proposal = app.db.get_proposal(pid)
        if not proposal or proposal['status'] != 'new': raise UserError('Предложение уже обработано')
        if not item['target_channel']: raise UserError('Не задан канал публикации')
        path = self.video(app,iid) if item['with_video'] else None
        if item['with_video'] and path is None:
            store.update(iid,status='error',error='Готовый видеоролик отсутствует; повторите подготовку вручную, текст сохранён.')
            raise UserError('Готовый видеоролик отсутствует; повторите подготовку вручную')
        text = app.channel_html(proposal)
        target = to_peer(item['target_channel'])
        full_caption = utf16_len(html.unescape(re.sub(r'<[^>]+>','',text))) <= 1024
        progress = {}
        store.update(iid,status='publishing',dispatch=progress)
        try:
            if path is not None:
                caption = text if full_caption else html.escape(proposal['title'] or item['topic'])
                progress['phase'] = 'video_dispatching'
                store.update(iid,dispatch=progress)
                meta = item['video_meta']
                async with asyncio.timeout(240):
                    message = await app.bot.send_file(target,str(path),caption=caption,parse_mode='html',supports_streaming=True,
                        attributes=[DocumentAttributeVideo(duration=int(meta['duration']),w=int(meta['width']),h=int(meta['height']),supports_streaming=True)])
                if type(message.id) is not int or message.id < 1: raise UserError('Отправка видео не подтверждена')
                progress.update(video_id=message.id,phase='video_confirmed')
                store.update(iid,dispatch=progress)
            if path is None or not full_caption:
                progress['phase'] = 'text_dispatching'
                store.update(iid,dispatch=progress)
                kwargs = {'parse_mode':'html','link_preview':False}
                if path is not None: kwargs['reply_to'] = progress['video_id']
                async with asyncio.timeout(120): message = await app.bot.send_message(target,text,**kwargs)
                if type(message.id) is not int or message.id < 1: raise UserError('Отправка текста не подтверждена')
                progress.update(text_id=message.id,phase='text_confirmed')
                store.update(iid,dispatch=progress)
            progress['phase'] = 'completed'
            store.published(iid,progress)
        except BaseException as error:
            store.update(iid,status='uncertain',dispatch=progress,error='Отправка неоднозначна или частично подтверждена. Проверьте канал; автоматический повтор запрещён.')
            app.event('error',f'content_plan #{iid}: отправка неоднозначна')
            if isinstance(error,asyncio.CancelledError): raise
            raise UserError('Отправка неоднозначна. Проверьте канал; автоматический повтор запрещён.') from None
        app.event('info',f'content_plan #{iid}: опубликовано')
        await app.sync_tg(pid)

    def public(self, app, item):
        payload = item['payload'] or {}
        proposal = app.db.get_proposal(item['proposal_id']) if item['proposal_id'] else None
        return {key: item[key] for key in ('id', 'topic', 'brief', 'scheduled_at', 'status', 'target_channel', 'proposal_id', 'with_video', 'video_meta', 'error')} | {
            'scheduled_local': datetime.fromtimestamp(item['scheduled_at'], app.tz).strftime('%Y-%m-%dT%H:%M'),
            'title': proposal['title'] if proposal else payload.get('title', ''),
            'text': proposal['text'] if proposal else payload.get('text', ''),
            'rendered_html': app.post_body(proposal).html() if proposal else '',
            'has_video': self.video(app,item['id']) is not None, 'cost': self.cost(app,item),
        }

    def listing(self, app):
        items, total = PlanStore(app.db).listing()
        return {'paused': app.db.get('plan_paused', '1') == '1', 'timezone': str(app.tz),
                'now_local': datetime.now(app.tz).strftime('%Y-%m-%dT%H:%M'),
                'items': [self.public(app, item) for item in items], 'total': total,
                'limit': 200, 'has_more': total > len(items)}

    def validate(self, app, data, target=None):
        topic, brief = data.get('topic'), data.get('brief', '')
        if not isinstance(topic, str) or not 1 <= len(topic.strip()) <= 300:
            raise UserError('Тема: от 1 до 300 символов')
        if not isinstance(brief, str) or len(brief) > 4000:
            raise UserError('Примечание: не больше 4000 символов')
        with_video = data.get('with_video', True)
        if type(with_video) is not bool: raise UserError('with_video — логическое значение')
        value = data.get('scheduled_local')
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}', value):
            raise UserError('Дата: YYYY-MM-DDTHH:MM')
        try:
            local = datetime.strptime(value, '%Y-%m-%dT%H:%M')
            candidates = {int(local.replace(tzinfo=app.tz, fold=fold).timestamp()) for fold in (0,1)
                          if datetime.fromtimestamp(local.replace(tzinfo=app.tz, fold=fold).timestamp(), app.tz).replace(tzinfo=None) == local}
        except (ValueError, OverflowError, OSError):
            raise UserError('Неверная дата') from None
        if len(candidates) != 1: raise UserError('Время отсутствует или неоднозначно при смене часового пояса')
        scheduled = candidates.pop()
        if scheduled <= time.time(): raise UserError('Выберите время в будущем')
        target = target if target is not None else app.db.get('target_channel')
        if not target: raise UserError('Сначала укажите канал публикации')
        return {'topic': topic.strip(), 'brief': brief.strip(), 'scheduled_at': scheduled,
                'target_channel': target, 'with_video': with_video}

    def create(self, app, data):
        return self.public(app, PlanStore(app.db).insert(self.validate(app, data)))

    def bulk(self, app, data):
        topics = data.get('topics')
        if isinstance(topics, str): topics = topics.splitlines()
        if not isinstance(topics, list) or not 1 <= len(topics) <= 30:
            raise UserError('Укажите от 1 до 30 тем, по одной на строку')
        day, clock = data.get('start_date'), data.get('time')
        if not isinstance(day,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day) or not isinstance(clock,str) or not re.fullmatch(r'\d{2}:\d{2}',clock):
            raise UserError('Дата YYYY-MM-DD и время HH:MM')
        try: start = datetime.strptime(day, '%Y-%m-%d')
        except ValueError: raise UserError('Неверная дата') from None
        entries = []
        for offset, topic in enumerate(topics):
            try: local = (start + timedelta(days=offset)).strftime('%Y-%m-%d') + 'T' + clock
            except OverflowError: raise UserError('Неверная дата') from None
            entries.append(self.validate(app, {'topic':topic, 'scheduled_local':local, 'with_video':data.get('with_video',True)}))
        return [self.public(app,item) for item in PlanStore(app.db).insert_many(entries)]

    def edit(self, app, iid, data):
        store = PlanStore(app.db)
        item = store.get(iid)
        if item['status'] != 'scheduled': raise UserError('Редактируется только ещё не подготовленное задание')
        old = self.public(app, item)
        allowed = {'topic','brief','scheduled_local','with_video'}
        if not data.keys() <= allowed: raise UserError('Неизвестное поле задания')
        values = self.validate(app, {**old, **data}, item['target_channel'])
        values.pop('target_channel')
        return self.public(app, store.update(iid, **values))

    def cancel_locked(self, app, iid):
        store = PlanStore(app.db)
        item = store.get(iid)
        if item['status'] in ('publishing','published','uncertain'):
            raise UserError('Нельзя отменить подтверждённую или неоднозначную публикацию')
        with store.conn:
            store.conn.execute("UPDATE content_plan SET status='cancelled' WHERE workspace_id=? AND id=?", (store.wid,iid))
            if item['proposal_id']:
                store.conn.execute("UPDATE proposals SET status='skipped' WHERE workspace_id=? AND id=?", (store.wid,item['proposal_id']))
        return self.public(app, store.get(iid))

    async def cancel(self, app, iid):
        item = PlanStore(app.db).get(iid)
        if not item['proposal_id']: return self.cancel_locked(app,iid)
        async with self.root._publish_locks.setdefault(item['proposal_id'],asyncio.Lock()):
            result = self.cancel_locked(app,iid)
            await app.sync_tg(item['proposal_id'])
            return result

    def preview_fields(self, app, proposal):
        if proposal['kind'] != 'planned': return {}
        item = PlanStore(app.db).for_proposal(proposal['id'])
        return {'plan_item_id':item['id'], 'plan_video_url': self.video_url(app,item) if item['video_meta'] else None}

    def video_url(self, app, item):
        return f"/api/content-plan/{item['id']}/video?workspace_id={app.db.workspace_id}"

    def pause(self, app, data):
        if type(data.get('paused')) is not bool: raise UserError('paused — логическое значение')
        app.db.set('plan_paused', '1' if data['paused'] else '0')
        return {'paused':data['paused'], 'semantics':'Текущая подготовка может завершиться; новые автоматические задания не запускаются.'}
