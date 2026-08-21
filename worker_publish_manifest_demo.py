from datetime import datetime, timezone
from pathlib import Path
import json

from artifact_store import publish_staged_artifact, stage_json

logical_name = "manifest_demo"
producer_task_id = "task_publish_manifest_demo_016"

payload = {
    "status": "success",
    "artifact_type": "manifest_publish_demo",
    "generated_at_utc": datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ),
    "message": (
        "This JSON artifact was staged and published through "
        "the v0.12 artifact manifest store."
    ),
}

staging_path = stage_json(logical_name, payload)

publication = publish_staged_artifact(
    staging_path=staging_path,
    logical_name=logical_name,
    producer_task_id=producer_task_id,
    schema_version="1.0",
)

output = {
    "status": "success",
    "task": producer_task_id,
    "publication": publication,
    "message": (
        "Manifest-backed artifact was published successfully."
    ),
}

output_path = Path("output/manifest_publish_demo.json")
output_path.write_text(
    json.dumps(output, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)

print(output["message"])
