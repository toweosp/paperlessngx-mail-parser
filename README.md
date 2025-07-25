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
These installation instructions are for docker based installations. For bare metal installations you have to do analogous steps manually, i.e. copy/link the source folder to your installation folder.  

1. Download current release or clone repository to a _folder_ of your choice.

2. Bind folder `org_toweosp_paperlessngx_mail_parser` to `/usr/src/paperless/src/org_toweosp_paperlessngx_mail_parser` for your Paperless-ngx webserver container. For example when using docker compose:

    ```
    services:
    [...]    
        webserver:
        [...]
            volumes:
            - <folder>/org_toweosp_paperlessngx_mail_parser:/usr/src/paperless/src/org_toweosp_paperlessngx_mail_parser
    ```
3. Add this parser to the `PAPERLESS_APPS` environment variable, e.g. 
   `PAPERLESS_APPS="org_toweosp_paperlessngx_mail_parser.apps.MailparserConfig"`

> **Note on using the PAPERLESS_APPS environment variable**
>
>This is a comma separated list of apps you would like to add to Paperless-ngx. Pay attention to not include any spaces in between when adding more than one app. So use e.g.
>
>`PAPERLESS_APPS="org_toweosp_paperlessngx_mail_parser.apps.MailparserConfig,paperless_my.apps.SpecialConfig"`
>
>instead of
>
>`PAPERLESS_APPS="org_toweosp_paperlessngx_mail_parser.apps.MailparserConfig, paperless_my.apps.SpecialConfig"`
