"""Symbol and reference extraction via tree-sitter. **Owned by Agent C.**

Given a file's text and its language, produce the declarations it makes
(:class:`~mergesignal.models.Symbol`) and the names it uses
(:class:`~mergesignal.models.Reference`) — FR-3.

Definitions vs references is the whole game for the semantic signal: a deleted
definition only matters because something still *references* it.

Extraction contract:

* Symbols carry 1-based lines and, where the grammar exposes one, a normalised
  ``signature`` (parameter list, whitespace collapsed) so signature changes are
  detectable without false positives from reformatting.
* Methods carry ``parent`` set to the enclosing class; nested functions carry
  their enclosing function.
* Imports are symbols of kind ``import`` whose ``name`` is the *bound local
  name* (what other code will reference), not the module path. The module path
  lands in ``parent``, so ``qualified_name`` reads ``lib.greet``.
* References exclude the definition site itself, and exclude attribute access on
  an obviously unrelated receiver where the grammar makes that clear.

Edge cases:

* Unsupported language -> ``([], [])``, never an exception.
* Binary content -> skipped by the caller; this module may assume text.
* Files above ``AnalysisConfig.max_file_bytes`` -> caller skips; if called
  anyway, implementations should still bound their work.
* Syntax errors -> extract from the non-``ERROR`` parts of the tree.

How extraction works
--------------------

Each supported language has a :class:`LanguageSpec` holding a handful of
*single-capture tree-sitter queries*, one per declaration flavour. Queries
select the declaration nodes; shared field-based helpers
(:func:`_field`, :func:`_signature`, :func:`_scope_of`) then pull the name,
signature and enclosing scope out of each node. Patterns are compiled
individually and a pattern the installed grammar does not know is simply
dropped, so a grammar upgrade that renames a node type degrades to fewer
symbols instead of an exception.

References are then found by a single shared traversal: every identifier-ish
node that is *not* a binding site (definition name, parameter name, keyword
argument label, object literal key) is a reference. Declaration handlers may
additionally emit explicit references — an ``import`` binds a local name *and*
references the imported one.

Signature format
----------------

``(params)`` for languages with no usable return type in the grammar, and
``(params) -> ret`` where one exists (Python annotations, Go results, Rust
return types, TypeScript and Java types). The text is passed through
:func:`normalize_signature`, which is what makes ``signature_changed`` immune
to reformatting.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from mergesignal.analysis.languages import language_for_path, parse_source
from mergesignal.models import Reference, Symbol

#: Files larger than this are not parsed; extraction returns empty lists.
MAX_PARSE_BYTES = 1_000_000

#: ``Reference.context`` is truncated to this many characters so findings stay
#: readable when a reference lands in a minified or generated line.
MAX_CONTEXT_CHARS = 200


# --------------------------------------------------------------- language specs


@dataclass(frozen=True)
class LanguageSpec:
    """Everything the shared extractor needs to know about one grammar.

    :param patterns: ``(role, query_source)`` pairs. ``role`` is one of
        ``function``/``class``/``method``/``variable``/``import`` and selects the
        handler; each query captures exactly one node.
    :param identifier_types: node types that count as a name *usage* during the
        shared reference traversal.
    :param class_types: node types that open a class-ish scope (used for
        ``parent`` and for promoting ``function`` to ``method``).
    :param function_types: node types that open a function scope.
    :param param_container_types: node types whose direct identifier children
        are parameter bindings, not references.
    :param param_name_fields: ``(node_type, field)`` pairs naming a binding.
    :param binding_fields: further ``(node_type, field)`` pairs that are
        bindings or labels rather than usages (``{key: value}``, ``f(kw=1)``).
    :param params_field / return_fields: grammar fields used to build a
        signature.
    """

    language: str
    patterns: tuple[tuple[str, str], ...]
    identifier_types: frozenset[str]
    class_types: frozenset[str] = frozenset()
    function_types: frozenset[str] = frozenset()
    param_container_types: frozenset[str] = frozenset()
    param_name_fields: tuple[tuple[str, str], ...] = ()
    param_first_child_types: frozenset[str] = frozenset()
    binding_fields: tuple[tuple[str, str], ...] = ()
    params_field: str = "parameters"
    return_fields: tuple[str, ...] = ()


_PYTHON = LanguageSpec(
    language="python",
    patterns=(
        ("function", "(function_definition) @d"),
        ("class", "(class_definition) @d"),
        ("variable", "(assignment) @d"),
        ("import", "(import_statement) @d"),
        ("import", "(import_from_statement) @d"),
    ),
    identifier_types=frozenset({"identifier"}),
    class_types=frozenset({"class_definition"}),
    function_types=frozenset({"function_definition"}),
    param_container_types=frozenset({"parameters", "lambda_parameters"}),
    param_name_fields=(
        ("default_parameter", "name"),
        ("typed_default_parameter", "name"),
    ),
    param_first_child_types=frozenset(
        {"typed_parameter", "list_splat_pattern", "dictionary_splat_pattern"}
    ),
    binding_fields=(("keyword_argument", "name"),),
    return_fields=("return_type",),
)

_JS_PATTERNS: tuple[tuple[str, str], ...] = (
    ("function", "(function_declaration) @d"),
    ("function", "(generator_function_declaration) @d"),
    ("class", "(class_declaration) @d"),
    ("method", "(method_definition) @d"),
    ("variable", "(variable_declarator) @d"),
    ("import", "(import_statement) @d"),
)

_TS_EXTRA_PATTERNS: tuple[tuple[str, str], ...] = (
    ("class", "(abstract_class_declaration) @d"),
    ("class", "(interface_declaration) @d"),
    ("class", "(type_alias_declaration) @d"),
    ("class", "(enum_declaration) @d"),
    ("function", "(function_signature) @d"),
    ("method", "(method_signature) @d"),
    ("method", "(abstract_method_signature) @d"),
    ("variable", "(public_field_definition) @d"),
)

_JS_IDENTIFIERS = frozenset(
    {"identifier", "property_identifier", "type_identifier", "shorthand_property_identifier"}
)
_JS_CLASS_TYPES = frozenset(
    {
        "class_declaration",
        "abstract_class_declaration",
        "class",
        "interface_declaration",
        "enum_declaration",
    }
)
_JS_FUNCTION_TYPES = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "function_expression",
        "function",
        "arrow_function",
        "method_definition",
    }
)
_JS_BINDING_FIELDS = (
    ("pair", "key"),
    ("arrow_function", "parameter"),
    ("assignment_pattern", "left"),
)


def _js_like(language: str, *, typescript: bool) -> LanguageSpec:
    """Build the spec shared by javascript, typescript and tsx."""
    return LanguageSpec(
        language=language,
        patterns=_JS_PATTERNS + (_TS_EXTRA_PATTERNS if typescript else ()),
        identifier_types=_JS_IDENTIFIERS,
        class_types=_JS_CLASS_TYPES,
        function_types=_JS_FUNCTION_TYPES,
        param_container_types=frozenset({"formal_parameters"}),
        param_name_fields=(
            ("required_parameter", "pattern"),
            ("optional_parameter", "pattern"),
        ),
        param_first_child_types=frozenset({"rest_pattern"}),
        binding_fields=_JS_BINDING_FIELDS,
        return_fields=("return_type",) if typescript else (),
    )


_JAVASCRIPT = _js_like("javascript", typescript=False)
_TYPESCRIPT = _js_like("typescript", typescript=True)
_TSX = _js_like("tsx", typescript=True)

_GO = LanguageSpec(
    language="go",
    patterns=(
        ("function", "(function_declaration) @d"),
        ("method", "(method_declaration) @d"),
        ("class", "(type_spec) @d"),
        ("variable", "(var_spec) @d"),
        ("variable", "(const_spec) @d"),
        ("import", "(import_spec) @d"),
    ),
    identifier_types=frozenset(
        {"identifier", "type_identifier", "field_identifier", "package_identifier"}
    ),
    function_types=frozenset({"function_declaration", "method_declaration", "func_literal"}),
    param_name_fields=(
        ("parameter_declaration", "name"),
        ("variadic_parameter_declaration", "name"),
        ("field_declaration", "name"),
    ),
    return_fields=("result",),
)

_RUST = LanguageSpec(
    language="rust",
    patterns=(
        ("function", "(function_item) @d"),
        ("function", "(function_signature_item) @d"),
        ("class", "(struct_item) @d"),
        ("class", "(enum_item) @d"),
        ("class", "(trait_item) @d"),
        ("class", "(type_item) @d"),
        ("variable", "(const_item) @d"),
        ("variable", "(static_item) @d"),
        ("import", "(use_declaration) @d"),
    ),
    identifier_types=frozenset({"identifier", "type_identifier", "field_identifier"}),
    class_types=frozenset({"impl_item", "trait_item"}),
    function_types=frozenset({"function_item", "closure_expression"}),
    param_name_fields=(("parameter", "pattern"), ("field_declaration", "name")),
    return_fields=("return_type",),
)

_JAVA = LanguageSpec(
    language="java",
    patterns=(
        ("class", "(class_declaration) @d"),
        ("class", "(interface_declaration) @d"),
        ("class", "(enum_declaration) @d"),
        ("class", "(record_declaration) @d"),
        ("method", "(method_declaration) @d"),
        ("method", "(constructor_declaration) @d"),
        ("variable", "(field_declaration) @d"),
        ("import", "(import_declaration) @d"),
    ),
    identifier_types=frozenset({"identifier", "type_identifier"}),
    class_types=frozenset(
        {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration"}
    ),
    function_types=frozenset({"method_declaration", "constructor_declaration", "lambda_expression"}),
    param_name_fields=(("formal_parameter", "name"), ("spread_parameter", "name")),
    return_fields=("type",),
)

#: Language id -> extraction rules. A grammar absent from here is *recognised*
#: (``DiffFile.language`` is set) but yields no symbols — a documented,
#: non-error degradation to textual handling.
SPECS: dict[str, LanguageSpec] = {
    "python": _PYTHON,
    "javascript": _JAVASCRIPT,
    "typescript": _TYPESCRIPT,
    "tsx": _TSX,
    "go": _GO,
    "rust": _RUST,
    "java": _JAVA,
}

#: Languages this module can extract symbols from, sorted.
SUPPORTED_LANGUAGES: tuple[str, ...] = tuple(sorted(SPECS))


# ------------------------------------------------------------- tree-sitter glue


_QUERY_CACHE: dict[tuple[str, str], Any] = {}
_UNCOMPILABLE: set[tuple[str, str]] = set()


def _compile(language: str, pattern: str) -> Any | None:
    """Compile one query pattern, caching both successes and failures.

    Returns ``None`` when the installed grammar has no such node type, which is
    how the extractor survives grammar upgrades that rename or drop nodes.
    """
    key = (language, pattern)
    if key in _UNCOMPILABLE:
        return None
    cached = _QUERY_CACHE.get(key)
    if cached is not None:
        return cached
    import tree_sitter

    from mergesignal.analysis.languages import get_language

    try:
        query = tree_sitter.Query(get_language(language), pattern)
    except Exception:  # noqa: BLE001 - QueryError, LookupError, anything the pack throws
        _UNCOMPILABLE.add(key)
        return None
    _QUERY_CACHE[key] = query
    return query


def _query_nodes(query: Any, root: Any) -> list[Any]:
    """Run ``query`` over ``root`` and return the captured nodes.

    Normalises the two py-tree-sitter capture shapes: the modern
    ``QueryCursor(q).captures(node) -> {name: [node]}`` and the legacy
    ``q.captures(node) -> [(node, name)]``.
    """
    import tree_sitter

    cursor_cls = getattr(tree_sitter, "QueryCursor", None)
    raw = cursor_cls(query).captures(root) if cursor_cls is not None else query.captures(root)
    if isinstance(raw, dict):
        return [node for nodes in raw.values() for node in nodes]
    return [item[0] for item in raw]


def node_text(node: Any, source_bytes: bytes) -> str:
    """Decode a tree-sitter node's byte span back to text.

    Uses ``errors="replace"`` — a file with mixed encodings must not crash the
    extractor.
    """
    if node is None:
        return ""
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def walk(node: Any) -> Iterator[Any]:
    """Depth-first iterator over a tree-sitter node and all its descendants.

    Uses a ``TreeCursor`` rather than recursion so deeply nested files (minified
    JS, generated code) do not blow the Python stack.
    """
    cursor = node.walk()
    visited_children = False
    while True:
        if not visited_children:
            yield cursor.node
            if not cursor.goto_first_child():
                visited_children = True
        elif cursor.goto_next_sibling():
            visited_children = False
        elif not cursor.goto_parent():
            return


def _field(node: Any, name: str) -> Any | None:
    """``node.child_by_field_name(name)`` that tolerates missing fields."""
    if node is None:
        return None
    try:
        return node.child_by_field_name(name)
    except Exception:  # noqa: BLE001 - grammars disagree about which fields exist
        return None


def _fields(node: Any, name: str) -> list[Any]:
    """Every child of ``node`` in field ``name`` (Go's repeated ``name:`` field)."""
    if node is None:
        return []
    try:
        return list(node.children_by_field_name(name))
    except Exception:  # noqa: BLE001
        child = _field(node, name)
        return [child] if child is not None else []


def _span(node: Any) -> tuple[int, int]:
    """Byte span, used as a stable node identity across traversals.

    py-tree-sitter hands out a *new* ``Node`` object on every access, so object
    identity is useless for "have I already claimed this node?".
    """
    return (node.start_byte, node.end_byte)


def _line(node: Any) -> int:
    """1-based line of a node's first character."""
    return node.start_point[0] + 1


def _end_line(node: Any) -> int:
    """1-based line of a node's last character."""
    return max(node.end_point[0] + 1, _line(node))


def _is_field(parent: Any, field_name: str, node: Any) -> bool:
    """``True`` when ``node`` occupies ``parent``'s ``field_name`` slot."""
    child = _field(parent, field_name)
    return child is not None and _span(child) == _span(node)


def _first_named_child(node: Any) -> Any | None:
    """First named child, or ``None``."""
    for child in node.named_children:
        return child
    return None


# --------------------------------------------------------------- normalisation


_COMMENT_SCAN_QUOTES = "\"'`"


def _strip_inline_comments(text: str) -> str:
    """Remove ``//``/``#`` line comments and ``/* */`` blocks from a signature.

    Only applied to multi-line signatures: a comment can only appear inside a
    parameter list if a newline follows it, and skipping single-line input keeps
    ``#`` or ``//`` inside a default string literal safe.
    """
    if "\n" not in text:
        return text
    out: list[str] = []
    in_block = False
    for raw_line in text.split("\n"):
        line_out: list[str] = []
        quote: str | None = None
        index = 0
        while index < len(raw_line):
            char = raw_line[index]
            if in_block:
                if raw_line.startswith("*/", index):
                    in_block = False
                    index += 2
                    continue
                index += 1
                continue
            if quote is not None:
                line_out.append(char)
                if char == "\\":
                    if index + 1 < len(raw_line):
                        line_out.append(raw_line[index + 1])
                    index += 2
                    continue
                if char == quote:
                    quote = None
                index += 1
                continue
            if char in _COMMENT_SCAN_QUOTES:
                quote = char
                line_out.append(char)
                index += 1
                continue
            if raw_line.startswith("//", index) or char == "#":
                break
            if raw_line.startswith("/*", index):
                in_block = True
                index += 2
                continue
            line_out.append(char)
            index += 1
        out.append("".join(line_out))
    return "\n".join(out)


def normalize_signature(signature: str | None) -> str | None:
    """Canonicalise a signature so formatting changes are not false positives.

    Collapses runs of whitespace, strips comments where trivially possible, and
    removes the trailing/leading parentheses padding. ``None`` passes through.
    Two signatures are considered "the same" iff their normalised forms are
    equal, so this function defines what ``signature_changed`` means.

    ``(  a : int = 1 ,\\n  b )`` and ``(a: int=1, b)`` both normalise to
    ``(a: int=1, b)``. ``::`` is left alone so Rust and C++ paths survive.
    """
    if signature is None:
        return None
    text = _strip_inline_comments(signature)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"([(\[])\s+", r"\1", text)
    text = re.sub(r"\s+([)\]])", r"\1", text)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r",(?=[^\s)\]}])", ", ", text)
    text = re.sub(r"\s*=\s*", "=", text)
    text = re.sub(r"(?<!:)\s*:\s*(?!:)", ": ", text)
    text = re.sub(r"\s*->\s*", " -> ", text)
    return text.strip()


