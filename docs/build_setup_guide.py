"""
Builds the Backblaze B2 setup guide as Markdown (docs/B2_SETUP.md) and PDF
(docs/B2_Setup_Guide.pdf) from one source. Run: python docs/build_setup_guide.py
Needs reportlab (pip install reportlab); not a runtime dependency of the app.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TITLE = 'Backblaze B2 Setup Guide'
SUBTITLE = 'PDF to Slides: uploads up to 100 MB on Vercel, no card needed'
MD_NAME, PDF_NAME = 'B2_SETUP.md', 'B2_Setup_Guide.pdf'

INTRO = [
    'Vercel rejects any request larger than 4.5 MB. With Backblaze B2, the browser uploads the PDF '
    'straight to B2 using a one-time link from the app, the app converts it, and the finished deck comes back as '
    'a download link. PDFs of up to 100 MB work, and large decks no longer need recompressing.',
    'B2 gives you 10 GB free with no card. The app deletes each PDF as soon as it is converted, and the bucket '
    'clears everything else after about two days, so you will use a tiny fraction of that.',
    'The code is already in the repository. This guide covers the account and settings only, and takes about '
    '20 minutes. Do the parts in order; each step is one action. If a button label differs slightly, look for the '
    'closest match on the same page.',
]

CHECKLIST = [
    ('Bucket name', 'Part 2, step 6', 'pdf-slides-blessing'),
    ('Endpoint', 'Part 2, step 12', 's3.us-east-005.backblazeb2.com'),
    ('Master keyID + applicationKey', 'Part 3, step 6', 'used once in Part 4, not stored'),
    ('App keyID', 'Part 5, step 9', '005a1b2c3d4e5f60000000001'),
    ('App applicationKey', 'Part 5, step 9', 'K005abcd... (shown only once)'),
]

PARTS = [
    {
        'title': 'Part 1: Create a free Backblaze account',
        'steps': [
            'Open <b>backblaze.com</b> in your browser.',
            'Click <b>Sign Up</b>, then choose <b>B2 Cloud Storage</b> (not Computer Backup).',
            'Enter your email address and a password.',
            'When asked for a region, choose <b>US East</b>. It is closest to Vercel\'s Washington servers, '
            'which keeps uploads and conversions fast.',
            'Click <b>Sign Up for B2</b> (or <b>Create Account</b>).',
            'Open the verification email from Backblaze and click the link in it.',
            'Sign back in at <b>secure.backblaze.com</b> if you are not taken there automatically.',
        ],
        'note': 'No card is needed. Without a card on file Backblaze cannot charge you; at worst, a request over a '
                'free daily allowance is refused until the next day.',
    },
    {
        'title': 'Part 2: Create the bucket',
        'steps': [
            'In the left menu, under B2 Cloud Storage, click <b>Buckets</b>.',
            'Click <b>Create a Bucket</b>.',
            'Type a bucket name. It must be unique across all of Backblaze: 6 to 63 characters, letters, '
            'numbers and hyphens only.',
            'If the name is taken, add something personal, for example <b>pdf-slides-blessing</b>.',
            'Check that the name is accepted (no red warning).',
            'Write the bucket name down.',
            'Set <b>Files in Bucket are</b> to <b>Private</b>.',
            'Leave <b>Default Encryption</b> on Disable.',
            'Leave <b>Object Lock</b> on Disable.',
            'Click <b>Create a Bucket</b>.',
            'Find your new bucket on the Buckets page.',
            'In its details, find <b>Endpoint</b> (it looks like s3.us-east-005.backblazeb2.com) and write it '
            'down exactly.',
        ],
    },
    {
        'title': 'Part 3: Get the master key (for setup only)',
        'intro': 'Two bucket settings (browser uploads and auto-delete) can only be set with your master key. '
                 'You use it once in Part 4. The app itself never gets it.',
        'steps': [
            'In the left menu, click <b>Application Keys</b>.',
            'Find the <b>Master Application Key</b> section at the top.',
            'Click <b>Generate New Master Application Key</b>.',
            'Read the warning (it replaces any previous master key) and click <b>Yes!</b> to confirm.',
            'The page shows a <b>keyID</b> and an <b>applicationKey</b>. The applicationKey is shown only once.',
            'Copy both into a temporary note. You will delete the note after Part 4.',
        ],
    },
    {
        'title': 'Part 4: Run the bucket setup script',
        'intro': 'The script sets a CORS rule (so browsers may upload to the bucket through the app\'s one-time '
                 'links) and auto-delete rules for the app\'s folders. It uses only built-in Python, so there is '
                 'nothing to install apart from Python itself.',
        'steps': [
            'Press the <b>Windows key</b>, type <b>cmd</b>, and press Enter to open Command Prompt. '
            '(Mac: open <b>Terminal</b>.)',
            'Type <b>python --version</b> and press Enter. (Mac: <b>python3 --version</b>.)',
            'If you see "Python 3.x", go to step 7.',
            'If not: open <b>python.org/downloads</b> and click the yellow <b>Download Python</b> button.',
            'Run the installer. On the first screen, tick <b>Add python.exe to PATH</b>, then click '
            '<b>Install Now</b>.',
            'When it finishes, close Command Prompt and open a new one (step 1).',
            'In your browser, open the repository on GitHub: <b>github.com/ctoriola/pdf.pptx</b>.',
            'Click the branch dropdown above the file list and choose <b>claude/optimistic-edison-qfmlzo</b> '
            '(or main, once this work is merged).',
            'Open the folder <b>scripts</b>, then the file <b>b2_setup.py</b>.',
            'Click the <b>Download raw file</b> button (the download icon at the top right of the file view).',
            'In Command Prompt, type <b>cd Downloads</b> and press Enter.',
            'Type this, with your bucket name in place of YOUR-BUCKET-NAME, then press Enter: '
            '<font face="Mono">python b2_setup.py --bucket YOUR-BUCKET-NAME</font> (Mac: start with python3).',
            'At "Master application key ID", paste the master keyID and press Enter.',
            'At "Master application key", paste the master applicationKey and press Enter. Nothing appears as '
            'you paste; that is normal.',
            'Check the result says <b>Bucket "your-bucket" is ready</b>.',
            'Delete the temporary note with the master key.',
        ],
        'note': 'For extra safety you can now generate a new master key (Part 3, steps 1 to 4) without saving it. '
                'That cancels the one you just used. You only need a master key again to re-run this script.',
    },
    {
        'title': 'Part 5: Create the app key',
        'intro': 'This key lets the app read and write files in this one bucket only.',
        'steps': [
            'Go back to <b>Application Keys</b>.',
            'Click <b>Add a New Application Key</b>.',
            'Name of Key: type <b>pdf-to-slides-app</b>.',
            'Allow access to Bucket(s): choose <b>your bucket</b> (not "All").',
            'Type of Access: choose <b>Read and Write</b>.',
            'Leave <b>Allow List All Bucket Names</b> unticked.',
            'Leave <b>File name prefix</b> and <b>Duration</b> empty.',
            'Click <b>Create New Key</b>.',
            'Copy the <b>keyID</b> and the <b>applicationKey</b> straight away. The applicationKey is shown only '
            'once; if you lose it, delete this key and create another.',
        ],
        'note': 'Treat the applicationKey like a password: it goes into Vercel only. Never commit it to GitHub, '
                'email it, or paste it into a chat.',
    },
    {
        'title': 'Part 6: Add the settings to Vercel',
        'steps': [
            'Open <b>vercel.com</b> and select the pdf.pptx project.',
            'Click <b>Settings</b>, then <b>Environment Variables</b>.',
            'For each row of the table below: type the <b>Key</b>, paste the <b>Value</b>, leave all '
            'environments ticked, and click <b>Save</b>.',
            'Mark <b>B2_APPLICATION_KEY</b> as <b>Sensitive</b> if that option is shown.',
            'Check the four keys are spelled exactly as shown. A missing or misspelled key makes the app quietly '
            'fall back to 4.4 MB direct uploads.',
            'Add <b>GROQ_API_KEY</b> too if you haven\'t already (for Condensed and Detailed slides).',
            'Open the <b>Deployments</b> tab.',
            'On the latest deployment, click the <b>...</b> menu, then <b>Redeploy</b>, then confirm. New settings '
            'only reach new deployments.',
            'Wait for the deployment to show <b>Ready</b>.',
        ],
        'table': {
            'head': ('Key', 'Value'),
            'rows': [
                ('B2_KEY_ID', 'the app keyID (Part 5)'),
                ('B2_APPLICATION_KEY', 'the app applicationKey (Part 5)'),
                ('B2_BUCKET', 'the bucket name (Part 2)'),
                ('B2_ENDPOINT', 'the endpoint, e.g. s3.us-east-005.backblazeb2.com (Part 2)'),
            ],
        },
    },
    {
        'title': 'Part 7: Test it',
        'steps': [
            'Open your Vercel site.',
            'Under the upload box, check the hint says <b>Up to 100 MB</b>. If it says 4.4 MB, a B2_ setting is '
            'missing: go back to Part 6.',
            'Choose a PDF larger than 5 MB (any report with images).',
            'Pick <b>Standard</b> and click <b>Convert to slides</b>.',
            'Watch the message count up: "Uploading... 37%".',
            'Wait for the deck to download.',
            'In Backblaze, open <b>Buckets</b>, then <b>Browse Files</b> on your bucket.',
            'Check there is an <b>outputs</b> folder with your deck, and no PDF left in <b>uploads</b>.',
        ],
    },
]

TROUBLE = {
    'head': ('What you see', 'Likely cause and fix'),
    'rows': [
        ('Hint says "Up to 4.4 MB"', 'A B2_ setting is missing or misspelled, or you didn\'t redeploy (Part 6).'),
        ('"The upload could not reach storage"', 'The CORS rule is missing. Re-run the script (Part 4). Check the '
                                                 'bucket name you typed matches exactly.'),
        ('"The upload was refused by storage"', 'B2_KEY_ID or B2_APPLICATION_KEY is wrong, or the key isn\'t '
                                                'allowed on this bucket. Create a new app key (Part 5).'),
        ('"Something went wrong" right after the upload', 'Open Vercel > project > Logs. Usually the app key is '
                                                          'Read Only (it must be Read and Write) or B2_ENDPOINT is '
                                                          'wrong.'),
        ('Script says "B2 error (401)"', 'You used the app key, or a typo. The script needs the master key '
                                         '(Part 3).'),
        ('Script says "No bucket named ..."', 'The bucket name is misspelled. Copy it from the Buckets page.'),
        ('"python" is not recognized', 'Python isn\'t installed or wasn\'t added to PATH. Reinstall and tick '
                                       '"Add python.exe to PATH" (Part 4, step 5).'),
        ('The page times out on a very long PDF', 'Vercel stops functions after 300 seconds. Try Standard, or split '
                                                  'the PDF. MAX_PAGES (default 80) caps the length.'),
    ],
}


# ============================================================
# Markdown
# ============================================================
def to_md_inline(text):
    text = re.sub(r'</?b>', '**', text)
    text = re.sub(r'<font face="Mono">(.*?)</font>', r'`\1`', text)
    return text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')


def md_table(table):
    lines = ['| ' + ' | '.join(table['head']) + ' |', '|' + '---|' * len(table['head'])]
    lines += ['| ' + ' | '.join(to_md_inline(c) for c in row) + ' |' for row in table['rows']]
    return '\n'.join(lines)


def build_markdown():
    out = [f'# {TITLE}', '', f'_{SUBTITLE}_', '']
    out += [to_md_inline(p) + '\n' for p in INTRO]
    out += ['## Write these down as you go', '', md_table({'head': ('Value', 'Where you get it', 'Example'),
                                                           'rows': CHECKLIST}), '']
    for part in PARTS:
        out += [f'## {part["title"]}', '']
        if part.get('intro'):
            out += [to_md_inline(part['intro']), '']
        out += [f'{n}. {to_md_inline(s)}' for n, s in enumerate(part['steps'], 1)] + ['']
        if part.get('code'):
            out += ['```', part['code'], '```', '']
        if part.get('table'):
            out += [md_table(part['table']), '']
        if part.get('note'):
            out += ['> ' + to_md_inline(part['note']), '']
    out += ['## Troubleshooting', '', md_table(TROUBLE), '']
    return '\n'.join(out)


# ============================================================
# PDF
# ============================================================
def build_pdf(path):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (KeepTogether, ListFlowable, ListItem, Paragraph, Preformatted,
                                    SimpleDocTemplate, Spacer, Table, TableStyle)

    fonts = Path('/usr/share/fonts/truetype/dejavu')
    if (fonts / 'DejaVuSans.ttf').exists():
        pdfmetrics.registerFont(TTFont('Body', str(fonts / 'DejaVuSans.ttf')))
        pdfmetrics.registerFont(TTFont('Body-Bold', str(fonts / 'DejaVuSans-Bold.ttf')))
        pdfmetrics.registerFont(TTFont('Mono', str(fonts / 'DejaVuSansMono.ttf')))
        pdfmetrics.registerFontFamily('Body', normal='Body', bold='Body-Bold')
        body_font, bold_font, mono_font = 'Body', 'Body-Bold', 'Mono'
    else:
        body_font, bold_font, mono_font = 'Helvetica', 'Helvetica-Bold', 'Courier'

    accent = colors.HexColor('#D44C3F')
    ink = colors.HexColor('#111827')
    muted = colors.HexColor('#6B7280')
    tint = colors.HexColor('#FEF7F6')
    line = colors.HexColor('#E5E7EB')

    s_title = ParagraphStyle('t', fontName=bold_font, fontSize=22, leading=27, textColor=ink, spaceAfter=4)
    s_sub = ParagraphStyle('s', fontName=body_font, fontSize=11, leading=15, textColor=accent, spaceAfter=14)
    s_body = ParagraphStyle('b', fontName=body_font, fontSize=10, leading=14.5, textColor=ink, spaceAfter=7,
                            alignment=TA_LEFT)
    s_h2 = ParagraphStyle('h', fontName=bold_font, fontSize=14, leading=18, textColor=accent, spaceBefore=14,
                          spaceAfter=6)
    s_step = ParagraphStyle('st', parent=s_body, spaceAfter=3)
    s_note = ParagraphStyle('n', parent=s_body, fontSize=9.5, leading=13.5, textColor=ink, backColor=tint,
                            borderPadding=(6, 8, 6, 8), spaceBefore=6, spaceAfter=10, leftIndent=8, rightIndent=8)
    s_cell = ParagraphStyle('c', fontName=body_font, fontSize=9, leading=12, textColor=ink)
    s_cell_head = ParagraphStyle('ch', parent=s_cell, fontName=bold_font, textColor=colors.white)
    s_code = ParagraphStyle('code', fontName=mono_font, fontSize=7.6, leading=10, textColor=ink,
                            backColor=colors.HexColor('#F6F7F9'), borderPadding=(6, 6, 6, 6),
                            spaceBefore=6, spaceAfter=8, leftIndent=6, rightIndent=6)

    def table(spec, widths):
        rows = [[Paragraph(h, s_cell_head) for h in spec['head']]]
        rows += [[Paragraph(c, s_cell) for c in r] for r in spec['rows']]
        t = Table(rows, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), accent),
            ('GRID', (0, 0), (-1, -1), 0.5, line),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F9FAFB')]),
            ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        return t

    width = A4[0] - 36 * mm
    story = [Paragraph(TITLE, s_title), Paragraph(SUBTITLE, s_sub)]
    story += [Paragraph(p, s_body) for p in INTRO]
    story += [Paragraph('Write these down as you go', s_h2),
              table({'head': ('Value', 'Where you get it', 'Example'), 'rows': CHECKLIST},
                    [width * 0.3, width * 0.25, width * 0.45])]
    for part in PARTS:
        head = [Paragraph(part['title'], s_h2)]
        if part.get('intro'):
            head.append(Paragraph(part['intro'], s_body))
        steps = ListFlowable([ListItem(Paragraph(s, s_step), value=n) for n, s in enumerate(part['steps'], 1)],
                             bulletType='1', bulletFontName=bold_font, bulletColor=accent, leftIndent=22,
                             bulletFontSize=10)
        story.append(KeepTogether(head + [steps]) if len(part['steps']) <= 10 else KeepTogether(head))
        if len(part['steps']) > 10:
            story.append(steps)
        if part.get('code'):
            story.append(Preformatted(part['code'], s_code))
        if part.get('table'):
            story.append(Spacer(1, 6))
            story.append(table(part['table'], [width * 0.38, width * 0.62]))
        if part.get('note'):
            story.append(Paragraph(part['note'], s_note))
    story += [Paragraph('Troubleshooting', s_h2), table(TROUBLE, [width * 0.35, width * 0.65])]

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(body_font, 8)
        canvas.setFillColor(muted)
        canvas.drawString(18 * mm, 10 * mm, TITLE)
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f'Page {doc.page}')
        canvas.restoreState()

    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                      bottomMargin=18 * mm, title=TITLE, author='PDF to Slides').build(
        story, onFirstPage=footer, onLaterPages=footer)


if __name__ == '__main__':
    (ROOT / 'docs' / MD_NAME).write_text(build_markdown(), encoding='utf-8')
    build_pdf(ROOT / 'docs' / PDF_NAME)
    print(f'Wrote docs/{MD_NAME} and docs/{PDF_NAME}')
