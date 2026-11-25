"""Small, versioned integer kernel contract; no C/C++ frontend is implied."""

from dataclasses import dataclass

from compiler_optimizer.shared.manifest import require


@dataclass(frozen=True)
class Kernel:
    kind: str
    size: int
    factor: int = 3
    schema_version: int = 1

    def __post_init__(self):
        require(
            type(self.schema_version) is int and self.schema_version == 1,
            "unsupported kernel version",
        )
        require(self.kind in ("add", "scale"), "unsupported kernel")
        require(type(self.size) is int and 1 <= self.size <= 4096, "invalid kernel size")
        require(type(self.factor) is int and -16 <= self.factor <= 16, "invalid factor")
        require(self.kind == "scale" or self.factor == 3, "add has no configurable factor")

    def inputs(self):
        return [i % 97 - 48 for i in range(self.size)], [i % 31 - 15 for i in range(self.size)]

    def expected(self):
        a, b = self.inputs()
        return (
            [x + y for x, y in zip(a, b, strict=True)]
            if self.kind == "add"
            else [x * self.factor for x in a]
        )

    def mlir(self):
        mem = f"memref<{self.size}xi32>"
        rhs = (
            f"%y = affine.load %b[%i] : {mem}"
            if self.kind == "add"
            else f"%y = arith.constant {self.factor} : i32"
        )
        op = "addi" if self.kind == "add" else "muli"
        return f"""module {{
  func.func @kernel(%a: {mem}, %b: {mem}, %out: {mem}) attributes {{llvm.emit_c_interface}} {{
    affine.for %i = 0 to {self.size} {{
      %x = affine.load %a[%i] : {mem}
      {rhs}
      %v = arith.{op} %x, %y : i32
      affine.store %v, %out[%i] : {mem}
    }}
    return
  }}
}}
""".encode()

    def driver(self):
        a, b = self.inputs()
        return f"""#include <stdint.h>
#include <stdio.h>
typedef struct {{ int32_t *allocated, *aligned; int64_t offset, size, stride; }} Memref;
extern void _mlir_ciface_kernel(Memref *, Memref *, Memref *);
int main(void) {{
  int32_t a[{self.size}] = {{{",".join(map(str, a))}}};
  int32_t b[{self.size}] = {{{",".join(map(str, b))}}};
  int32_t out[{self.size}] = {{0}};
  Memref x = {{a,a,0,{self.size},1}}, y = {{b,b,0,{self.size},1}},
         z = {{out,out,0,{self.size},1}};
  _mlir_ciface_kernel(&x, &y, &z);
  for (int i=0; i<{self.size}; ++i) printf("%d\\n", out[i]);
  return 0;
}}
""".encode()