# ------------------------------------------------------------ extraction state


@dataclass
class _Ctx:
    """Mutable accumulator threaded through the per-language handlers."""

    src: bytes
    path: str
    spec: LanguageSpec
    lines: list[str]
    symbols: list[Symbol] = field(default_factory=list)
    bindings: set[tuple[int, int]] = field(default_factory=set)
    refs: list[Reference] = field(default_factory=list)

    def text(self, node: Any) -> str:
        return node_text(node, self.src)

    def bind(self, node: Any) -> None:
        """Mark a node as a binding site so it is not also counted as a usage."""
        if node is not None:
            self.bindings.add(_span(node))

    def add_symbol(
        self,
        name_node: Any,
        kind: str,
        *,
        declaration: Any = None,
        signature: str | None = None,
        parent: str | None = None,
        bind: bool = True,
    ) -> None:
        """Record a declaration, skipping unusable names (empty, multi-line)."""
        if name_node is None:
            return
        name = self.text(name_node).strip()
        if not name or "\n" in name:
            return
        node = declaration if declaration is not None else name_node
        self.symbols.append(
            Symbol(
                name=name,
                kind=kind,  # type: ignore[arg-type]
                file=self.path,
                line=_line(name_node),
                end_line=_end_line(node),
                signature=signature,
                parent=parent,
            )
        )
        if bind:
            self.bind(name_node)

    def add_ref(self, node: Any, name: str | None = None) -> None:
        """Record an explicit usage (import targets, mostly)."""
        if node is None:
            return
        value = (name if name is not None else self.text(node)).strip()
        if not value or "\n" in value:
            return
        line = _line(node)
        self.refs.append(
            Reference(
                name=value,
                file=self.path,
                line=line,
                context=self.line_text(line),
                column=node.start_point[1],
            )
        )

    def line_text(self, line: int) -> str:
        """Trimmed, length-capped source line for human-readable evidence."""
        if 1 <= line <= len(self.lines):
            return self.lines[line - 1].strip()[:MAX_CONTEXT_CHARS]
        return ""


