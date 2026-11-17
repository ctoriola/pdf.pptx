"""
Command-line converter.

    python convert.py report.pdf --mode condensed --theme mint -o report.pptx
    python convert.py report.pdf --list                    # show the sections and their ids
    python convert.py report.pdf --sections 0,3-7,12       # build only those sections
    python convert.py notes.md --mode standard             # skip PDF parsing, start from Markdown
"""
import argparse
import logging
import tempfile
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from deck_builder import THEMES  # noqa: E402
from slide_plan import MODES  # noqa: E402


def parse_ids(text):
    ids = set()
    for part in (text or '').split(','):
        part = part.strip()
        if '-' in part:
            a, b = part.split('-', 1)
            ids.update(range(int(a), int(b) + 1))
        elif part:
            ids.add(int(part))
    return sorted(ids)


def main():
    ap = argparse.ArgumentParser(description='Convert a PDF (or Markdown) file into a PowerPoint deck.')
    ap.add_argument('input', help='a .pdf, or a .md file with images referenced relative to it')
    ap.add_argument('--mode', choices=MODES, default='standard')
    ap.add_argument('--theme', choices=list(THEMES), default='coral')
    ap.add_argument('--title', help='deck title (default: the document title or file name)')
    ap.add_argument('--list', action='store_true', help='list the chapters and sections, then stop')
    ap.add_argument('--sections', help='only these section ids, e.g. 0,3-7,12 (see --list)')
    ap.add_argument('-o', '--output', help='output .pptx path')
    ap.add_argument('--keep', help='folder to keep the intermediate Markdown and images in')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')

    from pipeline import build_from_plan, outline, plan_from_markdown, read_pdf, safe_filename
    src = Path(args.input)
    work = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix='pdf2pptx-'))
    md_path = src if src.suffix.lower() == '.md' else read_pdf(src, work, args.mode, print)
    plan = plan_from_markdown(md_path, args.mode, args.title, src.name, print)
    if args.list:
        for row in outline(plan, md_path.parent):
            indent = '' if row['chapter'] or row['group'] is None else '    '
            label = 'CHAPTER ' if row['chapter'] else ''
            print(f'{row["id"]:>4}  {indent}{label}{row["name"]}  ({row["slides"]} slides{", " + row["layout"] if row["layout"] else ""})')
        return
    selected = parse_ids(args.sections) if args.sections else None
    buffer, title, slides = build_from_plan(plan, md_path.parent, args.mode, args.theme, selected, print)
    out = Path(args.output or safe_filename(title) + '.pptx')
    out.write_bytes(buffer.getvalue())
    if src.suffix.lower() != '.md':
        print(f'Markdown: {md_path}')
    print(f'Wrote {out} ({slides} slides)')


if __name__ == '__main__':
    main()
