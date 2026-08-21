from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import re
import uuid

ARTIFACT_ROOT = Path("artifacts")
STAGING_DIR = ARTIFACT_ROOT / "staging"
OBJECTS_DIR = ARTIFACT_ROOT / "objects" / "sha256"
MANIFESTS_DIR = ARTIFACT_ROOT / "manifests"
LATEST_DIR = ARTIFACT_ROOT / "latest"

LOGICAL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_directories():
    for directory in [
        STAGING_DIR,
        OBJECTS_DIR,
        MANIFESTS_DIR,
        LATEST_DIR,
    ]:
        directory.mkdir(parents=True, exist_ok=True)


def validate_logical_name(logical_name):
    if not isinstance(logical_name, str):
        raise ValueError("logical_name must be a string.")

    if not LOGICAL_NAME_PATTERN.fullmatch(logical_name):
        raise ValueError(
            "logical_name may contain only letters, numbers, "
            "underscores, and hyphens."
        )


def atomic_write_bytes(destination, content):
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp"
    )

    with temporary.open("wb") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())

    os.replace(temporary, destination)


def atomic_write_json(destination, data):
    content = (
        json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")

    atomic_write_bytes(destination, content)


def stage_json(logical_name, data):
    ensure_directories()
    validate_logical_name(logical_name)

    staging_path = STAGING_DIR / (
        f"{logical_name}-{uuid.uuid4().hex}.json"
    )

    atomic_write_json(staging_path, data)

    return staging_path


def stream_hash_and_copy(source_path, temporary_object_path):
    hasher = hashlib.sha256()
    byte_size = 0

    temporary_object_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with source_path.open("rb") as source, temporary_object_path.open(
        "wb"
    ) as target:
        while True:
            chunk = source.read(1024 * 1024)

            if not chunk:
                break

            hasher.update(chunk)
            target.write(chunk)
            byte_size += len(chunk)

        target.flush()
        os.fsync(target.fileno())

    return hasher.hexdigest(), byte_size


def publish_staged_artifact(
    staging_path,
    logical_name,
    producer_task_id,
    schema_version="1.0",
    parent_artifact_id=None,
):
    ensure_directories()
    validate_logical_name(logical_name)

    staging_path = Path(staging_path)

    if not staging_path.exists():
        raise FileNotFoundError(
            f"Staging artifact does not exist: {staging_path}"
        )

    if not staging_path.is_file():
        raise ValueError(
            f"Staging artifact is not a file: {staging_path}"
        )

    temporary_object_path = OBJECTS_DIR / (
        f".pending-{uuid.uuid4().hex}.tmp"
    )

    digest, byte_size = stream_hash_and_copy(
        staging_path,
        temporary_object_path,
    )

    object_path = OBJECTS_DIR / digest[:2] / digest
    object_path.parent.mkdir(parents=True, exist_ok=True)

    if object_path.exists():
        temporary_object_path.unlink()
    else:
        os.replace(temporary_object_path, object_path)

    created_at_utc = utc_now()
    artifact_id = (
        f"artifact_{logical_name}_{digest[:12]}_"
        f"{uuid.uuid4().hex[:8]}"
    )

    manifest = {
        "manifest_version": "1.0",
        "artifact_id": artifact_id,
        "logical_name": logical_name,
        "logical_path": str(
            LATEST_DIR / f"{logical_name}.json"
        ),
        "object_path": str(object_path),
        "content_sha256": f"sha256:{digest}",
        "byte_size": byte_size,
        "schema_version": schema_version,
        "producer_task_id": producer_task_id,
        "parent_artifact_id": parent_artifact_id,
        "created_at_utc": created_at_utc,
        "immutable": True,
    }

    manifest_path = MANIFESTS_DIR / f"{artifact_id}.json"
    atomic_write_json(manifest_path, manifest)

    latest_pointer = {
        "logical_name": logical_name,
        "artifact_id": artifact_id,
        "manifest_path": str(manifest_path),
        "content_sha256": f"sha256:{digest}",
        "updated_at_utc": created_at_utc,
    }

    latest_path = LATEST_DIR / f"{logical_name}.json"
    atomic_write_json(latest_path, latest_pointer)

    staging_path.unlink(missing_ok=True)

    return {
        "artifact_id": artifact_id,
        "manifest_path": str(manifest_path),
        "latest_path": str(latest_path),
        "object_path": str(object_path),
        "content_sha256": f"sha256:{digest}",
        "byte_size": byte_size,
    }


def load_latest_pointer(logical_name):
    validate_logical_name(logical_name)

    latest_path = LATEST_DIR / f"{logical_name}.json"

    if not latest_path.exists():
        raise FileNotFoundError(
            f"Latest pointer does not exist: {latest_path}"
        )

    return json.loads(latest_path.read_text(encoding="utf-8"))
