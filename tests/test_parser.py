from contextlib import _GeneratorContextManager
from pathlib import Path

from pdf2image import convert_from_path
import numpy as np
from datetime import datetime, timezone

import pytest
from paperless.parsers import ParserContext
from paperlessngx_mail_parser.parsers import MailDocumentParser
from paperless_mail.models import MailRule


def test_mimetypes(make_mail_parser: _GeneratorContextManager):
    with make_mail_parser as parser:
        assert parser.supported_mime_types() == {
            "message/rfc822": ".eml",
        }


def test_score():
    assert MailDocumentParser.score("message/rfc822", "") == 30


def test_can_produce_archive(make_mail_parser: _GeneratorContextManager):
    with make_mail_parser as parser:
        assert parser.can_produce_archive


@pytest.mark.parametrize(
    "filename",
    [
        "txt_content_attachment",
        "txt_no_content_attachment",
    ],
)
@pytest.mark.django_db
def test_consumption_scope(
    make_mail_parser: _GeneratorContextManager,
    samples_dir: Path,
    filename: str,
    monkeypatch: pytest.MonkeyPatch,
):

    class Monkey:
        def __init__(self):
            self.pdf_layout = 1
            self.consumption_scope = MailRule.ConsumptionScope.EVERYTHING

    def monkey_get(pk):
        return Monkey()

    monkeypatch.setattr(MailRule.objects, "get", monkey_get)

    with make_mail_parser as parser:
        sample: Path = Path(f"{samples_dir}/{filename}.eml")
        parser.configure(ParserContext(mailrule_id=1))
        parser.parse(document_path=sample, mime_type="message/rfc822")
        gen_pages = convert_from_path(parser._archive_path)
        
        # PDF contains only 1 page (no attachment)
        assert len(gen_pages) == 1


@pytest.mark.parametrize(
    "filename",
    [
        "html_content_attachment",
        "html_content_no_attachment",
        "html_no_content_attachment",
        "html_no_content_no_attachment",
        "txt_content_attachment",
        "txt_content_no_attachment",
        "txt_no_content_attachment",
        "txt_no_content_no_attachment",
    ],
)
@pytest.mark.django_db
def test_mail_attachment_pdflayout(
    make_mail_parser: _GeneratorContextManager,
    samples_dir: Path,
    filename: str,
    monkeypatch: pytest.MonkeyPatch,
    pdf_layout,
):

    class Monkey:
        def __init__(self, pdf_layout):
            self.pdf_layout = pdf_layout
            self.consumption_scope = MailRule.ConsumptionScope.EML_ONLY

    def monkey_get(pk):
        return Monkey(pdf_layout)

    monkeypatch.setattr(MailRule.objects, "get", monkey_get)

    with make_mail_parser as parser:
        sample: Path = Path(f"{samples_dir}/{filename}.eml")

        parser.configure(ParserContext(mailrule_id=1))

        parser.parse(document_path=sample, mime_type="message/rfc822")

        expected_pdf = Path(f"{samples_dir}/{filename}-{pdf_layout}.pdf")

        gen_pages = convert_from_path(parser._archive_path)
        ref_pages = convert_from_path(expected_pdf)

        assert len(gen_pages) == len(ref_pages)

        for gen_page, ref_page in zip(gen_pages, ref_pages):
            assert gen_page.size == ref_page.size

            gen_arr = np.array(gen_page)
            ref_arr = np.array(ref_page)

            difference = np.sum(gen_arr != ref_arr)
            assert difference == 0, f"{difference} pixel differences"

@pytest.mark.django_db
def test_get_date(
    make_mail_parser: _GeneratorContextManager,
    samples_dir: Path,
):
    with make_mail_parser as parser:
        sample: Path = Path(f"{samples_dir}/txt_content_no_attachment.eml")
        parser.parse(document_path=sample, mime_type="message/rfc822")
        assert parser.get_date() == datetime(2026, 8, 2, 13, 21, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "filename,content",
    [
        (
            "html_content_no_attachment",
            "From: Alice Example <alice@example.com>\nSubject: test mail\nTo: Bob Example <bob@example.com>\nDate: 03.08.2026 19:57\nformatted text\n",
        ),
        (
            "txt_content_no_attachment",
            "From: Alice Example <alice@example.com>\nSubject: Test message\nTo: Bob Example <bob@example.com>\nDate: 02.08.2026 13:21\nTestmail",
        ),
    ],
)
@pytest.mark.django_db
def test_get_text(
    make_mail_parser: _GeneratorContextManager, samples_dir: Path, filename, content
):
    with make_mail_parser as parser:
        sample: Path = Path(f"{samples_dir}/{filename}.eml")
        parser.parse(document_path=sample, mime_type="message/rfc822")
        assert parser.get_text() == content
