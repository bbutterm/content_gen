"""Модуль «Срочные новости»: если одно событие быстро появилось в нескольких каналах — сразу присылает его."""
import re
import time

from collector import collect
from llm import LLM
from settings import UserError

from .base import Field, Module

WORD_RE = re.compile(r"[a-zа-яё0-9]+", re.I)
STOP = set("""
это этот эта эти этого этой того тому тоже также если когда чтобы потому поэтому более менее очень
будет будут было были быть может могут можно нужно после перед через около между только уже ещё
всех всего всем свой свои своих который которые которых которая которого даже меня тебя него нему
ними наших ваших нашей сегодня вчера завтра заявил заявила заявили сообщил сообщила сообщили
сообщает сообщают новости новость канал каналу подписаться подпишись источник время года году
против также однако теперь сейчас здесь там where which that with from have this будет пока
""".split())
SENSITIVITY = {"low": 0.38, "medium": 0.28, "high": 0.2}
MIN_COMMON = 4  # минимум общих слов у двух постов


def tokens(text: str) -> set:
    out = set()
    for w in WORD_RE.findall(text.lower()):
        if w.isdigit():
            if len(w) >= 2:
                out.add(w)
        elif len(w) >= 4 and w not in STOP:
            out.add(w[:6])  # грубая «основа» слова: учитывает падежи
    return out


def find_clusters(posts, threshold: float) -> list[list[int]]:
    """Группирует посты разных каналов про одно событие (по общим словам)."""
    toks = [tokens(p["text"]) for p in posts]
    parent = list(range(len(posts)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(posts)):
        for j in range(i + 1, len(posts)):
            if posts[i]["channel"] == posts[j]["channel"]:
                continue
            common = len(toks[i] & toks[j])
            if common < MIN_COMMON:
                continue
            if common / max(1, min(len(toks[i]), len(toks[j]))) >= threshold:
                parent[root(i)] = root(j)
    groups = {}
    for i in range(len(posts)):
        groups.setdefault(root(i), []).append(i)
    return list(groups.values())


class BreakingModule(Module):
    id = "breaking"
    name = "Срочные новости"
    description = ("Каждые несколько минут проверяет каналы. Если одно событие за короткое время "
                   "появилось в нескольких источниках — сразу присылает новость с пометкой 🔥, "
                   "не дожидаясь расписания.")
    fields = [
        Field("check_every", "Проверять каждые, минут", "int", "10", min=3, max=120),
        Field("window", "Окно, минут", "int", "40", min=10, max=360,
              help="Посты за этот период сравниваются между собой"),
        Field("min_sources", "Минимум каналов об одном событии", "int", "3", min=2, max=10),
        Field("sensitivity", "Чувствительность", "select", "medium",
              options=[["low", "Низкая — только явные совпадения"], ["medium", "Средняя"],
                       ["high", "Высокая — больше срочных, возможны ложные"]]),
        Field("auto_publish", "Публиковать срочные сразу, без модерации", "bool", "0",
              help="Срочная новость минует очередь и модерацию"),
        Field("at_night", "Работать в тихие часы", "bool", "0"),
    ]

    async def on_tick(self):
        app = self.app
        now = int(time.time())
        if now - self.geti("last_check") < self.geti("check_every") * 60:
            return
        if (app.lock.locked() or not app.user or self.db.get("paused") == "1"
                or not self.db.get("llm_api_key") or not self.db.get("llm_model")):
            return
        if app.in_quiet_hours() and self.get("at_night") != "1":
            return
        self.set("last_check", now)
        async with app.lock:
            await collect(app.user, self.db)
            found = self.detect(now)
            for group in found[:2]:  # не больше двух срочных за проверку
                await self.handle(group)

    def detect(self, now=None):
        now = now or int(time.time())
        posts = self.db.recent_pending(now - self.geti("window") * 60)
        need = self.geti("min_sources")
        if len({p["channel"] for p in posts}) < need:
            return []
        threshold = SENSITIVITY.get(self.get("sensitivity"), SENSITIVITY["medium"])
        groups = [[posts[i] for i in g] for g in find_clusters(posts, threshold)]
        groups = [g for g in groups if len({p["channel"] for p in g}) >= need]
        groups.sort(key=lambda g: len({p["channel"] for p in g}), reverse=True)
        return [sorted(g, key=lambda p: p["date"]) for g in groups]

    async def handle(self, posts):
        app = self.app
        keys = [(p["channel"], p["msg_id"]) for p in posts]
        extra = app.prompt_extra("propose")
        extra += ("\n\nЭто СРОЧНАЯ новость: о событии одновременно пишут несколько источников. "
                  "Напиши ровно один пост — по главному событию из этих постов.")
        items = await LLM(self.db).propose(posts, 1, self.db.recent_titles(30), extra=extra)
        self.db.mark_used(keys)
        if not items:
            return
        pid = await app.create_proposal(items[0], posts, kind="breaking", keys=keys)
        channels = len({p["channel"] for p in posts})
        app.event("info", f"🔥 Срочная новость #{pid} (источников: {channels}): {items[0]['title']}")
        self.set("last_found", int(time.time()))
        if self.get("auto_publish") == "1" and app.bot and app.target():
            try:
                await app.publish(pid)
                await app.notify_admins(f"🔥 Опубликована срочная новость #{pid}: {items[0]['title']}")
                return
            except Exception as e:
                app.event("error", f"Срочная #{pid} не опубликовалась: {e}")
        await app.send_proposal(pid)

    async def on_action(self, action, pid=None, data=None):
        if action == "check":
            self.set("last_check", 0)
            return "Проверю в ближайшие 30 секунд"
        raise UserError("Неизвестное действие")

    def panel_info(self):
        recent = self.db.proposals(kind="breaking", limit=5)
        return {
            "last_check": self.geti("last_check") or None,
            "items": [{"id": p["id"], "title": p["title"] or p["text"].split("\n", 1)[0][:80],
                       "created": p["created"], "status": p["status"]} for p in recent],
        }
