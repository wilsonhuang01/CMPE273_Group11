"""Portable CPU candidates with checked lexical/grammar preconditions, never source hashes."""

import json
import re

from compiler_optimizer.shared.c_syntax import (
    Cursor,
    TransformationAbstained,
    lex,
    main_body,
    supported,
)
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.workers.targets import compiler_flags


def scan_check(c, name, kind, low, high, *, array=None, free=None):
    c.take("if", "(", "scanf", "(", '"%d"' if kind == "int" else '"%lld"', ",", "&", name)
    if array:
        c.take("[", array, "]")
    c.take(")", "!=", "1", "||", name)
    if array:
        c.take("[", array, "]")
    c.take("<")
    minimum = c.number()
    c.take("||", name)
    if array:
        c.take("[", array, "]")
    c.take(">")
    maximum = c.number()
    c.take(")", "{")
    if free:
        c.take("free", "(", free, ")", ";")
    c.take("return", "1", ";", "}")
    supported(low <= minimum <= maximum <= high, "input bounds exceed their scalar type")
    return minimum, maximum


def loop(c, count, *, kind="int"):
    c.take("for", "(", kind)
    index = c.identifier()
    c.take("=", "0", ";", index, "<", count, ";")
    if c.accept("++"):
        c.take(index)
    else:
        c.take(index, "++")
    c.take(")", "{")
    return index


def bounded_sum(tokens, *, buffered=False):
    c = main_body(tokens)
    c.take("int")
    count = c.identifier()
    c.take(";")
    low, maximum = scan_check(c, count, "int", 0, 2147483647)
    supported(low == 0, "count must have an explicit zero lower bound")
    names = {count}
    if buffered:
        c.take("long", "long", "*")
        values = c.identifier()
        names.add(values)
        c.take("=")
        if c.accept("("):
            c.take("long", "long", "*", ")")
        c.take(
            "malloc",
            "(",
            "(",
            "(",
            "size_t",
            ")",
            count,
            "+",
            "1",
            ")",
            "*",
            "sizeof",
            "(",
            "*",
            values,
            ")",
            ")",
            ";",
        )
        c.take("if", "(", values, "==", "NULL", ")", "{", "return", "1", ";", "}")
        allocation_end = c.tokens[c.index - 1].end
        index = loop(c, count)
        minimum_value, maximum_value = scan_check(
            c, values, "long long", -(2**63), 2**63 - 1, array=index, free=values
        )
        c.take("}", "long", "long")
        total = c.identifier()
        c.take("=", "0", ";")
        second_index = loop(c, count)
        c.take(total, "+=", values, "[", second_index, "]", ";", "}", "free", "(", values, ")", ";")
        names.update({index, second_index, total})
        # Reusing i in separate loop scopes is allowed; all other identities must differ.
        supported(
            len({count, values, total, index}) == 4 and second_index not in {count, values, total},
            "ambiguous reduction identifiers",
        )
        result = {"values": values, "allocation_end": allocation_end}
    else:
        c.take("long", "long")
        total = c.identifier()
        c.take("=", "0", ";")
        index = loop(c, count)
        value_type = "int" if c.accept("int") else "long long"
        if value_type == "long long":
            c.take("long", "long")
        value = c.identifier()
        c.take(";")
        minimum_value, maximum_value = scan_check(c, value, value_type, -(2**31), 2**31 - 1)
        addition_start = c.tokens[c.index].start
        c.take(total, "+=", value, ";")
        addition_end = c.tokens[c.index - 1].end
        c.take("}")
        supported(len({count, total, index, value}) == 4, "ambiguous reduction identifiers")
        result = {"value": value, "addition_start": addition_start, "addition_end": addition_end}
    output_start = c.tokens[c.index].start
    c.take("printf", "(", '"%lld\\n"', ",", total, ")", ";", "return", "0", ";", "}")
    c.done()
    supported(
        maximum * max(abs(minimum_value), abs(maximum_value)) <= 2**63 - 1,
        "signed reduction lacks a representable bound for all partial sums",
    )
    supported(
        not buffered or maximum <= (2**64 - 1) // 8 - 1, "allocation size may overflow size_t"
    )
    return {
        **result,
        "count": count,
        "total": total,
        "index": index,
        "minimum": minimum_value,
        "maximum": maximum_value,
        "count_max": maximum,
        "output_start": output_start,
    }


