"""Unit tests for tree-sitter symbol and reference extraction.

Every case is an inline source snippet with exact assertions on the extracted
:class:`~mergesignal.models.Symbol` / :class:`~mergesignal.models.Reference`
values, because the semantic signal's correctness rests entirely on these.
"""

from __future__ import annotations

import pytest

from mergesignal.analysis.symbols import (
    MAX_PARSE_BYTES,
    SUPPORTED_LANGUAGES,
    extract,
    extract_path,
    extract_references,
    extract_symbols,
    normalize_signature,
    references_by_name,
    symbols_by_name,
    walk,
)
from mergesignal.models import Symbol


def brief(symbols: list[Symbol]) -> list[tuple[str, str, int, str | None, str | None]]:
    """``(kind, name, line, signature, parent)`` tuples — readable assertions."""
    return [(s.kind, s.name, s.line, s.signature, s.parent) for s in symbols]


def names(symbols: list[Symbol]) -> list[str]:
    return [s.name for s in symbols]


# ------------------------------------------------------------------- python


PYTHON_SOURCE = """import os
import os.path as osp
from lib import greet, other as o

CONST = 42


class Greeter:
    prefix = "hi"

    def __init__(self, name: str) -> None:
        self.name = name

    def greet(self, loud: bool = False) -> str:
        return greet(self.name)


def main(argv=None):
    g = Greeter("ada")
    return g.greet(loud=True)
"""


def test_python_symbols_are_exact() -> None:
    assert brief(extract_symbols(PYTHON_SOURCE, "app.py", "python")) == [
        ("import", "os", 1, None, None),
        ("import", "osp", 2, None, "os.path"),
        ("import", "greet", 3, None, "lib"),
        ("import", "o", 3, None, "lib"),
        ("variable", "CONST", 5, None, None),
        ("class", "Greeter", 8, None, None),
        ("variable", "prefix", 9, None, "Greeter"),
        ("method", "__init__", 11, "(self, name: str) -> None", "Greeter"),
        ("method", "greet", 14, "(self, loud: bool=False) -> str", "Greeter"),
        ("function", "main", 18, "(argv=None)", None),
    ]


def test_python_import_qualified_names_carry_the_module() -> None:
    symbols = extract_symbols(PYTHON_SOURCE, "app.py", "python")
    imported = {s.name: s for s in symbols if s.kind == "import"}
    assert imported["greet"].qualified_name == "lib.greet"
    assert imported["os"].qualified_name == "os"
    method = next(s for s in symbols if s.kind == "method" and s.name == "greet")
    assert method.qualified_name == "Greeter.greet"


def test_python_references_include_calls_and_import_targets() -> None:
    references = extract_references(PYTHON_SOURCE, "app.py", "python")
    by_name = references_by_name(references)
    assert [(r.line, r.column) for r in by_name["greet"]] == [(3, 16), (15, 15), (20, 13)]
    assert by_name["greet"][0].context == "from lib import greet, other as o"
    assert [r.line for r in by_name["Greeter"]] == [19]
    assert "other" in by_name, "the aliased-away original name is still referenced"


def test_python_bindings_are_not_references() -> None:
    reference_names = {r.name for r in extract_references(PYTHON_SOURCE, "app.py", "python")}
    assert "CONST" not in reference_names, "a module-level assignment target is a binding"
    assert "main" not in reference_names, "a def's own name is not a usage of itself"
    assert "argv" not in reference_names, "parameter names are bindings"
    assert "loud" not in reference_names, "keyword-argument labels are not usages"
    references = extract_references(PYTHON_SOURCE, "app.py", "python")
    assert [r.line for r in references if r.name == "g"] == [20], (
        "line 19 assigns g (a binding); line 20 uses it (a reference)"
    )


def test_python_nested_function_parent_is_the_enclosing_function() -> None:
    source = "def outer(a):\n    def inner(b):\n        return b\n    return inner\n"
    assert brief(extract_symbols(source, "n.py", "python")) == [
        ("function", "outer", 1, "(a)", None),
        ("function", "inner", 2, "(b)", "outer"),
    ]


def test_python_end_line_spans_the_body() -> None:
    source = "def f(a):\n    x = 1\n    return x\n"
    symbol = extract_symbols(source, "s.py", "python")[0]
    assert (symbol.line, symbol.end_line) == (1, 3)


def test_python_star_import_defines_nothing() -> None:
    assert extract_symbols("from lib import *\n", "w.py", "python") == []