# ---------------------------------------------------------------- scope lookup


def _scope_name(node: Any, ctx: _Ctx) -> str | None:
    """Name of a scope-opening node: its ``name`` field, else its ``type`` field.

    The ``type`` fallback is what gives a Rust ``impl Foo`` block the name
    ``Foo``, since the grammar has no ``name`` there.
    """
    for field_name in ("name", "type"):
        child = _field(node, field_name)
        if child is not None:
            text = ctx.text(child).strip()
            if text and "\n" not in text:
                return text.split("<", 1)[0].strip() or None
    return None


def _scope_of(node: Any, ctx: _Ctx) -> tuple[str | None, bool]:
    """Nearest enclosing scope of ``node`` as ``(name, is_class_scope)``.

    Walks strictly upward, so the node's own name is never its own parent.
    """
    spec = ctx.spec
    current = node.parent
    while current is not None:
        if current.type in spec.class_types:
            return (_scope_name(current, ctx), True)
        if current.type in spec.function_types:
            return (_scope_name(current, ctx), False)
        current = current.parent
    return (None, False)


def _inside_function(node: Any, ctx: _Ctx) -> bool:
    """``True`` when ``node`` sits inside any function body.

    Used to keep locals out of the variable index: a module-level constant is
    part of a file's API, ``x = 1`` three frames deep is not.
    """
    current = node.parent
    while current is not None:
        if current.type in ctx.spec.function_types:
            return True
        current = current.parent
    return False


