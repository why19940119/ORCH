"""Chat upload handling for ORCH Operator Console (docs + photos).

v0.17.0 — documents (pdf/txt/md) and images (png/jpeg/webp/gif).
Video and docx are out of scope. Uploads stay under uploads/chat/.

v0.18.2 — the whole batch is validated (magic bytes, size, PDF parse)
before anything touches disk; any failure removes the batch directory;
old upload batches are swept after UPLOAD_RETENTION_SECONDS. Every
AttachmentError carries a ``code`` (+ ``params``) so the UI can show a
localised message via ui_i18n (``err_att_<code>``).
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
import secrets
import shutil
import time
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
UPLOADS_ROOT = (PROJECT_ROOT / "uploads").resolve()
CHAT_UPLOADS_ROOT = (UPLOADS_ROOT / "chat").resolve()

MAX_FILES_PER_REQUEST = 3
MAX_DOC_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_EXTRACTED_CHARS = 24_000
MAX_PDF_PAGES = 50
UPLOAD_RETENTION_SECONDS = 24 * 60 * 60

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
    """User-facing attachment validation / extraction error.

    ``str(error)`` stays English (logs / tests); ``code`` + ``params``
    select the localised UI message ``err_att_<code>``.
    """

    def __init__(self, message, code="generic", **params):
        super().__init__(message)
        self.code = code
        self.params = params


# Magic-byte signatures checked against the (normalised) extension.
def _is_pdf(data):
    return b"%PDF-" in data[:1024]


def _is_png(data):
    return data.startswith(b"\x89PNG\r\n\x1a\n")


def _is_jpeg(data):
    return data.startswith(b"\xff\xd8\xff")


def _is_webp(data):
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _is_gif(data):
    return data[:6] in (b"GIF87a", b"GIF89a")


MAGIC_CHECKS = {
    ".pdf": _is_pdf,
    ".png": _is_png,
    ".jpg": _is_jpeg,
    ".webp": _is_webp,
    ".gif": _is_gif,
}


def decode_text(data: bytes) -> str:
    """TXT/MD must be real text: strict UTF-8 (BOM ok), no NUL bytes."""
    if b"\x00" in data:
        raise AttachmentError(
            "Text attachment contains binary data.", code="text_not_utf8"
        )
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise AttachmentError(
            "Text attachment is not valid UTF-8 text.", code="text_not_utf8"
        ) from error


def check_magic(data: bytes, ext: str):
    if ext in {".txt", ".md"}:
        decode_text(data)
        return
    check = MAGIC_CHECKS.get(ext)
    if check is None or not check(data):
        raise AttachmentError(
            f"File content does not match its {ext} extension.",
            code="magic_mismatch",
            ext=ext,
        )


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
                f"MIME type not allowed ({declared}).",
                code="mime_not_allowed", mime=declared,
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
        raise AttachmentError(
            "Attachment filename is missing or invalid.",
            code="filename_invalid",
        )
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
            "Attachment path escapes the uploads/chat containment root.",
            code="path_escape",
        ) from error
    return resolved


def classify_kind(ext: str, mime: str) -> str:
    if ext in IMAGE_EXTENSIONS or mime in ALLOWED_IMAGE_MIMES:
        return "image"
    if ext in DOC_EXTENSIONS or mime in ALLOWED_DOC_MIMES:
        return "document"
    raise AttachmentError(
        f"Unsupported attachment type: {ext or mime}",
        code="type_not_allowed", ext=ext or mime,
    )


def validate_upload_meta(filename: str, declared_mime: str | None, size: int):
    safe_name = sanitize_filename(filename)
    ext = _normalize_ext(safe_name)
    if ext not in ALLOWED_EXTENSIONS:
        raise AttachmentError(
            f"File type not allowed ({ext or 'unknown'}). "
            "Allowed: pdf, txt, md, png, jpeg, webp, gif.",
            code="type_not_allowed", ext=ext or "?",
        )
    mime = _guess_mime(safe_name, declared_mime)
    kind = classify_kind(ext, mime)
    if mime not in ALLOWED_MIMES:
        raise AttachmentError(
            f"MIME type not allowed ({mime}).",
            code="mime_not_allowed", mime=mime,
        )
    if kind == "image" and (
        ext not in IMAGE_EXTENSIONS or mime not in ALLOWED_IMAGE_MIMES
    ):
        raise AttachmentError(
            "Image extension/MIME mismatch.", code="type_mismatch"
        )
    if kind == "document" and (
        ext not in DOC_EXTENSIONS or mime not in ALLOWED_DOC_MIMES
    ):
        raise AttachmentError(
            "Document extension/MIME mismatch.", code="type_mismatch"
        )
    limit = MAX_IMAGE_BYTES if kind == "image" else MAX_DOC_BYTES
    if size is None or size < 0:
        raise AttachmentError(
            "Attachment size is missing.", code="empty"
        )
    if size > limit:
        limit_mb = limit // (1024 * 1024)
        raise AttachmentError(
            f"Attachment exceeds the {limit_mb}MB {kind} limit.",
            code="too_large", limit_mb=limit_mb,
        )
    if size == 0:
        raise AttachmentError("Attachment is empty.", code="empty")
    return {
        "safe_name": safe_name,
        "ext": ext,
        "mime": mime,
        "kind": kind,
        "size": size,
        "limit": limit,
    }


def _extract_pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        from io import BytesIO
    except ImportError as error:
        raise AttachmentError(
            "PDF support requires the pypdf package.",
            code="pdf_unreadable",
        ) from error
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception:
                unlocked = 0
            if not unlocked:
                raise AttachmentError(
                    "PDF is password-protected.", code="pdf_encrypted"
                )
        page_count = len(reader.pages)
        parts = []
        for index in range(min(page_count, MAX_PDF_PAGES)):
            parts.append(reader.pages[index].extract_text() or "")
    except AttachmentError:
        raise
    except Exception as error:  # pypdf raises many types on bad input
        raise AttachmentError(
            "PDF could not be read (damaged or unsupported).",
            code="pdf_unreadable",
        ) from error
    text = "\n".join(parts)
    if page_count > MAX_PDF_PAGES:
        text += f"\n…[only the first {MAX_PDF_PAGES} of {page_count} pages were read]"
    return text


def extract_text_from_bytes(data: bytes, mime: str, filename: str) -> str:
    ext = _normalize_ext(filename)
    if ext in {".txt", ".md"} or mime in {
        "text/plain",
        "text/markdown",
        "text/x-markdown",
    }:
        text = decode_text(data)
    elif ext == ".pdf" or mime == "application/pdf":
        text = _extract_pdf_text(data)
    else:
        raise AttachmentError(
            "No text extractor for this document type.",
            code="type_not_allowed", ext=ext or mime,
        )
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


def configured_upload_retention_seconds():
    """v0.20.0: admin retention setting (days); 24h if unavailable."""
    try:
        import orch_auth

        return int(orch_auth.settings()["upload_retention_days"]) * 24 * 60 * 60
    except Exception:
        return UPLOAD_RETENTION_SECONDS


def sweep_old_uploads(max_age_seconds=None, now=None):
    """Delete upload batch dirs/files older than the retention window."""
    if max_age_seconds is None:
        max_age_seconds = configured_upload_retention_seconds()
    max_age = max_age_seconds
    root = CHAT_UPLOADS_ROOT
    if not root.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - max_age
    removed = 0
    for entry in root.iterdir():
        try:
            if entry.is_symlink():
                entry.unlink()
                removed += 1
                continue
            if entry.stat().st_mtime >= cutoff:
                continue
            assert_within_chat_uploads(entry)
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink()
            removed += 1
        except (OSError, AttachmentError):
            continue
    return removed


def _prepare_record(storage):
    """Validate one upload fully in memory (no disk writes)."""
    filename = storage.filename or ""
    max_cap = MAX_DOC_BYTES
    data = storage.read(max_cap + 1)
    if data is None:
        data = b""
    if len(data) > max_cap:
        raise AttachmentError(
            f"Attachment exceeds the {MAX_DOC_BYTES // (1024 * 1024)}MB limit.",
            code="too_large", limit_mb=MAX_DOC_BYTES // (1024 * 1024),
        )
    meta = validate_upload_meta(
        filename,
        getattr(storage, "mimetype", None),
        len(data),
    )
    if meta["kind"] == "image" and len(data) > MAX_IMAGE_BYTES:
        raise AttachmentError(
            f"Attachment exceeds the {MAX_IMAGE_BYTES // (1024 * 1024)}MB image limit.",
            code="too_large", limit_mb=MAX_IMAGE_BYTES // (1024 * 1024),
        )
    check_magic(data, meta["ext"])

    record = {
        "name": meta["safe_name"],
        "kind": meta["kind"],
        "mime": meta["mime"],
        "ext": meta["ext"],
        "size": len(data),
        "sha256": f"sha256:{hashlib.sha256(data).hexdigest()}",
    }
    if meta["kind"] == "document":
        record["extracted_text"] = extract_text_from_bytes(
            data, meta["mime"], meta["safe_name"]
        )
    else:
        record["data_base64"] = base64.b64encode(data).decode("ascii")
    return record, data


def process_uploaded_files(file_storages, session_key: str | None = None):
    """Validate, contain, and prepare Flask FileStorage uploads.

    Returns a list of attachment dicts ready for ask_orch / session history.
    Nothing is written unless every file in the batch is valid; a failure
    while writing removes the whole batch directory.
    """
    files = [f for f in (file_storages or []) if f and getattr(f, "filename", None)]
    if not files:
        return []
    if len(files) > MAX_FILES_PER_REQUEST:
        raise AttachmentError(
            f"At most {MAX_FILES_PER_REQUEST} attachments per message.",
            code="too_many", max=MAX_FILES_PER_REQUEST,
        )

    prepared = [_prepare_record(storage) for storage in files]

    ensure_chat_upload_dirs()
    try:
        sweep_old_uploads()
    except Exception:
        pass

    token = (session_key or "anon")[:32]
    token = _SAFE_NAME_RE.sub("", token) or "anon"
    batch_dir = assert_within_chat_uploads(
        CHAT_UPLOADS_ROOT / f"{token}_{uuid.uuid4().hex[:12]}"
    )
    attachments = []
    try:
        batch_dir.mkdir(parents=True, exist_ok=True)
        assert_within_chat_uploads(batch_dir)
        for record, data in prepared:
            stored_name = f"{secrets.token_hex(8)}_{record['name']}"
            dest = assert_within_chat_uploads(batch_dir / stored_name)
            dest.write_bytes(data)
            attachments.append(
                {**record, "stored_name": stored_name, "path": str(dest)}
            )
    except Exception as error:
        shutil.rmtree(batch_dir, ignore_errors=True)
        if isinstance(error, AttachmentError):
            raise
        raise AttachmentError(
            "Attachment could not be stored.", code="storage_failed"
        ) from error

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
