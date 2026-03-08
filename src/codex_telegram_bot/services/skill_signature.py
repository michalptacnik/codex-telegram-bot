"""Ed25519 signature verification for skill packs.

Provides cryptographic trust verification for marketplace skill bundles.
When ``ed25519`` (PyNaCl or the pure-Python ``ed25519`` package) is not
available, verification always returns False with a clear reason string.

Signature scheme
----------------
The publisher signs the UTF-8 bytes of ``SKILL.md`` content (everything
after the YAML frontmatter ``---`` closing delimiter, or the whole file if
no frontmatter is detected).  The signature is stored as a hex string in
the YAML frontmatter ``signature`` field.

Trust model
-----------
* ``SKILL_TRUSTED_PUBLISHER_KEYS`` env var contains comma-separated
  ``<key_id>:<hex_public_key>`` pairs.
* A skill is *verified* when:
  1. Its ``public_key_id`` matches a trusted key entry.
  2. The Ed25519 signature over the content bytes is valid for that key.
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Try to import Ed25519 verification primitives.
_ED25519_AVAILABLE = False
_verify_fn = None

try:
    from nacl.signing import VerifyKey
    from nacl.exceptions import BadSignatureError as _NaClBadSig

    def _nacl_verify(public_key_hex: str, signature_hex: str, data: bytes) -> bool:
        try:
            vk = VerifyKey(bytes.fromhex(public_key_hex))
            vk.verify(data, bytes.fromhex(signature_hex))
            return True
        except (_NaClBadSig, Exception):
            return False

    _verify_fn = _nacl_verify
    _ED25519_AVAILABLE = True
except ImportError:
    pass

if not _ED25519_AVAILABLE:
    try:
        # Pure-Python fallback: ``ed25519`` package.
        import ed25519 as _ed25519_mod

        def _pure_verify(public_key_hex: str, signature_hex: str, data: bytes) -> bool:
            try:
                vk = _ed25519_mod.VerifyingKey(bytes.fromhex(public_key_hex))
                vk.verify(bytes.fromhex(signature_hex), data)
                return True
            except (_ed25519_mod.BadSignatureError, Exception):
                return False

        _verify_fn = _pure_verify
        _ED25519_AVAILABLE = True
    except ImportError:
        pass

if not _ED25519_AVAILABLE:
    try:
        # stdlib-only fallback using hashlib + hmac won't work for Ed25519,
        # but we can try cryptography package.
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature

        def _cryptography_verify(public_key_hex: str, signature_hex: str, data: bytes) -> bool:
            try:
                pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
                pk.verify(bytes.fromhex(signature_hex), data)
                return True
            except (InvalidSignature, Exception):
                return False

        _verify_fn = _cryptography_verify
        _ED25519_AVAILABLE = True
    except ImportError:
        pass


@dataclass(frozen=True)
class VerificationResult:
    """Result of a signature verification attempt."""
    verified: bool
    reason: str
    public_key_id: str = ""
    publisher: str = ""


@dataclass(frozen=True)
class TrustedKey:
    """A trusted publisher key entry."""
    key_id: str
    public_key_hex: str


def load_trusted_keys() -> Dict[str, TrustedKey]:
    """Parse ``SKILL_TRUSTED_PUBLISHER_KEYS`` env var.

    Format: ``key_id:hex_public_key,key_id2:hex_public_key2,...``
    Legacy format (key_id only, no colon) is also supported for backwards
    compatibility — those entries are stored but cannot be used for
    cryptographic verification.
    """
    raw = (os.environ.get("SKILL_TRUSTED_PUBLISHER_KEYS") or "").strip()
    if not raw:
        return {}
    out: Dict[str, TrustedKey] = {}
    for item in raw.split(","):
        entry = item.strip()
        if not entry:
            continue
        if ":" in entry:
            key_id, _, pub_hex = entry.partition(":")
            key_id = key_id.strip()
            pub_hex = pub_hex.strip()
            if key_id and pub_hex:
                out[key_id] = TrustedKey(key_id=key_id, public_key_hex=pub_hex)
        else:
            # Legacy format: key_id only (no crypto verification possible).
            out[entry] = TrustedKey(key_id=entry, public_key_hex="")
    return out


def _extract_signable_content(skill_md_text: str) -> bytes:
    """Extract the content portion of SKILL.md that was signed.

    The signed content is everything after the closing ``---`` of the YAML
    frontmatter.  If no frontmatter is detected, the entire text is signed.
    """
    text = skill_md_text or ""
    if text.startswith("---"):
        # Find the closing ---
        end_idx = text.find("\n---", 3)
        if end_idx >= 0:
            # Content starts after the closing --- and its newline
            content_start = end_idx + 4
            if content_start < len(text) and text[content_start] == "\n":
                content_start += 1
            return text[content_start:].encode("utf-8")
    return text.encode("utf-8")


def verify_skill_signature(
    skill_md_text: str,
    signature_hex: str,
    public_key_id: str,
    publisher: str = "",
    trusted_keys: Optional[Dict[str, TrustedKey]] = None,
) -> VerificationResult:
    """Verify the Ed25519 signature of a SKILL.md file.

    Returns a VerificationResult indicating whether the signature is valid.
    """
    if not signature_hex:
        return VerificationResult(
            verified=False,
            reason="no signature provided",
            public_key_id=public_key_id,
            publisher=publisher,
        )
    if not public_key_id:
        return VerificationResult(
            verified=False,
            reason="no public_key_id provided",
            public_key_id="",
            publisher=publisher,
        )

    keys = trusted_keys if trusted_keys is not None else load_trusted_keys()
    trusted = keys.get(public_key_id)
    if trusted is None:
        return VerificationResult(
            verified=False,
            reason=f"public_key_id '{public_key_id}' not in trusted keys",
            public_key_id=public_key_id,
            publisher=publisher,
        )
    if not trusted.public_key_hex:
        return VerificationResult(
            verified=False,
            reason=f"trusted key '{public_key_id}' has no public key material (legacy entry)",
            public_key_id=public_key_id,
            publisher=publisher,
        )

    if not _ED25519_AVAILABLE or _verify_fn is None:
        return VerificationResult(
            verified=False,
            reason="no Ed25519 library available (install PyNaCl, cryptography, or ed25519)",
            public_key_id=public_key_id,
            publisher=publisher,
        )

    content_bytes = _extract_signable_content(skill_md_text)
    try:
        valid = _verify_fn(trusted.public_key_hex, signature_hex, content_bytes)
    except Exception as exc:
        logger.warning("skill signature verification error: %s", exc)
        return VerificationResult(
            verified=False,
            reason=f"verification error: {exc}",
            public_key_id=public_key_id,
            publisher=publisher,
        )

    if valid:
        return VerificationResult(
            verified=True,
            reason="signature valid",
            public_key_id=public_key_id,
            publisher=publisher,
        )
    return VerificationResult(
        verified=False,
        reason="signature invalid",
        public_key_id=public_key_id,
        publisher=publisher,
    )


def is_ed25519_available() -> bool:
    """Check if an Ed25519 library is available for signature verification."""
    return _ED25519_AVAILABLE
