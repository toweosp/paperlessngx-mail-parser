import re
from subprocess import CalledProcessError
from uuid import uuid4
from pathlib import Path
from magic import from_buffer
from django.template import Context, Template
from django.conf import settings
from django.utils.timezone import is_naive, make_aware
from documents.parsers import ParseError
from documents.utils import run_subprocess
from gotenberg_client import GotenbergClient, SingleFileResponse
from gotenberg_client.constants import A4
from gotenberg_client.options import (
    Measurement,
    MeasurementUnitType,
    PageMarginsType,
    PdfAFormat,
)
from humanize import naturalsize
from imap_tools.message import MailAttachment, MailMessage
from paperless.parsers.tesseract import _SUPPORTED_MIME_TYPES as tesseract_mime_types
from paperless.parsers.tesseract import RasterisedDocumentParser
from paperless_mail.models import MailRule
from paperless.parsers.mail import MailDocumentParser as NativeMailDocumentParser
from tika_client import TikaClient
from tika_client.data_models import TikaResponse


class MailDocumentParser(NativeMailDocumentParser):
    """
    This parser is based on the default e-mail parser of Paperless-ngx and can be used as an alternative to it.

    Features:

        If consumption scope isn't EVERYTHING (i.e. parse mail and attachments separately)
        include attachments in the archived document where possible. If an attachment can't
        be converted to pdf, include a corresponding note in the archived version.

        Place a header in front of the pdf containing the text version of the e-mail
        as well as in front of the html-version.

        Only include either the text or html version of the e-mail in the archived document.
        The PDF-Layout values for Paperless-ngx PdfLayout.TEXT_HTML and PdfLayout.HTML_TEXT
        are therefore interpreted as "if available, use text, else use html version" resp.
        "if available, use html, else use text version".

        Preserve original html e-mail content as far as possible.
    """

    name = "Paperless-ngx mail parser"
    version = "3.0.0"
    author = "toweosp"
    url = "https://github.com/toweosp/paperlessngx-mail-parser"

    # Timeout for gotenberg in seconds. Default of 30s sometimes leads to error "503 Service Unavailable" when parsing mails
    GOTENBERG_TIMEOUT = 600.0

    @classmethod
    def score(
        cls,
        mime_type: str,
        filename: str,
        path: "Path | None" = None,
    ) -> int | None:
        return 30

    def parse(
        self,
        document_path: Path,
        mime_type: str,
        *,
        produce_archive: bool = True,
    ) -> None:
        tika_url = settings.TIKA_ENDPOINT
        gotenberg_url = settings.TIKA_GOTENBERG_ENDPOINT

        pdf_layout: MailRule.PdfLayout | None = None
        consumption_scope: MailRule.ConsumptionScope | None = None

        if self._mailrule_id:
            rule: MailRule = MailRule.objects.get(pk=self._mailrule_id)
            pdf_layout = MailRule.PdfLayout(rule.pdf_layout)
            consumption_scope = MailRule.ConsumptionScope(rule.consumption_scope)
        pdf_layout = pdf_layout or settings.EMAIL_PARSE_DEFAULT_LAYOUT

        def clean_filename(filename: str) -> str:
            return filename.replace("/", "_")

        def get_header(parsed: MailMessage) -> list[tuple[str, str]]:
            header: list[tuple[str, str]] = []
            header.append(
                (
                    "From",
                    (
                        parsed.from_values.full
                        if parsed.from_values
                        else "(NO SENDER PROVIDED)"
                    ),
                )
            )
            header.append(("Subject", parsed.subject))
            header.append(("To", ",".join([x.full for x in parsed.to_values])))

            if parsed.cc_values:
                header.append(("CC", ",".join([x.full for x in parsed.cc_values])))

            if is_naive(parsed.date):
                date = make_aware(parsed.date)
            else:
                date = parsed.date

            header.append(("Date", date.astimezone().strftime("%d.%m.%Y %H:%M")))

            # Only use attachments which are not inline and not signatures
            real_attachments: list[MailAttachment] = [
                att
                for att in parsed.attachments
                if att.content_disposition == "attachment"
                and att.content_type != "application/x-pkcs7-signature"
            ]

            if real_attachments:
                attachments: list[str] = []
                for a in real_attachments:
                    attachments.append(
                        f"{clean_filename(a.filename)} ({naturalsize(a.size, binary=True, format='%.2f')})"
                    )
                header.append(("Attachments", ", ".join(attachments)))

            return header

        def strip_duplicate_newlines(text: str) -> str:
            return re.sub(r"(\n)+", r"\n", text)

        def get_mail_only_content(parsed: MailMessage) -> str:
            ret: str = ""
            if parsed.text:
                ret = parsed.text
            elif parsed.html:
                with TikaClient(tika_url=tika_url) as client:
                    response: TikaResponse = client.tika.as_text.from_buffer(
                        parsed.html, "text/html"
                    )
                    ret = response.content if response.content else ""
            return strip_duplicate_newlines(ret)

        def create_txt_header(header: list[tuple[str, str]]) -> str:
            mail_header: str = ""
            for label, value in header:
                mail_header += f"{label}: {value}\n"
            return mail_header

        def create_html_header(header: list[tuple[str, str]]) -> str:
            template: Template = Template("""
                {% autoescape on %}
                <html>
                <style>
                    .toweosp {
                        background-color: lightgray;
                        border: 1px solid #888;
                        border-collapse: separate;
                        border-spacing: 2px;
                        font-family: monospace;
                    }

                    .toweosp td {
                        padding: 2px;
                    }

                    .toweosp td:first-child {
                        color: gray;
                        text-align: right;
                    }

                    .toweosp td:nth-child(2) {
                        color: black;
                    }

                    .toweosp tr:nth-child(2) td:nth-child(2) {
                        font-weight: bold;
                    }
                    
                </style>

                <head>
                    <meta charset="UTF-8">
                </head>

                <body>

                    <table class="toweosp" width="100%">
                    {% for label, value in header %}
                    <tr>
                        <td>{{ label }}</td>
                        <td>{{ value }}</td>
                    </tr>
                    {% endfor %}
                    </table>
                    <p></p>
                </body>
                </html>
                {% endautoescape %}                
            """)

            html_header = template.render(Context({"header": header}))
            return html_header

        def create_text_mail_pdf(parsed: MailMessage) -> Path:
            text_mail_html: Path = self._tempdir / "text-mail.html"
            text_mail_pdf: Path = self._tempdir / "text-mail.pdf"

            with TikaClient(tika_url=tika_url) as client:
                txt_content_as_html = (
                    "<tt>" + parsed.text.replace("\n", "<br>")
                    if parsed.text
                    else "" + "</tt>"
                )
            text_mail_html.write_text(
                f"{create_html_header(get_header(parsed))}{txt_content_as_html}"
            )

            with GotenbergClient(
                gotenberg_url, timeout=self.GOTENBERG_TIMEOUT
            ) as client:
                with client.chromium.html_to_pdf() as route:
                    # Set page size, margins
                    route.margins(
                        PageMarginsType(
                            top=Measurement(0.1, MeasurementUnitType.Inches),
                            bottom=Measurement(0.1, MeasurementUnitType.Inches),
                            left=Measurement(0.1, MeasurementUnitType.Inches),
                            right=Measurement(0.1, MeasurementUnitType.Inches),
                        ),
                    ).size(A4).scale(1.0)

                    response: SingleFileResponse = route.index(text_mail_html).run()
                    response.to_file(text_mail_pdf)
            return text_mail_pdf

        def create_html_mail_pdf(parsed: MailMessage):
            html_mail_html: Path = self._tempdir / "html-mail.html"
            html_mail_pdf: Path = self._tempdir / "html-mail.pdf"

            if parsed.html:
                content = parsed.html

                inline_attachments: list[Path] = []
                # include inline attachments
                for a in [
                    x for x in parsed.attachments if x.content_disposition == "inline"
                ]:
                    if a.content_id:
                        content_id_flatened = a.content_id.replace("/", "__")
                        inlineAttachment: Path = (
                            Path(self._tempdir) / content_id_flatened
                        )
                        inlineAttachment.write_bytes(a.payload)
                        inline_attachments.append(inlineAttachment)

                        # replace content id references with (temporary) filename of inline attachment
                        content = content.replace(
                            f"cid:{a.content_id}", f"{content_id_flatened}"
                        )

                # remove page css styles in order to combine mail header and content
                # in one page
                content = re.sub(r"\{page:.*?\}", "", content)

                html_mail_html.write_text(
                    create_html_header(get_header(parsed)) + content if content else ""
                )

                with GotenbergClient(
                    gotenberg_url, timeout=self.GOTENBERG_TIMEOUT
                ) as client:
                    with client.chromium.html_to_pdf() as route:
                        # Set page size, margins
                        route.margins(
                            PageMarginsType(
                                top=Measurement(0.1, MeasurementUnitType.Inches),
                                bottom=Measurement(0.1, MeasurementUnitType.Inches),
                                left=Measurement(0.1, MeasurementUnitType.Inches),
                                right=Measurement(0.1, MeasurementUnitType.Inches),
                            ),
                        ).size(A4).scale(1.0)

                        r = route.index(html_mail_html)
                        if inline_attachments:
                            for y in inline_attachments:
                                r = r.resource(y)

                        response: SingleFileResponse = r.run()

                        response.to_file(html_mail_pdf)
            return html_mail_pdf

        def create_attachments_pdfs(parsed: MailMessage) -> list[Path]:
            pdfs: list[Path] = []

            # Only include attachments which are not inline - expect inline attachments without content_id - and not signatures
            real_attachments: list[MailAttachment] = [
                att
                for att in parsed.attachments
                if (
                    att.content_disposition == "attachment"
                    or (att.content_disposition == "inline" and not att.content_id)
                )
                and att.content_type != "application/x-pkcs7-signature"
            ]

            for attachment in real_attachments:
                filename = (
                    clean_filename(attachment.filename)
                    if attachment.filename
                    else str(uuid4())
                )

                path: Path = self._tempdir / f"{filename}"
                path.write_bytes(attachment.payload)

                # don't trust attachment's content type (octet-stream might be pdf)
                mimetype = from_buffer(attachment.payload, mime=True)

                original_attachment_parsed = False
                rasterisedDocumentParser = RasterisedDocumentParser()
                if mimetype in tesseract_mime_types:
                    rasterisedDocumentParser.parse(path, mimetype)
                    if rasterisedDocumentParser.text:
                        self._text = (self._text if self._text else "") + (
                            f"\n\n= Content attachment: {filename} =\n"
                            + rasterisedDocumentParser.text
                        )
                        original_attachment_parsed = True

                if mimetype == "application/pdf":
                    pdfs.append(path)
                else:
                    path_pdf: Path = self._tempdir / f"{filename}.pdf"
                    try:
                        with GotenbergClient(
                            gotenberg_url, timeout=self.GOTENBERG_TIMEOUT
                        ) as client:
                            with client.libre_office.to_pdf() as route:
                                response: SingleFileResponse = route.convert(path).run()
                                response.to_file(path_pdf)
                                pdfs.append(path_pdf)
                        if not original_attachment_parsed:
                            # assuming mime-type "application/pdf" is/remains in paperless-ngx.parsers.tesseract._SUPPORTED_MIME_TYPES
                            rasterisedDocumentParser.parse(path_pdf, "application/pdf")
                            if rasterisedDocumentParser.text:
                                self._text = (self._text if self._text else "") + (
                                    f"\n\n= Content attachment: {filename} =\n"
                                    + rasterisedDocumentParser.text
                                )
                    except Exception:
                        # if we couldn't convert the attachment to pdf
                        # create a one-side pdf with a corresponding note
                        pdfs.append(
                            create_dummy_pdf(
                                f"The attachment (filename: <b>{attachment.filename if attachment.filename else 'unknown'}</b> content-type: <b>{attachment.content_type}</b>) could not be converted to PDF."
                            )
                        )
            return pdfs

        def merge_pdfs(pdfs) -> Path:
            tmp_filename = str(uuid4()) + ".pdf"
            merged_pdf: Path = self._tempdir / tmp_filename

            with GotenbergClient(
                gotenberg_url, timeout=self.GOTENBERG_TIMEOUT
            ) as client:
                response: SingleFileResponse = client.merge.merge().merge(pdfs).run()
                response.to_file(merged_pdf)
            return merged_pdf

        def create_dummy_pdf(message: str) -> Path:
            dummy_filename = str(uuid4())
            pdf_path: Path = Path(self._tempdir) / f"{dummy_filename}.pdf"

            with (
                GotenbergClient(
                    gotenberg_url, timeout=self.GOTENBERG_TIMEOUT
                ) as client,
                client.chromium.html_to_pdf() as route,
            ):
                try:
                    # Set page size, margins
                    route.margins(
                        PageMarginsType(
                            top=Measurement(0.1, MeasurementUnitType.Inches),
                            bottom=Measurement(0.1, MeasurementUnitType.Inches),
                            left=Measurement(0.1, MeasurementUnitType.Inches),
                            right=Measurement(0.1, MeasurementUnitType.Inches),
                        ),
                    ).size(A4).scale(1.0)

                    index_file_path: Path = (
                        Path(self._tempdir) / f"{dummy_filename}.html"
                    )

                    index_file_path.write_text(message)

                    response: SingleFileResponse = route.index(index_file_path).run()
                    pdf_path.write_bytes(response.content)

                    return pdf_path
                except Exception as err:
                    raise ParseError(
                        f"Error while creating dummy PDF: {err}",
                    ) from err

        message_payload: bytes = document_path.read_bytes()

        parsed = MailMessage.from_bytes(message_payload)

        # set document created date
        if is_naive(parsed.date):
            self.date = make_aware(parsed.date)
        else:
            self.date = parsed.date

        content = create_txt_header(get_header(parsed))
        mail_content = get_mail_only_content(parsed)
        self._text = content + mail_content if mail_content else ""

        # finally combine different pdfs to archived file
        pdfs_to_merge: list[Path] = []
        text_pdf: Path | None = None
        html_pdf: Path | None = None

        if pdf_layout != MailRule.PdfLayout.HTML_ONLY and parsed.text:
            text_pdf = create_text_mail_pdf(parsed)

        if pdf_layout != MailRule.PdfLayout.TEXT_ONLY and parsed.html:
            html_pdf = create_html_mail_pdf(parsed)

        # we include either text or html mail content
        match pdf_layout:
            case MailRule.PdfLayout.TEXT_HTML:  # interpreted as: prefer TEXT over HTML
                if text_pdf:
                    pdfs_to_merge.append(text_pdf)
                elif html_pdf:
                    pdfs_to_merge.append(html_pdf)
            case MailRule.PdfLayout.HTML_TEXT:  # interpreted as: prefer HTML over TEXT
                if html_pdf:
                    pdfs_to_merge.append(html_pdf)
                elif text_pdf:
                    pdfs_to_merge.append(text_pdf)
            case MailRule.PdfLayout.HTML_ONLY:
                if html_pdf:
                    pdfs_to_merge.append(html_pdf)
            case MailRule.PdfLayout.TEXT_ONLY:
                if text_pdf:
                    pdfs_to_merge.append(text_pdf)

        final_pdf: Path = merge_pdfs(pdfs_to_merge)
        if consumption_scope != MailRule.ConsumptionScope.EVERYTHING:
            pdfs: list[Path] = create_attachments_pdfs(parsed)
            if pdfs:
                # If we cannot merge attachments (e.g.because they are signed) we include a note after the e-mail text
                attachments_pdf: Path
                try:
                    attachments_pdf = merge_pdfs(pdfs)
                except Exception as e:
                    attachments_pdf = create_dummy_pdf(
                        f"The attachments could not be converted to PDF: {e.__str__()}"
                    )
                final_pdf = merge_pdfs([final_pdf, attachments_pdf])

        # Convert merged document to PDF/A if requested
        # using ghostscript as there are problems converting some
        # PDFs using gotenberg's pdfa routes.
        pdf_a_format = self._settings_to_gotenberg_pdfa()
        if pdf_a_format is not None:
            match pdf_a_format:
                case PdfAFormat.A2b:
                    pdfa_number = 2
                case PdfAFormat.A3b:
                    pdfa_number = 3
            if pdfa_number:
                final_pdfa_version = Path(self._tempdir) / "final_pdfa.pdf"
                cmd = [
                    settings.GS_BINARY,
                    "-q",
                    f"-dPDFA={pdfa_number}",
                    "-dBATCH",
                    "-dNOPAUSE",
                    f"-sColorConversionStrategy={settings.OCR_COLOR_CONVERSION_STRATEGY}",
                    "-sDEVICE=pdfwrite",
                    "-o",
                    final_pdfa_version,
                    final_pdf,
                ]
                final_pdf = final_pdfa_version
                try:
                    run_subprocess(cmd)
                except CalledProcessError as exception:
                    raise ParseError(f"PDF/A generation failed: {cmd}") from exception
            else:
                raise ParseError(
                    f"Error creating PDF/A. Ghostscript does not support PDF/A version {pdf_a_format}",
                )

        self._archive_path = final_pdf

    @property
    def can_produce_archive(self) -> bool:
        return True
