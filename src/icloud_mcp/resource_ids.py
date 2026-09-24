"""Optional HMAC-signed opaque DAV resource identifiers."""

import base64
import hashlib
import hmac

from .config import config

PREFIX = "icloud:v1:"


def encode_resource_id(value: str) -> str:
    if not config.RESOURCE_ID_SIGNING_KEY or not value.startswith(("https://", "http://")):
        return value
    payload = base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")
    signature = hmac.new(config.RESOURCE_ID_SIGNING_KEY.encode(), payload.encode(), hashlib.sha256).digest()
    sig = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{PREFIX}{payload}.{sig}"


def decode_resource_id(value: str) -> str:
    if not value.startswith(PREFIX):
        if config.RESOURCE_ID_SIGNING_KEY and not config.ALLOW_LEGACY_URL_IDS and value.startswith(("http://", "https://")):
            raise ValueError("Legacy URL resource IDs are disabled")
        return value
    if not config.RESOURCE_ID_SIGNING_KEY:
        raise ValueError("Signed resource ID support is not configured")
    payload, separator, signature = value[len(PREFIX):].partition(".")
    if not separator:
        raise ValueError("Malformed resource ID")
    expected = hmac.new(config.RESOURCE_ID_SIGNING_KEY.encode(), payload.encode(), hashlib.sha256).digest()
    supplied = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    if not hmac.compare_digest(expected, supplied):
        raise ValueError("Invalid resource ID signature")
    return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()


def encode_result(value):
    if isinstance(value, list):
        return [encode_result(item) for item in value]
    if isinstance(value, dict):
        return {
            key: encode_resource_id(item) if key in {"id", "url"} and isinstance(item, str) else encode_result(item)
            for key, item in value.items()
        }
    return value
