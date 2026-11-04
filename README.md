# PDF to Slides

A Flask web app that turns a PDF into a PowerPoint deck. The PDF is converted to
Markdown with [PyMuPDF4LLM](https://pymupdf.readthedocs.io/en/latest/pymupdf4llm/), the Markdown is
split into slide-sized sections (the same approach as PitchPerfectAI's deck builder), and the
images from the PDF are placed on the slides of the section they came from.

> **Licence note:** PyMuPDF is AGPL-3.0. That's fine for personal and non-commercial use. A
> closed-source commercial service would need an Artifex commercial licence.

## Pipeline

```
PDF ──PyMuPDF──▶ Markdown + images ──parse──▶ sections ──[LLM]──▶ deck builder ──▶ .pptx
     pdf_to_md.py                    md_parse.py      condense.py   deck_builder.py
```

| Stage | File | What it does |
|---|---|---|
| 1. PDF → Markdown | `pdf_to_md.py` | pymupdf4llm with its layout model: headings, paragraphs, lists, tables, figures. Page headers/footers are dropped. Each figure is saved to `images/` and referenced in the Markdown where it appeared. Heading levels are normalised to `# Title` / `## Section` / `### Sub`; bold lines on their own become headings. Icons, thin rules, blank boxes, duplicates and logos repeated on 3+ pages are filtered out; photos are recompressed to JPEG. |
| 2. Sections | `md_parse.py`, `pipeline.py` | Split on `##` headings into blocks (para, sub-heading, list, table, image). `Figure 3: …` paragraphs (plain or italic) become image captions. Empty headings merge into the next section. |
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

## Run locally

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

Sections over 2,500 words are first reduced to notes chunk by chunk to stay inside free-tier
token limits, and HTTP 429 responses are retried using the `Retry-After` header.

## Deploy on Vercel

The app is stateless: each request converts in a temporary folder and returns the file, so it
runs as a single Vercel Python function. Vercel detects Flask from `app.py`, and `.python-version`
pins Python 3.12. The installed dependencies take about 370 MB, under the 500 MB function limit.

1. Import the repo in Vercel. No build settings are needed.
2. Add `GROQ_API_KEY` under Project → Settings → Environment Variables.

### Large PDFs: Firebase Storage

Vercel functions accept and return at most 4.5 MB. When Firebase Storage is configured, the
browser uploads the PDF straight to Storage (up to 50 MB), the function gets only the file's path,
and the deck comes back as a signed link that expires after 1 hour. **Setup guide:
[`docs/Firebase_Setup_Guide.pdf`](docs/Firebase_Setup_Guide.pdf)**
(also as [`docs/FIREBASE_SETUP.md`](docs/FIREBASE_SETUP.md)).

| Piece | File |
|---|---|
| Server: token check, download, upload, signed links | `storage.py` |
| Browser: anonymous sign-in + resumable upload | bottom of `templates/index.html` |
| Upload rules (PDF only, <50 MB, own folder only) | `storage.rules` |
| Auto-delete after 1 day | `lifecycle.json` (or the console steps in the guide) |

Without the six `FIREBASE_*` variables the app falls back to direct uploads:

| Limit | Effect | How the app handles it |
|---|---|---|
| 4.5 MB request body | PDFs over ~4.4 MB can't be uploaded | Checked in the browser and on the server, with a clear message |
| 4.5 MB response body | Image-heavy decks could exceed it | The pictures inside the .pptx are recompressed in steps until it fits |

Either way, the function stops after 300 seconds, so `MAX_PAGES` (default 80) caps the input.

On other hosts (Render, Railway, Fly.io) direct uploads default to 50 MB:

```bash
gunicorn -w 2 --timeout 300 -b 0.0.0.0:8000 app:app
```

Settings: `MAX_UPLOAD_MB`, `MAX_RESPONSE_MB` (0 = no limit), `MAX_STORAGE_UPLOAD_MB`, `MAX_PAGES`.

Scanned PDFs (no text layer) are rejected with a message suggesting OCR first. Tesseract isn't
available on Vercel.

## Command line

```bash
python convert.py report.pdf --mode condensed --theme green -o report.pptx --keep out/
python convert.py out/markdown/document.md --mode standard   # rebuild from Markdown, skipping PDF parsing
```

## Tests

```bash
pip install pytest
python -m pytest -q
```

The tests run the real PDF conversion on `tests/fixtures/sample.pdf` (headings, a running
header/footer, a table and three figures), plus parsing, image filtering, LLM handling (HTTP
stubbed), size control, the Flask routes, and decks in every slide type and theme.