def _signature(node: Any, ctx: _Ctx, *, params_node: Any = None) -> str | None:
    """Build a normalised ``(params)``/``(params) -> ret`` signature for a node."""
    params = params_node if params_node is not None else _field(node, ctx.spec.params_field)
    if params is None:
        return None
    text = ctx.text(params)
    for field_name in ctx.spec.return_fields:
        returns = _field(node, field_name)
        if returns is not None:
            # TypeScript's ``return_type`` node spans ": number"; every other
            # grammar gives the bare type. Normalise to the bare type.
            returned = ctx.text(returns).lstrip().removeprefix(":").strip()
            if returned:
                text = f"{text} -> {returned}"
            break
    return normalize_signature(text)


# --------------------------------------------------------------- handlers: core


def _handle_generic(node: Any, role: str, ctx: _Ctx) -> None:
    """Declarations whose name lives in the ``name`` field — most of them."""
    name_node = _field(node, "name")
    if name_node is None:
        return
    parent_name, parent_is_class = _scope_of(node, ctx)
    kind = role
    if role == "function" and parent_is_class:
        kind = "method"
    signature = None if role == "class" else _signature(node, ctx)
    ctx.add_symbol(name_node, kind, declaration=node, signature=signature, parent=parent_name)


def _handle_method(node: Any, ctx: _Ctx) -> None:
    """Methods: identical to :func:`_handle_generic` but never demoted."""
    name_node = _field(node, "name")
    if name_node is None:
        return
    parent_name, _ = _scope_of(node, ctx)
    ctx.add_symbol(
        name_node, "method", declaration=node, signature=_signature(node, ctx), parent=parent_name
    )


