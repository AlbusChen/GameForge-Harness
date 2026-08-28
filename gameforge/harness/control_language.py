from __future__ import annotations

import ast
import json
import operator
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import Field

from gameforge.harness.base import StrictModel
from gameforge.harness.errors import InfrastructureFailure


class ControlLimits(StrictModel):
    maximum_source_bytes: int = Field(default=64 * 1024, ge=1, le=1024 * 1024)
    maximum_steps: int = Field(default=20_000, ge=1, le=1_000_000)
    maximum_loop_iterations: int = Field(default=1_000, ge=1, le=100_000)
    maximum_call_depth: int = Field(default=16, ge=1, le=100)
    maximum_wall_seconds: float = Field(default=10, gt=0, le=300)
    maximum_value_bytes: int = Field(default=4 * 1024 * 1024, ge=1, le=64 * 1024 * 1024)


class ControlExecution(StrictModel):
    value: object = None
    steps: int = Field(ge=0)
    variables: dict[str, object]
    functions: dict[str, str]


class ControlProgramError(RuntimeError):
    pass


@dataclass(frozen=True)
class SafeHostFunction:
    name: str
    callback: Callable[..., object]

    def invoke(self, arguments: list[object], keywords: dict[str, object]) -> object:
        return self.callback(*arguments, **keywords)


class SafeNamespace:
    def __init__(self, name: str, members: Mapping[str, object]) -> None:
        self.name = name
        self._members = dict(members)

    def member(self, name: str) -> object:
        if name.startswith("_") or name not in self._members:
            raise ControlProgramError(f"namespace member is not available: {self.name}.{name}")
        return self._members[name]


@dataclass(frozen=True)
class _UserFunction:
    node: ast.FunctionDef


class _ReturnSignal(Exception):
    def __init__(self, value: object) -> None:
        super().__init__()
        self.value = value


class _BreakSignal(Exception):
    pass


class _ContinueSignal(Exception):
    pass


_BINARY_OPERATORS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.BitAnd: operator.and_,
    ast.BitOr: operator.or_,
    ast.BitXor: operator.xor,
    ast.LShift: operator.lshift,
    ast.RShift: operator.rshift,
}
_UNARY_OPERATORS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.Not: operator.not_,
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_COMPARISON_OPERATORS: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda left, right: left in right,
    ast.NotIn: lambda left, right: left not in right,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}
_LITERAL_ALIASES: dict[str, object] = {
    "true": True,
    "false": False,
    "null": None,
}
_MAPPING_KEY_PREFIX = "\u0000gameforge-control-key:"
_MAPPING_STRING_KEY_PREFIX = f"{_MAPPING_KEY_PREFIX}string:"
_MAPPING_VALUE_KEY_PREFIX = f"{_MAPPING_KEY_PREFIX}json:"


def _mapping_key_component(value: object) -> bool:
    if value is None or isinstance(value, str | int | float | bool):
        return True
    return isinstance(value, list | tuple) and all(_mapping_key_component(item) for item in value)


def _encode_mapping_key(value: object) -> str:
    """Bridge common Python-like composite keys onto JSON object keys."""
    if isinstance(value, str):
        if value.startswith(_MAPPING_KEY_PREFIX):
            return f"{_MAPPING_STRING_KEY_PREFIX}{value}"
        return value
    if not _mapping_key_component(value):
        raise ControlProgramError(
            "mapping keys must be strings, JSON scalars, or bounded tuple/list composites"
        )
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise ControlProgramError("mapping key is not JSON serializable") from error
    return f"{_MAPPING_VALUE_KEY_PREFIX}{encoded}"


def _decode_mapping_key(value: str) -> object:
    if value.startswith(_MAPPING_STRING_KEY_PREFIX):
        return value.removeprefix(_MAPPING_STRING_KEY_PREFIX)
    if value.startswith(_MAPPING_VALUE_KEY_PREFIX):
        try:
            return json.loads(value.removeprefix(_MAPPING_VALUE_KEY_PREFIX))
        except json.JSONDecodeError:
            return value
    return value


