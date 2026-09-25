from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from . import VARIANT_ID


class ContractError(ValueError):
    pass


# These are the supplied public contracts. Changes require an explicit contract migration.
PUBLIC_SCHEMA_SHA256 = {
    "l3a-output-v2.schema.json": "28cfa3b4e58ae274f971a728f7c4804aa1ca8b304036a9d94ffa2aead1820fd1",
    "l3b-output-v2.schema.json": "6f207b95b426675b65d8de24adf76d61d4ff87bddc62d4e8afee20437900bcc0",
    "trace-event-v1.schema.json": (
        "f07497ba189f9d394b7b97cbf1928bd435af08ece2cb1efd7b4145a58434e8c2"
    ),
    "submission-manifest-v2.schema.json": (
        "d5976f4b4c0bcebbfa3006d8afb2cdaa0f4b24e325d54682e5c51532c2ed8549"
    ),
    "mcp-evidence-response-v1.schema.json": (
        "0335d8c92c9484331b3460ed92c8645b0bd64617f90edcb4cfd36b8ba5dc884c"
    ),
}


class Contracts:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        schemas: dict[str, dict[str, Any]] = {}
        registry = Registry()
        for name, expected in PUBLIC_SCHEMA_SHA256.items():
            path = self.root / name
            if not path.is_file():
                raise ContractError(f"public contract changed or missing: {name}")
            canonical = json.dumps(
                json.loads(path.read_text(encoding="utf-8")),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            if hashlib.sha256(canonical).hexdigest() != expected:
                raise ContractError(f"public contract changed or missing: {name}")
        for path in sorted(self.root.glob("*.schema.json")):
            schema = json.loads(path.read_text(encoding="utf-8"))
            schemas[path.name] = schema
            resource = Resource.from_contents(schema)
            registry = registry.with_resource(schema["$id"], resource)
        self._schemas = schemas
        self._registry = registry

    def validate(self, schema_name: str, value: Any, label: str) -> None:
        schema = self._schemas.get(schema_name)
        if schema is None:
            raise ContractError(f"contract not found: {schema_name}")
        validator = Draft202012Validator(
            schema, registry=self._registry, format_checker=FormatChecker()
        )
        errors = sorted(validator.iter_errors(value), key=lambda error: list(error.absolute_path))
        if errors:
            error = errors[0]
            location = ".".join(str(part) for part in error.absolute_path) or "$"
            raise ContractError(f"{label}:{location}: {error.message}")

    def validate_output(self, value: Any, label: str) -> None:
        self.validate(f"{VARIANT_ID}-output-v2.schema.json", value, label)

    def validate_trace(self, value: Any, label: str) -> None:
        self.validate("trace-event-v1.schema.json", value, label)

    def validate_manifest(self, value: Any, label: str = "manifest.json") -> None:
        self.validate("submission-manifest-v2.schema.json", value, label)

    def validate_evidence(self, value: Any, label: str = "MCP response") -> None:
        self.validate("mcp-evidence-response-v1.schema.json", value, label)
