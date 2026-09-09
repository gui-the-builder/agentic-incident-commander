"""Read-only evidence export for historical audits created before the evidence API."""

import argparse
import json
import subprocess
from pathlib import Path
from uuid import UUID

# Executes against the existing API image; only reads Repository.evidence().
EXPORT_SCRIPT = """
import json, sys
from uuid import UUID
from commander.config import Settings
from commander.storage import Repository, make_engine
engine = make_engine(Settings().database_url)
try:
    repository = Repository(engine)
    incident_id = UUID(sys.argv[1])
    repository.load(incident_id)
    print(json.dumps([e.model_dump(mode='json') for e in repository.evidence(incident_id)]))
finally:
    engine.dispose()
"""


def export(source: Path) -> int:
    count = 0
    for audit_path in sorted(source.glob("*/audit.json")):
        output = audit_path.with_name("evidence.json")
        if output.exists():
            continue
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        incident_id = UUID(audit["incident"]["id"])
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                ".env",
                "-f",
                "infra/compose/compose.yaml",
                "exec",
                "-T",
                "commander-api",
                "python",
                "-c",
                EXPORT_SCRIPT,
                str(incident_id),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        records = json.loads(result.stdout)
        if not isinstance(records, list) or any(
            r["incident_id"] != str(incident_id) for r in records
        ):
            raise ValueError("Evidence export does not match the requested incident")
        with output.open("x", encoding="utf-8") as file:
            json.dump(records, file, indent=2)
        count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    print(f"Exported evidence for {export(args.source)} completed audits; originals preserved.")


if __name__ == "__main__":
    main()