# ------------------------------------------------------------ handlers: python


def _python_variable(node: Any, ctx: _Ctx) -> None:
    """Module- and class-level assignments only; locals are noise, not API.

    Local assignment targets are still *bound* so that ``g = Greeter()`` does
    not report ``g`` as a usage of something — it is a binding either way, it
    simply is not part of the file's API.
    """
    left = _field(node, "left")
    if left is None:
        return
    targets = [left] if left.type == "identifier" else [
        child for child in left.named_children if child.type == "identifier"
    ]
    if _inside_function(node, ctx):
        for target in targets:
            ctx.bind(target)
        return
    parent_name, parent_is_class = _scope_of(node, ctx)
    for target in targets:
        ctx.add_symbol(
            target,
            "variable",
            declaration=node,
            parent=parent_name if parent_is_class else None,
        )


def _python_import(node: Any, ctx: _Ctx) -> None:
    """``import a.b``, ``import a as b``, ``from m import x [as y]``."""
    if node.type == "import_statement":
        for child in node.named_children:
            if child.type == "aliased_import":
                alias = _field(child, "alias")
                module = _field(child, "name")
                ctx.add_symbol(alias, "import", declaration=node, parent=ctx.text(module) or None)
                ctx.bind(module)
                for part in module.named_children if module is not None else []:
                    ctx.bind(part)
            elif child.type == "dotted_name":
                head = _first_named_child(child)
                ctx.add_symbol(head, "import", declaration=node)
                for part in child.named_children:
                    ctx.bind(part)
        return

    module_node = _field(node, "module_name")
    module = ctx.text(module_node).strip() if module_node is not None else None
    for part in module_node.named_children if module_node is not None else []:
        ctx.bind(part)
    if module_node is not None and module_node.type == "dotted_name":
        ctx.bind(module_node)
    for child in _fields(node, "name"):
        if child.type == "aliased_import":
            alias = _field(child, "alias")
            original = _field(child, "name")
            ctx.add_symbol(alias, "import", declaration=node, parent=module)
            ctx.add_ref(original)
            ctx.bind(original)
            for part in original.named_children if original is not None else []:
                ctx.bind(part)
        elif child.type == "dotted_name":
            target = _first_named_child(child)
            ctx.add_symbol(target, "import", declaration=node, parent=module)
            ctx.add_ref(target)


