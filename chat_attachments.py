"""Chat upload handling for ORCH Operator Console (docs + photos).

v0.17.0 — documents (pdf/txt/md) and images (png/jpeg/webp/gif).
Video and docx are out of scope. Uploads stay under uploads/chat/.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
import secrets
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
UPLOADS_ROOT = (PROJECT_ROOT / "uploads").resolve()
CHAT_UPLOADS_ROOT = (UPLOADS_ROOT / "chat").resolve()

MAX_FILES_PER_REQUEST = 3
MAX_DOC_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_EXTRACTED_CHARS = 24_000

DOC_EXTENSIONS = {".pdf", ".txt", ".md"}
IMAGE_EXTENSIONS = {".png", ".jpeg", ".jpg", ".webp", ".gif"}
ALLOWED_EXTENSIONS = DOC_EXTENSIONS | IMAGE_EXTENSIONS

ALLOWED_DOC_MIMES = {
    "application/pdf",
    "text/plain",
    "text/markdown",
    "text/x-markdown",
}
ALLOWED_IMAGE_MIMES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
}
ALLOWED_MIMES = ALLOWED_DOC_MIMES | ALLOWED_IMAGE_MIMES

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class AttachmentError(ValueError):
    """User-facing attachment validation / extraction error."""


def ensure_chat_upload_dirs():
    CHAT_UPLOADS_ROOT.mkdir(parents=True, exist_ok=True)
    return CHAT_UPLOADS_ROOT


def _normalize_ext(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".jpeg":
        return ".jpg"
    return suffix


def _guess_mime(filename: str, declared: str | None) -> str:
    declared = (declared or "").split(";")[0].strip().lower()
    if declared:
        if declared not in ALLOWED_MIMES:
            raise AttachmentError(
                f"MIME type not allowed ({declared})."
            )
        return declared
    guessed, _ = mimetypes.guess_type(filename or "")
    guessed = (guessed or "").lower()
    if guessed in ALLOWED_MIMES:
        return guessed
    ext = _normalize_ext(filename)
    fallback = {
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }
    return fallback.get(ext, "application/octet-stream")


def sanitize_filename(filename: str) -> str:
    raw = Path(filename or "").name.strip()
    if not raw or raw in {".", ".."}:
        raise AttachmentError("Attachment filename is missing or invalid.")
    cleaned = _SAFE_NAME_RE.sub("_", raw)
    cleaned = cleaned.strip("._") or "upload"
    if len(cleaned) > 120:
        stem = Path(cleaned).stem[:80]
        suffix = Path(cleaned).suffix[:20]
        cleaned = f"{stem}{suffix}"
    return cleaned


def assert_within_chat_uploads(path: Path) -> Path:
    ensure_chat_upload_dirs()
    resolved = path.resolve()
    try:
        resolved.relative_to(CHAT_UPLOADS_ROOT)
    except ValueError as error:
        raise AttachmentError(
            "Attachment path escapes the uploads/chat containment root."
        ) from error
    return resolved


def classify_kind(ext: str, mime: str) -> str:
    if ext in IMAGE_EXTENSIONS or mime in ALLOWED_IMAGE_MIMES:
        return "image"
    if ext in DOC_EXTENSIONS or mime in ALLOWED_DOC_MIMES:
        return "document"
    raise AttachmentError(f"Unsupported attachment type: {ext or mime}")


def validate_upload_meta(filename: str, declared_mime: str | None, size: int):
    safe_name = sanitize_filename(filename)
    ext = _normalize_ext(safe_name)
    if ext not in ALLOWED_EXTENSIONS:
        raise AttachmentError(
            f"File type not allowed ({ext or 'unknown'}). "
            "Allowed: pdf, txt, md, png, jpeg, webp, gif."
        )
    mime = _guess_mime(safe_name, declared_mime)
    kind = classify_kind(ext, mime)
    if mime not in ALLOWED_MIMES:
        raise AttachmentError(
            f"MIME type not allowed ({mime})."
        )
    if kind == "image" and (
        ext not in IMAGE_EXTENSIONS or mime not in ALLOWED_IMAGE_MIMES
    ):
        raise AttachmentError("Image extension/MIME mismatch.")
    if kind == "document" and (
        ext not in DOC_EXTENSIONS or mime not in ALLOWED_DOC_MIMES
    ):
        raise AttachmentError("Document extension/MIME mismatch.")
    limit = MAX_IMAGE_BYTES if kind == "image" else MAX_DOC_BYTES
    if size is None or size < 0:
        raise AttachmentError("Attachment size is missing.")
    if size > limit:
        limit_mb = limit // (1024 * 1024)
        raise AttachmentError(
            f"Attachment exceeds the {limit_mb}MB {kind} limit."
        )
    if size == 0:
        raise AttachmentError("Attachment is empty.")
    return {
        "safe_name": safe_name,
        "ext": ext,
        "mime": mime,
        "kind": kind,
        "size": size,
        "limit": limit,
    }


def extract_text_from_bytes(data: bytes, mime: str, filename: str) -> str:
    ext = _normalize_ext(filename)
    if ext in {".txt", ".md"} or mime in {
        "text/plain",
        "text/markdown",
        "text/x-markdown",
    }:
        text = data.decode("utf-8", errors="replace")
    elif ext == ".pdf" or mime == "application/pdf":
        try:
            from pypdf import PdfReader
            from io import BytesIO
        except ImportError as error:
            raise AttachmentError(
                "PDF support requires the pypdf package."
            ) from error
        reader = PdfReader(BytesIO(data))
        parts = []
        for page in reader.pages:
            parts.append(page.extract_text() or "")
        text = "\n".join(parts)
    else:
        raise AttachmentError("No text extractor for this document type.")
    text = text.strip()
    if len(text) > MAX_EXTRACTED_CHARS:
        text = text[:MAX_EXTRACTED_CHARS] + "\n…[truncated]"
    return text


def history_attachment_meta(records):
    """Session-safe metadata only (no bytes / no absolute paths)."""
    out = []
    for item in records or []:
        out.append(
            {
                "name": item.get("name"),
                "kind": item.get("kind"),
                "mime": item.get("mime"),
                "size": item.get("size"),
                "sha256": item.get("sha256"),
            }
        )
    return out


def attachment_audit_records(records):
    """Audit metadata only — never raw bytes."""
    return history_attachment_meta(records)


def process_uploaded_files(file_storages, session_key: str | None = None):
    """Validate, contain, and prepare Flask FileStorage uploads.

    Returns a list of attachment dicts ready for ask_orch / session history.
    """
    files = [f for f in (file_storages or []) if f and getattr(f, "filename", None)]
    if not files:
        return []
    if len(files) > MAX_FILES_PER_REQUEST:
        raise AttachmentError(
            f"At most {MAX_FILES_PER_REQUEST} attachments per message."
        )

    ensure_chat_upload_dirs()
    token = (session_key or "anon")[:32]
    token = _SAFE_NAME_RE.sub("", token) or "anon"
    batch_dir = assert_within_chat_uploads(
        CHAT_UPLOADS_ROOT / f"{token}_{uuid.uuid4().hex[:12]}"
    )
    batch_dir.mkdir(parents=True, exist_ok=True)
    assert_within_chat_uploads(batch_dir)

    attachments = []
    for storage in files:
        filename = storage.filename or ""
        max_cap = MAX_DOC_BYTES
        data = storage.read(max_cap + 1)
        if data is None:
            data = b""
        if len(data) > max_cap:
            raise AttachmentError(
                f"Attachment exceeds the {MAX_DOC_BYTES // (1024 * 1024)}MB limit."
            )
        meta = validate_upload_meta(
            filename,
            getattr(storage, "mimetype", None),
            len(data),
        )
        if meta["kind"] == "image" and len(data) > MAX_IMAGE_BYTES:
            raise AttachmentError(
                f"Attachment exceeds the {MAX_IMAGE_BYTES // (1024 * 1024)}MB image limit."
            )

        digest = hashlib.sha256(data).hexdigest()
        stored_name = (
            f"{secrets.token_hex(8)}_{meta['safe_name']}"
        )
        dest = assert_within_chat_uploads(batch_dir / stored_name)
        dest.write_bytes(data)

        record = {
            "name": meta["safe_name"],
            "stored_name": stored_name,
            "path": str(dest),
            "kind": meta["kind"],
            "mime": meta["mime"],
            "ext": meta["ext"],
            "size": len(data),
            "sha256": f"sha256:{digest}",
        }

        if meta["kind"] == "document":
            record["extracted_text"] = extract_text_from_bytes(
                data, meta["mime"], meta["safe_name"]
            )
        else:
            record["data_base64"] = base64.b64encode(data).decode("ascii")

        attachments.append(record)

    return attachments


def accept_attribute() -> str:
    return ",".join(
        [
            ".pdf",
            ".txt",
            ".md",
            ".png",
            ".jpeg",
            ".jpg",
            ".webp",
            ".gif",
            "application/pdf",
            "text/plain",
            "text/markdown",
            "image/png",
            "image/jpeg",
            "image/webp",
            "image/gif",
        ]
    )
