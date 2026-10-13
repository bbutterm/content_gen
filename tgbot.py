"""Обработчики Telegram-бота: привязка админа, команды, кнопки, правка ответом."""
import hmac
import re

from telethon import Button, events, utils
from telethon.tl.types import PeerChannel

import config
from settings import UserError

MARK_RE = re.compile(r"Предложение #(\d+)")

HELP = """<b>Новостной бот</b>

Я собираю посты из каналов-источников и присылаю сюда готовые новости. Под каждой есть кнопки: опубликовать, переписать или пропустить.

✏️ Чтобы поправить текст вручную, ответь (reply) на новость своим вариантом.
📥 Перешли мне пост из любого канала, и я предложу сделать этот канал источником или каналом для публикаций.

Команды:
/run — собрать новости прямо сейчас
/status — состояние
/pause, /resume — остановить или включить расписание
/panel — ссылка на панель управления

Каналы, промпт, характер модели и расписание настраиваются в панели. Там же, во вкладке «Модули», включаются картинки, очередь публикаций, обучение на правках и срочные новости."""


def register(app, client):
    def is_admin(e) -> bool:
        return e.is_private and e.sender_id in app.admin_ids()

    def cmd(name):
        return events.NewMessage(pattern=rf"(?i)^/{name}(?:@\w+)?(?:\s+([\s\S]*))?$", func=is_admin)

    @client.on(events.NewMessage(pattern=r"(?i)^/start(?:@\w+)?(?:\s+(\S+))?\s*$",
                                 func=lambda e: e.is_private))
    async def on_start(event):
        code = event.pattern_match.group(1) or ""
        if code and hmac.compare_digest(code, app.db.get("link_code")):
            app.add_admin(event.sender_id)
            sender = await event.get_sender()
            app.event("info", f"Привязан администратор {getattr(sender, 'first_name', '')} ({event.sender_id})")
            await event.reply("✅ Готово! Ты подключён как администратор. Сюда будут приходить новости "
                              "на модерацию.\n\n" + HELP, parse_mode="html")
            return
        if is_admin(event):
            await event.reply(HELP, parse_mode="html")
        else:
            await event.reply("Это закрытый бот. Чтобы подключиться, открой ссылку привязки "
                              "из панели управления (шаг «Привяжи свой Telegram»).")

    @client.on(cmd("help"))
    async def on_help(event):
        await event.reply(HELP, parse_mode="html")

    @client.on(cmd("panel"))
    async def on_panel(event):
        url = config.PANEL_URL or "адрес не указан (PANEL_URL в .env)"
        await event.reply(f"Панель управления: {url}", link_preview=False)

    @client.on(cmd("status"))
    async def on_status(event):
        st = app.state()
        s = st["stats"]
        nxt = st["next_run_reason"] or "по расписанию"
        await event.reply(
            f"Читатель: {st['user']['status']}\nКаналов: {st['channels_count']}\n"
            f"Интервал: {st['settings']['interval_min']} мин ({nxt})\n"
            f"Ждут модерации: {s['new']}, опубликовано за сутки: {s['published_24h']}\n"
            f"Необработанных постов: {s['pending_posts']}")

    @client.on(cmd("pause"))
    async def on_pause(event):
        app.db.set("paused", "1")
        await event.reply("⏸ Расписание на паузе. /resume — включить.")

    @client.on(cmd("resume"))
    async def on_resume(event):
        app.db.set("paused", "0")
        await event.reply("▶️ Расписание включено.")

    @client.on(cmd("run"))
    async def on_run(event):
        await event.reply("⏳ Собираю и готовлю новости…")
        try:
            r = await app.run_cycle(manual=True)
        except Exception as e:
            app.event("error", f"Ошибка сбора: {e}")
            await event.reply(f"⚠️ {e}")
            return
        if not r["posts"]:
            await event.reply("Новых постов в каналах нет.")
        elif not r["proposals"]:
            await event.reply(f"Обработал {r['posts']} постов — достойных новостей не нашлось.")
        else:
            await event.reply(f"Готово: обработано постов {r['posts']}, предложено новостей {r['proposals']}.")

    # ----- пересланный пост из канала -----
    @client.on(events.NewMessage(func=lambda e: is_admin(e) and e.fwd_from is not None))
    async def on_forward(event):
        peer = event.fwd_from.from_id
        if not isinstance(peer, PeerChannel):
            await event.reply("Не вижу, из какого канала этот пост. Перешли пост именно из канала "
                              "(если в канале запрещена пересылка, так не получится).")
            return
        peer_id = utils.get_peer_id(peer)
        await event.reply(
            f"Канал с id <code>{peer_id}</code>. Что с ним сделать?",
            parse_mode="html",
            buttons=[[Button.inline("📢 Публиковать сюда", f"tgt:{peer_id}".encode())],
                     [Button.inline("📥 Сделать источником", f"src:{peer_id}".encode())]])

    # ----- ручная правка ответом на предложение -----
    @client.on(events.NewMessage(func=lambda e: is_admin(e) and e.is_reply and e.fwd_from is None
                                 and not (e.raw_text or "").startswith("/")))
    async def on_reply_edit(event):
        replied = await event.get_reply_message()
        if not replied or replied.sender_id != app.bot_me.id:
            return
        m = MARK_RE.search(replied.raw_text or "")
        if not m:
            return
        pid = int(m.group(1))
        try:
            await app.workspace_for_proposal(pid).set_text(pid, event.raw_text or "")
        except UserError as e:
            await event.reply(str(e))
            return
        await event.reply(f"✏️ Текст предложения #{pid} обновлён — проверь сообщение выше.")

    # ----- кнопки -----
    @client.on(events.CallbackQuery(func=lambda e: e.sender_id in app.admin_ids()))
    async def on_button(event):
        action, _, arg = event.data.decode().partition(":")
        answered = False
        try:
            space = app
            if action in ('pub','skip','rw','m'):
                pid_text = arg.rsplit(':',1)[-1] if action == 'm' else arg
                if not re.fullmatch(r'[1-9][0-9]{0,17}',pid_text):
                    raise UserError('Неверная кнопка')
                space = app.workspace_for_proposal(int(pid_text))
            if action == "m":  # кнопка модуля: m:<модуль>:<действие>:<id>
                module_id, mod_action, pid = arg.split(":", 2)
                if mod_action in ("now",):  # долгие действия — сначала ответим
                    await event.answer("Публикую…")
                    answered = True
                msg = await space.module_action(module_id, mod_action, int(pid))
                if not answered:
                    await event.answer(msg[:190])
            elif action == "pub":
                msg = await space.approve(int(arg))
                await event.answer(msg[:190])
            elif action == "skip":
                await space.skip(int(arg))
                await event.answer("Пропущено")
            elif action == "rw":
                await event.answer("Переписываю…")
                answered = True
                await space.rewrite(int(arg))
            elif action == "tgt":
                await app.save_settings({"target_channel": arg})
                try:
                    title = await app.check_target()
                    await event.edit(f"📢 Публикую в «{title}». Права проверены ✅")
                except UserError as e:
                    await event.edit(f"📢 Канал {arg} сохранён для публикаций, но: {e}")
            elif action == "src":
                result = await app.add_channels(arg)
                text = "\n".join(["✅ " + x for x in result["ok"]] + ["⚠️ " + x for x in result["bad"]])
                await event.edit(text or "Ничего не добавлено")
        except Exception as e:
            msg = str(e)[:190]
            if answered:
                await event.respond(f"⚠️ {msg}")
            else:
                await event.answer(msg, alert=True)
