"""Opaque-to-clients, self-contained school-coverage coordinates.

The signature identifies a selected source key; it never grants access. Consumers
must still obtain current Thin authority and validate the requested operation.
"""

import base64
import hmac
import re
from hashlib import sha256
from uuid import UUID

_FAMILIES = frozenset(("f2", "f3", "f4", "f5", "f6"))
_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_HEX = re.compile(r"^[0-9a-f]{64}$")
_ENCODED = re.compile(r"^[A-Za-z0-9_-]+$")
_DOMAIN = b"school-facts-school-scope-reference-v1\0"


def is_school_scope_reference(reference: str) -> bool:
    return isinstance(reference, str) and reference.startswith("schoolv1:")


def school_scope_reference_coordinates(reference: str) -> tuple[str, str, bool] | None:
    """Untrusted routing hints only; consumers must decode and authorize them."""
    if type(reference) is not str or len(reference) > 1600:
        return None
    parts = reference.split(":")
    if (
        len(parts) != 6
        or parts[0] != "schoolv1"
        or parts[1] not in _FAMILIES
        or _TOKEN.fullmatch(parts[2]) is None
        or parts[3] not in {"c", "o"}
        or _ENCODED.fullmatch(parts[4]) is None
        or _HEX.fullmatch(parts[5]) is None
    ):
        return None
    return parts[1], parts[2], parts[3] == "c"


def _coordinates(
    secret: bytes,
    family: str,
    school_id: UUID,
    assignment_id: UUID,
    fact_kind: str,
    source_key: str,
) -> bytes:
    if (
        type(secret) is not bytes
        or len(secret) < 16
        or family not in _FAMILIES
        or type(school_id) is not UUID
        or type(assignment_id) is not UUID
        or type(fact_kind) is not str
        or _TOKEN.fullmatch(fact_kind) is None
        or type(source_key) is not str
        or not source_key
        or source_key != source_key.strip()
        or len(source_key) > 255
        or "*" in source_key
        or "?" in source_key
        or any(ord(char) < 32 or ord(char) == 127 for char in source_key)
    ):
        raise ValueError("Invalid school-scope reference coordinates")
    encoded = source_key.encode("utf-8")
    if len(encoded) > 1024:
        raise ValueError("Invalid school-scope reference key length")
    return encoded


def encode_school_scope_reference(
    *,
    secret: bytes,
    family: str,
    school_id: UUID,
    assignment_id: UUID,
    fact_kind: str,
    source_key: str,
    creation: bool,
) -> str:
    if type(creation) is not bool:
        raise ValueError("Invalid school-scope reference action")
    source = _coordinates(secret, family, school_id, assignment_id, fact_kind, source_key)
    key = base64.urlsafe_b64encode(source).decode("ascii").rstrip("=")
    payload = f"schoolv1:{family}:{fact_kind}:{'c' if creation else 'o'}:{key}"
    bound = f"{school_id}:{assignment_id}:{payload}".encode()
    digest = hmac.new(secret, _DOMAIN + bound, sha256).hexdigest()
    return f"{payload}:{digest}"


def decode_school_scope_reference(
    reference: str,
    *,
    secret: bytes,
    family: str,
    school_id: UUID,
    assignment_id: UUID,
    fact_kind: str,
    creation: bool,
) -> str:
    if type(reference) is not str or len(reference) > 1600:
        raise ValueError("Invalid school-scope reference")
    parts = reference.split(":")
    if (
        len(parts) != 6
        or parts[:4] != ["schoolv1", family, fact_kind, "c" if creation else "o"]
        or _ENCODED.fullmatch(parts[4]) is None
        or _HEX.fullmatch(parts[5]) is None
    ):
        raise ValueError("Invalid school-scope reference")
    try:
        source = base64.urlsafe_b64decode(parts[4] + "=" * (-len(parts[4]) % 4))
        source_key = source.decode("utf-8")
        expected = encode_school_scope_reference(
            secret=secret,
            family=family,
            school_id=school_id,
            assignment_id=assignment_id,
            fact_kind=fact_kind,
            source_key=source_key,
            creation=creation,
        )
    except (UnicodeError, ValueError):
        raise ValueError("Invalid school-scope reference") from None
    if not hmac.compare_digest(reference, expected):
        raise ValueError("Invalid school-scope reference")
    return source_key
