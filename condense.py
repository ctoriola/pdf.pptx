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
def llm_config():
    return {
        'base_url': os.environ.get('LLM_BASE_URL', 'https://api.groq.com/openai/v1').rstrip('/'),
        'api_key': os.environ.get('LLM_API_KEY') or os.environ.get('GROQ_API_KEY', ''),
        'model': os.environ.get('LLM_MODEL', 'llama-3.3-70b-versatile'),
    }


def llm_available():
    cfg = llm_config()
    # A local endpoint (e.g. Ollama) needs no key
    return bool(cfg['api_key']) or cfg['base_url'].startswith(('http://localhost', 'http://127.0.0.1'))


def chat_json(system, user, max_tokens=1500, retries=5):
    """Send one chat request and return the parsed JSON object the model replied with."""
    cfg = llm_config()
    headers = {'Content-Type': 'application/json'}
    if cfg['api_key']:
        headers['Authorization'] = f'Bearer {cfg["api_key"]}'
    payload = {
        'model': cfg['model'],
        'temperature': 0.3,
        'max_tokens': max_tokens,
        'response_format': {'type': 'json_object'},
        'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
    }
    delay = 2.0
    for attempt in range(retries):
        try:
            resp = requests.post(f'{cfg["base_url"]}/chat/completions', headers=headers, json=payload, timeout=120)
        except requests.RequestException as exc:
            if attempt == retries - 1:
                raise LLMError(f'Could not reach the AI service: {exc}') from exc
            time.sleep(delay); delay *= 2
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == retries - 1:
                raise LLMError(f'The AI service is busy (HTTP {resp.status_code}). Try again in a minute.')
            wait = _retry_after(resp) or delay
            log.info('LLM HTTP %s, retrying in %.1fs', resp.status_code, wait)
            time.sleep(min(wait, 60)); delay *= 2
            continue
        if resp.status_code >= 400:
            raise LLMError(f'The AI service rejected the request (HTTP {resp.status_code}): {resp.text[:300]}')
        try:
            content = resp.json()['choices'][0]['message']['content']
            return json.loads(content)
        except (KeyError, IndexError, ValueError) as exc:
            if attempt == retries - 1:
                raise LLMError('The AI service returned an unreadable answer.') from exc
    raise LLMError('The AI service did not respond.')


def _retry_after(resp):
    try:
        return float(resp.headers.get('retry-after', ''))
    except ValueError:
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
- "body": Markdown. One short framing sentence, then 3 to 5 bullet points ("- ").
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


def condense_sections(doc_title, sections, plan, progress=None):
    progress = progress or (lambda msg: None)
    groups = group_sections(sections, plan['max_sections'])
    out = []
    for n, group in enumerate(groups, 1):
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
        progress(f'Expanding section {n} of {len(sections)}…')
        # Tables and captions go to the LLM (so it can explain them); tables and images are re-attached verbatim
        text_blocks = blocks
        kept = [b for b in blocks if b['type'] in ('image', 'table')]
        words = word_count([b for b in text_blocks if b['type'] != 'table'])
        if words < 12:
            out.append((name, blocks))
            continue
        target = min(MAX_DETAILED_WORDS, max(plan['words'], int(words * 1.3)))
        reply = chat_json(SYSTEM, detailed_prompt(doc_title, name, source_text(doc_title, name, text_blocks), target),
                          max_tokens=min(3000, target * 2 + 200))
        title = str(reply.get('title') or name).strip()
        out.append((title, _body_blocks(reply) + kept))
    return out
