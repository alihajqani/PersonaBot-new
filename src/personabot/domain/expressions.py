"""Sandboxed evaluator for expressions embedded in the survey YAML.

Config files carry small expressions — ``max(1, age - marriage_age)``,
``min(age - 1, 29)``, ``clamp(0.1 + 0.2*sigmoid(z.anxiety), 0.03, 0.5)``,
``acquiescence > 0.30``. They are **data**, so they must not be able to execute
arbitrary code: a survey spec is exactly the kind of file someone will accept
from a colleague without reading every line.

``eval()`` is therefore not used. Expressions are parsed to an AST and walked
with an explicit allowlist; anything outside it — imports, attribute access on
real objects, subscripting, comprehensions, lambdas, walrus — raises
:class:`ExpressionError` at *load* time, not at sample time.
"""

from __future__ import annotations

import ast
import math
import random
from typing import Any, Callable, Mapping

from personabot.errors import ExpressionError

# ===== ALLOWED SYNTAX =====

_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.Call,
    ast.IfExp,
    ast.Attribute,   # only for namespace lookups such as `z.marital_satisfaction`
    ast.Tuple,
    ast.List,
)

_ALLOWED_OPS: tuple[type[ast.AST], ...] = (
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd, ast.Not,
    ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)

_BIN_OPS: dict[type[ast.AST], Callable[[Any, Any], Any]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}

_CMP_OPS: dict[type[ast.AST], Callable[[Any, Any], bool]] = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
}

# Guard against `2 ** 10**9` locking up the process.
_MAX_POW_EXPONENT = 1024


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _sigmoid(x: float) -> float:
    # Split by sign so neither branch overflows for large |x|.
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


# Deterministic functions: always available.
_PURE_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "min": min,
    "max": max,
    "abs": abs,
    "round": round,
    "int": int,
    "float": float,
    "clamp": _clamp,
    "sigmoid": _sigmoid,
    "exp": math.exp,
    "log": math.log,
    "sqrt": math.sqrt,
    "floor": math.floor,
    "ceil": math.ceil,
}

# Stochastic functions: only available when an rng is supplied, so that a
# context meant to be deterministic cannot silently draw randomness.
_RANDOM_FUNCTION_NAMES = ("randint", "uniform", "gauss")

ALLOWED_FUNCTION_NAMES = tuple(_PURE_FUNCTIONS) + _RANDOM_FUNCTION_NAMES


class Expression:
    """A parsed, validated expression, reusable across many evaluations.

    Parsing happens once at config load so a typo surfaces immediately with the
    offending source text, rather than midway through generating persona 400.
    """

    __slots__ = ("source", "_tree")

    def __init__(self, source: str) -> None:
        self.source = source
        try:
            tree = ast.parse(source, mode="eval")
        except SyntaxError as exc:
            raise ExpressionError(f"Cannot parse expression {source!r}: {exc.msg}") from exc
        _validate(tree, source)
        self._tree = tree

    def __repr__(self) -> str:
        return f"Expression({self.source!r})"

    def evaluate(
        self,
        context: Mapping[str, Any] | None = None,
        *,
        rng: random.Random | None = None,
    ) -> Any:
        """Evaluate against ``context``.

        Args:
            context: Names visible to the expression. Nested mappings support
                dotted access, so ``{"z": {"anxiety": 1.2}}`` allows ``z.anxiety``.
            rng: Enables ``randint`` / ``uniform`` / ``gauss``. Omit for
                expressions that must be deterministic.
        """
        functions = dict(_PURE_FUNCTIONS)
        if rng is not None:
            functions["randint"] = rng.randint
            functions["uniform"] = rng.uniform
            functions["gauss"] = rng.gauss

        try:
            return _eval_node(self._tree.body, dict(context or {}), functions)
        except ExpressionError:
            raise
        except ZeroDivisionError as exc:
            raise ExpressionError(f"Division by zero in {self.source!r}") from exc
        except Exception as exc:
            raise ExpressionError(
                f"Error evaluating {self.source!r}: {type(exc).__name__}: {exc}"
            ) from exc

    def evaluate_bool(
        self,
        context: Mapping[str, Any] | None = None,
        *,
        rng: random.Random | None = None,
    ) -> bool:
        return bool(self.evaluate(context, rng=rng))