def numeric_kernels(tokens):
    """Find whole top-level void pointwise kernels; reject unknown outer declarations."""
    c, kernels = Cursor(tokens), []
    while c.peek():
        start = c.index
        if c.peek() == "int":
            main = main_body(c.tokens[c.index :])
            c.index += main.index
            depth = 1
            while depth:
                token = c.peek()
                supported(bool(token), "unterminated main")
                depth += (token == "{") - (token == "}")
                c.index += 1
            continue
        c.take("void")
        name = c.identifier()
        c.take("(")
        source_const = c.accept("const")
        c.take("unsigned", "int", "*")
        source = c.identifier()
        c.take(",", "unsigned", "int", "*")
        destination = c.identifier()
        c.take(",", "unsigned", "int")
        count = c.identifier()
        c.take(")", "{", "for", "(", "unsigned", "int")
        index = c.identifier()
        c.take("=", "0", ";", index, "<", count, ";")
        if c.accept("++"):
            c.take(index)
        else:
            c.take(index, "++")
        c.take(")", "{")
        c.take(destination, "[", index, "]", "=", source, "[", index, "]")
        operator = c.peek()
        supported(operator in {"+", "*", "^", "&", "|"}, "unsupported pointwise arithmetic")
        c.index += 1
        literal = c.peek()
        supported(
            re.fullmatch(r"(?:0|[1-9][0-9]*)[uU]?", literal) is not None,
            "pointwise operation needs a constant unsigned operand",
        )
        supported(int(literal.rstrip("uU")) <= 2**32 - 1, "constant exceeds unsigned int")
        supported(
            literal.endswith(("u", "U")) or int(literal) <= 2**31 - 1,
            "large constants require an explicit unsigned suffix",
        )
        c.index += 1
        c.take(";", "}", "}")
        supported(
            len({name, source, destination, count, index}) == 5, "ambiguous kernel identifiers"
        )
        kernels.append(
            {
                "start": tokens[start].start,
                "end": tokens[c.index - 1].end,
                "name": name,
                "source": source,
                "destination": destination,
                "count": count,
                "index": index,
                "operator": operator,
                "literal": literal,
                "source_const": source_const,
            }
        )
    supported(bool(kernels), "no supported pointwise kernel")
    return kernels


def pointwise(kernel, *, guarded):
    source, destination, count, index = (
        kernel[k] for k in ("source", "destination", "count", "index")
    )

    def statement(offset):
        at = index if offset == 0 else f"({index} + {offset}U)"
        return f"{destination}[{at}] = {source}[{at}] {kernel['operator']} {kernel['literal']};"

    lanes = 4 if guarded else 2
    body = "\n".join("        " + statement(i) for i in range(lanes))
    unrolled = (
        f"unsigned int {index} = 0;\n"
        f"    for (; {count} - {index} >= {lanes}U; {index} += {lanes}U) {{\n{body}\n    }}\n"
        f"    for (; {index} < {count}; ++{index}) {{\n        {statement(0)}\n    }}"
    )
    if guarded:
        fallback = (
            f"for (unsigned int {index} = 0; {index} < {count}; ++{index}) {{\n"
            f"        {statement(0)}\n    }}"
        )
        body = (
            f"if ({source} == {destination}) {{\n    {unrolled}\n"
            f"    }} else {{\n    {fallback}\n    }}"
        )
    else:
        body = unrolled
    qualifier = "const " if kernel["source_const"] else ""
    return (
        f"void {kernel['name']}({qualifier}unsigned int *{source}, unsigned int *{destination}, "
        f"unsigned int {count}) {{\n    {body}\n}}"
    )


