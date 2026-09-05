"""
JSON Schema generation for tool parameters and results, from msgspec-supported
types and the annotations of tool functions.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import msgspec
import msgspec.json
from django.core.exceptions import ImproperlyConfigured


def is_struct_type(obj: Any) -> bool:
    try:
        return issubclass(obj, msgspec.Struct)
    except TypeError:
        return False


def inspect_types(func: Callable[..., Any], tool_name: str) -> tuple[Any, Any]:
    """
    Read a tool function's input type from the annotation of its params
    argument, the second parameter, plus its return annotation. A function
    taking only the request has no input type.
    """
    parameters = list(inspect.signature(func).parameters.values())
    if len(parameters) not in (1, 2):
        raise ImproperlyConfigured(
            f"Tool function for {tool_name!r} must accept the request,"
            " optionally followed by one params argument."
        )
    try:
        # On Python 3.14+, this resolves annotations with annotationlib.
        annotations = inspect.get_annotations(func, eval_str=True)
    except NameError as exc:
        raise ImproperlyConfigured(
            f"Could not resolve the type annotations of the tool function for"
            f" {tool_name!r} ({exc}). Ensure the types are resolvable at"
            " module level."
        ) from exc
    input_type = None
    if len(parameters) == 2:
        parameter = parameters[1]
        if parameter.kind not in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            raise ImproperlyConfigured(
                f"Tool function for {tool_name!r} must accept its params"
                f" argument positionally, unlike {parameter.name!r}."
            )
        if parameter.name not in annotations:
            raise ImproperlyConfigured(
                f"Tool function for {tool_name!r} params argument"
                f" {parameter.name!r} requires a type annotation, or pass"
                " input_schema."
            )
        input_type = annotations[parameter.name]
    return input_type, annotations.get("return")


def type_schema(type_: Any) -> dict[str, Any]:
    """
    Generate a JSON Schema for a msgspec-supported type, inlining the
    top-level $ref so that the properties sit at the schema root.
    """
    schema = msgspec.json.schema(type_)
    ref = schema.get("$ref", "")
    defs = schema.get("$defs")
    if not (
        isinstance(ref, str) and ref.startswith("#/$defs/") and isinstance(defs, dict)
    ):
        return schema
    name = ref.removeprefix("#/$defs/")
    # A self-referencing (recursive) definition cannot be inlined.
    if f'"#/$defs/{name}"'.encode() in msgspec.json.encode(defs):
        return schema
    root = dict(defs[name])
    remaining = {key: value for key, value in defs.items() if key != name}
    if remaining:
        root["$defs"] = remaining
    return root


def reject_header_annotations(schema: object) -> None:
    """
    Raise ImproperlyConfigured if a schema uses the x-mcp-header extension.

    Declaring it would oblige this server to validate the mirrored
    Mcp-Param-* headers on every call, which django-mcpz does not implement.
    """
    if isinstance(schema, dict):
        if "x-mcp-header" in schema:
            raise ImproperlyConfigured(
                "The x-mcp-header schema extension is not supported by"
                " django-mcpz. Remove it from the input schema."
            )
        for value in schema.values():
            reject_header_annotations(value)
    elif isinstance(schema, list):
        for item in schema:
            reject_header_annotations(item)
