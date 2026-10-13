"""Controlled **bold** only. Model HTML is always literal escaped text.

Runs retain visible text separately from markup so Telegram UTF-16 budgets never
slice HTML entities or bold spans. Raw drafts/history are never changed.
"""
import html
import re
from dataclasses import dataclass


def utf16_len(text):
    return len(text.encode('utf-16-le')) // 2


@dataclass(frozen=True)
class PostBody:
    runs: tuple

    @property
    def plain(self):
        return ''.join(text for text, _ in self.runs)

    def clipped(self, budget):
        runs = []
        for text, bold in self.runs:
            size = utf16_len(text)
            if size <= budget:
                runs.append((text, bold)); budget -= size
                continue
            if not bold:
                chars = []
                for ch in text:
                    size = 2 if ord(ch) > 0xFFFF else 1
                    if size > budget:
                        break
                    chars.append(ch); budget -= size
                runs.append((''.join(chars), False))
            # A bold span is atomic: omit it rather than truncate its meaning.
            break
        return PostBody(tuple(runs))

    def split_topic_hashtags(self):
        """Reserve only an existing final hashtag row; never invent topic tags."""
        plain = self.plain.rstrip()
        start = plain.rfind('\n') + 1
        line = plain[start:].strip()
        if not start or not re.fullmatch(r'#\w+(?:[ \t]+#\w+)*', line):
            return self, []
        from settings import clean_hashtags, UserError
        try:
            tags = clean_hashtags(line).split()
        except UserError:
            return self, []
        end = len(plain[:start].rstrip())
        return PostBody(tuple(_slice(self.runs, 0, end))), tags

    def html(self):
        out = []
        for text, bold in self.runs:
            escaped = html.escape(text).replace('**', '&#42;&#42;')
            out.append('<b>' + escaped + '</b>' if bold else escaped)
        return ''.join(out)


def _bold_runs(text):
    parts = text.split('**')
    if len(parts) % 2 == 0 or re.search(r'\*{3,}', text):
        return [(text, False)]
    for span in parts[1::2]:
        if not span or span != span.strip() or '\n' in span:
            return [(text, False)]
    return [(part, bool(i % 2)) for i, part in enumerate(parts) if part]


_EMOJI = re.compile('[\U0001f000-\U0001faff\u2600-\u27bf]')
_EMOJI_CLUSTER = re.compile(_EMOJI.pattern + '(?:[\ufe0f\ufe0e]|\u200d' + _EMOJI.pattern + ')*')


def _without_emoji(text):
    # Keycaps become their factual digit; ©, arithmetic and ordinary text stay.
    text = re.sub(r'([#*0-9])[\ufe0f\ufe0e]?\u20e3', r'\1', text)
    return _EMOJI_CLUSTER.sub('', text)


def _slice(runs, start, end):
    result, pos = [], 0
    for text, bold in runs:
        lo, hi = max(0, start - pos), min(len(text), end - pos)
        if hi > lo:
            result.append((text[lo:hi], bold))
        pos += len(text)
    return result


def _paragraphs(runs):
    """Change whitespace only, outside bold spans, URLs and initials."""
    plain = ''.join(text for text, _ in runs)
    protected, pos = [], 0
    for text, bold in runs:
        if bold:
            protected.append((pos, pos + len(text)))
        pos += len(text)
    breaks = []
    for paragraph in re.finditer(r'[^\n]+(?:\n(?!\n)[^\n]+)*', plain):
        if len(paragraph.group()) <= 360:
            continue
        last, sentences = paragraph.start(), 0
        for gap in re.finditer(r'\s+', paragraph.group()):
            start, end = paragraph.start() + gap.start(), paragraph.start() + gap.end()
            if any(lo < start < hi for lo, hi in protected):
                continue
            previous = plain[last:start].rsplit(None, 1)[-1] if plain[last:start].strip() else ''
            following = plain[end:paragraph.end()].split(None, 1)[0] if plain[end:paragraph.end()].strip() else ''
            # Never separate initials/abbreviations, decimal fractions or URLs.
            if re.fullmatch(r'[A-Za-zА-Яа-яЁё]\.', previous) or re.fullmatch(r'[A-Za-zА-Яа-яЁё]\.', following):
                continue
            if previous.casefold() in ('г.', 'ул.', 'млн.', 'млрд.', 'dr.', 'mr.', 'mrs.', 'т.е.', 'т.д.'):
                continue
            sentence = previous.endswith(('.', '!', '?')) and '://' not in previous and not previous.startswith('www.')
            sentences += bool(sentence)
            if (sentence and (sentences >= 2 or start - last >= 320)) or start - last >= 500:
                breaks.append((start, end)); last, sentences = end, 0
    out, last = [], 0
    for start, end in breaks:
        out += _slice(runs, last, start) + [('\n\n', False)]
        last = end
    return out + _slice(runs, last, len(plain))


def render_post(text, title='', emoji=True):
    """Return safe visible runs, ready for the shared preview/publication path."""
    title = title or ''  # Legacy schema permits NULL titles.
    runs = _bold_runs(text.strip())
    if not emoji:
        runs = [(clean.strip() if bold else clean, bold) for part, bold in runs
                if (clean := _without_emoji(part))]
        title = _without_emoji(title)
    plain = ''.join(part for part, _ in runs)
    first, separator, rest = plain.partition('\n\n')
    has_heading = (bool(separator) and '\n' not in first and len(first) <= 160 and len(first.split()) <= 22)
    # A standalone explicitly bold headline is also a heading.
    has_heading = has_heading or (len(first) <= 160 and not separator and '\n' not in first
                                  and (plain == title.strip() or (len(runs) == 1 and runs[0][1])))
    if has_heading:
        heading = first
        body = _slice(runs, len(first) + len(separator), len(plain))
    else:
        heading = ''.join(part for part, _ in _bold_runs(title.strip().split('\n', 1)[0]))[:160]
        body = runs
    if emoji and not _EMOJI.search(plain + heading):
        heading = '📰 ' + heading if heading else '📰'
    out = [(heading, True), ('\n\n', False)] if heading and body else ([(heading, True)] if heading else [])
    return PostBody(tuple(out + _paragraphs(body)))
