from datetime import datetime, timezone
from pathlib import Path
import json
import sys

source_file = Path("output/ai_advisory.json")
output_file = Path("output/ai_advisory_validation.json")

errors = []

if not source_file.exists():
    errors.append("ai_advisory.json does not exist.")

advisory = {}

if source_file.exists():
    try:
        advisory = json.loads(source_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        errors.append(f"ai_advisory.json is invalid JSON: {error}")

required_values = {
    "status": "success",
    "mode": "advisory",
    "execution_authority": "none",
    "external_api_called": False,
}

for field, expected_value in required_values.items():
    if advisory.get(field) != expected_value:
        errors.append(
            f"AI advisory field is invalid: {field}"
        )

if not isinstance(advisory.get("provider"), str):
    errors.append("AI advisory provider must be a string.")

if not isinstance(advisory.get("model"), str):
    errors.append("AI advisory model must be a string.")

if not isinstance(advisory.get("summary"), str):
    errors.append("AI advisory summary must be a string.")

if not isinstance(advisory.get("recommendations"), list):
    errors.append("AI advisory recommendations must be a list.")

if not isinstance(advisory.get("proposed_actions"), list):
    errors.append("AI advisory proposed_actions must be a list.")

if advisory.get("proposed_actions") != []:
    errors.append(
        "Mock AI advisory must not contain proposed actions."
    )

validation = {
    "status": "success" if not errors else "failed",
    "task": "task_validate_ai_advisory_014",
    "validated_input": str(source_file),
    "validated_at_utc": datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ),
    "errors": errors,
    "message": (
        "AI advisory passed read-only advisory validation."
        if not errors
        else "AI advisory failed validation."
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