# --------------------------------------------------------------- javascript


JS_SOURCE = """import defaultThing, { greet, other as o } from './lib';
import * as ns from 'mod';

export const LIMIT = 10;

export function compute(a, b = 2) {
  return greet(a) + b;
}

const arrow = (x) => x * 2;

export class Widget extends Base {
  constructor(name) { this.name = name; }
  render(mode) { return compute(mode); }
}
"""


def test_javascript_symbols_are_exact() -> None:
    assert brief(extract_symbols(JS_SOURCE, "app.js", "javascript")) == [
        ("import", "defaultThing", 1, None, "./lib"),
        ("import", "greet", 1, None, "./lib"),
        ("import", "o", 1, None, "./lib"),
        ("import", "ns", 2, None, "mod"),
        ("variable", "LIMIT", 4, None, None),
        ("function", "compute", 6, "(a, b=2)", None),
        ("function", "arrow", 10, "(x)", None),
        ("class", "Widget", 12, None, None),
        ("method", "constructor", 13, "(name)", "Widget"),
        ("method", "render", 14, "(mode)", "Widget"),
    ]


def test_javascript_arrow_assigned_to_const_is_a_function() -> None:
    symbol = next(
        s for s in extract_symbols(JS_SOURCE, "app.js", "javascript") if s.name == "arrow"
    )
    assert (symbol.kind, symbol.signature) == ("function", "(x)")


def test_javascript_references() -> None:
    by_name = references_by_name(extract_references(JS_SOURCE, "app.js", "javascript"))
    assert [r.line for r in by_name["greet"]] == [1, 7], "the import target counts as a usage"
    assert [r.line for r in by_name["compute"]] == [14]
    assert "Base" in by_name, "an extends clause is a usage"
    assert "LIMIT" not in by_name


def test_jsx_is_parsed_as_javascript() -> None:
    source = "export function App() { return <div>{title}</div>; }\n"
    assert brief(extract_symbols(source, "App.jsx", "javascript")) == [
        ("function", "App", 1, "()", None)
    ]


# --------------------------------------------------------------- typescript


TS_SOURCE = """import { greet } from './lib';

export interface Shape { area(): number; }
type Id = string;
export enum Color { Red, Blue }

export function compute(a: number, b: string = "x"): number {
  return a;
}

export class Widget implements Shape {
  private label: string = "w";
  area(): number { return 1; }
}
"""


def test_typescript_symbols_are_exact() -> None:
    assert brief(extract_symbols(TS_SOURCE, "app.ts", "typescript")) == [
        ("import", "greet", 1, None, "./lib"),
        ("class", "Shape", 3, None, None),
        ("method", "area", 3, "() -> number", "Shape"),
        ("class", "Id", 4, None, None),
        ("class", "Color", 5, None, None),
        ("function", "compute", 7, '(a: number, b: string="x") -> number', None),
        ("class", "Widget", 11, None, None),
        ("variable", "label", 12, None, "Widget"),
        ("method", "area", 13, "() -> number", "Widget"),
    ]


def test_tsx_components_and_props() -> None:
    source = (
        "import React from 'react';\n"
        "export const Button = ({label}: Props) => <button>{label}</button>;\n"
        "export default function App() { return <Button label='x' />; }\n"
    )
    assert brief(extract_symbols(source, "App.tsx", "tsx")) == [
        ("import", "React", 1, None, "react"),
        ("function", "Button", 2, "({label}: Props)", None),
        ("function", "App", 3, "()", None),
    ]
    assert "Button" in {r.name for r in extract_references(source, "App.tsx", "tsx")}


# --------------------------------------------------------------------- go


GO_SOURCE = """package main

import (
\t"fmt"
\talias "example.com/pkg/thing"
)

const Limit = 10
var counter, total int

type Server struct {
\tName string
}

func (s *Server) Start(port int) error {
\tfmt.Println(s.Name)
\treturn nil
}

func Compute(a int, b string) (int, error) {
\treturn a, nil
}
"""


def test_go_symbols_are_exact() -> None:
    assert brief(extract_symbols(GO_SOURCE, "main.go", "go")) == [
        ("import", "fmt", 4, None, "fmt"),
        ("import", "alias", 5, None, "example.com/pkg/thing"),
        ("variable", "Limit", 8, None, None),
        ("variable", "counter", 9, None, None),
        ("variable", "total", 9, None, None),
        ("class", "Server", 11, None, None),
        ("method", "Start", 15, "(port int) -> error", "Server"),
        ("function", "Compute", 20, "(a int, b string) -> (int, error)", None),
    ]


