"""
Sprint 1 deliverable: schema loader + request validator.

The whole point of Sprint 1 is that a compiled request body can be checked
against the API contract BEFORE it is sent. The planner (LLM) is never trusted
to produce valid JSON on its own — this module is the gate.

Usage:
    from schema import SchemaMap
    sm = SchemaMap.load()
    result = sm.validate("POST /companies", body)
    if not result.ok:
        print(result.errors)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCHEMA_PATH = os.path.join(_HERE, "..", "schema", "schema_map.yaml")


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # The cleaned body after compiler hygiene (empty arrays dropped, etc.)
    cleaned: dict[str, Any] = field(default_factory=dict)


class SchemaMap:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.enums = data["enums"]
        self.rules = data["rules"]
        self.endpoints = data["endpoints"]

    @classmethod
    @lru_cache(maxsize=1)
    def load(cls, path: str = _SCHEMA_PATH) -> "SchemaMap":
        with open(path, "r") as f:
            return cls(yaml.safe_load(f))

    # -- public API ---------------------------------------------------------
    def routes(self) -> list[str]:
        return list(self.endpoints.keys())

    def default_sort(self, route: str) -> str | None:
        return self.endpoints[route].get("default_sort")

    def validate(self, route: str, body: dict[str, Any]) -> ValidationResult:
        if route not in self.endpoints:
            return ValidationResult(False, [f"unknown route: {route!r}"])

        spec = self.endpoints[route]
        errors: list[str] = []
        warnings: list[str] = []

        cleaned = self._clean(body)

        # 1) top-level / nested field + enum validation
        self._validate_block(route, spec.get("blocks", {}), cleaned, "", errors, warnings)

        # 2) pagination + sort
        self._validate_pagination(spec, cleaned, errors)
        self._validate_sort(route, spec, cleaned, errors)
        self._validate_person_type(route, spec, cleaned, errors)

        # 3) hard product rules (mutual exclusion, companion fields)
        self._validate_rules(route, cleaned, errors)

        return ValidationResult(ok=not errors, errors=errors, warnings=warnings, cleaned=cleaned)

    # -- internals ----------------------------------------------------------
    def _clean(self, body: Any) -> Any:
        """Compiler hygiene: drop null/empty, dedupe arrays. Recurses."""
        if isinstance(body, dict):
            out = {}
            for k, v in body.items():
                cv = self._clean(v)
                if cv is None:
                    continue
                if isinstance(cv, (list, dict)) and len(cv) == 0:
                    continue
                out[k] = cv
            return out
        if isinstance(body, list):
            cleaned = [self._clean(v) for v in body]
            cleaned = [v for v in cleaned if v is not None and v != {} and v != []]
            # dedupe scalars while preserving order
            if all(not isinstance(v, (dict, list)) for v in cleaned):
                seen, deduped = set(), []
                for v in cleaned:
                    if v not in seen:
                        seen.add(v)
                        deduped.append(v)
                return deduped
            return cleaned
        return body

    def _resolve_enum(self, field_spec: dict) -> list | None:
        ref = field_spec.get("enum_ref")
        if ref:
            return self.enums.get(ref)
        return field_spec.get("enum")

    def _validate_block(self, route, block_spec, body, path, errors, warnings):
        for block_name, fields in block_spec.items():
            if block_name not in body:
                continue
            value = body[block_name]
            if not isinstance(value, dict):
                errors.append(f"{path}{block_name}: expected object")
                continue
            for fname, fval in value.items():
                fspec = fields.get(fname)
                if fspec is None:
                    errors.append(f"{path}{block_name}.{fname}: unsupported field for {route}")
                    continue
                # nested sub-block (e.g. latest_deal, investor.deals)
                if isinstance(fspec, dict) and "type" not in fspec and "enum_ref" not in fspec:
                    self._validate_block(
                        route, {fname: fspec}, {fname: fval},
                        f"{path}{block_name}.", errors, warnings,
                    )
                    continue
                self._validate_field(route, f"{path}{block_name}.{fname}", fspec, fval, errors)

    def _validate_field(self, route, dotted, fspec, val, errors):
        ftype = fspec.get("type")
        enum_vals = self._resolve_enum(fspec)

        if ftype == "financing_type_object_array":
            fin_enum = self.enums["financing_type"]
            if not isinstance(val, list):
                errors.append(f"{dotted}: expected array of financing_type objects")
                return
            for item in val:
                if not isinstance(item, dict) or "type" not in item:
                    errors.append(f"{dotted}: each item needs a 'type'")
                    continue
                if item["type"] not in fin_enum:
                    errors.append(f"{dotted}: invalid financing_type {item['type']!r} (would 422)")
            return

        if ftype in ("enum_array",) and enum_vals is not None:
            if not isinstance(val, list):
                errors.append(f"{dotted}: expected array")
                return
            for v in val:
                if v not in enum_vals:
                    errors.append(f"{dotted}: invalid value {v!r} (allowed: {enum_vals})")
            return

        if ftype == "enum" and enum_vals is not None:
            if val not in enum_vals:
                errors.append(f"{dotted}: invalid value {val!r} (allowed: {enum_vals})")
            return

        if ftype in ("uuid_array", "string_array"):
            if not isinstance(val, list):
                errors.append(f"{dotted}: expected array")
                return
            maxlen = fspec.get("max")
            if maxlen and len(val) > maxlen:
                errors.append(f"{dotted}: exceeds max length {maxlen}")
            return

        if ftype == "number":
            if not isinstance(val, (int, float)):
                errors.append(f"{dotted}: expected number")
            return

        if ftype in ("int",):
            if not isinstance(val, int):
                errors.append(f"{dotted}: expected integer")
                return
            if "min" in fspec and val < fspec["min"]:
                errors.append(f"{dotted}: below min {fspec['min']}")
            return

        # number range check when min/max given (e.g. min_relevance)
        if ftype == "number" or ("min" in fspec or "max" in fspec):
            if isinstance(val, (int, float)):
                if "min" in fspec and val < fspec["min"]:
                    errors.append(f"{dotted}: below min {fspec['min']}")
                if "max" in fspec and val > fspec["max"]:
                    errors.append(f"{dotted}: above max {fspec['max']}")

    def _validate_pagination(self, spec, body, errors):
        pag = spec.get("pagination", {})
        ps = body.get("page_size")
        if ps is not None:
            lo, hi = pag.get("page_size", {}).get("min", 1), pag.get("page_size", {}).get("max", 500)
            if not isinstance(ps, int) or ps < lo or ps > hi:
                errors.append(f"page_size: must be int in [{lo},{hi}]")
        pg = body.get("page")
        if pg is not None and (not isinstance(pg, int) or pg < 0):
            errors.append("page: must be a non-negative integer")

    def _validate_sort(self, route, spec, body, errors):
        sb = body.get("sort_by")
        if sb is not None and sb not in spec.get("sort_options", []):
            errors.append(f"sort_by: {sb!r} not in {spec.get('sort_options')}")

    def _validate_person_type(self, route, spec, body, errors):
        pt_spec = spec.get("person_type")
        pt = body.get("person_type")
        if pt is not None:
            if pt_spec is None:
                errors.append(f"person_type not supported for {route}")
            elif pt not in pt_spec["enum"]:
                errors.append(f"person_type: {pt!r} not in {pt_spec['enum']}")

    def _validate_rules(self, route, body, errors):
        # mutual exclusion within a block
        for rule in self.rules["mutually_exclusive_within_block"]:
            group = rule["group"]
            for block_path in rule["applies_to_blocks"]:
                r, block = block_path.split(".", 1)
                if _route_key(r) != route:
                    continue
                blk = _dig(body, block)
                if not isinstance(blk, dict):
                    continue
                present = [g for g in group if g in blk]
                if "search_query" in present and (
                    "industries" in present or "super_categories" in present
                ):
                    errors.append(f"{block_path}: {rule['message']}")

        # companion-required fields (min_relevance requires search_query) — global scan
        for rule in self.rules["requires_companion"]:
            _check_companion(body, rule["field"], rule["requires"], rule["message"], errors)


def _route_key(short: str) -> str:
    return {
        "deals": "POST /deals",
        "companies": "POST /companies",
        "investors": "POST /investors",
        "people": "POST /people",
    }.get(short, short)


def _dig(body: dict, dotted: str) -> Any:
    cur = body
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _check_companion(node, field_name, requires, message, errors, _path=""):
    if isinstance(node, dict):
        if field_name in node and requires not in node:
            errors.append(f"{_path or '<root>'}: {message}")
        for k, v in node.items():
            _check_companion(v, field_name, requires, message, errors, f"{_path}.{k}" if _path else k)