class PortableRuleBackend:
    role = pb.AGENT_ROLE_UNSPECIFIED
    # All admitted CPU ABIs have 32-bit int and 64-bit size_t/long long. RISC-V
    # may use these portable edits for correctness, never performance qualification.
    targets = frozenset({"linux-aarch64", "linux-x86_64", "linux-riscv64"})

    def generate(self, request, sources):
        supported(request.role == self.role, "rule assigned to the wrong role")
        supported(request.manifest.language in {"c17", "cpp17"}, "unsupported language")
        supported(
            request.compiler.target in self.targets,
            "rules require a controlled native CPU target"
            if self.role == pb.ARCHITECTURE
            else "rules require a controlled CPU target",
        )
        supported(len(sources) == 1, "rules require one translation unit and no local headers")
        supported(
            request.compiler.language == request.manifest.language
            and list(request.compiler.flags)
            == compiler_flags(
                request.compiler.target,
                request.manifest.language,
                request.compiler.execution_profile,
            ),
            "rule requires the fixed compiler language/flags",
        )
        path, original = next(iter(sources.items()))
        text, tokens, includes = lex(original)
        supported(
            not any(
                t.text
                in {
                    "volatile",
                    "restrict",
                    "__restrict",
                    "__restrict__",
                    "asm",
                    "__asm",
                    "__asm__",
                    "__attribute__",
                    "__declspec",
                    "__builtin_assume",
                    "__builtin_assume_aligned",
                }
                for t in tokens
            ),
            "unsupported volatility, alias, target or compiler assumptions",
        )
        changed, identifier, rationale = self.transform(text, tokens, includes)
        supported(changed != text, "no safe progress")
        return json.dumps(
            {
                "schema_version": 2,
                "contract": {
                    "schema_version": 1,
                    "mode": "portable",
                    "target": request.compiler.target,
                    "language": request.manifest.language,
                    "required_features": [],
                },
                "parent_source_sha256": request.source.bundle.sha256,
                "transformation": identifier,
                "rationale": rationale,
                "edits": [{"path": path, "old": text, "new": changed}],
            },
            sort_keys=True,
        ).encode()


class PerformanceRuleBackend(PortableRuleBackend):
    role = pb.PERFORMANCE

    def transform(self, text, tokens, includes):
        try:
            kernel = numeric_kernels(tokens)[0]
        except TransformationAbstained:
            return self.buffer(text, tokens, includes)
        return (
            text[: kernel["start"]] + pointwise(kernel, guarded=False) + text[kernel["end"] :],
            "portable-pointwise-unroll-2",
            "Unroll the checked unsigned pointwise loop by two "
            "with a scalar tail. Preserve operation order, including overlapping arrays; "
            "n-i guards prevent counter wrap. No alias promises or compiler flags change.",
        )

    def buffer(self, text, tokens, includes):
        supported("stdio.h" in includes, "buffering requires stdio.h")
        recognized = False
        for buffered in (False, True):
            try:
                bounded_sum(tokens, buffered=buffered)
                recognized = True
                break
            except TransformationAbstained:
                pass
        if not recognized:
            # A bounded integer echo is another complete, side-effect-limited grammar.
            echo = main_body(tokens)
            echo.take("int")
            value = echo.identifier()
            echo.take(";")
            scan_check(echo, value, "int", -(2**31), 2**31 - 1)
            echo.take("printf", "(", '"%d\\n"', ",", value, ")", ";", "return", "0", ";", "}")
            echo.done()
        c = main_body(tokens)
        names = {t.text for t in tokens}
        supported(
            "setvbuf" not in names and "setbuf" not in names,
            "input buffering is already configured",
        )
        name = "optimizer_input_buffer"
        supported(name not in names, "buffer identifier collision")
        brace = tokens[c.index - 1].end
        addition = (
            f"\n    static char {name}[65536];\n"
            f"    (void)setvbuf(stdin, {name}, _IOFBF, sizeof({name}));\n"
        )
        return (
            text[:brace] + addition + text[brace:],
            "stdio-input-buffer-64k",
            "Configure stdin before its first operation with a static 64 KiB buffer. "
            "The buffer lives until exit. Correctness and any runtime/RSS gain "
            "require measurement.",
        )


