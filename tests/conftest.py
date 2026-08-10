from pathlib import Path

import pytest
from contextlib import contextmanager
from paperless_mail.models import MailRule
from paperlessngx_mail_parser.parsers import MailDocumentParser


@pytest.fixture
def make_mail_parser():
    @contextmanager
    def _make_parser():
        with MailDocumentParser() as parser:
            yield parser

    return _make_parser()


@pytest.fixture
def samples_dir() -> Path:
    return (Path(__file__).parent / "samples").resolve()


@pytest.fixture(
    params=[
        MailRule.PdfLayout.TEXT_HTML,
        MailRule.PdfLayout.HTML_TEXT,
        MailRule.PdfLayout.HTML_ONLY,
        MailRule.PdfLayout.TEXT_ONLY,
    ]
)
def pdf_layout(request):
    return request.param
