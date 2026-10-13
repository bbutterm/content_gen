"""Схема настроек, которые хранятся в БД и меняются из панели."""
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from prompts import DEFAULT_PROMPT, PERSONAS, OUTLOOK_HORIZONS

PROVIDERS = {
    "openai": {"name": "OpenAI", "base_url": "https://api.openai.com/v1",
               "keys_url": "https://platform.openai.com/api-keys"},
    "deepseek": {"name": "DeepSeek", "base_url": "https://api.deepseek.com",
                 "keys_url": "https://platform.deepseek.com/api_keys"},
    "openrouter": {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
                   "keys_url": "https://openrouter.ai/keys"},
    "custom": {"name": "Свой (OpenAI-совместимый)", "base_url": "", "keys_url": ""},
}

DEFAULTS = {
    # Telegram
    "tg_api_id": "", "tg_api_hash": "", "user_session": "", "user_phone": "",
    "bot_token": "", "bot_session": "", "bot_session_token": "",
    "admin_ids": "", "target_channel": "", "link_code": "",
    # LLM
    "llm_provider": "openai", "llm_base_url": "", "llm_api_key": "", "llm_model": "",
    "llm_ok": "0", "llm_local_no_bill": "0",
    "llm_input_usd_per_million": "", "llm_output_usd_per_million": "",
    # Independent planned-content connection and workspace defaults.
    "plan_provider": "shared", "plan_api_key": "", "plan_model": "",
    "plan_input_usd_per_million": "", "plan_output_usd_per_million": "",
    "plan_paused": "1", "plan_post_max_len": "2200",
    # Текст
    "prompt": DEFAULT_PROMPT, "persona": "editor", "persona_custom": "",
    "style": "", "language": "русский", "min_len": "400", "max_len": "900", "emoji": "1",
    "news_outlook_enabled": "1", "news_outlook_horizon": "days_weeks",
    "news_outlook_max_len": "600", "news_outlook_focus": "",
    # Публикация и расписание
    "interval_min": "120", "proposals": "3", "paused": "0",
    "quiet_from": "", "quiet_to": "", "auto_publish": "0",
    "show_sources": "1", "signature": "", "stop_words": "",
    "publication_hashtags": "", "publication_hashtags_enabled": "1",
    "max_posts": "80", "max_chars": "1000",
    # Служебное
    "last_run": "0", "last_result": "", "panel_password_generated": "",
}

PLAN_RATE_KEYS = {'plan_input_usd_per_million', 'plan_output_usd_per_million'}
SECRET_KEYS = {"tg_api_hash", "bot_token", "llm_api_key", "plan_api_key"}

INT_RANGES = {
    "interval_min": (5, 10080), "proposals": (1, 10),
    "min_len": (100, 3500), "max_len": (150, 3800),
    "max_posts": (5, 300), "max_chars": (200, 4000), "plan_post_max_len": (600, 3500),
    "news_outlook_max_len": (200, 1200),
}
BOOL_KEYS = {"plan_paused", "llm_local_no_bill", "emoji", "paused", "auto_publish", "show_sources", "publication_hashtags_enabled", "news_outlook_enabled"}
TEXT_KEYS = {"style", "language", "signature", "stop_words", "persona_custom", "prompt",
             "llm_model", "llm_base_url", "plan_model", "news_outlook_focus"}

RATE_KEYS = {"llm_input_usd_per_million", "llm_output_usd_per_million"} | PLAN_RATE_KEYS

EDITABLE = (RATE_KEYS | set(INT_RANGES) | BOOL_KEYS | TEXT_KEYS | SECRET_KEYS |
            {"tg_api_id", "admin_ids", "target_channel", "persona", "llm_provider",
             "quiet_from", "quiet_to", "publication_hashtags", "plan_provider", "news_outlook_horizon"})


class ProposalNotFound(Exception):
    pass


class UserError(Exception):
    """Ошибка, которую можно показать пользователю как есть."""


_LINK_RE = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]{4,})/?$", re.I)


def normalize_channel(raw: str):
    """@name, name, t.me/name, https://t.me/s/name, -100123 -> ключ канала (или None)."""
    s = (raw or "").strip()
    if not s:
        return None
    m = _LINK_RE.match(s)
    if m:
        s = m.group(1)
    s = s.lstrip("@")
    if re.fullmatch(r"-?\d+", s):
        return s
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", s):
        return s.lower()
    return None


def to_peer(key: str):
    return int(key) if re.fullmatch(r"-?\d+", str(key)) else key


