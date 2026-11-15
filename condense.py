"""
Stage 3: LLM rewriting for the Condensed and Detailed slide types.

Talks to any OpenAI-compatible chat endpoint. The default is Groq's free tier
(https://console.groq.com); point LLM_BASE_URL / LLM_MODEL at another free
provider (OpenRouter, a local Ollama, ...) to swap it.

Images and tables never go through the LLM: they are stripped before the call
and re-attached to the rewritten section afterwards, so figures stay with their
section and table figures stay exactly as they were in the PDF.
"""
import json
import logging
import os
import re
import threading
import time

import requests

from md_parse import blocks_to_markdown, parse_blocks, word_count

log = logging.getLogger(__name__)

CHUNK_WORDS = 2500          # keeps each request inside free-tier token-per-minute limits
NOTE_WORDS = 160            # summary length for each chunk of an over-long section
MAX_DETAILED_WORDS = 900
CONDENSED_MAX_IMAGES = 2    # per slide group


class LLMError(Exception):
    pass


# ============================================================
# Client
# ============================================================
DEFAULT_MODELS = ('qwen/qwen3.8-27b', 'openai/gpt-oss-120b', 'openai/gpt-oss-20b')
# Order of preference among whatever the provider currently offers (matched as substrings)
MODEL_PREFERENCE = ('qwen', 'gpt-oss-120b', 'llama-3.3-70b', 'llama-4', 'llama', 'gpt-oss-20b', 'mistral', 'gemma')
NOT_CHAT_MODELS = ('whisper', 'guard', 'orpheus', 'tts', 'embed', 'allam', 'rerank')
MIN_CONTEXT = 16000

_models = {'for': None, 'list': None}   # models discovered for the current provider
_cooldown = {}                          # model -> time when its rate limit resets
_models_lock = threading.Lock()


def llm_config():
    return {
        'base_url': os.environ.get('LLM_BASE_URL', 'https://api.groq.com/openai/v1').rstrip('/'),
        'api_key': os.environ.get('LLM_API_KEY') or os.environ.get('GROQ_API_KEY', ''),
        # one model, or several separated by commas, tried in order
        'models': [m.strip() for m in os.environ.get('LLM_MODEL', '').split(',') if m.strip()],
    }


def llm_available():
    cfg = llm_config()
    # A local endpoint (e.g. Ollama) needs no key
    return bool(cfg['api_key']) or cfg['base_url'].startswith(('http://localhost', 'http://127.0.0.1'))


def _headers(cfg):
    headers = {'Content-Type': 'application/json'}
    if cfg['api_key']:
        headers['Authorization'] = f'Bearer {cfg["api_key"]}'
    return headers


def available_models(cfg=None):
    """
    The chat models to use, best first. Providers retire models without notice (a hard-coded
    one stops working overnight), so the provider's own list is consulted: configured models
    that still exist come first, then the rest by MODEL_PREFERENCE. If the list can't be
    fetched, the configured models (or the defaults) are used as given.
    """
    cfg = cfg or llm_config()
    key = (cfg['base_url'], tuple(cfg['models']))
    with _models_lock:
        if _models['for'] == key and _models['list']:
            return list(_models['list'])
    listed = []
    try:
        resp = requests.get(f'{cfg["base_url"]}/models', headers=_headers(cfg), timeout=10)
        if resp.status_code == 200:
            for m in resp.json().get('data', []):
                mid = str(m.get('id', ''))
                if mid and m.get('active', True) and not any(x in mid.lower() for x in NOT_CHAT_MODELS) \
                        and (m.get('context_window') or MIN_CONTEXT) >= MIN_CONTEXT:
                    listed.append(mid)
    except (requests.RequestException, ValueError) as exc:
        log.info('Could not list models: %s', exc)
    if listed:
        def rank(mid):
            return next((n for n, pat in enumerate(MODEL_PREFERENCE) if pat in mid.lower()), len(MODEL_PREFERENCE))
        missing = [m for m in cfg['models'] if m not in listed]
        if missing:
            log.warning('Configured model(s) no longer offered: %s', ', '.join(missing))
        chosen = [m for m in cfg['models'] if m in listed]
        chosen += sorted((m for m in listed if m not in chosen), key=lambda m: (rank(m), m))
    else:
        chosen = cfg['models'] or list(DEFAULT_MODELS)
    with _models_lock:
        _models['for'], _models['list'] = key, chosen
    return list(chosen)


def _drop_model(model):
    with _models_lock:
        if _models['list'] and model in _models['list'] and len(_models['list']) > 1:
            _models['list'].remove(model)


def _parse_json(content):
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        m = re.search(r'\{.*\}', content or '', re.S)  # a JSON object wrapped in prose or code fences
        if m:
            return json.loads(m.group(0))
        raise


