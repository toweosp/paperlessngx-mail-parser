from django.apps import AppConfig
from org_toweosp_paperlessngx_mail_parser.signals import consumer_declaration

class MailparserConfig(AppConfig):
    name = "org_toweosp_paperlessngx_mail_parser"

    def ready(self):
        from documents.signals import document_consumer_declaration
        document_consumer_declaration.connect(consumer_declaration)
        AppConfig.ready(self)
