# PDF to Slides

A Flask web app that turns a PDF into a PowerPoint deck. The PDF is converted to
Markdown with [Docling](https://github.com/docling-project/docling), the Markdown is split
into slide-sized sections (the same approach as PitchPerfectAI's deck builder), and the
images from the PDF are placed on the slides of the section they came from.

## Pipeline

```
PDF ──Docling──▶ Markdown + images ──parse──▶ sections ──[LLM]──▶ deck builder ──▶ .pptx
     pdf_to_md.py                    md_parse.py      condense.py   deck_builder.py
```

| Stage | File | What it does |
|---|---|---|
| 1. PDF → Markdown | `pdf_to_md.py` | Docling layout analysis: headings, paragraphs, lists, tables, figures. Page headers/footers are dropped. Each figure is saved to `images/` and referenced in the Markdown where it appeared. Icons, thin rules, blank boxes, duplicates and logos repeated on 3+ pages are filtered out. Scanned PDFs are retried with OCR. |
| 2. Sections | `md_parse.py`, `pipeline.py` | Split on `##` headings into blocks (para, sub-heading, list, table, image). `Figure 3: …` paragraphs become image captions. Empty headings (e.g. "3 Methods" followed by "3.1 Data") merge into the next section. |
| 3. LLM (optional) | `condense.py` | Rewrites sections for the Condensed and Detailed slide types. Images and tables are taken out before the call and put back afterwards, so figures stay with their section and table data stays exact. |
| 4. Slide plan | `slide_plan.py` | The three slide types and their word budgets. |
| 5. Deck | `deck_builder.py` | Picks a layout per section (table, stat cards, numbered cards, lead + body, text + image, image gallery), sizes text to fit, and continues long sections on "(cont.)" slides. Three themes. |

## Slide types

| Type | Text source | Slides |
|---|---|---|
| **Condensed** | LLM summary, ≤70 words per slide | Adjacent sections grouped into at most 12 slides; up to 2 images each (the largest). |
| **Standard** | The full text from the PDF, no LLM | One section → as many slides as it needs; every image kept. |
| **Detailed** | LLM expansion (explanations, definitions, key takeaways) | Like Standard, with original tables and images re-attached. |

The LLM prompts forbid adding facts, numbers or quotes that aren't in the source text.

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # add GROQ_API_KEY for Condensed/Detailed
python app.py               # http://localhost:5000
```

### LLM (free)

The default is Groq's free tier: get a key at https://console.groq.com/keys and set
`GROQ_API_KEY`. Default model: `llama-3.3-70b-versatile`. Any OpenAI-compatible endpoint
works through `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL` (OpenRouter free models, or a local
Ollama, which needs no key). See `.env.example`. Without a key, only Standard is offered.

Requests stay under free-tier token limits: sections over 2,500 words are first reduced to
notes chunk by chunk, and HTTP 429 responses are retried using the `Retry-After` header.

### Docling models

On first use, Docling downloads its layout and table models (a few hundred MB) from
Hugging Face. The app loads them in the background at startup. For a server without access to
Hugging Face, download them once and point the app at them:

```bash
docling-tools models download -o ./docling-models
export DOCLING_ARTIFACTS_PATH=./docling-models
```

If the models can't be loaded, every job fails with a "server setup problem" message,
not a message blaming the user's PDF.

## Command line

```bash
python convert.py report.pdf --mode condensed --theme green -o report.pptx --keep out/
python convert.py out/markdown/document.md --mode standard   # rebuild from Markdown, skipping Docling
```

## Deploying

Jobs are kept in memory and run in threads, so run **one** worker process with threads:

```bash
gunicorn -w 1 --threads 8 --timeout 120 -b 0.0.0.0:8000 app:app
```

Docling needs about 2–4 GB of RAM. Serverless hosts (Vercel, Netlify) are not a fit; use a VM
or a container host (Render, Railway, Fly.io, a VPS). Settings: `MAX_UPLOAD_MB`,
`JOB_TTL_SECONDS` (job folders are deleted after this), `MAX_ACTIVE_JOBS`, `JOBS_DIR`,
`DOCLING_WARM_UP=0` to skip loading the models at startup.

## Tests

```bash
pip install pytest
python -m pytest -q
```

The tests cover everything after Docling: parsing, image filtering, section merging, LLM
handling (HTTP stubbed), and building decks in every slide type and theme. The fixture is
`tests/fixtures/sample.md` with its images.
