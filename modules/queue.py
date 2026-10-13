"""Модуль «Очередь публикаций»: «Опубликовать» ставит пост в очередь, бот выпускает его по слотам."""
from datetime import datetime, timedelta

from settings import UserError

from .base import Field, Module, parse_times

WINDOW_MIN = 15  # если сервис был выключен, слот ещё «ловится» в течение 15 минут


class QueueModule(Module):
    id = "queue"
    name = "Очередь публикаций"
    description = ("Кнопка «Опубликовать» превращается в «В очередь», а бот сам выпускает посты "
                   "в заданное время. Срочно опубликовать можно кнопкой «⚡ Сразу».")
    fields = [
        Field("slots", "Время выхода постов", "times", "09:00, 13:00, 19:00",
              help="Через запятую, например: 09:00, 12:30, 18:00"),
        Field("per_slot", "Постов за один слот", "int", "1", min=1, max=5),
        Field("notify", "Сообщать в Telegram, когда пост вышел из очереди", "bool", "1"),
    ]

    def validate(self, changes):
        if "slots" in changes and not changes["slots"]:
            raise UserError("Укажи хотя бы одно время выхода")

    # ---------- расчёт времени ----------
    def slots(self):
        try:
            return parse_times(self.get("slots"))
        except UserError:
            return []

    def next_slots(self, count: int) -> list[datetime]:
        slots, now, result, day = self.slots(), datetime.now(self.app.tz), [], 0
        if not slots:
            return []
        while len(result) < count and day < 400:
            base = (now + timedelta(days=day)).replace(second=0, microsecond=0)
            for h, m in slots:
                t = base.replace(hour=h, minute=m)
                if t > now:
                    result.append(t)
            day += 1
        return result[:count]

    def eta(self, position: int):
        idx = (position - 1) // max(1, self.geti("per_slot"))
        nxt = self.next_slots(idx + 1)
        return nxt[idx] if len(nxt) > idx else None

    def position(self, pid: int):
        ids = [p["id"] for p in self.db.proposals("queued", limit=500)]
        return ids.index(pid) + 1 if pid in ids else None

    @staticmethod
    def fmt(dt) -> str:
        if not dt:
            return "—"
        today = datetime.now(dt.tzinfo).date()
        day = "сегодня" if dt.date() == today else ("завтра" if dt.date() == today + timedelta(days=1)
                                                    else dt.strftime("%d.%m"))
        return f"{day} в {dt:%H:%M}"

    # ---------- хуки ----------
    def approve_label(self, p):
        return "📥 В очередь" if p["status"] == "new" else None

    async def intercept_approve(self, p):
        if p["status"] == "queued":
            return "Уже в очереди"
        if not self.slots():
            return None
        self.db.set_proposal_status(p["id"], "queued")
        pos = self.position(p["id"])
        when = self.fmt(self.eta(pos))
        self.app.event("info", f"#{p['id']} в очереди №{pos}, выйдет {when}")
        await self.app.sync_tg(p["id"])
        return f"В очереди №{pos}, выйдет {when}"

    def preview_lines(self, p):
        if p["status"] != "queued":
            return []
        pos = self.position(p["id"])
        return [f"📥 В очереди №{pos} · выйдет {self.fmt(self.eta(pos))}"]

    def buttons(self, p):
        if p["status"] == "new":
            return [[("⚡ Опубликовать сразу", "now")]]
        if p["status"] == "queued":
            return [[("⚡ Сразу", "now"), ("⬆️ Раньше", "up"), ("↩️ Из очереди", "back")]]
        return []

    async def on_action(self, action, pid=None, data=None):
        p = self.db.get_proposal(pid) if pid else None
        if not p or p["status"] not in ("new", "queued"):
            raise UserError("Предложение уже обработано")
        if action == "now":
            await self.app.publish(pid)
            return "Опубликовано ✅"
        if action == "back":
            self.db.set_proposal_status(pid, "new")
            return "Убрано из очереди"
        if action == "up":
            queue = self.db.proposals("queued", limit=500)
            ids = [q["id"] for q in queue]
            if pid not in ids:
                raise UserError("Не в очереди")
            i = ids.index(pid)
            if i == 0:
                return "Уже первый"
            self.db.swap_queue(pid, ids[i - 1])
            for other in (ids[i - 1],):
                await self.app.sync_tg(other)
            return f"Теперь №{i}"
        raise UserError("Неизвестное действие")

    async def on_tick(self):
        now = datetime.now(self.app.tz)
        for h, m in self.slots():
            slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if not slot <= now < slot + timedelta(minutes=WINDOW_MIN):
                continue
            key = slot.strftime("%Y-%m-%d %H:%M")
            if self.get("last_fired") == key:
                continue
            self.set("last_fired", key)
            await self.fire()

    async def fire(self):
        queue = self.db.proposals("queued", limit=self.geti("per_slot"))
        for p in queue:
            try:
                await self.app.publish(p["id"])
            except Exception as e:
                self.app.event("error", f"Очередь: не удалось опубликовать #{p['id']}: {e}")
                continue
            if self.get("notify") == "1":
                await self.app.notify_admins(f"📤 Вышел пост из очереди #{p['id']}: "
                                             f"{p['title'] or p['text'][:60]}")
        # у оставшихся сдвинулись номера и время выхода
        for p in self.db.proposals("queued", limit=20):
            await self.app.sync_tg(p["id"])

    def panel_info(self):
        queue = self.db.proposals("queued", limit=100)
        nxt = self.next_slots(1)
        return {
            "next_slot": int(nxt[0].timestamp()) if nxt else None,
            "items": [{"id": p["id"], "title": p["title"] or p["text"].split("\n", 1)[0][:80],
                       "eta": self.fmt(self.eta(i))} for i, p in enumerate(queue, 1)],
        }
