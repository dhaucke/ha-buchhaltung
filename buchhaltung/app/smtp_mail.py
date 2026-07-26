from __future__ import annotations

import html
import smtplib
import ssl
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


class SmtpConfigurationError(RuntimeError):
    pass


class SmtpClient:
    """Plain SMTP sender, offered as an alternative to Microsoft Graph for anyone
    without an M365 tenant (e.g. a mailbox at a regular hosting/e-mail provider)."""

    def __init__(self, settings: dict[str, str]):
        self.host = settings.get("smtp_host", "").strip()
        try:
            self.port = int(settings.get("smtp_port") or 587)
        except ValueError:
            self.port = 587
        self.username = settings.get("smtp_username", "").strip()
        self.password = settings.get("smtp_password", "")
        self.sender = settings.get("smtp_sender", "").strip() or settings.get("email", "").strip()
        self.encryption = settings.get("smtp_encryption", "starttls")

    def configured(self) -> bool:
        return bool(self.host and self.port and self.sender)

    def _connect(self) -> smtplib.SMTP:
        if not self.configured():
            raise SmtpConfigurationError("SMTP ist noch nicht vollständig eingerichtet.")
        try:
            if self.encryption == "ssl":
                server = smtplib.SMTP_SSL(
                    self.host, self.port, timeout=20, context=ssl.create_default_context()
                )
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=20)
                if self.encryption == "starttls":
                    server.starttls(context=ssl.create_default_context())
            if self.username:
                server.login(self.username, self.password)
        except (smtplib.SMTPException, OSError) as exc:
            raise RuntimeError(f"SMTP-Verbindung fehlgeschlagen: {exc}") from exc
        return server

    def test_authentication(self) -> None:
        """Validate host/credentials without sending mail."""
        server = self._connect()
        server.quit()

    def send_mail(
        self,
        recipient: str,
        subject: str,
        body_html: str,
        filename: str | None = None,
        pdf_bytes: bytes | None = None,
    ):
        message = MIMEMultipart()
        message["Subject"] = subject
        message["From"] = self.sender
        message["To"] = recipient
        message.attach(MIMEText(body_html, "html"))
        if filename and pdf_bytes is not None:
            attachment = MIMEApplication(pdf_bytes, _subtype="pdf")
            attachment.add_header("Content-Disposition", "attachment", filename=filename)
            message.attach(attachment)
        server = self._connect()
        try:
            server.sendmail(self.sender, [recipient], message.as_string())
        except smtplib.SMTPException as exc:
            raise RuntimeError(f"SMTP-Fehler beim Versand: {exc}") from exc
        finally:
            server.quit()

    def send_pdf(
        self,
        recipient: str,
        subject: str,
        body_html: str,
        filename: str,
        pdf_bytes: bytes,
    ):
        return self.send_mail(recipient, subject, body_html, filename, pdf_bytes)

    def send_test_email(self, recipient: str):
        return self.send_mail(
            recipient,
            "Buchhaltung – Testmail",
            "<p>Dies ist eine Testmail der Buchhaltung-App über SMTP.</p>"
            f"<p>Gesendet als <b>{html.escape(self.sender)}</b>.</p>"
            "<p>Wenn Sie diese Nachricht erhalten, funktioniert der Mailversand.</p>",
        )
