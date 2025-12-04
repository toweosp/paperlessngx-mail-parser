import subprocess
from django.conf import settings
from django.template.loader import render_to_string
from django.utils.safestring import SafeText
from django.utils.timezone import is_naive
from django.utils.timezone import make_aware
from documents.parsers import make_thumbnail_from_pdf
from documents.parsers import ParseError
from gotenberg_client import GotenbergClient, SingleFileResponse
from gotenberg_client.constants import A4
from gotenberg_client.options import Measurement
from gotenberg_client.options import MeasurementUnitType
from gotenberg_client.options import PageMarginsType
from humanize import naturalsize
from imap_tools.message import MailMessage, MailAttachment
from paperless_mail.models import MailRule
from paperless_mail.parsers import MailDocumentParser as Parent
from pathlib import Path
from tika_client import TikaClient
from tika_client.data_models import TikaResponse
import magic
import re
import uuid
from documents.utils import run_subprocess
from gotenberg_client.options import Measurement, PdfAFormat


class MailDocumentParser(Parent):
    """
    This parser can be used as an alternative to the default e-mail parser provided by Paperless-ngx.

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

    This parser is based on the default e-mail parser of Paperless-ngx, from which it reuses
    the following functions:

        - Metadata parsing (extract_metadata)
        - Determination of PDF/A version (_settings_to_gotenberg_pdfa())


    Technical note:
        documents.ConsumerPlugin.run only delivers MailRule if parser is instance
        of paperless_mail.parsers.MailDocumentParser

        Therefore this parser's super class needs to be paperless_mail.parsers.MailDocumentParser
        in order to get the mailrule in action.
    """

    # Timeout for gotenberg in seconds. Default of 30s sometimes leads to error "503 Service Unavailable" when parsing mails
    GOTENBERG_TIMEOUT = 600.0

    def parse(
        self,
        document_path: Path,
        mime_type: str,
        file_name=None,
        mailrule_id: int | None = None,
    ) -> None:
        tika_url = settings.TIKA_ENDPOINT
        gotenberg_url = settings.TIKA_GOTENBERG_ENDPOINT

        pdf_layout: MailRule.PdfLayout|None = None
        consumption_scope: MailRule.ConsumptionScope|None = None

        if mailrule_id:
            rule: MailRule = MailRule.objects.get(pk=mailrule_id)
            pdf_layout = MailRule.PdfLayout(rule.pdf_layout)
            consumption_scope = MailRule.ConsumptionScope(
                rule.consumption_scope
            )
        pdf_layout = pdf_layout or settings.EMAIL_PARSE_DEFAULT_LAYOUT

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
                        f"{a.filename} ({naturalsize(a.size, binary=True, format='%.2f')})"
                    )
                header.append(("Attachments", ", ".join(attachments)))

            return header

        def strip_duplicate_newlines(text: str) -> str:
            return re.sub(r"(\n)+", r"\n", text)

        def get_mail_only_content(parsed: MailMessage) -> str:
            ret: str = parsed.text
            if not ret:
                with TikaClient(tika_url=tika_url) as client:
                    response: TikaResponse = client.tika.as_text.from_buffer(
                        parsed.html, "text/html"
                    )
                    ret = response.content if response.content else ""
            return strip_duplicate_newlines(ret)

        def get_mail_and_attachments_content(message_payload) -> str:
            with TikaClient(tika_url=tika_url) as client:
                content: str | None = client.tika.as_text.from_buffer(
                    message_payload
                ).content

                ret = ''
                # if subject of email is present it will be the first line in the content
                if parsed.subject and content:
                    ret_splitted = content.strip().splitlines()
                    if len(ret_splitted) > 1:
                        ret = '\n'.join(ret_splitted[1:])
                return strip_duplicate_newlines(ret)

        def create_txt_header(header: list[tuple[str, str]]) -> str:
            mail_header: str = ""
            for label, value in header:
                mail_header += f"{label}: {value}\n"
            return mail_header

        def create_html_header(header: list[tuple[str, str]]) -> str:
            html_header: SafeText = render_to_string(
                "header_template.html", {"header": header}
            )
            return html_header

        def create_text_mail_pdf(parsed: MailMessage) -> Path:
            text_mail_html: Path = self.tempdir / "text-mail.html"
            text_mail_pdf: Path = self.tempdir / "text-mail.pdf"
            if parsed.text:
                with TikaClient(tika_url=tika_url) as client:
                    txt_content_as_html = (
                        "<tt>" + parsed.text.replace("\n", "<br>") + "</tt>"
                    )
                text_mail_html.write_text(
                    f"{create_html_header(get_header(parsed))}{txt_content_as_html}"
                )

                with GotenbergClient(gotenberg_url, timeout=self.GOTENBERG_TIMEOUT) as client:
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
            html_mail_html: Path = self.tempdir / "html-mail.html"
            html_mail_pdf: Path = self.tempdir / "html-mail.pdf"

            if parsed.html:
                content = parsed.html

                inline_attachments: list[Path] = []
                # include inline attachments
                for a in [
                    x for x in parsed.attachments if x.content_disposition == "inline"
                ]:
                    if a.content_id:
                        inlineAttachment: Path = Path(self.tempdir) / a.content_id
                        inlineAttachment.write_bytes(a.payload)
                        inline_attachments.append(inlineAttachment)

                        # replace content id references with (temporary) filename of inline attachment
                        content = content.replace(
                            f"cid:{a.content_id}", f"{a.content_id}"
                        )

                # remove page css styles in order to combine mail header and content
                # in one page
                content = re.sub(r"\{page:.*?\}", "", content)

                html_mail_html.write_text(
                    create_html_header(get_header(parsed)) + content
                )

                with GotenbergClient(gotenberg_url,timeout=self.GOTENBERG_TIMEOUT) as client:
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

            # Only use attachments which are not inline and not signatures
            real_attachments: list[MailAttachment] = [
                att
                for att in parsed.attachments
                if att.content_disposition == "attachment"
                and att.content_type != "application/x-pkcs7-signature"
            ]

            for attachment in real_attachments:
                filename = (
                    attachment.filename if attachment.filename else str(uuid.uuid4())
                )

                path: Path = self.tempdir / f"{filename}"
                path.write_bytes(attachment.payload)

                # don't trust attachment's content type (octet-stream might be pdf)
                mimetype = magic.from_buffer(attachment.payload, mime=True)


                from paperless_tesseract.signals import get_parser as get_tesseract_parser, tesseract_consumer_declaration
                if mimetype in tesseract_consumer_declaration(None)['mime_types']:                   
                    rasterisedDocumentParser = get_tesseract_parser(self.logging_group) 
                    rasterisedDocumentParser.parse(path,mimetype)
                    if rasterisedDocumentParser.text:
                        self.text += f'\n\n= Content attachment: {filename} =\n' + rasterisedDocumentParser.text

                if mimetype == "application/pdf":
                    pdfs.append(path)
                else:
                    path_pdf: Path = self.tempdir / f"{filename}.pdf"
                    try:
                        with GotenbergClient(gotenberg_url,timeout=self.GOTENBERG_TIMEOUT) as client:
                            with client.libre_office.to_pdf() as route:
                                response: SingleFileResponse = route.convert(path).run()
                                response.to_file(path_pdf)
                                                
                        rasterisedDocumentParser = get_tesseract_parser(self.logging_group) 
                        rasterisedDocumentParser.parse(path_pdf,mimetype)
                        if rasterisedDocumentParser.text:
                            self.text += f'\n\n= Content attachment: {filename} =\n' + rasterisedDocumentParser.text

                        pdfs.append(path_pdf)
                    except:
                        # if we couldn't convert the attachment to pdf
                        # create a one-side pdf with a corresponding note
                        pdfs.append(create_dummy_pdf(f"The attachment (filename: <b>{attachment.filename if attachment.filename else 'unknown'}</b> content-type: <b>{attachment.content_type}</b>) could not be converted to PDF."))
            return pdfs

        def merge_pdfs(pdfs) -> Path:
            tmp_filename = str(uuid.uuid4()) + ".pdf"
            merged_pdf: Path = self.tempdir / tmp_filename

            with GotenbergClient(gotenberg_url,timeout=self.GOTENBERG_TIMEOUT) as client:
                response: SingleFileResponse = client.merge.merge().merge(pdfs).run()
                response.to_file(merged_pdf)
            return merged_pdf

        def create_dummy_pdf(message: str) -> Path:
            dummy_filename = str(uuid.uuid4())
            pdf_path: Path = Path(self.tempdir) / f"{dummy_filename}.pdf"

            with (
                GotenbergClient(gotenberg_url,timeout=self.GOTENBERG_TIMEOUT) as client,
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
                        Path(self.tempdir) / f"{dummy_filename}.html"
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
        self.text = content + mail_content if mail_content else ""

        # finally combine different pdfs to archived file
        pdfs_to_merge: list[Path] = []
        text_pdf: Path
        html_pdf: Path

        if pdf_layout != MailRule.PdfLayout.HTML_ONLY:
            text_pdf = create_text_mail_pdf(parsed)

        if pdf_layout != MailRule.PdfLayout.TEXT_ONLY:
            html_pdf = create_html_mail_pdf(parsed)

        # we include either text or html mail content
        match pdf_layout:
            case MailRule.PdfLayout.TEXT_HTML:  # interpreted as: prefer TEXT over HTML
                if text_pdf.exists():
                    pdfs_to_merge.append(text_pdf)
                elif html_pdf.exists():
                    pdfs_to_merge.append(html_pdf)
            case MailRule.PdfLayout.HTML_TEXT:  # interpreted as: prefer HTML over TEXT
                if html_pdf.exists():
                    pdfs_to_merge.append(html_pdf)
                elif text_pdf.exists():
                    pdfs_to_merge.append(text_pdf)
            case MailRule.PdfLayout.HTML_ONLY:
                if html_pdf.exists():
                    pdfs_to_merge.append(html_pdf)
            case MailRule.PdfLayout.TEXT_ONLY:
                if text_pdf.exists():
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
                    attachments_pdf = create_dummy_pdf(f"The attachments could not be converted to PDF: {e.__str__()}")
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
                final_pdfa_version = Path(self.tempdir) / "final_pdfa.pdf"
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
                except subprocess.CalledProcessError as exception:
                    raise ParseError(f"PDF/A generation failed: {cmd}") from exception
            else:
                raise ParseError(
                    f"Error creating PDF/A. Ghostscript does not support PDF/A version {pdf_a_format}",
                )

        self.archive_path: str = str(final_pdf)

    def get_settings(self) -> None:
        """
        This parser does not implement additional settings yet
        """
        return None

    def get_thumbnail(
        self,
        document_path: Path,
        mime_type: str,
        file_name=None,
    ) -> Path:

        return make_thumbnail_from_pdf(
            self.archive_path,
            self.tempdir,
            self.logging_group,
        )