def chat_json(system, user, max_tokens=1500, retries=5):
    """
    Send one chat request and return the parsed JSON object the model replied with.
    Rate limits are per model, so a limited model is skipped in favour of the next one;
    the call only waits when every model is limited.
    """
    cfg = llm_config()
    headers = _headers(cfg)
    last_error = 'The AI service did not respond.'
    for attempt in range(retries + 3):
        models = available_models(cfg)
        now = time.time()
        ready = [m for m in models if _cooldown.get(m, 0) <= now]
        if not ready:
            wait = min(60, max(0.5, min(_cooldown[m] for m in models) - now))
            log.info('All models rate-limited, waiting %.1fs', wait)
            time.sleep(wait)
            ready = models
        model = ready[0]
        payload = {
            'model': model,
            'temperature': 0.3,
            'max_tokens': max_tokens,
            'response_format': {'type': 'json_object'},
            'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
        }
        if 'gpt-oss' in model:  # reasoning models: keep the hidden reasoning short, and leave room for it
            payload.update(reasoning_effort='low', max_tokens=max_tokens + 400)
        try:
            resp = requests.post(f'{cfg["base_url"]}/chat/completions', headers=headers, json=payload, timeout=120)
        except requests.RequestException as exc:
            last_error = f'Could not reach the AI service: {exc}'
            time.sleep(min(2 ** attempt, 20))
            continue
        if resp.status_code == 429:
            _cooldown[model] = time.time() + min(_retry_after(resp) or 15, 90)
            last_error = 'The AI service is busy (rate limit). Try again in a minute.'
            continue
        if resp.status_code >= 500:
            last_error = f'The AI service is busy (HTTP {resp.status_code}). Try again in a minute.'
            time.sleep(min(2 ** attempt, 20))
            continue
        if resp.status_code in (400, 404):
            text = resp.text[:400]
            if re.search(r'model.{0,40}(not.{0,10}(exist|found)|decommission)|model_not_found', text, re.I):
                log.warning('Model %s is not available; trying another', model)
                _drop_model(model)
                last_error = f'The AI model {model} is no longer available.'
                if len(models) > 1:
                    continue
            elif 'json_validate_failed' in text or 'failed_generation' in text:
                last_error = 'The AI service returned an unreadable answer.'
                _cooldown[model] = time.time() + 2  # let another model try
                continue
            raise LLMError(f'The AI service rejected the request (HTTP {resp.status_code}): {text[:300]}')
        if resp.status_code >= 400:
            raise LLMError(f'The AI service rejected the request (HTTP {resp.status_code}): {resp.text[:300]}')
        try:
            reply = _parse_json(resp.json()['choices'][0]['message']['content'])
            if isinstance(reply, dict):
                return reply
            last_error = 'The AI service returned an unreadable answer.'
        except (KeyError, IndexError, TypeError, ValueError):
            last_error = 'The AI service returned an unreadable answer.'
    raise LLMError(last_error)


def _retry_after(resp):
    try:
        return float(resp.headers.get('retry-after', ''))
    except (TypeError, ValueError):
        return None


# ============================================================
# Prompts
# ============================================================
SYSTEM = (
    'You turn document text into presentation slide content. Use only facts, names and figures '
    'found in the provided text; never invent statistics, quotes, dates or examples. '
    'Write in clear, plain English. Reply with a single JSON object and nothing else.'
)


def condensed_prompt(doc_title, heading, text, words):
    return f"""Document: {doc_title or 'Untitled'}
Section(s): {heading}

Summarise the text below into ONE slide.
Return JSON: {{"title": "...", "body": "..."}}
- "title": a slide title of at most 8 words.
- "body": Markdown. One short framing sentence, then 3 short bullet points ("- ") of a few words each.
  At most {words} words in total. Keep the most important numbers. Bold (**like this**) at most one key phrase per bullet.
  No headings, no tables, no images.

TEXT:
{text}"""


def detailed_prompt(doc_title, heading, text, words):
    return f"""Document: {doc_title or 'Untitled'}
Section: {heading}

Expand the text below into richer slide content that a presenter could teach from.
Return JSON: {{"title": "...", "body": "..."}}
- "title": the section title, at most 10 words (keep the original wording where it is already good).
- "body": Markdown, about {words} words. Explain the ideas in more depth, define technical terms,
  and connect points to the rest of the document where the text supports it.
  You may use "### " sub-headings and "- " bullets. End with a "### Key takeaways" sub-heading
  followed by 2 to 4 bullets. Do not include tables or images.
  Do not add facts that are not supported by the text.

TEXT:
{text}"""


def notes_prompt(doc_title, heading, text):
    return f"""Document: {doc_title or 'Untitled'}
Section: {heading}

Write dense factual notes of the text below in at most {NOTE_WORDS} words, keeping every key number and name.
Return JSON: {{"notes": "..."}}

TEXT:
{text}"""


# ============================================================
# Section handling
# ============================================================
def group_sections(sections, max_groups):
    """Merge adjacent sections (smallest neighbouring pair first) until at most max_groups remain."""
    groups = [[s] for s in sections]
    while len(groups) > max_groups:
        sizes = [sum(word_count(b) for _, b in g) for g in groups]
        i = min(range(len(groups) - 1), key=lambda k: sizes[k] + sizes[k + 1])
        groups[i:i + 2] = [groups[i] + groups[i + 1]]
    return groups