def hashtag_char(ch: str) -> bool:
    return ch == '_' or unicodedata.category(ch).startswith('L') or unicodedata.category(ch) == 'Nd'


def clean_hashtags(value) -> str:
    if not isinstance(value, str):
        raise UserError('Хэштеги — строка с #тегами через пробел или запятую')
    tokens = [x for x in re.split(r'[,\s]+', value.strip()) if x]
    if len(tokens) > 8:
        raise UserError('Не больше 8 хэштегов')
    result, seen = [], set()
    for token in tokens:
        body = token[1:]
        if (not token.startswith('#') or not 1 <= len(body) <= 64 or
                not all(hashtag_char(ch) for ch in body) or not any(ch.isalpha() for ch in body)):
            raise UserError('Хэштег: # и 1–64 буквы, цифры или _, минимум одна буква')
        if token.casefold() not in seen:
            result.append(token); seen.add(token.casefold())
    return ' '.join(result)


def clean_value(key: str, value) -> str:
    """Проверяет и нормализует значение настройки. Бросает UserError."""
    if key not in EDITABLE:
        raise UserError(f"Неизвестная настройка: {key}")
    if key == 'publication_hashtags':
        return clean_hashtags(value)
    if key == 'news_outlook_focus':
        if (not isinstance(value, str) or len(value) > 500 or
                any(unicodedata.category(ch).startswith('C') and ch != '\n' for ch in value)):
            raise UserError('Фокус прогноза — строка до 500 символов без управляющих символов')
        return value.strip()
    v = "" if value is None else str(value).strip()

    if key == 'news_outlook_enabled' and (type(value) not in (bool, int, str) or
            v not in ('0', '1', 'true', 'false', 'True', 'False', 'on', 'off')):
        raise UserError('news_outlook_enabled — логическое значение')
    if key == 'news_outlook_horizon':
        if not isinstance(value, str) or v not in OUTLOOK_HORIZONS:
            raise UserError('Горизонт прогноза: days_weeks, days, weeks или month')
        return v
    if key == 'news_outlook_max_len' and not re.fullmatch(r'[0-9]{3,4}', v):
        raise UserError('Длина прогноза — целое число от 200 до 1200 символов')

    if key == 'plan_provider':
        if v not in ('shared','anthropic'): raise UserError('Провайдер плана: shared или anthropic')
        return v
    if key == 'plan_paused' and v not in ('0','1','true','false','True','False','on','off'):
        raise UserError('plan_paused — логическое значение')
    if key == 'plan_model' and len(v) > 300:
        raise UserError('Название модели слишком длинное')
    if key in RATE_KEYS:
        if not v:
            return ""
        try:
            rate = Decimal(v)
            if not rate.is_finite() or rate <= 0 or rate > Decimal('1000000') or rate.adjusted() < -12 or len(v) > 40:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            raise UserError("Тариф — положительная конечная сумма USD за 1 млн токенов (от 0.000000000001 до 1000000), либо пусто")
        return str(rate)
    if key in INT_RANGES:
        lo, hi = INT_RANGES[key]
        if not v.isdigit() or not lo <= int(v) <= hi:
            raise UserError(f"Значение «{key}» должно быть числом от {lo} до {hi}")
        return str(int(v))
    if key in BOOL_KEYS:
        return "1" if v in ("1", "true", "True", "on") else "0"
    if key == "tg_api_id":
        if v and not v.isdigit():
            raise UserError("api_id — это число")
        return v
    if key == "admin_ids":
        ids = [x for x in re.split(r"[,\s]+", v) if x]
        if any(not re.fullmatch(r"\d+", x) for x in ids):
            raise UserError("ID администраторов — числа через запятую")
        return ",".join(dict.fromkeys(ids))
    if key == "target_channel":
        if not v:
            return ""
        norm = normalize_channel(v)
        if not norm:
            raise UserError("Канал: укажи @username, ссылку t.me/… или id вида -100…")
        return norm
    if key == "persona":
        if v not in PERSONAS:
            raise UserError("Неизвестный характер")
        return v
    if key == "llm_provider":
        if v not in PROVIDERS:
            raise UserError("Неизвестный провайдер")
        return v
    if key in ("quiet_from", "quiet_to"):
        if v == "":
            return ""
        if not v.isdigit() or not 0 <= int(v) <= 23:
            raise UserError("Тихие часы — от 0 до 23")
        return str(int(v))
    if key == "prompt":
        return v or DEFAULT_PROMPT
    return v