class SafeInterpreter:
    def __init__(
        self,
        *,
        namespaces: Mapping[str, SafeNamespace],
        variables: Mapping[str, object] | None = None,
        function_sources: Mapping[str, str] | None = None,
        limits: ControlLimits | None = None,
    ) -> None:
        self.namespaces = dict(namespaces)
        self.variables = _json_copy(dict(variables or {}))
        self.function_sources = dict(function_sources or {})
        self.functions: dict[str, _UserFunction] = {}
        self.limits = limits or ControlLimits()
        self.steps = 0
        self.call_depth = 0
        self._started = 0.0
        self._last_value: object = None
        self._builtins = _safe_builtins(self.limits)
        self._restore_functions()
        self._validate_identifiers()

    def execute(self, source: str) -> ControlExecution:
        if len(source.encode("utf-8")) > self.limits.maximum_source_bytes:
            raise ControlProgramError("control cell exceeds source byte limit")
        try:
            module = ast.parse(source, mode="exec")
        except SyntaxError as error:
            raise ControlProgramError(f"invalid control syntax: {error.msg}") from error
        variables_before = _json_copy(self.variables)
        functions_before = dict(self.functions)
        sources_before = dict(self.function_sources)
        self.steps = 0
        self._started = time.monotonic()
        self._last_value = None
        try:
            self._execute_block(module.body, self.variables)
            self._validate_state()
        except Exception as error:
            self.variables = variables_before
            self.functions = functions_before
            self.function_sources = sources_before
            if isinstance(error, (ControlProgramError, InfrastructureFailure)):
                raise
            raise ControlProgramError(str(error)) from error
        return ControlExecution(
            value=_json_copy(self._last_value),
            steps=self.steps,
            variables=_json_copy(self.variables),
            functions=dict(self.function_sources),
        )

    def _restore_functions(self) -> None:
        for name, source in self.function_sources.items():
            try:
                module = ast.parse(source, mode="exec")
            except SyntaxError as error:
                raise ControlProgramError(f"invalid persisted function: {name}") from error
            if len(module.body) != 1 or not isinstance(module.body[0], ast.FunctionDef):
                raise ControlProgramError(f"persisted function is not a definition: {name}")
            node = module.body[0]
            self._validate_function_definition(node)
            if node.name != name:
                raise ControlProgramError(f"persisted function name mismatch: {name}")
            self.functions[name] = _UserFunction(node)

    def _execute_block(self, statements: list[ast.stmt], scope: dict[str, object]) -> None:
        for statement in statements:
            self._step(statement)
            self._execute_statement(statement, scope)

    def _execute_statement(self, node: ast.stmt, scope: dict[str, object]) -> None:
        if isinstance(node, ast.Expr):
            self._last_value = self._evaluate(node.value, scope)
            return
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1:
                raise ControlProgramError("chained assignment is not allowed")
            self._assign(node.targets[0], self._evaluate(node.value, scope), scope)
            return
        if isinstance(node, ast.AugAssign):
            current = self._evaluate(node.target, scope)
            operation = _BINARY_OPERATORS.get(type(node.op))
            if operation is None:
                raise ControlProgramError("augmented operator is not allowed")
            self._assign(node.target, operation(current, self._evaluate(node.value, scope)), scope)
            return
        if isinstance(node, ast.If):
            branch = node.body if bool(self._evaluate(node.test, scope)) else node.orelse
            self._execute_block(branch, scope)
            return
        if isinstance(node, ast.For):
            items = self._bounded_iterable(self._evaluate(node.iter, scope))
            broken = False
            for item in items:
                self._assign_pattern(node.target, item, scope)
                try:
                    self._execute_block(node.body, scope)
                except _ContinueSignal:
                    continue
                except _BreakSignal:
                    broken = True
                    break
            if not broken:
                self._execute_block(node.orelse, scope)
            return
        if isinstance(node, ast.While):
            broken = False
            iterations = 0
            while bool(self._evaluate(node.test, scope)):
                iterations += 1
                if iterations > self.limits.maximum_loop_iterations:
                    raise ControlProgramError("while loop iteration limit exceeded")
                try:
                    self._execute_block(node.body, scope)
                except _ContinueSignal:
                    continue
                except _BreakSignal:
                    broken = True
                    break
            if not broken:
                self._execute_block(node.orelse, scope)
            return
        if isinstance(node, ast.FunctionDef):
            self._validate_function_definition(node)
            self.functions[node.name] = _UserFunction(node)
            self.function_sources[node.name] = ast.unparse(node)
            return
        if isinstance(node, ast.Return):
            raise _ReturnSignal(None if node.value is None else self._evaluate(node.value, scope))
        if isinstance(node, ast.Try):
            self._execute_try(node, scope)
            return
        if isinstance(node, ast.Break):
            raise _BreakSignal
        if isinstance(node, ast.Continue):
            raise _ContinueSignal
        if isinstance(node, ast.Pass):
            return
        raise ControlProgramError(f"statement is not allowed: {type(node).__name__}")

    def _execute_try(self, node: ast.Try, scope: dict[str, object]) -> None:
        if node.finalbody:
            raise ControlProgramError("try/finally is not allowed")
        try:
            self._execute_block(node.body, scope)
        except InfrastructureFailure:
            raise
        except (_ReturnSignal, _BreakSignal, _ContinueSignal):
            raise
        except Exception as error:
            matched = False
            for handler in node.handlers:
                if not _allowed_exception_handler(handler.type):
                    raise ControlProgramError("exception handler type is not allowed") from error
                matched = True
                if handler.name is not None:
                    self._assign_name(handler.name, str(error), scope)
                self._execute_block(handler.body, scope)
                break
            if not matched:
                raise
        else:
            self._execute_block(node.orelse, scope)

    def _evaluate(self, node: ast.expr, scope: dict[str, object]) -> object:
        self._step(node)
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, str | int | float | bool | None):
                raise ControlProgramError("constant type is not allowed")
            return node.value
        if isinstance(node, ast.Name):
            return self._resolve_name(node.id, scope)
        if isinstance(node, ast.List):
            return [self._evaluate(item, scope) for item in node.elts]
        if isinstance(node, ast.Tuple):
            return [self._evaluate(item, scope) for item in node.elts]
        if isinstance(node, ast.Dict):
            result: dict[str, object] = {}
            for key_node, value_node in zip(node.keys, node.values, strict=True):
                if key_node is None:
                    expanded = self._evaluate(value_node, scope)
                    if not isinstance(expanded, dict):
                        raise ControlProgramError("dictionary expansion requires an object")
                    result.update(expanded)
                    continue
                key = _encode_mapping_key(self._evaluate(key_node, scope))
                result[key] = self._evaluate(value_node, scope)
            return result
        if isinstance(node, ast.Subscript):
            value = self._evaluate(node.value, scope)
            index = self._evaluate_slice(node.slice, scope)
            if isinstance(value, dict):
                index = _encode_mapping_key(index)
            try:
                return value[index]  # type: ignore[index]
            except (IndexError, KeyError, TypeError) as error:
                raise ControlProgramError(f"invalid subscript: {error}") from error
        if isinstance(node, ast.Attribute):
            return self._safe_attribute(self._evaluate(node.value, scope), node.attr)
        if isinstance(node, ast.Call):
            return self._evaluate_call(node, scope)
        if isinstance(node, ast.Await):
            return self._evaluate(node.value, scope)
        if isinstance(node, ast.BinOp):
            operation = _BINARY_OPERATORS.get(type(node.op))
            if operation is None:
                raise ControlProgramError("binary operator is not allowed")
            left = self._evaluate(node.left, scope)
            right = self._evaluate(node.right, scope)
            if isinstance(node.op, ast.Mod) and isinstance(left, str) and isinstance(right, list):
                right = tuple(right)
            try:
                return operation(left, right)
            except (TypeError, ValueError, ZeroDivisionError) as error:
                raise ControlProgramError(f"invalid binary operation: {error}") from error
        if isinstance(node, ast.UnaryOp):
            operation = _UNARY_OPERATORS.get(type(node.op))
            if operation is None:
                raise ControlProgramError("unary operator is not allowed")
            return operation(self._evaluate(node.operand, scope))
        if isinstance(node, ast.BoolOp):
            values = node.values
            result = self._evaluate(values[0], scope)
            for value in values[1:]:
                if isinstance(node.op, ast.And):
                    if not result:
                        return result
                elif result:
                    return result
                result = self._evaluate(value, scope)
            return result
        if isinstance(node, ast.Compare):
            left = self._evaluate(node.left, scope)
            for operation_node, comparator in zip(node.ops, node.comparators, strict=True):
                right = self._evaluate(comparator, scope)
                operation = _COMPARISON_OPERATORS.get(type(operation_node))
                comparison_left = left
                if isinstance(right, dict) and isinstance(operation_node, ast.In | ast.NotIn):
                    comparison_left = _encode_mapping_key(left)
                if operation is None or not operation(comparison_left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            branch = node.body if self._evaluate(node.test, scope) else node.orelse
            return self._evaluate(branch, scope)
        if isinstance(node, ast.ListComp):
            return self._evaluate_list_comprehension(node, scope)
        if isinstance(node, ast.GeneratorExp):
            return self._evaluate_generator_expression(node, scope)
        if isinstance(node, ast.DictComp):
            return self._evaluate_dict_comprehension(node, scope)
        if isinstance(node, ast.JoinedStr):
            return "".join(str(self._evaluate_formatted(item, scope)) for item in node.values)
        raise ControlProgramError(f"expression is not allowed: {type(node).__name__}")

    def _evaluate_call(self, node: ast.Call, scope: dict[str, object]) -> object:
        function = self._evaluate(node.func, scope)
        arguments: list[object] = []
        for argument in node.args:
            if isinstance(argument, ast.Starred):
                expanded = self._bounded_iterable(self._evaluate(argument.value, scope))
                arguments.extend(expanded)
            else:
                arguments.append(self._evaluate(argument, scope))
        keywords: dict[str, object] = {}
        for keyword in node.keywords:
            value = self._evaluate(keyword.value, scope)
            if keyword.arg is None:
                if not isinstance(value, dict):
                    raise ControlProgramError("keyword expansion requires an object")
                if not all(isinstance(key, str) for key in value):
                    raise ControlProgramError("expanded keyword names must be strings")
                overlap = set(keywords) & set(value)
                if overlap:
                    raise ControlProgramError(f"duplicate keyword arguments: {sorted(overlap)}")
                keywords.update(value)
            else:
                if keyword.arg in keywords:
                    raise ControlProgramError(f"duplicate keyword argument: {keyword.arg}")
                keywords[keyword.arg] = value
        if isinstance(function, SafeHostFunction):
            try:
                result = function.invoke(arguments, keywords)
            except InfrastructureFailure:
                raise
            except ControlProgramError:
                raise
            except Exception as error:
                raise ControlProgramError(f"host call {function.name} failed: {error}") from error
            self._ensure_json_value(result)
            return result
        if isinstance(function, _UserFunction):
            return self._call_user_function(function.node, arguments, keywords)
        raise ControlProgramError("only reviewed host functions and local functions are callable")

    def _call_user_function(
        self,
        node: ast.FunctionDef,
        arguments: list[object],
        keywords: dict[str, object],
    ) -> object:
        self.call_depth += 1
        if self.call_depth > self.limits.maximum_call_depth:
            self.call_depth -= 1
            raise ControlProgramError("maximum function call depth exceeded")
        try:
            positional = [argument.arg for argument in node.args.args]
            default_offset = len(positional) - len(node.args.defaults)
            local: dict[str, object] = {}
            if len(arguments) > len(positional):
                raise ControlProgramError(f"too many arguments for {node.name}")
            for name, value in zip(positional, arguments, strict=False):
                local[name] = value
            for name, value in keywords.items():
                if name not in positional or name in local:
                    raise ControlProgramError(f"invalid keyword for {node.name}: {name}")
                local[name] = value
            for index, name in enumerate(positional):
                if name in local:
                    continue
                default_index = index - default_offset
                if default_index < 0:
                    raise ControlProgramError(f"missing argument for {node.name}: {name}")
                local[name] = self._evaluate(node.args.defaults[default_index], self.variables)
            try:
                self._execute_block(node.body, local)
            except _ReturnSignal as signal:
                self._ensure_json_value(signal.value)
                return signal.value
            return None
        finally:
            self.call_depth -= 1

    def _evaluate_list_comprehension(
        self, node: ast.ListComp, scope: dict[str, object]
    ) -> list[object]:
        result: list[object] = []
        self._walk_comprehension(
            node.generators,
            scope,
            lambda nested: result.append(self._evaluate(node.elt, nested)),
        )
        return result

    def _evaluate_generator_expression(
        self, node: ast.GeneratorExp, scope: dict[str, object]
    ) -> list[object]:
        """Materialize a bounded generator so existing reviewed hosts can consume it."""
        result: list[object] = []
        self._walk_comprehension(
            node.generators,
            scope,
            lambda nested: result.append(self._evaluate(node.elt, nested)),
        )
        return result

    def _evaluate_dict_comprehension(
        self, node: ast.DictComp, scope: dict[str, object]
    ) -> dict[str, object]:
        result: dict[str, object] = {}

        def append(nested: dict[str, object]) -> None:
            key = _encode_mapping_key(self._evaluate(node.key, nested))
            result[key] = self._evaluate(node.value, nested)

        self._walk_comprehension(node.generators, scope, append)
        return result

    def _walk_comprehension(
        self,
        generators: list[ast.comprehension],
        scope: dict[str, object],
        emit: Callable[[dict[str, object]], None],
        index: int = 0,
    ) -> None:
        if index == len(generators):
            emit(scope)
            return
        generator = generators[index]
        if generator.is_async:
            raise ControlProgramError("asynchronous comprehensions are not allowed")
        for item in self._bounded_iterable(self._evaluate(generator.iter, scope)):
            nested = dict(scope)
            self._assign_pattern(generator.target, item, nested)
            if all(bool(self._evaluate(condition, nested)) for condition in generator.ifs):
                self._walk_comprehension(generators, nested, emit, index + 1)

    def _evaluate_formatted(self, node: ast.expr, scope: dict[str, object]) -> object:
        if isinstance(node, ast.FormattedValue):
            if node.format_spec is not None:
                raise ControlProgramError("formatted value specifications are not allowed")
            return self._evaluate(node.value, scope)
        return self._evaluate(node, scope)

    def _safe_attribute(self, value: object, name: str) -> object:
        if name.startswith("_"):
            raise ControlProgramError("private and dunder attributes are not allowed")
        if isinstance(value, SafeNamespace):
            return value.member(name)
        if isinstance(value, dict) and name in value:
            return value[name]
        if isinstance(value, dict) and name in {"items", "keys", "values"}:
            callbacks: dict[str, Callable[[], object]] = {
                "items": lambda: [[_decode_mapping_key(key), item] for key, item in value.items()],
                "keys": lambda: [_decode_mapping_key(key) for key in value],
                "values": lambda: list(value.values()),
            }
            return SafeHostFunction(f"dict.{name}", callbacks[name])
        if isinstance(value, dict) and name in {"get", "pop", "setdefault"}:

            def mapping_method(*arguments: object) -> object:
                if not 1 <= len(arguments) <= 2:
                    raise ControlProgramError(f"dict.{name} accepts a key and at most one default")
                key = _encode_mapping_key(arguments[0])
                if name == "get":
                    default = arguments[1] if len(arguments) == 2 else None
                    return value.get(key, default)
                if name == "setdefault":
                    default = arguments[1] if len(arguments) == 2 else None
                    self._ensure_json_value(default)
                    return value.setdefault(key, default)
                if len(arguments) == 2:
                    return value.pop(key, arguments[1])
                try:
                    return value.pop(key)
                except KeyError as error:
                    raise ControlProgramError("dict.pop key is missing") from error

            return SafeHostFunction(f"dict.{name}", mapping_method)
        methods: dict[type[object], frozenset[str]] = {
            dict: frozenset({"update"}),
            list: frozenset(
                {"append", "count", "extend", "index", "insert", "pop", "reverse", "sort"}
            ),
            str: frozenset(
                {
                    "casefold",
                    "capitalize",
                    "center",
                    "count",
                    "endswith",
                    "expandtabs",
                    "find",
                    "index",
                    "isalnum",
                    "isalpha",
                    "isascii",
                    "isdecimal",
                    "isdigit",
                    "isidentifier",
                    "islower",
                    "isnumeric",
                    "isprintable",
                    "isspace",
                    "istitle",
                    "isupper",
                    "join",
                    "ljust",
                    "lower",
                    "lstrip",
                    "partition",
                    "removeprefix",
                    "removesuffix",
                    "replace",
                    "rfind",
                    "rindex",
                    "rjust",
                    "rpartition",
                    "rsplit",
                    "rstrip",
                    "split",
                    "splitlines",
                    "startswith",
                    "strip",
                    "swapcase",
                    "title",
                    "upper",
                    "zfill",
                }
            ),
        }
        for expected_type, allowed in methods.items():
            if isinstance(value, expected_type) and name in allowed:
                method = getattr(value, name)
                return SafeHostFunction(f"{expected_type.__name__}.{name}", method)
        raise ControlProgramError(f"attribute is not available: {type(value).__name__}.{name}")

    def _assign(self, target: ast.expr, value: object, scope: dict[str, object]) -> None:
        self._ensure_json_value(value)
        if isinstance(target, ast.Name):
            self._assign_name(target.id, value, scope)
            return
        if isinstance(target, ast.Subscript):
            container = self._evaluate(target.value, scope)
            index = self._evaluate_slice(target.slice, scope)
            if isinstance(container, dict):
                container[_encode_mapping_key(index)] = value
                return
            if isinstance(container, list) and isinstance(index, int):
                try:
                    container[index] = value
                except IndexError as error:
                    raise ControlProgramError("list assignment index is out of range") from error
                return
        raise ControlProgramError("assignment target is not allowed")

    def _assign_pattern(self, target: ast.expr, value: object, scope: dict[str, object]) -> None:
        if isinstance(target, ast.Name):
            self._assign_name(target.id, value, scope)
            return
        if isinstance(target, ast.Tuple | ast.List):
            if not isinstance(value, list | tuple) or len(value) != len(target.elts):
                raise ControlProgramError("unpacking target and value lengths must match")
            for nested_target, nested_value in zip(target.elts, value, strict=True):
                self._assign_pattern(nested_target, nested_value, scope)
            return
        raise ControlProgramError("unpacking target must contain public identifiers")

    def _assign_name(self, name: str, value: object, scope: dict[str, object]) -> None:
        if not name.isidentifier() or name.startswith("_") or self._reserved(name):
            raise ControlProgramError(f"assignment identifier is not allowed: {name}")
        self._ensure_json_value(value)
        scope[name] = value

    def _resolve_name(self, name: str, scope: dict[str, object]) -> object:
        if name.startswith("_"):
            raise ControlProgramError("private identifiers are not allowed")
        if name in _LITERAL_ALIASES:
            return _LITERAL_ALIASES[name]
        if name in scope:
            return scope[name]
        if scope is not self.variables and name in self.variables:
            return self.variables[name]
        if name in self.functions:
            return self.functions[name]
        if name in self.namespaces:
            return self.namespaces[name]
        if name in self._builtins:
            return self._builtins[name]
        raise ControlProgramError(f"unknown identifier: {name}")

    def _evaluate_slice(self, node: ast.expr, scope: dict[str, object]) -> object:
        if isinstance(node, ast.Slice):
            return slice(
                None if node.lower is None else self._evaluate(node.lower, scope),
                None if node.upper is None else self._evaluate(node.upper, scope),
                None if node.step is None else self._evaluate(node.step, scope),
            )
        return self._evaluate(node, scope)

    def _bounded_iterable(self, value: object) -> list[object]:
        if not isinstance(value, list | tuple | dict | str | range):
            raise ControlProgramError("iteration requires a bounded JSON collection")
        items: Iterable[object] = (
            (_decode_mapping_key(key) for key in value) if isinstance(value, dict) else value
        )
        result = list(items)
        if len(result) > self.limits.maximum_loop_iterations:
            raise ControlProgramError("loop iteration limit exceeded")
        return result

    def _validate_function_definition(self, node: ast.FunctionDef) -> None:
        if not node.name.isidentifier() or node.name.startswith("_") or self._reserved(node.name):
            raise ControlProgramError(f"function name is not allowed: {node.name}")
        if node.decorator_list or node.args.vararg or node.args.kwarg or node.args.kwonlyargs:
            raise ControlProgramError(
                "decorators and variadic/keyword-only functions are not allowed"
            )
        names = tuple(argument.arg for argument in node.args.args)
        if len(names) != len(set(names)) or any(
            not name.isidentifier() or name.startswith("_") for name in names
        ):
            raise ControlProgramError("function arguments contain unsafe or duplicate names")

    def _validate_identifiers(self) -> None:
        for name in (*self.variables, *self.function_sources):
            if not name.isidentifier() or name.startswith("_") or self._reserved(name):
                raise ControlProgramError(f"unsafe persisted identifier: {name}")

    def _validate_state(self) -> None:
        self._ensure_json_value(self.variables)
        encoded = json.dumps(self.variables, sort_keys=True).encode("utf-8")
        if len(encoded) > self.limits.maximum_value_bytes:
            raise ControlProgramError("control state exceeds value byte limit")

    def _ensure_json_value(self, value: object) -> None:
        try:
            encoded = json.dumps(value, sort_keys=True).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ControlProgramError("control values must be JSON serializable") from error
        if len(encoded) > self.limits.maximum_value_bytes:
            raise ControlProgramError("control value exceeds byte limit")

    def _reserved(self, name: str) -> bool:
        return name in self.namespaces or name in self._builtins or name in _LITERAL_ALIASES

    def _step(self, node: ast.AST) -> None:
        del node
        self.steps += 1
        if self.steps > self.limits.maximum_steps:
            raise ControlProgramError("control step limit exceeded")
        if time.monotonic() - self._started > self.limits.maximum_wall_seconds:
            raise ControlProgramError("control cell wall-clock limit exceeded")


def _safe_builtins(limits: ControlLimits) -> dict[str, SafeHostFunction]:
    def bounded_print(
        *values: object,
        sep: object = " ",
        end: object = "\n",
    ) -> str:
        if not isinstance(sep, str) or not isinstance(end, str):
            raise ControlProgramError("print sep and end must be strings")
        rendered = sep.join(str(value) for value in values) + end
        if len(rendered.encode("utf-8")) > 32 * 1024:
            raise ControlProgramError("print output exceeds 32 KiB")
        return rendered

    def bounded_range(*arguments: object) -> list[int]:
        if not 1 <= len(arguments) <= 3 or not all(
            isinstance(value, int) and not isinstance(value, bool) for value in arguments
        ):
            raise ControlProgramError("range accepts one to three integers")
        result = list(range(*arguments))  # type: ignore[arg-type]
        if len(result) > limits.maximum_loop_iterations:
            raise ControlProgramError("range exceeds loop iteration limit")
        return result

    def bounded_sorted(value: object, *, reverse: bool = False) -> list[object]:
        if not isinstance(value, list | tuple | dict | str):
            raise ControlProgramError("sorted requires a bounded JSON collection")
        items = (
            [_decode_mapping_key(key) for key in value] if isinstance(value, dict) else list(value)
        )
        if len(items) > limits.maximum_loop_iterations:
            raise ControlProgramError("sorted input exceeds iteration limit")
        return sorted(items, reverse=reverse)

    def bounded_zip(*values: object) -> list[list[object]]:
        collections: list[list[object]] = []
        for value in values:
            if not isinstance(value, list | tuple):
                raise ControlProgramError("zip requires lists")
            collections.append(list(value))
        result = [list(items) for items in zip(*collections, strict=False)]
        if len(result) > limits.maximum_loop_iterations:
            raise ControlProgramError("zip output exceeds iteration limit")
        return result

    def bounded_enumerate(value: object, start: object = 0) -> list[list[object]]:
        if not isinstance(start, int) or isinstance(start, bool):
            raise ControlProgramError("enumerate start must be an integer")
        if not isinstance(value, list | tuple | dict | str):
            raise ControlProgramError("enumerate requires a bounded JSON collection")
        items = (
            [_decode_mapping_key(key) for key in value] if isinstance(value, dict) else list(value)
        )
        if len(items) > limits.maximum_loop_iterations:
            raise ControlProgramError("enumerate input exceeds loop iteration limit")
        return [[index, item] for index, item in enumerate(items, start=start)]

    def safe_isinstance(value: object, expected: object) -> bool:
        descriptors = expected if isinstance(expected, list | tuple) else [expected]
        types_by_name: dict[str, type[object]] = {
            "bool": bool,
            "dict": dict,
            "float": float,
            "int": int,
            "list": list,
            "str": str,
        }
        resolved: list[type[object]] = []
        for descriptor in descriptors:
            if not isinstance(descriptor, SafeHostFunction) or descriptor.name not in types_by_name:
                raise ControlProgramError(
                    "isinstance type must be one of bool, dict, float, int, list, or str"
                )
            resolved.append(types_by_name[descriptor.name])
        if not resolved:
            raise ControlProgramError("isinstance requires at least one safe JSON type")
        return isinstance(value, tuple(resolved))

    functions: dict[str, Callable[..., object]] = {
        "all": lambda value: all(value),
        "any": lambda value: any(value),
        "bool": bool,
        "dict": lambda value=None: {} if value is None else dict(value),
        "enumerate": bounded_enumerate,
        "float": float,
        "int": int,
        "isinstance": safe_isinstance,
        "len": len,
        "list": lambda value=(): list(value),
        "max": max,
        "min": min,
        "print": bounded_print,
        "range": bounded_range,
        "sorted": bounded_sorted,
        "str": str,
        "sum": sum,
        "zip": bounded_zip,
    }
    return {name: SafeHostFunction(name, function) for name, function in functions.items()}


def _allowed_exception_handler(node: ast.expr | None) -> bool:
    return node is None or (
        isinstance(node, ast.Name)
        and node.id in {"Exception", "RuntimeError", "CapabilityCallError", "ControlProgramError"}
    )


def _json_copy(value: object) -> Any:
    try:
        return json.loads(json.dumps(value, sort_keys=True))
    except (TypeError, ValueError) as error:
        raise ControlProgramError("control values must be JSON serializable") from error


def namespace(name: str, members: Mapping[str, object]) -> SafeNamespace:
    return SafeNamespace(name, members)


def host_function(name: str, callback: Callable[..., object]) -> SafeHostFunction:
    return SafeHostFunction(name, callback)
