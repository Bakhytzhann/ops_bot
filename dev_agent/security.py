import logging
import re

from dotenv import dotenv_values

from .config import ROOT


def authorized(update, admins):
    return bool(
        update.effective_user
        and update.effective_chat
        and update.effective_chat.type == "private"
        and update.effective_user.id in admins
    )


class Redactor:
    def __init__(self):
        self.secrets = [
            str(v)
            for k, v in dotenv_values(ROOT / ".env").items()
            if v and any(s in k.upper() for s in ("TOKEN", "KEY", "SECRET", "PASSWORD"))
        ]

    def __call__(self, value):
        value = str(value)
        for secret in sorted(self.secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{20,}", "[REDACTED]", value)
        value = re.sub(r"(https?://)[^/\s@]+@", r"\1[REDACTED]@", value)
        return re.sub(
            r"(?im)((?:[\w-]*(?:token|password|secret|api_key))[\w-]*[ \t]*[=:][ \t]*)[^\s,]+",
            r"\1[REDACTED]",
            value,
        )

    def contains_secret(self, text):
        return bool(
            any(secret in text for secret in self.secrets)
            or re.search(r"\b\d{6,}:[A-Za-z0-9_-]{20,}", text)
            or re.search(r"\bsk-[A-Za-z0-9_-]{20,}", text)
            or re.search(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY", text)
        )


class SecretFilter(logging.Filter):
    def __init__(self, redact):
        super().__init__()
        self.redact = redact

    def filter(self, record):
        record.msg = self.redact(record.getMessage())
        record.args = ()
        record.exc_info = None
        return True
