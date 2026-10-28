"""Small complete SPL grammar, parsed by the existing Lark dependency.

Pipelines retain order: WHERE before a LEFT JOIN is not a post-join WHERE.
Identifiers are metadata names, never SQL fragments; :parameters are separate.
The Pipeline dataclasses are also the typed construction surface for
server-side callers (bridge adapters): such callers must run validate_pipeline.
DISTINCT and contains are typed-only operators with no text grammar yet.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from lark import Lark, Transformer, UnexpectedInput
from lark.exceptions import VisitError

from .contracts import QueryError, stable_hash

_GRAMMAR = r"""
start: "from"i ident "as"i ident ("|" command)*
?command: where | fields | join | stats | sort | limit
where: "where"i expr
fields: "fields"i projection ("," projection)*
projection: ref ("as"i ident)?
join: "join"i ident "as"i ident "on"i ref "=" ref join_type?
join_type: "type"i "=" JOIN_KIND
stats: "stats"i aggregate ("," aggregate)* group?
aggregate: AGG "(" [ref] ")" "as"i ident
group: "by"i ref ("," ref)*
sort: "sort"i sort_item ("," sort_item)*
sort_item: output_ref DIRECTION? null_order?
null_order: "nulls"i NULL_ORDER
?output_ref: ref | ident -> output_name
limit: "limit"i UINT ("offset"i UINT)?
?expr: or_expr
?or_expr: and_expr ("or"i and_expr)* -> or_expr
?and_expr: not_expr ("and"i not_expr)* -> and_expr
?not_expr: "not"i not_expr -> negate
         | "(" expr ")"
         | predicate
?predicate: ref COMP value -> compare
          | ref "=" value -> equal
          | ref "in"i "(" value ("," value)* ")" -> in_list
          | ref "is"i "null"i -> is_null
          | ref "is"i "not"i "null"i -> is_not_null
?value: ":" ident -> parameter
      | SIGNED_NUMBER -> number
      | ESCAPED_STRING -> string
      | "true"i -> true
      | "false"i -> false
ref: ident "." ident
?ident: CNAME -> identifier
      | QUOTED_IDENT -> quoted_identifier
AGG.2: /(?i:count_distinct|count|sum|avg|min|max)(?=\s*\()/
JOIN_KIND: /(?i:inner|left)/
DIRECTION: /(?i:asc|desc)/
NULL_ORDER: /(?i:first|last)/
COMP: "!=" | "<>" | ">=" | "<=" | ">" | "<"
UINT: /0|[1-9][0-9]*/
QUOTED_IDENT: /`([^`\x00]|``)+`/
%import common.CNAME
%import common.SIGNED_NUMBER
%import common.ESCAPED_STRING
%import common.WS
%ignore WS
"""


@dataclass(frozen=True)
class Ref:
    alias: str
    name: str


@dataclass(frozen=True)
class Value:
    value: Any
    parameter: bool = False


@dataclass(frozen=True)
class Expr:
    op: str
    args: tuple[Any, ...]


@dataclass(frozen=True)
class Command:
    op: str
    args: tuple[Any, ...]


@dataclass(frozen=True)
class Pipeline:
    source: str
    alias: str
    commands: tuple[Command, ...]

    @property
    def sources(self) -> list[tuple[str, str]]:
        return [(self.source, self.alias)] + [
            (cmd.args[0], cmd.args[1]) for cmd in self.commands if cmd.op == "join"]


class _Build(Transformer):
    def identifier(self, args): return str(args[0])
    def quoted_identifier(self, args): return str(args[0])[1:-1].replace("``", "`")
    def ref(self, args): return Ref(*args)
    def parameter(self, args): return Value(args[0], True)
    def number(self, args):
        text = str(args[0])
        return Value(Decimal(text) if any(c in text.lower() for c in ".e") else int(text))
    def string(self, args): return Value(json.loads(str(args[0])))
    def true(self, args): return Value(True)
    def false(self, args): return Value(False)
    def compare(self, args): return Expr(str(args[1]), (args[0], args[2]))
    def equal(self, args): return Expr("=", tuple(args))
    def in_list(self, args): return Expr("in", tuple(args))
    def is_null(self, args): return Expr("is_null", tuple(args))
    def is_not_null(self, args): return Expr("is_not_null", tuple(args))
    def negate(self, args): return Expr("not", tuple(args))
    def and_expr(self, args): return args[0] if len(args) == 1 else Expr("and", tuple(args))
    def or_expr(self, args): return args[0] if len(args) == 1 else Expr("or", tuple(args))
    def where(self, args): return Command("where", tuple(args))
    def projection(self, args): return (args[0], args[1] if len(args) == 2 else args[0].name)
    def fields(self, args): return Command("fields", tuple(args))
    def join_type(self, args): return str(args[0]).lower()
    def join(self, args): return Command("join", (*args[:4], args[4] if len(args) > 4 else "inner"))
    def aggregate(self, args):
        if len(args) == 2: return (str(args[0]).lower(), None, args[1])
        return (str(args[0]).lower(), args[1], args[2])
    def group(self, args): return ("group", tuple(args))
    def stats(self, args): return Command("stats", tuple(args))
    def output_name(self, args): return str(args[0])
    def null_order(self, args): return str(args[0]).lower()
    def sort_item(self, args):
        direction = str(args[1]).lower() if len(args) > 1 and str(args[1]).lower() in {"asc", "desc"} else "asc"
        nulls = str(args[-1]).lower() if len(args) > 1 and str(args[-1]).lower() in {"first", "last"} else "last"
        return (args[0], direction, nulls)
    def sort(self, args): return Command("sort", tuple(args))
    def limit(self, args): return Command("limit", tuple(int(v) for v in args))
    def start(self, args): return Pipeline(args[0], args[1], tuple(args[2:]))


