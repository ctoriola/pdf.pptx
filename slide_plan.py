"""
Slide-type plans: how much text each slide carries and how sections become slides.

  condensed  LLM-summarised; sections are grouped and each group fits on one slide
  standard   full text from the PDF; long sections continue onto extra slides
  detailed   LLM-expanded explanations; long sections continue onto extra slides
"""

MODES = ('condensed', 'standard', 'detailed')
DEFAULT_MODE = 'standard'

PLANS = {
    'condensed': {
        'label': 'Condensed',
        'description': 'AI-summarised. Key points only, one slide per topic.',
        'uses_llm': True,
        'one_slide_per_section': True,
        'words': 70,            # word budget per slide given to the LLM
        'max_sections': 12,     # adjacent sections are grouped down to this many slides
        'agenda': True,
    },
    'standard': {
        'label': 'Standard',
        'description': 'The full text pulled from the PDF, split across as many slides as it needs.',
        'uses_llm': False,
        'one_slide_per_section': False,
        'words': None,
        'max_sections': None,
        'agenda': True,
    },
    'detailed': {
        'label': 'Detailed',
        'description': 'AI-expanded. Each section explained in more depth with context and takeaways.',
        'uses_llm': True,
        'one_slide_per_section': False,
        'words': 220,           # target words per section after expansion
        'max_sections': None,
        'agenda': True,
    },
}


def normalize_mode(value):
    value = (value or '').strip().lower()
    return value if value in PLANS else DEFAULT_MODE


def get_plan(value):
    return {'mode': normalize_mode(value), **PLANS[normalize_mode(value)]}