# ---------------------------------------------------------- handlers: js family


def _js_variable(node: Any, ctx: _Ctx) -> None:
    """``variable_declarator`` and TS ``public_field_definition``.

    ``const f = (a) => a`` is recorded as a *function*, because that is what
    callers see and what a signature change would break.
    """
    name_node = _field(node, "name")
    if name_node is None or name_node.type not in {"identifier", "property_identifier"}:
        return
    value = _field(node, "value")
    parent_name, parent_is_class = _scope_of(node, ctx)
    if value is not None and value.type in {"arrow_function", "function_expression", "function"}:
        params = _field(value, "parameters") or _field(value, "parameter")
        ctx.add_symbol(
            name_node,
            "method" if parent_is_class else "function",
            declaration=node,
            signature=_signature(value, ctx, params_node=params),
            parent=parent_name,
        )
        return
    if node.type == "variable_declarator" and _inside_function(node, ctx):
        return
    ctx.add_symbol(
        name_node,
        "variable",
        declaration=node,
        parent=parent_name if parent_is_class else None,
    )


def _js_import(node: Any, ctx: _Ctx) -> None:
    """``import d, {a as b}, * as ns from 'mod'`` -> one import symbol each."""
    source_node = _field(node, "source")
    module = ctx.text(source_node).strip("\"'`") if source_node is not None else None
    for child in walk(node):
        if child.type == "import_specifier":
            original = _field(child, "name")
            alias = _field(child, "alias")
            ctx.add_symbol(alias or original, "import", declaration=node, parent=module)
            ctx.add_ref(original)
            ctx.bind(original)
        elif child.type == "namespace_import":
            target = _first_named_child(child)
            ctx.add_symbol(target, "import", declaration=node, parent=module)
        elif child.type == "import_clause":
            for grandchild in child.named_children:
                if grandchild.type == "identifier":
                    ctx.add_symbol(grandchild, "import", declaration=node, parent=module)


# -------------------------------------------------------------- handlers: go


def _go_type(node: Any, ctx: _Ctx) -> None:
    """``type_spec`` — a named struct/interface/alias."""
    ctx.add_symbol(_field(node, "name"), "class", declaration=node)


def _go_variable(node: Any, ctx: _Ctx) -> None:
    """``var``/``const`` specs at package level; ``name:`` repeats for ``a, b = ...``."""
    if _inside_function(node, ctx):
        return
    for name_node in _fields(node, "name"):
        ctx.add_symbol(name_node, "variable", declaration=node)


def _go_method(node: Any, ctx: _Ctx) -> None:
    """``func (r Receiver) Name(...)`` — ``parent`` is the receiver type."""
    receiver = _field(node, "receiver")
    parent_name = None
    if receiver is not None:
        for child in walk(receiver):
            if child.type == "type_identifier":
                parent_name = ctx.text(child)
                break
    ctx.add_symbol(
        _field(node, "name"),
        "method",
        declaration=node,
        signature=_signature(node, ctx),
        parent=parent_name,
    )


def _go_import(node: Any, ctx: _Ctx) -> None:
    """``import_spec``: bound name is the alias, else the final path segment."""
    path_node = _field(node, "path")
    module = ctx.text(path_node).strip("\"`") if path_node is not None else None
    alias = _field(node, "name")
    if alias is not None:
        ctx.add_symbol(alias, "import", declaration=node, parent=module)
        return
    if not module:
        return
    bound = module.rsplit("/", 1)[-1]
    ctx.symbols.append(
        Symbol(
            name=bound,
            kind="import",
            file=ctx.path,
            line=_line(node),
            end_line=_end_line(node),
            parent=module,
        )
    )


# ------------------------------------------------------------- handlers: rust


def _rust_function(node: Any, ctx: _Ctx) -> None:
    """``fn`` items; promoted to ``method`` inside an ``impl``/``trait`` block."""
    parent_name, parent_is_class = _scope_of(node, ctx)
    ctx.add_symbol(
        _field(node, "name"),
        "method" if parent_is_class else "function",
        declaration=node,
        signature=_signature(node, ctx),
        parent=parent_name,
    )