def chunk_blocks(blocks, limit=CHUNK_WORDS):
    chunks, current, n = [], [], 0
    for b in _split_long_paras(blocks, limit):
        w = word_count([b])
        if current and n + w > limit:
            chunks.append(current)
            current, n = [], 0
        current.append(b)
        n += w
    if current:
        chunks.append(current)
    return chunks


def _split_long_paras(blocks, limit):
    for b in blocks:
        words = b['text'].split() if b['type'] == 'para' else []
        if len(words) <= limit:
            yield b
            continue
        for k in range(0, len(words), limit):
            yield {'type': 'para', 'text': ' '.join(words[k:k + limit])}


def source_text(doc_title, heading, blocks):
    """Markdown for the LLM; over-long input is first reduced to per-chunk notes."""
    if word_count(blocks) <= CHUNK_WORDS:
        return blocks_to_markdown(blocks)
    notes = []
    for chunk in chunk_blocks(blocks):
        reply = chat_json(SYSTEM, notes_prompt(doc_title, heading, blocks_to_markdown(chunk)), max_tokens=500)
        notes.append(str(reply.get('notes', '')).strip())
    return '\n\n'.join(n for n in notes if n)


def _body_blocks(reply):
    body = str(reply.get('body', '')).strip()
    # Models sometimes return the body as a list of bullets
    if isinstance(reply.get('body'), list):
        body = '\n'.join('- ' + str(x) for x in reply['body'])
    return [b for b in parse_blocks(body) if b['type'] in ('para', 'sub', 'list')]


def _largest_images(images, n):
    from PIL import Image

    def area(img):
        try:
            with Image.open(img['path']) as im:
                return im.size[0] * im.size[1]
        except OSError:
            return 0
    keep = sorted(images, key=area, reverse=True)[:n]
    return [img for img in images if img in keep]  # preserve document order


def _is_designed(blocks):
    """A product or process section: already slide-sized structured content, not prose to rewrite."""
    return any(b['type'] == 'meta' and b.get('layout') for b in blocks[:2])


def _group_prose_runs(sections, max_groups):
    """Group prose sections as before, keeping each designed section as a group of its own."""
    prose_total = sum(1 for _, b in sections if not _is_designed(b)) or 1
    budget = max(1, max_groups - sum(1 for _, b in sections if _is_designed(b)))
    groups, run = [], []

    def flush():
        if run:
            groups.extend(group_sections(run, max(1, round(budget * len(run) / prose_total))))
            run.clear()
    for section in sections:
        if _is_designed(section[1]):
            flush()
            groups.append([section])
        else:
            run.append(section)
    flush()
    return groups


def condense_sections(doc_title, sections, plan, progress=None):
    progress = progress or (lambda msg: None)
    groups = _group_prose_runs(sections, plan['max_sections'])
    out = []
    for n, group in enumerate(groups, 1):
        if len(group) == 1 and _is_designed(group[0][1]):
            out.append(group[0])  # a product or process slide stays exactly as it is
            continue
        progress(f'Summarising section {n} of {len(groups)}…')
        heading = ' / '.join(name for name, _ in group)
        text_blocks, images = [], []
        for name, blocks in group:
            if len(group) > 1:
                text_blocks.append({'type': 'sub', 'text': name})
            text_blocks += blocks  # image captions reach the LLM as "[Figure: ...]" context
            images += [b for b in blocks if b['type'] == 'image']
        if word_count(text_blocks) < 12:
            out.append((group[0][0], text_blocks))  # nothing to summarise
            continue
        reply = chat_json(SYSTEM, condensed_prompt(doc_title, heading, source_text(doc_title, heading, text_blocks),
                                                   plan['words']), max_tokens=600)
        title = str(reply.get('title') or group[0][0]).strip()
        out.append((title, _body_blocks(reply) + _largest_images(images, CONDENSED_MAX_IMAGES)))
    return out


def expand_sections(doc_title, sections, plan, progress=None):
    progress = progress or (lambda msg: None)
    out = []
    for n, (name, blocks) in enumerate(sections, 1):
        if _is_designed(blocks):
            out.append((name, blocks))  # a product or process slide stays exactly as it is
            continue
        progress(f'Expanding section {n} of {len(sections)}…')
        # Tables and captions go to the LLM (so it can explain them); tables and images are re-attached verbatim
        text_blocks = blocks
        kept = [b for b in blocks if b['type'] in ('image', 'table', 'price', 'steps')]
        words = word_count([b for b in text_blocks if b['type'] != 'table'])
        if words < 12:
            out.append((name, blocks))
            continue
        target = min(MAX_DETAILED_WORDS, max(plan['words'], int(words * 1.3)))
        reply = chat_json(SYSTEM, detailed_prompt(doc_title, name, source_text(doc_title, name, text_blocks), target),
                          max_tokens=min(3000, target * 2 + 200))
        chapter = [b for b in blocks[:1] if b['type'] == 'chapter']
        title = name if chapter else str(reply.get('title') or name).strip()  # chapter titles stay as printed
        out.append((title, chapter + _body_blocks(reply) + kept))
    return out
