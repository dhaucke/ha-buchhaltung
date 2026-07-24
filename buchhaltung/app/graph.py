from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


class GraphConfigurationError(RuntimeError):
    pass


class GraphClient:
    """Microsoft Graph app-only sender using a client assertion certificate.

    PyJWT is deliberately avoided in the core application. The production
    container includes cryptography, which is used to sign the RS256 assertion.
    """

    def __init__(self, settings: dict[str, str]):
        self.tenant_id = settings.get("graph_tenant_id", "")
        self.client_id = settings.get("graph_client_id", "")
        self.sender = settings.get("graph_sender", "")
        self.cert_path = Path(settings.get("graph_certificate_path", ""))
        self.key_path = Path(settings.get("graph_private_key_path", ""))

    @staticmethod
    def _b64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    def configured(self) -> bool:
        return bool(
            self.tenant_id
            and self.client_id
            and self.sender
            and self.cert_path.is_file()
            and self.key_path.is_file()
        )

    def _assertion(self) -> str:
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError as exc:
            raise GraphConfigurationError("Python-Paket cryptography fehlt.") from exc

        certificate = x509.load_pem_x509_certificate(self.cert_path.read_bytes())
        private_key = serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
        thumbprint = certificate.fingerprint(hashes.SHA1())
        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT", "x5t": self._b64(thumbprint)}
        claims = {
            "aud": f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
            "iss": self.client_id,
            "sub": self.client_id,
            "jti": str(uuid.uuid4()),
            "nbf": now - 60,
            "exp": now + 600,
        }
        signing_input = (
            self._b64(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + self._b64(json.dumps(claims, separators=(",", ":")).encode())
        )
        signature = private_key.sign(
            signing_input.encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return signing_input + "." + self._b64(signature)

    def _token(self) -> str:
        if not self.configured():
            raise GraphConfigurationError(
                "Microsoft Graph ist noch nicht vollständig eingerichtet."
            )
        body = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "scope": "https://graph.microsoft.com/.default",
                "client_assertion": self._assertion(),
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "grant_type": "client_credentials",
            }
        ).encode()
        request = urllib.request.Request(
            f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())["access_token"]

    def send_pdf(
        self,
        recipient: str,
        subject: str,
        body_html: str,
        filename: str,
        pdf_bytes: bytes,
    ):
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": body_html},
                "toRecipients": [{"emailAddress": {"address": recipient}}],
                "attachments": [
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": filename,
                        "contentType": "application/pdf",
                        "contentBytes": base64.b64encode(pdf_bytes).decode(),
                    }
                ],
            },
            "saveToSentItems": True,
        }
        request = urllib.request.Request(
            f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(self.sender)}/sendMail",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            details = exc.read().decode(errors="replace")
            raise RuntimeError(f"Graph-Fehler {exc.code}: {details[:500]}") from exc