class MemoryRuleBackend(PortableRuleBackend):
    role = pb.MEMORY

    def transform(self, text, tokens, includes):
        supported({"stdio.h", "stdlib.h"} <= includes, "streaming requires stdio.h and stdlib.h")
        node = bounded_sum(tokens, buffered=True)
        count, total, index, values = (node[k] for k in ("count", "total", "index", "values"))
        value = "optimizer_value"
        supported(value not in {t.text for t in tokens}, "streaming identifier collision")
        replacement = (
            f"\n    free({values});\n    long long {total} = 0;\n"
            f"    for (int {index} = 0; {index} < {count}; ++{index}) {{\n"
            f"        long long {value};\n"
            f'        if (scanf("%lld", &{value}) != 1 || {value} < {node["minimum"]} || '
            f"{value} > {node['maximum']}) {{\n            return 1;\n        }}\n"
            f"        {total} += {value};\n    }}\n    "
        )
        changed = text[: node["allocation_end"]] + replacement + text[node["output_start"] :]
        return (
            changed,
            "stream-bounded-integer-sum",
            "Fuse bounded input and reduction, releasing "
            "the untouched heap allocation before streaming. Preserve the allocation request "
            "and NULL failure check. The parsed count/value bounds prove every long long "
            "partial sum representable; peak RSS savings remain an empirical result.",
        )


class ArchitectureRuleBackend(PortableRuleBackend):
    role = pb.ARCHITECTURE
    targets = frozenset({"linux-aarch64", "linux-x86_64"})

    def transform(self, text, tokens, includes):
        try:
            node = bounded_sum(tokens)
        except TransformationAbstained:
            kernel = numeric_kernels(tokens)[0]
            return (
                text[: kernel["start"]] + pointwise(kernel, guarded=True) + text[kernel["end"] :],
                "portable-guarded-vectorization",
                "For equal input/output pointers, expose "
                "four independent unsigned lanes with a scalar tail. Otherwise retain the "
                "original ordered loop, including partially overlapping arrays. The compiler "
                "chooses whether to vectorize under the unchanged native target profile.",
            )
        supported("stdio.h" in includes, "bounded reduction requires stdio.h")
        count, total, index, value = (node[k] for k in ("count", "total", "index", "value"))
        supported("alternate" not in {t.text for t in tokens}, "accumulator identifier collision")
        changed = (
            text[: node["addition_start"]]
            + (
                f"if (({index} & 1) == 0) {{\n"
                f"            {total} += {value};\n        }} else {{\n"
                f"            alternate += {value};\n        }}"
            )
            + text[node["addition_end"] :]
        )
        # Whole-token offsets identify the declaration and output, not comments/string text.
        declaration = next(i for i, t in enumerate(tokens) if t.text == total)
        start, end = tokens[declaration].start, tokens[declaration + 3].end
        changed = changed[:start] + f"{total} = 0, alternate = 0;" + changed[end:]
        # The output occurs after the edited loop; locate it by its unique full token span.
        old = text[node["output_start"] :]
        # Whitespace is arbitrary; construct the complete checked output instead.
        new = f'printf("%lld\\n", {total} + alternate);\n    return 0;\n}}\n'
        changed = changed[: -len(old)] + new
        return (
            changed,
            "portable-independent-accumulators",
            f"Split the bounded reduction into "
            f"independent streams. Parsed limits ({count} <= {node['count_max']}, values "
            f"in [{node['minimum']}, {node['maximum']}]) bound all partial and final sums "
            "inside long long. Use portable scalar source with unchanged compiler flags.",
        )
