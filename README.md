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
| 1. PDF → Markdown | `pdf_to_md.py`, `pdf_structure.py` | pymupdf4llm with its layout model, page by page: headings, paragraphs, lists, tables, figures; page headers/footers dropped; each figure saved to `images/` and referenced where it appeared. `pdf_structure.py` then recovers what the converter gets wrong, from the PDF's own font sizes: **chapter titles** (the display-size headings, often scrambled or skipped), the **document title** from the cover, and it drops **contents, copyright and bare cover pages** and the text inside figures. Heading levels are normalised to `# Title` / `## Section` / `### Sub`, ALL-CAPS headings become Title Case (the document's acronyms kept). Icons, rules, blank boxes, resized duplicates and header logos are filtered; photos are recompressed. |
| 2. Sections | `md_parse.py`, `pipeline.py` | Split on `##` headings into blocks (para, pull-quote, sub-heading, list, table, image); `## X {.chapter}` opens a chapter. Lists keep their real numbers and `a.`/`b.` sub-points; bullets and paragraphs the PDF cut mid-sentence are rejoined. `Figure 3: …` lines and italic titles above a figure become captions. |
| 2b. Meaning | `semantics.py`, `planner.py` | Works out what kind of document it is (brochure, handbook, paper…) and what each piece *is*: a product (photo + spec sheet + price), a price, a spec sheet (label/value table), a numbered process, a kicker label, a tagline. `semantics.py` does this by rules. With an LLM key, `planner.py` shows the model a compact outline (titles and block types, not the full text) and lets it adjust the arrangement: merge a fragment into the section before it, promote a section to a process or product layout, name a generically titled section. Every suggestion is validated against the real blocks; if the LLM is missing, rate-limited or wrong, the rules' plan is used. The LLM arranges and never rewrites. |
| 3. LLM (optional) | `condense.py` | Rewrites sections for the Condensed and Detailed slide types. Images and tables are taken out before the call and put back afterwards, so figures stay with their section and table data stays exact. |
| 4. Slide plan | `slide_plan.py` | The three slide types and their word budgets. |
| 5. Deck | `deck_builder.py` | Picks a layout per section (product slide, process slide, spec sheet, table, stat cards, numbered cards, lead + body, text + image, image gallery), sizes text to fit, and continues long sections on "(cont.)" slides. Four themes, including Mint & Black, which matches the web app. |

Each slide carries at most half the text it could physically hold (`TEXT_FILL = 0.5` in `deck_builder.py`), set as large as the box allows up to 24pt. Past 90 slides the budget grows automatically (to at most 85%) so a long handbook stays a usable length. Chapters get divider slides and drive the agenda and slide badges. Long sections split at their own sub-headings; short ones share a slide, keeping numbered lists together. Page breaks are balanced, never strand a list's last item or separate a question from its options, and wide diagrams get the slide's full width.

## Choosing sections

Converting is two steps. **Step 1** (`POST /plan`) reads the PDF and plans its chapters and
sections without making slides, and the page lists them: chapters with their sections, each
with a preview, its layout and a slide estimate, all ticked to start. **Step 2** (`POST /build`)
makes the deck from only the ticked sections; the agenda and chapter numbers follow the choice,
and a chapter whose opening section is left out still gets its divider slide. In the AI slide
types, only the chosen sections are sent to the LLM.

Vercel keeps nothing between requests, so step 1 hands the plan to the browser, which sends it
back (gzip-compressed) with the choice. The server checks it and does not read the PDF again: the
plan records where each picture sits on its page, so step 2 cuts only the chosen sections' pictures
straight from their pages (one chapter of a 500-page textbook builds in under a second). Image
paths stay inside that conversion's own folder, and no AI planning repeats. A PDF in B2 storage
stays there between the steps and is deleted once the deck is built; the Markdown download is
prepared in step 1, while the whole document is at hand.
`POST /convert` still does both steps at once with every section.

On the command line: `python convert.py report.pdf --list`, then `--sections 0,3-7,12`.

## Slide types

