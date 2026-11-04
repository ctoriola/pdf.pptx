"""
Command-line converter.

    python convert.py report.pdf --mode condensed --theme green -o report.pptx
    python convert.py notes.md --mode standard          # skip Docling, start from Markdown
"""
import argparse
import logging
import tempfile
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from deck_builder import THEMES  # noqa: E402
from slide_plan import MODES  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description='Convert a PDF (or Markdown) file into a PowerPoint deck.')
    ap.add_argument('input', help='a .pdf, or a .md file with images referenced relative to it')
    ap.add_argument('--mode', choices=MODES, default='standard')
    ap.add_argument('--theme', choices=list(THEMES), default='coral')
    ap.add_argument('--title', help='deck title (default: the document title or file name)')
    ap.add_argument('-o', '--output', help='output .pptx path')
    ap.add_argument('--keep', help='folder to keep the intermediate Markdown and images in')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')

    src = Path(args.input)
    if src.suffix.lower() == '.md':
        from pipeline import build_from_markdown, safe_filename
        buffer, title, slides = build_from_markdown(src, args.mode, args.theme, args.title, src, print)
        out = Path(args.output or safe_filename(title) + '.pptx')
        out.write_bytes(buffer.getvalue())
    else:
        from pipeline import convert
        work = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix='pdf2pptx-'))
        path, md, title, slides = convert(src, work, args.mode, args.theme, args.title, print, src.name)
        out = Path(args.output) if args.output else Path(path.name)
        out.write_bytes(path.read_bytes())
        print(f'Markdown: {md}')
    print(f'Wrote {out} ({slides} slides)')


if __name__ == '__main__':
    main()