def test_go_method_parent_is_the_receiver_type() -> None:
    method = next(s for s in extract_symbols(GO_SOURCE, "main.go", "go") if s.name == "Start")
    assert method.parent == "Server"
    assert method.qualified_name == "Server.Start"


def test_go_references() -> None:
    by_name = references_by_name(extract_references(GO_SOURCE, "main.go", "go"))
    assert [r.line for r in by_name["Println"]] == [16]
    assert "fmt" in by_name
    assert "port" not in by_name, "parameter names are bindings"


# ------------------------------------------------------------------- rust


RUST_SOURCE = """use std::collections::HashMap;
use crate::util::{helper, other as o};

pub const LIMIT: usize = 10;
static NAME: &str = "x";

pub struct Server { pub name: String }

pub trait Runner { fn run(&self) -> bool; }

impl Server {
    pub fn start(&self, port: u16) -> Result<(), String> {
        helper(port);
        Ok(())
    }
}

pub fn compute(a: i32, b: i32) -> i32 { a + b }
"""


def test_rust_symbols_are_exact() -> None:
    assert brief(extract_symbols(RUST_SOURCE, "lib.rs", "rust")) == [
        ("import", "HashMap", 1, None, "std::collections"),
        ("import", "helper", 2, None, "crate::util"),
        ("import", "o", 2, None, "crate::util"),
        ("variable", "LIMIT", 4, None, None),
        ("variable", "NAME", 5, None, None),
        ("class", "Server", 7, None, None),
        ("class", "Runner", 9, None, None),
        ("method", "run", 9, "(&self) -> bool", "Runner"),
        ("method", "start", 12, "(&self, port: u16) -> Result<(), String>", "Server"),
        ("function", "compute", 18, "(a: i32, b: i32) -> i32", None),
    ]


def test_rust_impl_methods_are_parented_by_the_impl_type() -> None:
    start = next(s for s in extract_symbols(RUST_SOURCE, "lib.rs", "rust") if s.name == "start")
    assert (start.kind, start.parent) == ("method", "Server")


def test_rust_references() -> None:
    by_name = references_by_name(extract_references(RUST_SOURCE, "lib.rs", "rust"))
    assert [r.line for r in by_name["helper"]] == [13]
    assert "other" in by_name, "the aliased-away original name is still referenced"


# ------------------------------------------------------------------- java


JAVA_SOURCE = """package demo;

import java.util.List;
import demo.util.Helper;

public class App {
    private int counter = 0;

    public App(String name) { this.name = name; }

    public int compute(int a, String b) {
        return Helper.help(a);
    }
}

interface Shape { int area(); }
"""


def test_java_symbols_are_exact() -> None:
    assert brief(extract_symbols(JAVA_SOURCE, "App.java", "java")) == [
        ("import", "List", 3, None, "java.util"),
        ("import", "Helper", 4, None, "demo.util"),
        ("class", "App", 6, None, None),
        ("variable", "counter", 7, None, "App"),
        ("method", "App", 9, "(String name)", "App"),
        ("method", "compute", 11, "(int a, String b) -> int", "App"),
        ("class", "Shape", 16, None, None),
        ("method", "area", 16, "() -> int", "Shape"),
    ]


def test_java_references() -> None:
    by_name = references_by_name(extract_references(JAVA_SOURCE, "App.java", "java"))
    assert [r.line for r in by_name["Helper"]] == [12]
    assert [r.line for r in by_name["help"]] == [12]
    assert "a" in by_name
    assert "counter" not in by_name


# ------------------------------------------------------------------ robustness


def test_unsupported_language_yields_empty_lists() -> None:
    assert extract("BEGIN\n  do thing\nEND\n", "script.zzz", None) == ([], [])


def test_recognised_grammar_without_rules_yields_empty_lists() -> None:
    """``.yaml`` is recognised (so ``DiffFile.language`` is set) but has no rules."""
    assert extract("key: value\n", "conf.yaml", "yaml") == ([], [])


def test_partial_syntax_errors_still_yield_the_parseable_parts() -> None:
    source = (
        "def good(a):\n"
        "    return a\n"
        "\n"
        "def bad(:::\n"
        "    ???\n"
        "\n"
        "class Later:\n"
        "    def method(self, x):\n"
        "        return x\n"
    )
    assert names(extract_symbols(source, "broken.py", "python")) == ["good", "Later", "method"]