def _validate(tree: ast.AST, source: str) -> None:
    """Reject anything outside the allowlist, before it can ever run."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.operator, ast.unaryop, ast.boolop, ast.cmpop)):
            if not isinstance(node, _ALLOWED_OPS):
                raise ExpressionError(
                    f"Operator {type(node).__name__} is not allowed in {source!r}"
                )
            continue

        if not isinstance(node, _ALLOWED_NODES):
            raise ExpressionError(
                f"{type(node).__name__} is not allowed in expression {source!r}. "
                f"Expressions support arithmetic, comparisons, and these functions: "
                f"{', '.join(sorted(ALLOWED_FUNCTION_NAMES))}."
            )

        # Only `name(...)` calls — never `obj.method(...)` or `f()()`.
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ExpressionError(
                    f"Only direct calls to allowed functions are permitted in {source!r}"
                )
            if node.func.id not in ALLOWED_FUNCTION_NAMES:
                raise ExpressionError(
                    f"Unknown function {node.func.id!r} in {source!r}. Allowed: "
                    f"{', '.join(sorted(ALLOWED_FUNCTION_NAMES))}."
                )
            if node.keywords:
                raise ExpressionError(f"Keyword arguments are not supported in {source!r}")

        # `z.anxiety` is fine; `foo.bar.__class__` style chains are not.
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise ExpressionError(
                    f"Attribute {node.attr!r} is not accessible in {source!r}"
                )


def _eval_node(node: ast.AST, context: dict[str, Any], functions: dict[str, Callable[..., Any]]) -> Any:
    match node:
        case ast.Constant(value=value):
            return value

        case ast.Name(id=name):
            if name in context:
                return context[name]
            if name in functions:
                return functions[name]
            available = ", ".join(sorted(k for k in context if not k.startswith("_")))
            raise ExpressionError(
                f"Unknown name {name!r}. Available: {available or '(none)'}"
            )

        case ast.Attribute(value=value_node, attr=attr):
            container = _eval_node(value_node, context, functions)
            if isinstance(container, Mapping):
                if attr not in container:
                    raise ExpressionError(
                        f"{attr!r} not found; available: {', '.join(sorted(map(str, container)))}"
                    )
                return container[attr]
            raise ExpressionError(f"Cannot read attribute {attr!r} from {type(container).__name__}")

        case ast.BinOp(left=left, op=op, right=right):
            lhs = _eval_node(left, context, functions)
            rhs = _eval_node(right, context, functions)
            if isinstance(op, ast.Pow) and isinstance(rhs, (int, float)) and rhs > _MAX_POW_EXPONENT:
                raise ExpressionError(f"Exponent {rhs} exceeds the limit of {_MAX_POW_EXPONENT}")
            return _BIN_OPS[type(op)](lhs, rhs)

        case ast.UnaryOp(op=op, operand=operand):
            value = _eval_node(operand, context, functions)
            if isinstance(op, ast.USub):
                return -value
            if isinstance(op, ast.UAdd):
                return +value
            return not value

        case ast.BoolOp(op=op, values=values):
            if isinstance(op, ast.And):
                result: Any = True
                for value_node in values:
                    result = _eval_node(value_node, context, functions)
                    if not result:
                        return result
                return result
            result = False
            for value_node in values:
                result = _eval_node(value_node, context, functions)
                if result:
                    return result
            return result

        case ast.Compare(left=left, ops=ops, comparators=comparators):
            current = _eval_node(left, context, functions)
            for op, comparator_node in zip(ops, comparators):
                right = _eval_node(comparator_node, context, functions)
                if not _CMP_OPS[type(op)](current, right):
                    return False
                current = right
            return True

        case ast.IfExp(test=test, body=body, orelse=orelse):
            branch = body if _eval_node(test, context, functions) else orelse
            return _eval_node(branch, context, functions)

        case ast.Call(func=ast.Name(id=name), args=args):
            function = functions.get(name)
            if function is None:
                raise ExpressionError(
                    f"Function {name!r} is unavailable here. "
                    f"{'Random functions need an rng.' if name in _RANDOM_FUNCTION_NAMES else ''}".strip()
                )
            return function(*(_eval_node(arg, context, functions) for arg in args))

        case ast.Tuple(elts=elements) | ast.List(elts=elements):
            return [_eval_node(element, context, functions) for element in elements]

    raise ExpressionError(f"Unsupported expression node {type(node).__name__}")


def as_expression(value: Any) -> "Expression | None":
    """Coerce a YAML scalar to an Expression when it is a string.

    Numeric bounds stay numbers; ``"min(age - 1, 29)"`` becomes an Expression.
    """
    if value is None or isinstance(value, (int, float, bool)):
        return None
    if isinstance(value, str):
        return Expression(value)
    raise ExpressionError(f"Cannot interpret {value!r} as an expression")


def resolve(
    value: Any,
    context: Mapping[str, Any] | None = None,
    *,
    rng: random.Random | None = None,
) -> Any:
    """Return a literal unchanged; evaluate a string as an expression."""
    if isinstance(value, str):
        return Expression(value).evaluate(context, rng=rng)
    return value
