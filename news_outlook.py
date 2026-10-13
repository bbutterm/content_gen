"""Assemble news inferences, with a labelled generic editorial safety scenario.

Only called by explicit news generation/rewrite, not rendering or manual edits.
"""
import re
import unicodedata

from post_format import _bold_runs

OUTLOOK_HORIZONS = {'days_weeks': 'ближайшие дни/недели', 'days': 'ближайшие 1–3 дня',
                   'weeks': 'ближайшие 1–2 недели', 'month': 'ближайший месяц'}
# Not a model prediction: no topic-specific claim or new factual detail.
FALLBACK = ('Редакционный сценарий: на ближайшие дни/недели это может повлиять на обсуждение новости. '
            'При новых подтверждённых деталях оценку следует пересмотреть; последствия пока неизвестны.')
_TAG_ROW = re.compile(r'#\w+(?:[ \t]+#\w+)*')
_CONDITIONAL = re.compile(r'\b(?:если|при условии|if|provided)\b', re.I)
_HEDGE = re.compile(r'\b(?:может|могут|возможн\w*|вероятно\w*|may|might|could)\b', re.I)

_UNCERTAINTY = re.compile(r'не (?:подтвержден\w*|извест\w*|ясн\w*)|неизвест\w*|неопредел\w*|может измен\w*|unknown|uncertain|not confirmed|may change', re.I)
_DIAGNOSIS = re.compile(r'недостаточно\s+данных', re.I)
_DIAGNOSIS_PARAGRAPH = re.compile(
    r'недостаточно\s+данных(?:\s+для\s+(?:над[ёе]жного\s+)?'
    r'(?:ии[\s\-‐‑‒–—]*)?прогноза)?[.!?…]*', re.I)


def _safe_fragment(value, max_len=400):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= max_len
            and not any(unicodedata.category(ch).startswith('C') for ch in value)
            and not _DIAGNOSIS.search(value)
            and not re.search(r'[%％#<>*\n\r]|ИИ[ -]прогноз', value, re.I))


def _structured_prediction(outlook):
    if not isinstance(outlook, dict):
        return FALLBACK
    consequences = outlook.get('consequences')
    assumptions, uncertainty = outlook.get('assumptions'), outlook.get('uncertainty')
    if (not isinstance(consequences, list) or not 1 <= len(consequences) <= 2
            or not all(_safe_fragment(v) for v in consequences + [assumptions, uncertainty])):
        return FALLBACK
    if (not all(_HEDGE.search(v) for v in consequences)
            or not _UNCERTAINTY.search(uncertainty)
            or any(_CERTAINTY.search(v) for v in consequences + [assumptions, uncertainty])):
        return FALLBACK
    return ' '.join(v.strip() for v in consequences + [assumptions, uncertainty])


_ASSUMPTION = re.compile(r'\b(?:услови\w*|допущени\w*|предположени\w*|assumptions?|condition\w*)\b', re.I)
_CERTAINTY = re.compile(r'\b(?:гарантирован\w*|неизбежн\w*|точно|definitely|guaranteed|certainly)\b', re.I)
_LABEL = re.compile(r'^(?:🤖[ \t]*)?(?:\*\*)?(?:🤖[ \t]*)?ИИ[- ]прогноз\b(?:\*\*)?[ \t]*:?[ \t]*(?:\n)?', re.I)


def _take_blocks(text):
    """Only labelled inference paragraphs are removed, never arbitrary fact text."""
    facts, predictions = [], []
    pending_label = False
    for paragraph in re.split(r'\n[ \t]*\n', text.strip()):
        match = _LABEL.match(paragraph)
        if match:
            prediction = paragraph[match.end():].strip()
            predictions.append(prediction)
            pending_label = not prediction
        elif pending_label and not _TAG_ROW.fullmatch(paragraph.strip()):
            predictions[-1] = paragraph.strip()
            pending_label = False
        else:
            facts.append(paragraph)
            pending_label = False
    return '\n\n'.join(facts), predictions


def without_outlook(text):
    """Prepare rewrite context without treating the prior inference as evidence."""
    return _take_blocks(text)[0]


def _plain_prediction(value):
    if not _safe_fragment(value, 1200):
        return FALLBACK
    if not (len(_CONDITIONAL.findall(value)) <= 2 and _HEDGE.search(value)
            and _UNCERTAINTY.search(value)
            and _ASSUMPTION.search(value)) or _CERTAINTY.search(value):
        return FALLBACK
    # Count actual modal consequence sentences, not an uncertainty sentence's
    # "may change". Conditions/uncertainty can be stated once for the paragraph.
    consequences = [sentence for sentence in re.split(r'(?<=[.!?])\s+', value.strip())
                    if not (_ASSUMPTION.search(sentence) or _UNCERTAINTY.search(sentence))]
    if not 1 <= len(consequences) <= 2 or not all(_HEDGE.search(v) for v in consequences):
        return FALLBACK
    return value.strip()


def editorial_fallback(horizon='days_weeks'):
    """Local, generic, uncertainty-preserving scenario; fits the minimum 200 chars."""
    return FALLBACK.replace(OUTLOOK_HORIZONS['days_weeks'], OUTLOOK_HORIZONS.get(horizon, OUTLOOK_HORIZONS['days_weeks']))


def assemble_outlook(text, outlook, *, emoji=True, rewrite=False, has_evidence=True, enabled=True,
                     horizon='days_weeks', max_len=600):
    """Keep authored facts/tags and place the separately labelled inference last."""
    text = text.strip()
    if not text:
        return ''
    last = text.rsplit('\n', 1)
    tags = ''
    if len(last) == 2 and _TAG_ROW.fullmatch(last[1].strip()):
        text, tags = last[0].rstrip(), last[1].strip()
    text, predictions = _take_blocks(text)
    # Recognize the whole visible paragraph using the renderer's canonical
    # balanced-bold parser; malformed markers and sourced prose stay literal.
    text = '\n\n'.join(paragraph for paragraph in re.split(r'\n[ \t]*\n', text)
                       if not _DIAGNOSIS_PARAGRAPH.fullmatch(
                           ''.join(part for part, _ in _bold_runs(paragraph.strip()))))
    if not text.strip():
        return ''
    if not enabled:
        return text + ('\n\n' + tags if tags else '')
    prediction = (_plain_prediction(predictions[-1]) if rewrite and predictions
                  else _structured_prediction(outlook))
    if not has_evidence or prediction == FALLBACK or len(prediction) > max_len:
        prediction = editorial_fallback(horizon)
    label = ('🤖 ' if emoji else '') + '**ИИ-прогноз**'
    return text + '\n\n' + label + '\n' + prediction + ('\n\n' + tags if tags else '')
