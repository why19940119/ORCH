from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import sys

source_file = Path("output/manifest_publish_demo.json")
output_file = Path("output/manifest_validation_demo.json")

errors = []

if not source_file.exists():
    errors.append("manifest_publish_demo.json does not exist.")

publication = {}

if source_file.exists():
    try:
        publication = json.loads(
            source_file.read_text(encoding="utf-8")
        )
    except json.JSONDecodeError as error:
        errors.append(
            f"manifest_publish_demo.json is invalid JSON: {error}"
        )

details = publication.get("publication", {})
manifest_path = Path(details.get("manifest_path", ""))
latest_path = Path(details.get("latest_path", ""))
object_path = Path(details.get("object_path", ""))
expected_hash = details.get("content_sha256")
artifact_id = details.get("artifact_id")

manifest = {}
latest_pointer = {}

if not errors and not manifest_path.exists():
    errors.append(f"Manifest does not exist: {manifest_path}")

if not errors and not latest_path.exists():
    errors.append(f"Latest pointer does not exist: {latest_path}")

if not errors and not object_path.exists():
    errors.append(f"Immutable object does not exist: {object_path}")

if not errors:
    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
    except json.JSONDecodeError as error:
        errors.append(f"Manifest is invalid JSON: {error}")

if not errors:
    try:
        latest_pointer = json.loads(
            latest_path.read_text(encoding="utf-8")
        )
    except json.JSONDecodeError as error:
        errors.append(f"Latest pointer is invalid JSON: {error}")

if not errors and object_path.exists():
    hasher = hashlib.sha256()

    with object_path.open("rb") as file:
        while True:
            chunk = file.read(1024 * 1024)

            if not chunk:
                break

            hasher.update(chunk)

    actual_hash = f"sha256:{hasher.hexdigest()}"

    if actual_hash != expected_hash:
        errors.append(
            "Immutable object hash does not match publication hash."
        )

if manifest.get("manifest_version") != "1.0":
    errors.append("Manifest version must be 1.0.")

if manifest.get("artifact_id") != artifact_id:
    errors.append("Manifest artifact_id does not match publication.")

if manifest.get("content_sha256") != expected_hash:
    errors.append(
        "Manifest content_sha256 does not match publication."
    )

if manifest.get("immutable") is not True:
    errors.append("Manifest immutable flag must be true.")

if latest_pointer.get("artifact_id") != artifact_id:
    errors.append(
        "Latest pointer artifact_id does not match publication."
    )

if latest_pointer.get("content_sha256") != expected_hash:
    errors.append(
        "Latest pointer content_sha256 does not match publication."
    )

validation = {
    "status": "success" if not errors else "failed",
    "task": "task_validate_manifest_demo_017",
    "validated_at_utc": datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ),
    "artifact_id": artifact_id,
    "content_sha256": expected_hash,
    "errors": errors,
    "message": (
        "Artifact manifest, immutable object, and latest pointer "
        "passed validation."
        if not errors
        else "Artifact manifest validation failed."
    ),
}

output_file.write_text(
    json.dumps(validation, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)

print(validation["message"])

if errors:
    for error in errors:
        print(f"- {error}", file=sys.stderr)

    sys.exit(1)
