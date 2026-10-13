"""Сбор постов из каналов через аккаунт-читатель (Telethon)."""
import logging
import re
import time

import config
from settings import to_peer

log = logging.getLogger("collector")


def stop_words(db):
    return [w.strip().lower() for w in re.split(r"[,\n]+", db.get("stop_words")) if w.strip()]


async def collect(client, db) -> int:
    """Забирает новые посты из включённых каналов. Возвращает число добавленных."""
    total, now = 0, time.time()
    stops = stop_words(db)
    max_chars = db.geti("max_chars")

    for row in db.channels(enabled_only=True):
        key, last_id = row["username"], row["last_id"]
        try:
            entity = await client.get_entity(to_peer(key))
            username = getattr(entity, "username", None)
            max_id, added = last_id, 0
            if last_id:
                messages = client.iter_messages(entity, min_id=last_id)
            else:
                messages = client.iter_messages(entity, limit=config.FIRST_FETCH_LIMIT)

            async for msg in messages:
                max_id = max(max_id, msg.id)
                if not last_id and msg.date.timestamp() < now - config.FIRST_FETCH_HOURS * 3600:
                    continue
                text = (msg.message or "").strip()
                if len(text) < config.MIN_POST_LEN:
                    continue
                low = text.lower()
                if stops and any(w in low for w in stops):
                    continue
                link = (f"https://t.me/{username}/{msg.id}" if username
                        else f"https://t.me/c/{entity.id}/{msg.id}")
                media = "photo" if getattr(msg, "photo", None) else None
                if db.add_post(key, msg.id, int(msg.date.timestamp()), text[:max_chars], link, media):
                    added += 1
        except Exception as e:  # канал удалён, приватный, опечатка, лимиты
            log.warning("Канал %s: %s", key, e)
            db.channel_error(key, f"{type(e).__name__}: {e}")
            continue

        db.channel_fetched(key, max_id, getattr(entity, "title", None), added)
        total += added
        log.info("%s: +%d постов", key, added)
    return total
