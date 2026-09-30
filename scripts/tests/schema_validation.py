# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""A small JSON Schema validator for the published result schemas (standard library only).

It supports the keywords the schemas use: $ref (same file or a sibling file), type, enum, const, required,
properties, additionalProperties (false), items, minItems, maxItems, minimum, oneOf, anyOf, allOf, if/then, not.
"""

import json
from pathlib import Path

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"
TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def load(name):
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


class Validator:
    def __init__(self, root_name="result-envelope.schema.json"):
        self.documents = {root_name: load(root_name)}
        self.root_name = root_name

    def document(self, name):
        if name not in self.documents:
            self.documents[name] = load(name)
        return self.documents[name]

    def resolve(self, reference, current):
        name, _, fragment = reference.partition("#")
        name = name or current
        node = self.document(name)
        for part in [item for item in fragment.split("/") if item]:
            node = node[part]
        return node, name

    def problems(self, value, schema=None, name=None, path="$"):
        schema = self.documents[self.root_name] if schema is None else schema
        name = name or self.root_name
        found = []
        if "$ref" in schema:
            target, target_name = self.resolve(schema["$ref"], name)
            found += self.problems(value, target, target_name, path)
        for sub in schema.get("allOf", []):
            found += self.problems(value, sub, name, path)
        if "if" in schema and not self.problems(value, schema["if"], name, path) and "then" in schema:
            found += self.problems(value, schema["then"], name, path)
        if "oneOf" in schema:
            matches = [option for option in schema["oneOf"] if not self.problems(value, option, name, path)]
            if len(matches) != 1:
                found.append("%s matches %d of the allowed shapes, not exactly one" % (path, len(matches)))
        if "anyOf" in schema and not any(not self.problems(value, option, name, path) for option in schema["anyOf"]):
            found.append("%s matches none of the allowed shapes" % path)
        if "not" in schema and not self.problems(value, schema["not"], name, path):
            found.append("%s matches a shape that is not allowed" % path)
        if "const" in schema and value != schema["const"]:
            found.append("%s must be %r" % (path, schema["const"]))
        if "enum" in schema and value not in schema["enum"]:
            found.append("%s=%r is not one of %r" % (path, value, schema["enum"]))
        if "type" in schema:
            names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            if not any(self.is_type(value, item) for item in names):
                found.append("%s is %s, expected %s" % (path, type(value).__name__, "/".join(names)))
                return found
        if "minimum" in schema and isinstance(value, (int, float)) and value < schema["minimum"]:
            found.append("%s must be at least %r" % (path, schema["minimum"]))
        if isinstance(value, dict):
            found += ["%s.%s is missing" % (path, key) for key in schema.get("required", []) if key not in value]
            for key, sub in schema.get("properties", {}).items():
                if key in value:
                    found += self.problems(value[key], sub, name, "%s.%s" % (path, key))
            if schema.get("additionalProperties") is False:
                extra = set(value) - set(schema.get("properties", {}))
                found += ["%s.%s is not allowed" % (path, key) for key in sorted(extra)]
        if isinstance(value, list):
            if "minItems" in schema and len(value) < schema["minItems"]:
                found.append("%s needs at least %d items" % (path, schema["minItems"]))
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                found.append("%s allows at most %d items" % (path, schema["maxItems"]))
            if "items" in schema:
                for index, item in enumerate(value):
                    found += self.problems(item, schema["items"], name, "%s[%d]" % (path, index))
        return found

    @staticmethod
    def is_type(value, name):
        if name == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if name == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        return isinstance(value, TYPES[name])


def envelope_problems(document):
    return Validator().problems(document)
