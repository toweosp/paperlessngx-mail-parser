def get_parser(*args, **kwargs):
    from org_toweosp_paperlessngx_mail_parser.parsers import MailDocumentParser

    return MailDocumentParser(*args, **kwargs)

def consumer_declaration(sender, **kwargs):
    return {
        "parser": get_parser,
        "weight": 30,
        "mime_types": {
            "message/rfc822": ".eml",
        },
    }
