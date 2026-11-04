"""
Builds the Firebase setup guide as Markdown (docs/FIREBASE_SETUP.md) and PDF
(docs/Firebase_Setup_Guide.pdf) from one source. Run: python docs/build_setup_guide.py
Needs reportlab (pip install reportlab); not a runtime dependency of the app.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RULES = (ROOT / 'storage.rules').read_text().strip()

TITLE = 'Firebase Storage Setup Guide'
SUBTITLE = 'PDF to Slides: lifting the 4.4 MB upload limit on Vercel'

INTRO = [
    'Vercel rejects any request larger than 4.5 MB. With Firebase Storage, the browser uploads the PDF '
    'straight to Google\'s storage, sends the app only the file\'s location, and gets the finished deck back as a '
    'download link. Uploads of up to 50 MB work, and large decks no longer need recompressing.',
    'The code is already in the repository. This guide covers the accounts and settings only. '
    'It takes about 20 minutes. Do the parts in order; each step is one action.',
    'Firebase and Google Cloud rename menu items from time to time. If a label doesn\'t match exactly, '
    'look for the closest equivalent on the same page.',
]

CHECKLIST = [
    ('Project ID', 'Part 1, step 6', 'pdf-to-slides-4f2a1'),
    ('Bucket name', 'Part 3, step 9', 'pdf-to-slides-4f2a1.firebasestorage.app'),
    ('apiKey', 'Part 6, step 6', 'AIzaSy...'),
    ('authDomain', 'Part 6, step 6', 'pdf-to-slides-4f2a1.firebaseapp.com'),
    ('appId', 'Part 6, step 6', '1:1234567890:web:abc123...'),
    ('Service-account JSON file', 'Part 7, step 5', 'a downloaded .json file'),
]

PARTS = [
    {
        'title': 'Part 1: Create the Firebase project',
        'steps': [
            'Open <b>console.firebase.google.com</b> in your browser.',
            'Sign in with the Google account you want to own the project.',
            'Click <b>Create a project</b> (or <b>Add project</b>, or <b>Create a Firebase project</b>).',
            'Type a project name, for example <b>pdf-to-slides</b>.',
            'Look under the name field: Firebase shows the <b>project ID</b> it will use (for example pdf-to-slides-4f2a1).',
            'Write the project ID down. You need it in Part 9.',
            'Tick the terms checkbox if asked, then click <b>Continue</b>.',
            'If asked about AI assistance (Gemini), choose either option. It doesn\'t affect this app.',
            'On the Google Analytics screen, switch Analytics <b>off</b>. The app doesn\'t use it.',
            'Click <b>Create project</b>.',
            'Wait for "Your new project is ready", then click <b>Continue</b>.',
        ],
    },
    {
        'title': 'Part 2: Switch the project to the Blaze plan',
        'intro': 'New projects must be on the pay-as-you-go Blaze plan to use Storage. Google\'s free monthly '
                 'allowance still applies, and this app deletes every file within a day, so usage stays tiny.',
        'steps': [
            'In the left sidebar, at the bottom, find the plan label <b>Spark</b>.',
            'Click <b>Upgrade</b> next to it.',
            'Choose <b>Blaze</b> and click <b>Select plan</b> (or <b>Continue</b>).',
            'Click <b>Create a Cloud Billing account</b> if you don\'t already have one.',
            'Choose your country (Nigeria).',
            'Enter your card details and billing address.',
            'Click <b>Confirm</b> or <b>Start my free trial</b>, whichever appears.',
            'When asked to set a budget, enter a small amount, for example <b>1 USD</b>.',
            'Click <b>Continue</b>, then <b>Link Cloud Billing account</b> (or <b>Purchase</b>).',
            'Check that the sidebar label now says <b>Blaze</b>.',
        ],
        'note': 'A budget only sends you alert emails; it does not stop spending. For a personal tool with files '
                'deleted daily, expect a bill of zero or a few cents.',
    },
    {
        'title': 'Part 3: Turn on Storage',
        'steps': [
            'In the left sidebar, open <b>Build</b> (or <b>Databases &amp; Storage</b>).',
            'Click <b>Storage</b>.',
            'Click <b>Get started</b>.',
            'Choose the bucket location <b>US-EAST1</b>. It is close to Vercel\'s Washington servers and '
            'inside Google\'s no-cost region list (US-CENTRAL1 and US-WEST1 also qualify).',
            'Click <b>Continue</b>.',
            'Choose <b>Start in production mode</b>.',
            'Click <b>Create</b> (or <b>Done</b>).',
            'Wait for the Files tab to appear.',
            'At the top of the Files tab you\'ll see a path like <b>gs://pdf-to-slides-4f2a1.firebasestorage.app</b>.',
            'Write down everything after <b>gs://</b>. That is your <b>bucket name</b>.',
        ],
        'note': 'The location cannot be changed later. If you pick the wrong one, create a new project.',
    },
    {
        'title': 'Part 4: Paste the security rules',
        'intro': 'These rules let a browser upload only a PDF under 50 MB, only into its own folder, and stop '
                 'anyone reading files through Firebase. The app itself uses the service account (Part 7), '
                 'which isn\'t bound by these rules.',
        'steps': [
            'Still in Storage, click the <b>Rules</b> tab.',
            'Click inside the editor and select everything (Ctrl+A).',
            'Press Delete so the editor is empty.',
            'Copy the rules below. They are also in the repository file <b>storage.rules</b>.',
            'Paste them into the editor (Ctrl+V).',
            'Click <b>Publish</b>.',
            'Check that the "Rules published" message appears.',
        ],
        'code': RULES,
    },
    {
        'title': 'Part 5: Enable anonymous sign-in',
        'intro': 'Each visitor gets an invisible, temporary identity. The rules use it to keep each upload in its '
                 'owner\'s folder, and the app checks it before touching a file. Visitors never see a login screen.',
        'steps': [
            'In the left sidebar, open <b>Build</b> (or <b>Security</b>) and click <b>Authentication</b>.',
            'Click <b>Get started</b>.',
            'Open the <b>Sign-in method</b> tab.',
            'In the list of providers, click <b>Anonymous</b>.',
            'Switch the <b>Enable</b> toggle on.',
            'Click <b>Save</b>.',
            'Check that Anonymous now shows <b>Enabled</b>.',
        ],
    },
    {
        'title': 'Part 6: Register the web app and copy its config',
        'steps': [
            'Click <b>Project Overview</b> at the top of the left sidebar.',
            'Click the web icon <b>&lt;/&gt;</b> (if apps already exist: <b>Add app</b>, then the web icon).',
            'Type an app nickname, for example <b>pdf-to-slides-web</b>.',
            'Leave <b>Also set up Firebase Hosting</b> unticked.',
            'Click <b>Register app</b>.',
            'A code block appears containing <b>firebaseConfig</b>. Copy these five values into your notes: '
            '<b>apiKey</b>, <b>authDomain</b>, <b>projectId</b>, <b>storageBucket</b>, <b>appId</b>. '
            'Copy only the text inside the quotes.',
            'Ignore messagingSenderId and measurementId. The app doesn\'t need them.',
            'Click <b>Continue to console</b>.',
        ],
        'note': 'These values are public (they end up in every visitor\'s browser), so they\'re safe in Vercel '
                'settings. To find them again: gear icon > Project settings > General > Your apps.',
    },
    {
        'title': 'Part 7: Create the service-account key',
        'intro': 'This key lets the app read uploads and create download links. Treat it like a password.',
        'steps': [
            'Click the gear icon next to Project Overview.',
            'Click <b>Project settings</b>.',
            'Open the <b>Service accounts</b> tab.',
            'Click <b>Generate new private key</b>.',
            'Click <b>Generate key</b>. A .json file downloads.',
            'Move the file somewhere private, outside the project folder.',
            'Never commit it to GitHub, email it, or paste it into a chat.',
        ],
        'note': 'If the key ever leaks: Google Cloud console > IAM &amp; Admin > Service accounts > the '
                'firebase-adminsdk account > Keys > delete it, then generate a new one and update Vercel.',
    },
    {
        'title': 'Part 8: Auto-delete files after one day',
        'intro': 'The app deletes each uploaded PDF as soon as the deck is built. This rule removes everything '
                 'else (finished decks, abandoned uploads) after a day, so the bucket never fills up.',
        'steps': [
            'Open <b>console.cloud.google.com</b> (same Google account).',
            'In the project picker at the top of the page, select your Firebase project.',
            'Open the menu (three lines, top left) and click <b>Cloud Storage</b>, then <b>Buckets</b>.',
            'Click your bucket (the bucket name from Part 3).',
            'Open the <b>Lifecycle</b> tab.',
            'Click <b>Add a rule</b>.',
            'Under action, choose <b>Delete object</b>, then click <b>Continue</b>.',
            'Under conditions, tick <b>Age</b> and enter <b>1</b> (day).',
            'Click <b>Continue</b>, then <b>Create</b>.',
            'Check that the rule appears in the Lifecycle list.',
        ],
        'note': 'Download links expire after 1 hour anyway; this rule removes the files themselves.',
    },
    {
        'title': 'Part 9: Add the settings to Vercel',
        'steps': [
            'Open <b>vercel.com</b> and select the pdf.pptx project.',
            'Click <b>Settings</b>, then <b>Environment Variables</b>.',
            'For each row of the table below: type the <b>Key</b>, paste the <b>Value</b>, leave all '
            'environments ticked (Production, Preview, Development), and click <b>Save</b>.',
            'For FIREBASE_SERVICE_ACCOUNT: open the .json file from Part 7 in Notepad, press Ctrl+A, Ctrl+C, and '
            'paste the whole contents as the value. Mark it <b>Sensitive</b> if that option is shown.',
            'Check that all six FIREBASE_ keys are spelled exactly as shown. A missing or misspelled key makes '
            'the app quietly fall back to 4.4 MB direct uploads.',
            'Add <b>GROQ_API_KEY</b> too if you haven\'t already (for Condensed and Detailed slides).',
            'Open the <b>Deployments</b> tab.',
            'On the latest deployment, click the <b>...</b> menu, then <b>Redeploy</b>, then confirm. '
            'New settings only reach new deployments.',
            'Wait for the deployment to show <b>Ready</b>.',
        ],
        'table': {
            'head': ('Key', 'Value'),
            'rows': [
                ('FIREBASE_API_KEY', 'apiKey (Part 6)'),
                ('FIREBASE_AUTH_DOMAIN', 'authDomain (Part 6)'),
                ('FIREBASE_PROJECT_ID', 'projectId (Part 6)'),
                ('FIREBASE_STORAGE_BUCKET', 'storageBucket, e.g. xxx.firebasestorage.app, without gs://'),
                ('FIREBASE_APP_ID', 'appId (Part 6)'),
                ('FIREBASE_SERVICE_ACCOUNT', 'the entire contents of the .json key file (Part 7)'),
            ],
        },
    },
    {
        'title': 'Part 10: Test it',
        'steps': [
            'Open your Vercel site.',
            'Under the upload box, check the hint says <b>Up to 50 MB</b>. If it says 4.4 MB, a FIREBASE_ '
            'setting is missing: go back to Part 9.',
            'Choose a PDF larger than 5 MB (any report with images).',
            'Pick <b>Standard</b> and click <b>Convert to slides</b>.',
            'Watch the message count up: "Uploading... 37%".',
            'Wait for the deck to download.',
            'In the Firebase console, open <b>Storage</b> > <b>Files</b>.',
            'Check there is an <b>outputs</b> folder with your deck, and that the uploaded PDF is gone from <b>uploads</b>.',
        ],
    },
]

TROUBLE = {
    'head': ('What you see', 'Likely cause and fix'),
    'rows': [
        ('Hint says "Up to 4.4 MB"', 'One of the six FIREBASE_ settings is missing or misspelled, or you didn\'t '
                                     'redeploy (Part 9).'),
        ('"Could not start an upload session"', 'Anonymous sign-in is off (Part 5).'),
        ('"The upload was refused"', 'Rules not published (Part 4), the file isn\'t a PDF, or it is over 50 MB.'),
        ('"Your session has expired"', 'FIREBASE_PROJECT_ID doesn\'t match the project. Copy projectId again.'),
        ('"Something went wrong" right after the upload',
         'Open Vercel > project > Logs. Usually FIREBASE_SERVICE_ACCOUNT was pasted incompletely, or '
         'FIREBASE_STORAGE_BUCKET includes gs://.'),
        ('Download link shows AccessDenied or SignatureDoesNotMatch',
         'The key belongs to a different project. Generate a new key in this project (Part 7).'),
        ('The page times out on a very long PDF', 'Vercel stops functions after 300 seconds. Try Standard, or '
                                                   'split the PDF. MAX_PAGES (default 80) caps the length.'),
    ],
}


# ============================================================
# Markdown
# ============================================================
def to_md_inline(text):
    text = re.sub(r'</?b>', '**', text)
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
    (ROOT / 'docs' / 'FIREBASE_SETUP.md').write_text(build_markdown(), encoding='utf-8')
    build_pdf(ROOT / 'docs' / 'Firebase_Setup_Guide.pdf')
    print('Wrote docs/FIREBASE_SETUP.md and docs/Firebase_Setup_Guide.pdf')
