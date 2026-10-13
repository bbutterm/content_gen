"""Работа с LLM через OpenAI-совместимый API. Настройки берутся из БД при каждом вызове."""
import asyncio
import json
import logging
import re
import time
from urllib.parse import urlparse

import openai
from openai import AsyncOpenAI

from cycle_cost import active_cycle
import prompts
from news_outlook import assemble_outlook, without_outlook
from settings import PROVIDERS, UserError

log = logging.getLogger("llm")


def _ago(ts: int) -> str:
    minutes = max(0, int((time.time() - ts) // 60))
    return f"{minutes} мин назад" if minutes < 60 else f"{minutes // 60} ч назад"


def _has_photo(p) -> bool:
    try:
        return p["media"] == "photo"
    except (IndexError, KeyError):
        return False


def format_posts(posts) -> str:
    return "\n\n".join(f"[{i}] @{p['channel']} · {_ago(p['date'])}{' · 📷' if _has_photo(p) else ''}\n{p['text']}"
                       for i, p in enumerate(posts, 1))


def parse_json(raw: str) -> dict:
    m = re.search(r"\{[\s\S]*\}", raw or "")
    if not m:
        raise ValueError(f"Модель вернула не JSON: {(raw or '')[:200]!r}")
    return json.loads(m.group(0))


class LLM:
    def __init__(self, db):
        self.db = db
        provider = self.provider = db.get("llm_provider")
        self.input_rate = db.get("llm_input_usd_per_million")
        self.output_rate = db.get("llm_output_usd_per_million")
        base_url = db.get("llm_base_url").strip() or PROVIDERS.get(provider, {}).get("base_url") or None
        self.provider_cost_supported = (provider == "openrouter" and
            urlparse(base_url or "").hostname in ("openrouter.ai",))
        self.local_no_bill = (provider == 'custom' and db.get('llm_local_no_bill') == '1' and
                              urlparse(base_url or '').hostname in ('localhost', '127.0.0.1', '::1'))
        key, self.model = db.get("llm_api_key"), db.get("llm_model").strip()
        if not key:
            raise UserError("Не указан API-ключ нейросети")
        self.client = AsyncOpenAI(api_key=key, base_url=base_url, timeout=180, max_retries=0)

    def _need_model(self):
        if not self.model:
            raise UserError("Не выбрана модель нейросети")

    async def _request(self, kwargs, stage):
        # SDK retries hide requests/usage; keep the same two retries explicitly.
        for retry in range(3):
            cycle = active_cycle()
            attempt = cycle.attempt(self.provider, self.model, stage, self.input_rate, self.output_rate) if cycle else None
            try:
                response = await self.client.chat.completions.create(**kwargs)
            except BaseException as error:
                if cycle:
                    attempt['status'] = 'cancelled' if isinstance(error, asyncio.CancelledError) else 'failed'
                    attempt['error_type'] = type(error).__name__
                    if isinstance(error, openai.APIStatusError):
                        attempt['http_status'] = error.status_code
                    cycle.save()
                status = error.status_code if isinstance(error, openai.APIStatusError) else None
                retryable = (isinstance(error, openai.APIConnectionError) or
                             status in (408, 409, 429) or (status is not None and status >= 500))
                if retry < 2 and retryable:
                    await asyncio.sleep(0.5 * (2 ** retry))
                    continue
                raise
            if cycle:
                cycle.response(attempt, response, self.provider_cost_supported, self.local_no_bill)
            return response

    async def chat(self, system: str, user: str, json_mode: bool = False, stage='chat', max_tokens=None) -> str:
        self._need_model()
        kwargs = dict(model=self.model, messages=[{"role": "system", "content": system},
                                                  {"role": "user", "content": user}])
        if max_tokens is not None:
            kwargs['max_tokens'] = max_tokens
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = await self._request(kwargs, stage)
        except openai.BadRequestError:
            if not json_mode:
                raise
            kwargs.pop("response_format")  # не все провайдеры это поддерживают
            resp = await self._request(kwargs, stage)
        return (resp.choices[0].message.content or "").strip()

    async def propose(self, posts, count: int, recent_titles, extra: str = "") -> list[dict]:
        system = prompts.build_propose(self.db.get, count, extra)
        recent = "\n".join(f"- {t}" for t in recent_titles) or "—"
        user = f"Уже предлагалось раньше:\n{recent}\n\nСвежие посты:\n\n{format_posts(posts)}"
        data = parse_json(await self.chat(system, user, json_mode=True, stage="propose"))
        result = []
        for item in data.get("proposals", [])[:count]:
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            sources = [int(s) for s in item.get("sources", []) if str(s).strip().isdigit()]
            has_evidence = any(1 <= s <= len(posts) and str(posts[s - 1]['text']).strip() for s in sources)
            text = assemble_outlook(text, item.get('outlook'), emoji=self.db.get('emoji') == '1',
                                    has_evidence=has_evidence, enabled=self.db.get('news_outlook_enabled') != '0',
                                    horizon=self.db.get('news_outlook_horizon'), max_len=self.db.geti('news_outlook_max_len'))
            if not text:
                continue
            result.append({"title": str(item.get("title", "")).strip(), "text": text, "sources": sources})
        return result

    async def rewrite(self, posts, current_text: str, extra: str = "") -> str:
        user = f"Источники:\n\n{format_posts(posts) or '—'}\n\nТекущая версия поста (без прежнего ИИ-прогноза):\n{without_outlook(current_text)}"
        text = await self.chat(prompts.build_rewrite(self.db.get, extra), user)
        return assemble_outlook(text, None, emoji=self.db.get('emoji') == '1', rewrite=True,
                                has_evidence=any(str(p['text']).strip() for p in posts),
                                enabled=self.db.get('news_outlook_enabled') != '0',
                                horizon=self.db.get('news_outlook_horizon'), max_len=self.db.geti('news_outlook_max_len'))

    async def test(self) -> str:
        return await self.chat("Ты проверяешь подключение. Отвечай очень коротко.",
                               "Ответь одним словом по-русски: работает?")

    async def models(self) -> list[str]:
        page = await self.client.models.list()
        return sorted(m.id for m in page.data)