def _rust_use(node: Any, ctx: _Ctx) -> None:
    """Walk a ``use`` tree, binding the local name each leaf introduces."""

    def visit(current: Any, prefix: str) -> None:
        if current is None:
            return
        kind = current.type
        if kind == "use_as_clause":
            path = _field(current, "path")
            alias = _field(current, "alias")
            ctx.add_symbol(alias, "import", declaration=node, parent=_use_prefix(prefix, path, ctx))
            leaf = _use_leaf(path)
            ctx.add_ref(leaf)
            ctx.bind(leaf)
        elif kind == "scoped_use_list":
            path = _field(current, "path")
            nested = _use_prefix(prefix, path, ctx, include_leaf=True)
            for child in _fields(current, "list") or [_field(current, "list")]:
                visit(child, nested)
        elif kind == "use_list":
            for child in current.named_children:
                visit(child, prefix)
        elif kind == "scoped_identifier":
            name_node = _field(current, "name")
            ctx.add_symbol(
                name_node,
                "import",
                declaration=node,
                parent=_use_prefix(prefix, _field(current, "path"), ctx, include_leaf=True) or None,
            )
        elif kind in {"identifier", "type_identifier", "crate", "self", "super"}:
            ctx.add_symbol(current, "import", declaration=node, parent=prefix or None)
        elif kind == "use_wildcard":
            return

    argument = _field(node, "argument")
    visit(argument if argument is not None else _first_named_child(node), "")


def _use_leaf(path: Any) -> Any | None:
    """Final identifier of a (possibly scoped) Rust path."""
    if path is None:
        return None
    if path.type == "scoped_identifier":
        return _field(path, "name")
    return path


def _use_prefix(prefix: str, path: Any, ctx: _Ctx, *, include_leaf: bool = False) -> str:
    """Join an accumulated ``use`` prefix with a path segment."""
    if path is None:
        return prefix
    text = ctx.text(path).strip()
    if not include_leaf:
        if path.type != "scoped_identifier":
            # A bare ``other`` in ``{helper, other as o}`` contributes no extra
            # module segment — the prefix already names the module.
            return prefix
        inner = _field(path, "path")
        text = ctx.text(inner).strip() if inner is not None else ""
    if not text:
        return prefix
    return f"{prefix}::{text}" if prefix else text


# ------------------------------------------------------------- handlers: java


def _java_field(node: Any, ctx: _Ctx) -> None:
    """``field_declaration`` holds one or more ``variable_declarator`` children."""
    parent_name, _ = _scope_of(node, ctx)
    for child in walk(node):
        if child.type == "variable_declarator":
            ctx.add_symbol(_field(child, "name"), "variable", declaration=node, parent=parent_name)


def _java_import(node: Any, ctx: _Ctx) -> None:
    """``import a.b.C;`` binds ``C``; ``parent`` keeps the package path."""
    scoped = None
    for child in node.named_children:
        if child.type in {"scoped_identifier", "identifier"}:
            scoped = child
            break
    if scoped is None:
        return
    text = ctx.text(scoped).strip()
    if text.endswith(".*"):
        return
    bound = text.rsplit(".", 1)
    name_node = _field(scoped, "name") if scoped.type == "scoped_identifier" else scoped
    ctx.add_symbol(
        name_node,
        "import",
        declaration=node,
        parent=bound[0] if len(bound) == 2 else None,
    )


# --------------------------------------------------------------- handler table


def _dispatch(node: Any, role: str, ctx: _Ctx) -> None:
    """Route one captured declaration node to the right handler."""
    language = ctx.spec.language
    node_type = node.type
    if language == "python":
        if role == "variable":
            return _python_variable(node, ctx)
        if role == "import":
            return _python_import(node, ctx)
    elif language in {"javascript", "typescript", "tsx"}:
        if role == "variable":
            return _js_variable(node, ctx)
        if role == "import":
            return _js_import(node, ctx)
    elif language == "go":
        if role == "import":
            return _go_import(node, ctx)
        if role == "variable":
            return _go_variable(node, ctx)
        if role == "method":
            return _go_method(node, ctx)
        if node_type == "type_spec":
            return _go_type(node, ctx)
    elif language == "rust":
        if role == "import":
            return _rust_use(node, ctx)
        if node_type in {"function_item", "function_signature_item"}:
            return _rust_function(node, ctx)
    elif language == "java":
        if role == "import":
            return _java_import(node, ctx)
        if node_type == "field_declaration":
            return _java_field(node, ctx)
    if role == "method":
        return _handle_method(node, ctx)
    return _handle_generic(node, role, ctx)


# ------------------------------------------------------------------ public API


def _prepare(source: str, path: str, language: str | None) -> tuple[_Ctx, Any] | None:
    """Parse and build the extraction context, or ``None`` when unparseable."""
    if not language:
        return None
    spec = SPECS.get(language)
    if spec is None:
        return None
    payload = source.encode("utf-8", errors="replace")
    if len(payload) > MAX_PARSE_BYTES:
        return None
    try:
        tree = parse_source(payload, language)
    except Exception:  # noqa: BLE001 - grammar unavailable, parser blow-up: degrade
        return None
    ctx = _Ctx(src=payload, path=path, spec=spec, lines=source.splitlines())
    return (ctx, tree.root_node)