| Type | Text source | Slides |
|---|---|---|
| **Condensed** | LLM summary, ≤35 words per slide (a framing sentence and 3 short bullets) | Adjacent sections grouped into at most 12 slides; up to 2 images each (the largest). |
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
`GROQ_API_KEY`. The app asks the provider which chat models it currently offers and picks from
them (preferring Qwen, then GPT-OSS, then Llama), so a retired model never breaks it; when one
model is rate-limited it moves to the next instead of waiting. Any OpenAI-compatible endpoint
works through `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL` (OpenRouter free models, or a local
Ollama, which needs no key). See `.env.example`. Without a key, only Standard is offered.

Sections over 2,500 words are first reduced to notes chunk by chunk to stay inside free-tier
token limits, and HTTP 429 responses are retried using the `Retry-After` header.

## Deploy on Vercel

The app is stateless: each request converts in a temporary folder and returns the file, so it
runs as a single Vercel Python function. Vercel detects Flask from `app.py`, and `.python-version`
pins Python 3.12. The installed dependencies take about 330 MB (plus 4 MB of OCR language data), under the 500 MB function limit.

1. Import the repo in Vercel. No build settings are needed.
2. Add `GROQ_API_KEY` under Project → Settings → Environment Variables.

### Large PDFs: Backblaze B2 (free, no card)

Vercel functions accept and return at most 4.5 MB. When B2 is configured, the browser gets a
one-time upload link from `/upload-url` and uploads the PDF straight to a private B2 bucket (up to
**200 MB**; the size is signed into the link, so B2 rejects anything else). The function downloads it,
converts it, deletes it, and returns the deck as a download link that expires after 1 hour.
Setup: create a private bucket and a bucket-limited Read and Write key in Backblaze and add the four
`B2_*` variables in Vercel. Then, to set the bucket's CORS rule (browser uploads) and auto-delete rules:

1. Add `SETUP_TOKEN` in Vercel (a long random password, 16+ characters) and redeploy.
2. Open `/setup`, enter that token, then paste the account's master key and run the setup.
3. Remove `SETUP_TOKEN` and redeploy.

The setup page is for the owner only. Without `SETUP_TOKEN` it does not exist (a 404 like any unknown
address). With it, the page shows a password box and nothing else until the token is entered: no
settings, no bucket name, and no request to Backblaze. Nothing on the site links to it. The master key
is used for that request only and never stored.

| Piece | File |
|---|---|
| Server: presigned links (AWS SigV4, no SDK), download, upload, delete | `storage.py` |
| Browser: upload with progress | bottom of `templates/index.html` |
| One-time bucket setup page: CORS for browser uploads, auto-delete after ~2 days | `b2_admin.py`, `templates/setup.html` |

Without the four `B2_*` variables the app falls back to direct uploads:

| Limit | Effect | How the app handles it |
|---|---|---|
| 4.5 MB request body | PDFs over ~4.4 MB can't be uploaded | Checked in the browser and on the server, with a clear message |
| 4.5 MB response body | Image-heavy decks could exceed it | The pictures inside the .pptx are recompressed in steps until it fits |

### Textbooks

Either way, a Vercel function stops after 300 seconds. A textbook (up to `MAX_PAGES`, 3,000 pages)
is therefore read by several requests at once:

```
browser ──/plan──▶ "needs parts": the book cut into 300-page parts (saved beside the upload in B2)
        ──/read ×4 at a time──▶ each part: its pages' Markdown + each picture's fingerprint, size, position
        ──/plan + parts──▶ plan from the whole book's text ──▶ section picker
        ──/build──▶ only the chosen sections; their pictures cut from the PDF
```

- **Same result as one read.** pymupdf4llm ranks heading sizes over the pages it is given, so each
  heading is tagged with its font size and `relevel_headings` ranks them once over the whole book.
  Tests check that a book read in parts plans exactly like one read in one go.
- **No pictures in the plan step.** The parts send what filtering (duplicates, repeated logos,
  blanks) and layout need: fingerprint, size, kind and position. The plan is made from those, and
  `/build` cuts the chosen pictures from the PDF.
- **Scanned books** are OCR'd first (below), and each part gets the text of its own scanned pages.
- Each part request downloads only its part from B2, not the whole book.

