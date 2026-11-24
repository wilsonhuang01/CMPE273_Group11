"""Small lexical/cursor layer for a deliberately bounded C17/C++17 rule grammar.

This is not a general C parser. Unrecognized directives, tokens and grammar abstain;
comments and literals are never matched as executable syntax.
"""

import re
from dataclasses import dataclass

from compiler_optimizer.shared.manifest import ContractError


class TransformationAbstained(ContractError):
    """A rule cannot establish its documented preconditions for this source."""


def supported(condition, reason):
    if not condition:
        raise TransformationAbstained("rule backend abstained: " + reason)


@dataclass(frozen=True)
class Token:
    text: str
    start: int
    end: int


LEX = re.compile(
    r"(?P<space>\s+)|(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)|"
    r'(?P<directive>\#[^\n]*)|(?P<string>"(?:\\[^\n]|[^"\\\n])*")|'
    r"(?P<char>'(?:\\[^\n]|[^'\\\n])*')|"
    r"(?P<identifier>[A-Za-z_]\w*)|(?P<number>[0-9]+[uUlL]*)|"
    r"(?P<operator>\+\+|--|\+=|-=|\*=|==|!=|<=|>=|&&|\|\||<<|>>|->|::|"
    r"[{}()\[\];,*+\-/&|^~!<>=?:.%])",
    re.ASCII,
)
INCLUDE = re.compile(r"#[ \t]*include[ \t]*<([A-Za-z0-9_.]+)>[ \t]*(?://[^\n]*)?")
HEADERS = frozenset({"stdio.h", "stdlib.h", "stdint.h", "stddef.h", "limits.h"})
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
RESERVED = frozenset(
    {
        "int",
        "long",
        "void",
        "unsigned",
        "const",
        "for",
        "if",
        "return",
        "static",
        "volatile",
        "restrict",
        "while",
        "sizeof",
        "else",
        "main",
    }
)


def lex(data, *, headers=HEADERS, maximum=131072):
    supported(isinstance(data, bytes) and len(data) <= maximum, "source exceeds rule size limit")
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        raise TransformationAbstained("rule backend abstained: source must be UTF-8") from None
    tokens, includes, offset = [], set(), 0
    while offset < len(text):
        match = LEX.match(text, offset)
        supported(match is not None, "unsupported or unterminated lexical construct")
        kind, value = match.lastgroup, match.group()
        if kind == "directive":
            include = INCLUDE.fullmatch(value)
            supported(not tokens, "includes must precede all executable declarations")
            supported(
                not text[text.rfind("\n", 0, offset) + 1 : offset].strip() and include is not None,
                "only direct system includes are supported",
            )
            name = include[1]
            supported(headers is None or name in headers, "unsupported header: " + name)
            includes.add(name)
        elif kind not in ("space", "comment"):
            tokens.append(Token(value, offset, match.end()))
        offset = match.end()
    supported(
        not any(a.text == "?" and b.text == "?" for a, b in zip(tokens, tokens[1:], strict=False)),
        "trigraph preprocessing is outside the rule grammar",
    )
    return text, tokens, includes


class Cursor:
    def __init__(self, tokens):
        self.tokens, self.index = tokens, 0

    def peek(self):
        return self.tokens[self.index].text if self.index < len(self.tokens) else ""

    def take(self, *values):
        start = self.index
        for value in values:
            supported(self.peek() == value, "unsupported syntax; expected " + value)
            self.index += 1
        return self.tokens[start : self.index]

    def accept(self, value):
        if self.peek() == value:
            self.index += 1
            return True
        return False

    def identifier(self):
        value = self.peek()
        supported(
            IDENTIFIER.fullmatch(value) and value not in RESERVED, "expected an ordinary identifier"
        )
        self.index += 1
        return value

    def number(self):
        sign = -1 if self.accept("-") else 1
        value = self.peek()
        supported(re.fullmatch(r"(?:0|[1-9][0-9]*)", value), "expected a decimal integer")
        self.index += 1
        return sign * int(value)

    def done(self):
        supported(self.index == len(self.tokens), "unsupported trailing syntax")


def main_body(tokens):
    cursor = Cursor(tokens)
    cursor.take("int", "main", "(")
    cursor.accept("void")
    cursor.take(")", "{")
    return cursor
