#!/usr/bin/env python3
"""Offline tests for Phase 2.0: the structured-output extraction schema.

Validates the schema is structurally sound for the API's rules (every
object enumerates its keys with additionalProperties: false) and that both
response shapes the prompt allows — the full extraction JSON and the
alternate {"no_floor_plans_found": ...} — conform, using a minimal local
validator for the schema subset we use.

Run: python3 test_structured_outputs.py
"""
import importlib.util as iu
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = iu.spec_from_file_location("T", os.path.join(HERE, "Takeoff_DIRECT.py"))
T = iu.module_from_spec(spec)
spec.loader.exec_module(T)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def validate(instance, schema, path="$"):
    """Minimal validator for the schema subset we emit (type/type-arrays,
    object+properties+additionalProperties, array+items). Returns list of
    violation strings."""
    errs = []
    stype = schema.get("type")
    types = stype if isinstance(stype, list) else [stype]

    def is_type(v, t):
        return {"object": dict, "array": list, "string": str,
                "boolean": bool, "null": type(None)}.get(t) and \
            isinstance(v, {"object": dict, "array": list, "string": str,
                           "boolean": bool, "null": type(None)}[t]) or \
            (t == "number" and isinstance(v, (int, float))
             and not isinstance(v, bool))

    if not any(is_type(instance, t) for t in types):
        return [f"{path}: {type(instance).__name__} not in {types}"]
    if isinstance(instance, dict) and "object" in types:
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for k in instance:
                if k not in props:
                    errs.append(f"{path}.{k}: unexpected key")
        for k in schema.get("required", []):
            if k not in instance:
                errs.append(f"{path}.{k}: required key missing")
        for k, v in instance.items():
            if k in props:
                errs += validate(v, props[k], f"{path}.{k}")
    if isinstance(instance, list) and "array" in types and "items" in schema:
        for i, v in enumerate(instance):
            errs += validate(v, schema["items"], f"{path}[{i}]")
    return errs


def walk_schema(schema, path="$"):
    """Every object node must set additionalProperties: False (API rule)."""
    errs = []
    stype = schema.get("type")
    types = stype if isinstance(stype, list) else [stype]
    if "object" in types:
        if schema.get("additionalProperties") is not False:
            errs.append(f"{path}: object missing additionalProperties:false")
        for k, sub in (schema.get("properties") or {}).items():
            errs += walk_schema(sub, f"{path}.{k}")
    if "array" in types and isinstance(schema.get("items"), dict):
        errs += walk_schema(schema["items"], f"{path}[]")
    return errs


def main():
    S = T._EXTRACTION_OUTPUT_SCHEMA

    check("schema is JSON-serializable", bool(json.dumps(S)))
    errs = walk_schema(S)
    check("every object sets additionalProperties:false", not errs, errs[:3])

    def make_instance(schema):
        """Minimal valid instance: every required key, null where allowed."""
        t = schema.get("type")
        types = t if isinstance(t, list) else [t]
        if "null" in types:
            return None
        if "object" in types:
            return {k: make_instance(schema["properties"][k])
                    for k in schema.get("required", [])}
        if "array" in types:
            return []
        if "string" in types:
            return ""
        if "boolean" in types:
            return False
        return 0

    full = make_instance(S)
    # Overlay a realistic room so the nested path is exercised end-to-end
    room_schema = S["properties"]["floors"]["items"]["properties"]["rooms"]["items"]
    room = {k: make_instance(room_schema["properties"][k])
            for k in room_schema["required"]}
    room.update({"room_id": "F2-U201-LIV", "room_name": "Living Room",
                 "source_page": 6, "source_sheet": "A-102",
                 "unit_multiplier": 1, "in_scope": True})
    room["dimensions"] = {"length_feet": 20, "width_feet": 15,
                          "ceiling_height_feet": 9, "floor_area_sqft": 300,
                          "perimeter_lf": 70, "wall_area_sqft": 630,
                          "ceiling_area_sqft": 300}
    floor_schema = S["properties"]["floors"]["items"]
    full["floors"] = [{k: make_instance(floor_schema["properties"][k])
                       for k in floor_schema["required"]}]
    full["floors"][0]["floor_name"] = "2nd Floor"
    full["floors"][0]["rooms"] = [room]
    errs = validate(full, S)
    check("full extraction shape validates", not errs, errs[:3])

    alt = make_instance(S)
    alt["no_floor_plans_found"] = True
    alt["pages_reviewed"] = "schedules and details only"
    errs = validate(alt, S)
    check("alternate no-floor-plans shape validates", not errs, errs[:3])

    # The 24-optional-parameter API limit (discovered live): every object
    # must require its keys.
    def count_optional(s, n=0):
        t = s.get("type"); types = t if isinstance(t, list) else [t]
        if "object" in types:
            props = s.get("properties", {}); req = set(s.get("required", []))
            n += len([k for k in props if k not in req])
            for v in props.values():
                n = count_optional(v, n)
        if "array" in types and isinstance(s.get("items"), dict):
            n = count_optional(s["items"], n)
        return n
    check("optional parameter count within API limit (<=24)",
          count_optional(S) <= 24, count_optional(S))

    bad = dict(full)
    bad["surprise_field"] = 1
    check("unexpected top-level key rejected", bool(validate(bad, S)))

    # Helper behavior
    os.environ.pop("NIGHTSHIFT_STRUCTURED_OUTPUTS", None)
    check("enabled by default", T._structured_outputs_enabled() is True)
    kw = T._extraction_output_kwargs()
    check("kwargs carry output_config.format.json_schema",
          kw.get("output_config", {}).get("format", {}).get("type") == "json_schema")
    os.environ["NIGHTSHIFT_STRUCTURED_OUTPUTS"] = "0"
    check("env kill switch respected", T._extraction_output_kwargs() == {})
    os.environ.pop("NIGHTSHIFT_STRUCTURED_OUTPUTS", None)

    # Schema-rejection detection flips the process-wide switch
    class FakeBRE(T.anthropic.BadRequestError):
        def __init__(self, msg):
            Exception.__init__(self, msg)
            self.message = msg

        def __str__(self):
            return self.message

    fake = FakeBRE("output_config: schema compilation failed")
    check("schema rejection flips kill switch",
          T._maybe_disable_structured_outputs(fake) is True
          and T._extraction_output_kwargs() == {})
    check("non-schema 400 does not flip",
          T._maybe_disable_structured_outputs(FakeBRE("request too large")) is False)
    T._STRUCTURED_OUTPUTS_BROKEN = False  # reset

    check("prompt cache control is 1h ephemeral",
          T._PROMPT_CACHE_CONTROL == {"type": "ephemeral", "ttl": "1h"})

    print(f"\n=== {PASS} passed, {FAIL} failed ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