def test_file_above_the_parse_cap_is_skipped() -> None:
    huge = "x = 1\n" * (MAX_PARSE_BYTES // 3)
    assert len(huge.encode()) > MAX_PARSE_BYTES
    assert extract(huge, "huge.py", "python") == ([], [])


def test_empty_file_is_not_an_error() -> None:
    assert extract("", "empty.py", "python") == ([], [])


def test_mojibake_does_not_crash_the_extractor() -> None:
    source = "# \udce9 broken encoding\ndef f(a):\n    return a\n"
    assert names(extract_symbols(source, "enc.py", "python")) == ["f"]


def test_extract_path_infers_the_language() -> None:
    symbols, references = extract_path("def f(a):\n    return a\n", "src/x.py")
    assert names(symbols) == ["f"]
    assert [r.name for r in references] == ["a"]
    assert extract_path("whatever", "x.zzz") == ([], [])


def test_output_is_sorted_and_deterministic() -> None:
    symbols, references = extract(PYTHON_SOURCE, "app.py", "python")
    assert [s.line for s in symbols] == sorted(s.line for s in symbols)
    assert [r.line for r in references] == sorted(r.line for r in references)
    assert extract(PYTHON_SOURCE, "app.py", "python") == (symbols, references)


def test_reference_context_is_capped() -> None:
    source = "def f():\n    return " + " + ".join(["value"] * 200) + "\n"
    references = extract_references(source, "long.py", "python")
    assert references
    assert all(len(r.context) <= 200 for r in references)


def test_every_advertised_language_extracts_something() -> None:
    snippets = {
        "python": "def f(a):\n    return a\n",
        "javascript": "function f(a) { return a; }\n",
        "typescript": "function f(a: number): number { return a; }\n",
        "tsx": "function f(a: number): number { return a; }\n",
        "go": "package p\nfunc F(a int) int { return a }\n",
        "rust": "pub fn f(a: i32) -> i32 { a }\n",
        "java": "class C { int f(int a) { return a; } }\n",
    }
    assert set(snippets) == set(SUPPORTED_LANGUAGES)
    for language, source in snippets.items():
        assert extract_symbols(source, f"x.{language}", language), language


# --------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("(a, b)", "(a, b)"),
        ("(  a ,   b )", "(a, b)"),
        ("(\n    a,\n    b,\n)", "(a, b,)"),
        ("(a = 1)", "(a=1)"),
        ("(a : int)", "(a: int)"),
        ("(x: std::vec::Vec)", "(x: std::vec::Vec)"),
        ("(a)  ->   int", "(a) -> int"),
        ("(\n    a,  # the first\n    b,\n)", "(a, b,)"),
    ],
)
def test_normalize_signature(raw: str | None, expected: str | None) -> None:
    assert normalize_signature(raw) == expected


def test_normalize_signature_is_idempotent() -> None:
    once = normalize_signature("(  a : int = 1 ,\n  b )")
    assert once == normalize_signature(once)


def test_reformatting_alone_is_not_a_signature_change() -> None:
    compact = extract_symbols("def f(a,b=1):\n    pass\n", "a.py", "python")[0]
    spread = extract_symbols("def f(\n    a,\n    b = 1,\n):\n    pass\n", "a.py", "python")[0]
    assert compact.signature == "(a, b=1)"
    assert spread.signature == "(a, b=1,)"


def test_walk_visits_every_node_without_recursion() -> None:
    from mergesignal.analysis.languages import parse_source

    tree = parse_source("def f(a):\n    return a\n", "python")
    types = [node.type for node in walk(tree.root_node)]
    assert types[0] == "module"
    assert "function_definition" in types
    assert "identifier" in types


def test_walk_survives_deep_nesting() -> None:
    from mergesignal.analysis.languages import parse_source

    tree = parse_source("x = " + "(" * 400 + "1" + ")" * 400 + "\n", "python")
    assert sum(1 for _ in walk(tree.root_node)) > 400


def test_symbols_by_name_groups_overloads() -> None:
    symbols = extract_symbols(TS_SOURCE, "app.ts", "typescript")
    grouped = symbols_by_name(symbols)
    assert len(grouped["area"]) == 2
    assert {s.parent for s in grouped["area"]} == {"Shape", "Widget"}


def test_references_by_name_is_empty_for_unknown_names() -> None:
    grouped = references_by_name(extract_references(PYTHON_SOURCE, "app.py", "python"))
    assert grouped.get("nope") is None
