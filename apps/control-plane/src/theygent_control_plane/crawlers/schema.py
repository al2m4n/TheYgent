"""An actor input schema → the function a model calls, plus the input the node keeps fixed.

An actor's input schema is written for a form: dozens of fields, proxy settings, CSS selectors,
hidden tuning knobs, and editors (``requestListSources``, ``proxy``, ``json``) whose values have
shapes no model should be asked to produce. A crawler node splits it in two:

* **exposed** — the few fields the model fills per call (the search query, the URL). They become
  the tool's JSON-Schema ``parameters``, stored on the node as ``parameterSchema``.
* **fixed** — everything else, set once by the person building the agent (or left to the actor's
  defaults). The node sends it on every call; the model never sees it.

At call time :func:`build_run_input` maps the model's simple values back into the shapes the actor
wants: a URL string becomes ``[{"url": ...}]`` for a ``requestListSources`` field, and a field the
node offers as one value (``scalar``) is wrapped into a one-item list. The URL coercion is marked
on the stored schema property (``x-theygent-coerce``) because the actor's full schema is not kept
on the node; :func:`model_parameters` strips the marker before the schema reaches a model.

Everything here is pure — no I/O — so it is shared by the catalog endpoints and both runtimes.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any

#: Editors whose values a model cannot sensibly write: they stay fixed on the node.
FIXED_EDITORS = frozenset(
    {"hidden", "proxy", "javascript", "python", "json", "keyValue", "fileupload", "schemaBased"}
)
SIMPLE_TYPES = frozenset({"string", "integer", "number", "boolean"})
#: Fields offered to a model by default, beyond the required ones.
DEFAULT_MAX_EXPOSED = 5
#: The schema-property marker recording how a model value is reshaped for the actor.
COERCE_KEY = "x-theygent-coerce"
#: A model value for this field is a URL (or list of URLs) the actor wants as ``[{"url": …}]``.
URL_OBJECTS = "url_objects"

_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


@dataclass
class Conversion:
    """The node config derived from an actor input schema: what the model fills, what stays
    fixed, and what nobody can fill (a required field with no default the model can't write)."""

    parameter_schema: dict[str, Any]
    expose: list[str]
    scalar: list[str]
    input: dict[str, Any]
    unfillable: list[str] = field(default_factory=list)
    total_fields: int = 0


def clean_text(text: str | None, limit: int = 280) -> str:
    """A schema description as plain text for a model: tags stripped, whitespace collapsed,
    capped at ``limit`` characters."""
    text = _SPACE.sub(" ", _TAGS.sub("", text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def model_shape(name: str, prop: dict[str, Any]) -> dict[str, Any] | None:
    """The JSON Schema a model fills for one field, or ``None`` when a model can't fill it (a
    secret, a fixed editor, an object or an array of objects)."""
    editor = prop.get("editor")
    if prop.get("isSecret") or editor in FIXED_EDITORS:
        return None
    ptype = prop.get("type")
    desc = clean_text(prop.get("description") or prop.get("title"))
    if ptype in SIMPLE_TYPES:
        out: dict[str, Any] = {"type": ptype, "description": desc}
        for key in ("enum", "minimum", "maximum"):
            if key in prop:
                out[key] = prop[key]
        return out
    if ptype == "array":
        items = prop.get("items") or {}
        if editor == "requestListSources" or name in ("startUrls", "urls"):
            return {
                "type": "array",
                "items": {"type": "string"},
                "description": desc or "URLs to visit.",
                COERCE_KEY: URL_OBJECTS,
            }
        if editor in ("stringList", "select") or items.get("type") == "string":
            out = {"type": "array", "items": {"type": "string"}, "description": desc}
            if isinstance(items.get("enum"), list):
                out["items"]["enum"] = items["enum"]
            return out
    return None


#: Field names that size the work (and, for pay-per-event actors, the bill): they stay fixed by
#: default rather than being handed to the model.
_SIZING_NAME = re.compile(r"max|limit|count|perSearch|depth|pages", re.IGNORECASE)


def default_expose(schema: dict[str, Any], max_exposed: int = DEFAULT_MAX_EXPOSED) -> list[str]:
    """The fields offered to a model when the builder hasn't chosen. The required fields a model
    can fill come first; then the fillable fields of the schema's first section (a schema's first
    section holds its primary fields) that the actor can't do without — no default, no prefill,
    and not a sizing knob (``maxResults``, ``limit``, …), because a model offered result counts and
    paid add-ons sets the spend. Up to ``max_exposed``; when nothing qualifies, the first fillable
    field, so a fresh node still takes an argument."""
    props: dict[str, Any] = schema.get("properties") or {}
    required = list(schema.get("required") or [])
    fillable = [n for n, p in props.items() if isinstance(p, dict) and model_shape(n, p)]
    expose = [n for n in required if n in fillable]
    for index, (name, prop) in enumerate(props.items()):
        if len(expose) >= max_exposed:
            break
        if index > 0 and prop.get("sectionCaption") and name not in required:
            break
        if name in expose or name not in fillable:
            continue
        if "default" in prop or "prefill" in prop or _SIZING_NAME.search(name):
            continue
        expose.append(name)
    return expose or fillable[:1]


def convert(
    schema: dict[str, Any],
    *,
    expose: list[str] | None = None,
    scalar: list[str] | None = None,
    fixed: dict[str, Any] | None = None,
    max_exposed: int = DEFAULT_MAX_EXPOSED,
) -> Conversion:
    """Split an actor input schema into the exposed parameters and the fixed input.

    ``expose`` names the fields to offer (the builder's choice); without it
    :func:`default_expose` picks. Names a model can't fill are dropped. ``scalar`` names list
    fields to offer as a single string; without it every exposed list field is scalar — local
    models write list arguments unreliably, and one value per call is the safer shape. ``fixed``
    is the builder's input for everything else; a required field that is neither exposed nor
    fixed falls back to the schema's ``prefill`` or ``default``, and is reported ``unfillable``
    when it has neither."""
    props: dict[str, Any] = schema.get("properties") or {}
    required = list(schema.get("required") or [])
    chosen = default_expose(schema, max_exposed) if expose is None else list(expose)

    parameters: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    scalar_out: list[str] = []
    for name in dict.fromkeys(chosen):
        shape = model_shape(name, props.get(name) or {}) if name in props else None
        if shape is None:
            continue
        is_list = shape.get("type") == "array"
        if is_list and (scalar is None or name in scalar):
            choices = (shape.get("items") or {}).get("enum")
            shape = {k: v for k, v in shape.items() if k not in ("type", "items")} | {
                "type": "string"
            }
            if choices:
                shape["enum"] = choices
            scalar_out.append(name)
        parameters["properties"][name] = shape
        if name in required:
            parameters["required"].append(name)

    fixed_input = {k: v for k, v in (fixed or {}).items() if k not in parameters["properties"]}
    unfillable: list[str] = []
    for name in required:
        if name in parameters["properties"] or name in fixed_input:
            continue
        prop = props.get(name) or {}
        for key in ("prefill", "default"):
            if key in prop:
                fixed_input[name] = copy.deepcopy(prop[key])
                break
        else:
            unfillable.append(name)

    return Conversion(
        parameter_schema=parameters,
        expose=list(parameters["properties"]),
        scalar=scalar_out,
        input=fixed_input,
        unfillable=unfillable,
        total_fields=len(props),
    )


def model_parameters(parameter_schema: dict[str, Any] | None) -> dict[str, Any]:
    """The stored schema as a model reads it: the coercion markers removed."""
    schema = copy.deepcopy(parameter_schema or {"type": "object", "properties": {}})
    for prop in (schema.get("properties") or {}).values():
        if isinstance(prop, dict):
            prop.pop(COERCE_KEY, None)
    return schema


def _coercions(parameter_schema: dict[str, Any] | None, scalar: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, prop in ((parameter_schema or {}).get("properties") or {}).items():
        if isinstance(prop, dict) and prop.get(COERCE_KEY) == URL_OBJECTS:
            out[name] = URL_OBJECTS
    for name in scalar:
        out.setdefault(name, "wrap_list")
    return out


def _coerce_value(kind: str, value: Any) -> Any:
    if isinstance(value, str) and value.lstrip().startswith("["):
        # Offered one string, models sometimes write a JSON-encoded list into it.
        try:
            value = json.loads(value)
        except ValueError:
            pass
    if kind == URL_OBJECTS:
        values = value if isinstance(value, list) else [value]
        return [{"url": v} if isinstance(v, str) else v for v in values]
    return value if isinstance(value, list) else [value]


def build_run_input(
    fixed: dict[str, Any],
    args: dict[str, Any],
    *,
    parameter_schema: dict[str, Any] | None,
    scalar: list[str],
) -> dict[str, Any]:
    """The actor input for one model call: the node's fixed input, then the model's arguments in
    the actor's shapes. Arguments outside the exposed set are dropped, so a model cannot reach a
    fixed field (result counts, paid add-ons, proxy) by inventing it."""
    exposed = set(((parameter_schema or {}).get("properties") or {}).keys())
    coercions = _coercions(parameter_schema, scalar)
    run_input = copy.deepcopy(fixed)
    for name, value in args.items():
        if name not in exposed:
            continue
        kind = coercions.get(name)
        run_input[name] = _coerce_value(kind, value) if kind else value
    return run_input


def coerce_input(
    run_input: dict[str, Any],
    *,
    parameter_schema: dict[str, Any] | None,
    scalar: list[str],
) -> dict[str, Any]:
    """A step's resolved input with the exposed fields reshaped the same way a model's values
    are — so ``startUrls: "$in"`` fed a URL string reaches the actor as ``[{"url": …}]``."""
    coercions = _coercions(parameter_schema, scalar)
    return {
        name: _coerce_value(coercions[name], value) if name in coercions else value
        for name, value in run_input.items()
    }


def project(items: list[Any], fields: list[str]) -> list[Any]:
    """Keep only ``fields`` (top-level keys) of each dataset item; no fields keeps everything."""
    if not fields:
        return list(items)
    return [{k: i[k] for k in fields if k in i} if isinstance(i, dict) else i for i in items]


def budget(items: list[Any], *, max_chars: int, fields: list[str] | None = None) -> str:
    """Dataset items as the text a model reads: only the chosen fields, each item given an equal
    share of ``max_chars`` so every result is seen, long items cut with a marker. A crawl easily
    returns megabytes; a local model's context holds a few thousand tokens of it."""
    if not items:
        return "[no items]"
    rows = project(items, fields or [])
    share = max(200, max_chars // len(rows))
    out: list[str] = []
    used = 0
    for item in rows:
        if used >= max_chars:
            break
        text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        text = text if len(text) <= share else text[: share - 1] + "…"
        out.append(text)
        used += len(text)
    if len(out) < len(rows):
        out.append(f"[{len(rows) - len(out)} more item(s) not shown]")
    return "\n".join(out)


#: The input-schema keys a form needs to render one field.
_FORM_KEYS = (
    "title",
    "type",
    "editor",
    "default",
    "prefill",
    "example",
    "enum",
    "enumTitles",
    "minimum",
    "maximum",
    "unit",
    "sectionCaption",
    "isSecret",
    "nullable",
)


def form_fields(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """The actor's input fields as a form renders them, in schema order: each with its type and
    editor, defaults, choices, whether it is required, and whether a model could fill it
    (``exposable``). List items' choices are lifted to ``enum`` so one select renders both."""
    props: dict[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    fields: list[dict[str, Any]] = []
    for name, prop in props.items():
        if not isinstance(prop, dict):
            continue
        entry: dict[str, Any] = {"name": name}
        for key in _FORM_KEYS:
            if key in prop:
                entry[key] = prop[key]
        items = prop.get("items") or {}
        if isinstance(items, dict):
            if "enum" not in entry and isinstance(items.get("enum"), list):
                entry["enum"] = items["enum"]
                if isinstance(items.get("enumTitles"), list):
                    entry["enumTitles"] = items["enumTitles"]
            if items.get("type"):
                entry["itemType"] = items["type"]
        entry["description"] = clean_text(prop.get("description"), 600)
        entry["required"] = name in required
        entry["exposable"] = model_shape(name, prop) is not None
        fields.append(entry)
    return fields
