# Alternative e-mail parser for Paperless-ngx

This parser can be used as an alternative to the default e-mail parser provided by Paperless-ngx.

## Features
* If consumption scope isn't EVERYTHING (i.e. parse mail and attachments separately) include attachments in the archived document where possible. If an attachment can't be converted to pdf, include a corresponding note in the archived version. Signature attachments (mime type: application/x-pkcs7-signature) are always excluded.

* Place a header in front of the pdf containing the text version of the e-mail as well as in front of the html-version.

* Only include either the text or html version of the e-mail in the archived document. The PDF-Layout values for Paperless-ngx PdfLayout.TEXT_HTML and PdfLayout.HTML_TEXT are therefore interpreted as "if available, use text, else use html version" resp. "if available, use html, else use text version".

* Preserve original html e-mail content as far as possible. PDF/A version is created using ghostscript preserving links included in the e-mail.

## Prerequisites
All python modules used by this parser should already be included in your Paperless-ngx installation.

Ghostscript ist used for creating PDF/A version of archived file if requested, see 

https://docs.paperless-ngx.com/configuration/#PAPERLESS_GS_BINARY

https://docs.paperless-ngx.com/configuration/#PAPERLESS_OCR_COLOR_CONVERSION_STRATEGY

## Installation

1. Install using PyPI

    `pip install paperlessngx-mail-parser`

    For docker based installations use custom container initialization as described here: https://docs.paperless-ngx.com/advanced_usage/#custom-container-initialization

    Place a script with the following content in the directory for your container initialization scripts and make it executable:

    ```
    #!/bin/bash
    pip install paperlessngx-mail-parser
    ```

2. Add this parser to the `PAPERLESS_APPS` environment variable, e.g. in your `paperless.conf`:
   `PAPERLESS_APPS="paperlessngx-mail-parser.apps.MailparserConfig"`
