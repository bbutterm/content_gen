"""Модуль «Картинки»: публикует новость с фото из поста-источника."""
import html
import io
import os
import re
import time

from settings import UserError, to_peer

from .base import Field, Module

CAPTION_LIMIT = 1024


def plain_len(html_text: str) -> int:
    """Длина подписи так, как её считает Telegram (без HTML-тегов)."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", html_text)).encode('utf-16-le')) // 2


class MediaModule(Module):
    id = "media"
    name = "Картинки"
    description = ("Если у поста-источника есть фото, новость публикуется вместе с ним. "
                   "Под предложением появляется кнопка «Без фото» / «С фото».")
    fields = [
        Field("fit", "Укладывать текст в подпись к фото", "bool", "1",
              help="Подпись к фото в Telegram — до 1024 символов. Нейросеть будет писать такие посты короче."),
        Field("long_mode", "Если текст всё равно длиннее 1024 символов", "select", "text_only",
              options=[["text_only", "Публиковать без фото"],
                       ["photo_then_text", "Сначала фото, следом текст отдельным сообщением"]]),
    ]

    @property
    def cache_path(self) -> str:
        return os.path.join(os.path.dirname(self.db.path), "media", f"workspace-{self.db.workspace_id}")

    @property
    def media_dir(self) -> str:
        path = self.cache_path
        os.makedirs(path, mode=0o700, exist_ok=True)
        return path

    def candidates(self, p) -> list[tuple]:
        return [(r["channel"], r["msg_id"]) for r in self.db.posts_by_keys(p["sources"])
                if r["media"] == "photo"]

    # ---------- хуки ----------
    async def on_proposal_created(self, pid, posts):
        photo = next(((r["channel"], r["msg_id"]) for r in posts if r["media"] == "photo"), None)
        if photo:
            self.db.set_proposal_media(pid, photo)

    def prompt_extra(self, kind, p=None):
        if self.get("fit") != "1":
            return ""
        if kind == "propose":
            return ("Посты-источники с пометкой 📷 содержат фото — его опубликуют вместе с новостью. "
                    "Если в посте используешь такой источник, уложи текст в 850 символов, "
                    "чтобы он поместился в подпись к фото.")
        if kind == "rewrite" and p and p["media"]:
            return "Пост публикуется с фото: уложи текст в 850 символов."
        return ""

    def preview_lines(self, p):
        return ["🖼 Будет с фото из источника"] if p["media"] else []

    def buttons(self, p):
        if p["status"] not in ("new", "queued"):
            return []
        if p["media"]:
            return [[("🖼 Без фото", "off")]]
        if self.candidates(p):
            return [[("🖼 С фото", "on")]]
        return []

    async def on_action(self, action, pid=None, data=None):
        p = self.db.get_proposal(pid) if pid else None
        if not p or p["status"] not in ("new", "queued"):
            raise UserError("Предложение уже обработано")
        if action == "off":
            self.db.set_proposal_media(pid, None)
            return "Фото убрано"
        if action == "on":
            found = self.candidates(p)
            if not found:
                raise UserError("У источников нет фото")
            self.db.set_proposal_media(pid, found[0])
            return "Фото добавлено"
        raise UserError("Неизвестное действие")

    async def fetch(self, p):
        """Скачивает фото через аккаунт-читатель (с кэшем на диске)."""
        if not p["media"]:
            return None
        channel, msg_id = p["media"]
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(channel))
        path = os.path.join(self.media_dir, f"{safe}_{msg_id}.jpg")
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
        if not self.app.user:
            return None
        msg = await self.app.user.get_messages(to_peer(channel), ids=msg_id)
        if not msg or not getattr(msg, "photo", None):
            return None
        data = await self.app.user.download_media(msg, file=bytes)
        if data:
            with open(path, "wb") as f:
                f.write(data)
        return data

    async def send_post(self, target, html_text, p):
        if not p["media"]:
            return False
        data = await self.fetch(p)
        if not data:
            return False
        photo = io.BytesIO(data)
        photo.name = "photo.jpg"
        bot = self.app.bot
        if plain_len(html_text) <= CAPTION_LIMIT:
            await bot.send_file(target, photo, caption=html_text, parse_mode="html")
            return True
        if self.get("long_mode") == "photo_then_text":
            await bot.send_file(target, photo)
            await bot.send_message(target, html_text, parse_mode="html", link_preview=False)
            return True
        return False  # опубликуется обычным текстом

    async def on_tick(self):
        # раз в сутки чистим кэш картинок старше 14 дней
        if time.time() - self.geti("last_cleanup") < 86400:
            return
        self.set("last_cleanup", int(time.time()))
        border = time.time() - 14 * 86400
        for path in self.cache_files():
            try:
                if os.path.getmtime(path) < border:
                    os.remove(path)
            except FileNotFoundError:
                pass

    def cache_files(self):
        try:
            with os.scandir(self.cache_path) as entries:
                return [entry.path for entry in entries if entry.is_file(follow_symlinks=False)]
        except FileNotFoundError:
            return []

    def panel_info(self):
        return {"cached": len(self.cache_files())}