_PARSER = Lark(_GRAMMAR, parser="lalr", maybe_placeholders=False)


def validate_pipeline(pipeline: Pipeline) -> Pipeline:
    """Shared checks for parsed text and typed-constructed pipelines."""
    if not isinstance(pipeline, Pipeline):
        raise QueryError(422, "invalid_pipeline", "A typed Pipeline object is required")
    if len(pipeline.commands) > 64 or len(pipeline.sources) > 16:
        raise QueryError(422, "query_too_complex", "Too many pipeline operators or sources")
    aliases = [alias for _, alias in pipeline.sources]
    identifiers = aliases + [source for source, _ in pipeline.sources]
    if any(len(identifier) > 63 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier)
           for identifier in identifiers):
        raise QueryError(422, "invalid_identifier", "Source binding names and aliases must be ASCII identifiers")
    if len(aliases) != len(set(aliases)):
        raise QueryError(422, "duplicate_alias", "Source aliases must be unique")
    return pipeline


def parse(text: str) -> Pipeline:
    if not isinstance(text, str) or not text.strip() or len(text) > 32768:
        raise QueryError(422, "invalid_query", "Query must contain 1..32768 characters")
    try:
        tree = _PARSER.parse(text)
        if sum(1 for _ in tree.iter_subtrees()) > 2048:
            raise QueryError(422, "query_too_complex", "Query expression exceeds parser budget")
        result = _Build().transform(tree)
    except UnexpectedInput as exc:
        raise QueryError(422, "invalid_syntax", "Unsupported or invalid SPL syntax",
                         position={"line": exc.line, "column": exc.column,
                                   "offset": exc.pos_in_stream}) from exc
    except (VisitError, RecursionError, ValueError) as exc:
        raise QueryError(422, "invalid_query", "Invalid query value or excessive nesting") from exc
    return validate_pipeline(result)


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, Ref):
        return {"$ref": [value.alias, value.name]}
    if isinstance(value, Value):
        payload = {"isParam": value.parameter}
        payload["param" if value.parameter else "raw"] = (
            value.value if not isinstance(value.value, Decimal) else {"$decimal": str(value.value)})
        return {"$value": payload}
    if isinstance(value, Expr):
        return {"$expr": [value.op, [_to_jsonable(a) for a in value.args]]}
    if isinstance(value, Command):
        return {"$cmd": [value.op, [_to_jsonable(a) for a in value.args]]}
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise QueryError(422, "invalid_pipeline", f"Pipeline holds unserializable {type(value).__name__}")


def _from_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        if "$decimal" in value: return Decimal(value["$decimal"])
        if "$ref" in value: return Ref(*value["$ref"])
        if "$value" in value:
            payload = value["$value"]
            if payload.get("isParam"):
                return Value(payload.get("param"), True)
            return Value(_from_jsonable_literal(payload.get("raw")), False)
        if "$expr" in value:
            return Expr(value["$expr"][0], tuple(_from_jsonable(a) for a in value["$expr"][1]))
        if "$cmd" in value:
            return Command(value["$cmd"][0], tuple(_from_jsonable(a) for a in value["$cmd"][1]))
        raise QueryError(422, "invalid_pipeline", "Unknown serialized node")
    if isinstance(value, list):
        return tuple(_from_jsonable(item) for item in value)
    return value


def _from_jsonable_literal(value: Any) -> Any:
    if isinstance(value, dict) and "$decimal" in value:
        return Decimal(value["$decimal"])
    return value


def pipeline_to_dict(pipeline: Pipeline) -> dict:
    return {"source": pipeline.source, "alias": pipeline.alias,
            "commands": [_to_jsonable(command) for command in pipeline.commands]}


def pipeline_from_dict(data: dict) -> Pipeline:
    try:
        pipeline = Pipeline(data["source"], data["alias"],
                            tuple(_from_jsonable(command) for command in data["commands"]))
    except (KeyError, TypeError, ArithmeticError) as exc:
        raise QueryError(422, "invalid_pipeline", "Serialized pipeline is malformed") from exc
    return validate_pipeline(pipeline)


def params_in(commands) -> set[str]:
    """Parameter names referenced by commands (for per-source param scoping)."""
    names = set()
    for arg in refs_in_values(commands):
        if isinstance(arg, Value) and arg.parameter:
            names.add(str(arg.value))
    return names


def refs_in_values(value: Any):
    for item in _iter_nodes(value):
        yield item


def _iter_nodes(value: Any):
    if isinstance(value, (Ref, Value, Expr)):
        yield value
    if isinstance(value, (Expr, Command)):
        for arg in value.args:
            yield from _iter_nodes(arg)
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _iter_nodes(item)


def plan_fingerprint(pipeline: Pipeline, params: dict[str, Any], origin: Any = None) -> str:
    """Canonical identity of a typed plan; covers derived definitions via origin."""
    return stable_hash({"pipeline": pipeline_to_dict(pipeline), "params": params,
                        "origin": origin})