def _collect_definitions(ctx: _Ctx, root: Any) -> None:
    """Run every declaration query and dispatch the captures in source order."""
    captured: list[tuple[int, int, str, Any]] = []
    for role, pattern in ctx.spec.patterns:
        query = _compile(ctx.spec.language, pattern)
        if query is None:
            continue
        for node in _query_nodes(query, root):
            captured.append((node.start_byte, node.end_byte, role, node))
    seen: set[tuple[int, int, str]] = set()
    for start, end, role, node in sorted(captured, key=lambda item: (item[0], item[1], item[2])):
        key = (start, end, role)
        if key in seen:
            continue
        seen.add(key)
        try:
            _dispatch(node, role, ctx)
        except Exception:  # noqa: BLE001 - one bad node must not lose the file
            continue


def _is_binding_position(node: Any, ctx: _Ctx) -> bool:
    """``True`` when an identifier is introducing a name rather than using one."""
    spec = ctx.spec
    parent = node.parent
    if parent is None:
        return False
    if parent.type in spec.param_container_types:
        return True
    for node_type, field_name in spec.param_name_fields:
        if parent.type == node_type and _is_field(parent, field_name, node):
            return True
    if parent.type in spec.param_first_child_types:
        first = _first_named_child(parent)
        if first is not None and _span(first) == _span(node):
            return True
    for node_type, field_name in spec.binding_fields:
        if parent.type == node_type and _is_field(parent, field_name, node):
            return True
    return False


def _collect_references(ctx: _Ctx, root: Any) -> None:
    """Shared traversal: every identifier-ish node that is not a binding site."""
    spec = ctx.spec
    for node in walk(root):
        if node.type not in spec.identifier_types:
            continue
        if _span(node) in ctx.bindings:
            continue
        if _is_binding_position(node, ctx):
            continue
        ctx.add_ref(node)


def _dedupe_references(references: list[Reference]) -> list[Reference]:
    """Drop duplicate usages of the same name at the same position."""
    seen: set[tuple[str, str, int, int | None]] = set()
    out: list[Reference] = []
    for ref in references:
        key = (ref.name, ref.file, ref.line, ref.column)
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    return out


def _sort_symbols(symbols: list[Symbol]) -> list[Symbol]:
    """Deterministic order: line, then name, then kind."""
    return sorted(symbols, key=lambda s: (s.line, s.name, s.kind))


def _sort_references(references: list[Reference]) -> list[Reference]:
    """Deterministic order: line, then column, then name."""
    return sorted(references, key=lambda r: (r.line, r.column if r.column is not None else -1, r.name))


def extract(source: str, path: str, language: str | None) -> tuple[list[Symbol], list[Reference]]:
    """Extract every definition and reference from one file.

    The primary entry point; :func:`extract_symbols` and
    :func:`extract_references` are the halves, exposed for targeted tests.

    :param language: ``None`` (unsupported) yields ``([], [])``.
    :returns: ``(symbols, references)``, both sorted by line then name so that
        snapshots are stable.
    """
    prepared = _prepare(source, path, language)
    if prepared is None:
        return ([], [])
    ctx, root = prepared
    _collect_definitions(ctx, root)
    _collect_references(ctx, root)
    return (_sort_symbols(ctx.symbols), _sort_references(_dedupe_references(ctx.refs)))


def extract_symbols(source: str, path: str, language: str) -> list[Symbol]:
    """Extract declarations (functions, classes, methods, variables, imports)."""
    prepared = _prepare(source, path, language)
    if prepared is None:
        return []
    ctx, root = prepared
    _collect_definitions(ctx, root)
    return _sort_symbols(ctx.symbols)


def extract_references(source: str, path: str, language: str) -> list[Reference]:
    """Extract name usages: calls, attribute access, type names, import targets.

    ``Reference.context`` is the source line, stripped, capped at a readable
    length so it can be embedded in findings verbatim.
    """
    prepared = _prepare(source, path, language)
    if prepared is None:
        return []
    ctx, root = prepared
    _collect_definitions(ctx, root)
    _collect_references(ctx, root)
    return _sort_references(_dedupe_references(ctx.refs))


def extract_path(source: str, path: str) -> tuple[list[Symbol], list[Reference]]:
    """:func:`extract` with the language inferred from ``path``."""
    return extract(source, path, language_for_path(path))


def symbols_by_name(symbols: list[Symbol]) -> dict[str, list[Symbol]]:
    """Group symbols by unqualified name; a name may have several definitions."""
    grouped: dict[str, list[Symbol]] = {}
    for symbol in symbols:
        grouped.setdefault(symbol.name, []).append(symbol)
    return grouped


def references_by_name(references: list[Reference]) -> dict[str, list[Reference]]:
    """Group references by name — the lookup the semantic signal performs."""
    grouped: dict[str, list[Reference]] = {}
    for reference in references:
        grouped.setdefault(reference.name, []).append(reference)
    return grouped