| Setting | Default | Why |
|---|---|---|
| `MAX_PAGES` | 3000 | Long books are read in parts |
| `READ_PAGES` | 300 | Pages one request reads (about 0.15–0.3 s a page) |
| `READ_TIME_LIMIT` | 240 s | Past this, a request stops with a clear message instead of being cut off by Vercel |
| `MAX_AI_WORDS` | 30,000 | Condensed and Detailed rewrite only the chosen sections, up to this many words (roughly 75–120 pages). The picker shows the count and says when to untick some. Standard has no such limit |
| `MAX_OCR_PAGES` / `OCR_BATCH` | 40 / 30 | Scanned pages take 1–3 s each: past 40, the browser has them read in batches of 30, four requests at a time |
| `MAX_STORAGE_UPLOAD_MB` | 200 | B2 uploads |

Measured on a 2,105-page, 92 MB test book (here, one request at a time; Vercel may be slower):
each 300-page part took 47–88 s and sends back 0.16 MB; the final plan took 57 s (505 sections,
409 chapters) from a 1.1 MB request, and returns a 0.9 MB plan; building one chapter took 0.1 s.
With four parts at once, step 1 is about 3–4 minutes for a book that size.

Step 2 builds only the chosen sections, so its time depends on the slides made, not the book's
length. python-pptx's part naming, which made each slide slower than the last, is replaced with a
linear one in `deck_builder.py`. Plans and parts travel gzip-compressed, keeping them under
Vercel's 4.5 MB request and response limits.

On other hosts (Render, Railway, Fly.io) direct uploads default to 50 MB:

```bash
gunicorn -w 2 --timeout 300 -b 0.0.0.0:8000 app:app
```

Settings: `MAX_UPLOAD_MB`, `MAX_RESPONSE_MB` (0 = no limit), `MAX_STORAGE_UPLOAD_MB`, `MAX_PAGES`,
`READ_TIME_LIMIT`, `MAX_AI_WORDS`.

### Scanned PDFs (OCR)

Pages that are pictures of text (almost no text layer, mostly covered by an image) are read with
OCR before anything else, so chapters, layouts and images work as on a normal PDF. Only those pages
are OCR'd; a mixed PDF keeps its real text elsewhere. `ocr.py` uses the Tesseract engine built into
PyMuPDF, so nothing is installed: it needs only Tesseract's language data, shipped in `tessdata/`
(English, "fast" model, Apache 2.0, 4 MB). Each page is read twice, at 200 dpi for body text and at
50 dpi for display-size headings, which Tesseract misses at high resolution; the text goes back
into the page as an invisible layer, line by line at its real size, so chapter titles are still
recognised by their size.

Up to `MAX_OCR_PAGES` (40) scanned pages are read inside the normal request. A scanned textbook is
read in batches instead: `/plan` answers with the scanned pages split into batches of `OCR_BATCH`
(30), saved as small PDFs beside the upload in B2 so each request downloads only its batch; the
browser sends them to `POST /ocr` four at a time, shows "Reading scanned pages… 120 of 480", and
then asks `/plan` again with the text read (checked on the server: only scanned pages, numbers
clamped, text capped). Measured on a 41-page scanned handbook: 1.7 s a page to OCR, then 0.3 s a
page to read with that text, so a 300-page scanned book takes about 2–3 minutes of OCR and 1.5
minutes of reading. The reading step is bound by `READ_TIME_LIMIT`, which puts the practical
ceiling for a scanned book around 500–700 pages on Vercel. `convert.py` has no time limit and
reads every scanned page in one run.

Limits: English only unless you add languages (`OCR_LANGUAGES`, e.g. `eng+fra`); symbols such as ₦
aren't recognised; stylised display type can be misread; scanned brochures lose some product
layouts because prices and spec tables are harder to tell apart. Settings: `OCR_LANGUAGES`,
`OCR_DPI`, `MAX_OCR_PAGES`, `OCR_TESSDATA`.

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

The tests run the real PDF conversion on three fixtures in `tests/fixtures/`: `sample.pdf` (a
simple report), `brochure.pdf` (tagline heading, letter-spaced labels, repeated pictures) and
`handbook.pdf` (cover, copyright and contents pages, display-size chapter titles, numbered steps
with sub-points, a wide diagram, a quiz). They also cover parsing, image filtering, LLM handling
(HTTP stubbed), page breaking, size control, B2 storage and the Flask routes.
