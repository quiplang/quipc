#!/usr/bin/env python3
"""Check MIR simplification, SSA, alias safety, aggregate forwarding and dumps."""
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPILER = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / 'target/debug/quipc'


def function(ir, name):
    found = re.search(r'define [^\n]*@' + name + r'[^\n]*\{\n(.*?)\n}', ir, re.S)
    assert found, (name, ir)
    return found[1]


with tempfile.TemporaryDirectory(prefix='quip-mir-opt-') as directory:
    work = pathlib.Path(directory)
    source = work / 'main.quip'
    many = 'make many (n: int) -> int {\n'
    many += '\n'.join('    make a%d = n + %d' % (i, i) for i in range(70))
    many += '\n    make values = [1]\n    if values.len() != 1 { return 0 }\n    return '
    many += ' + '.join('a%d' % i for i in range(70)) + '\n}\n'
    source.write_text('''@import "RUNTIME"
make identity (n: int) -> int { return n }
make chain (n: int) -> int { return identity(identity(n)) }
make dead (n: int) -> int { make unused = n * 37 return n }
make keep_division (n: int) -> int { make unused = 10 / n return n }
make constant_branch (n: int) -> int {
    make flag = !(2 + 3 < 4)
    if flag { return n + 0 } else { return n * 1 }
}
make loop (n: int) -> int {
    make i = 0
    make total = 0
    while i < n {
        i++
        if i == 3 { continue }
        if i > 8 { break }
        total += i
    }
    return total
}
make join (n: int) -> int {
    make value = 1
    if n > 0 { value = 2 } else { value = 3 }
    return value
}
make inc (p: ref<int>) -> int { p.* += 1 return p.* }
make snapshot () -> int {
    make n = 2
    make result = n + inc(n.pin)
    return result
}
make read (p: ref<int>) -> int { return p.* + p.* }
make after_write (p: ref<int>) -> int {
    make first = p.*
    p.* = 9
    return first + p.*
}
make after_call (p: ref<int>) -> int {
    make first = p.*
    inc(p)
    return first + p.*
}
make general_read (p: ref<int>) -> char {
    make first = p.*
    p.* = 2
    if first + p.* == 3 { return 'y' }
    return 'n'
}
make type Pair { @pub make left: int @pub make right: int }
make pair (n: int) -> Pair { return Pair { left = n, right = n + 1 } }
make pair_value (n: int) -> int { make p = pair(n) return p.left + p.right }
make flip (p: ref<Pair>) -> Pair { return Pair { left = p.*.right, right = p.*.left } }
make choose (flag: bool) -> str {
    if flag { make a = "a" return a }
    make b = "b"
    return b
}
@pub @entry
make main () -> int {
    if chain(dead(constant_branch(7))) != 7 { return 1 }
    if loop(20) != 33 { return 2 }
    if join(0) != 3 || join(1) != 2 { return 3 }
    if snapshot() != 5 { return 4 }
    if many(1) != 2485 { return 13 }
    make n = 1
    if read(n.pin) != 2 { return 5 }
    if after_write(n.pin) != 10 { return 6 }
    if after_call(n.pin) != 19 { return 7 }
    n = 1
    if general_read(n.pin) != 'y' { return 8 }
    if pair_value(2) != 5 { return 9 }
    @unsafe make address: ptr<int> = n.pin
    make optional: ptr<int>? = address
    make copied: ptr<int>? = optional
    if copied == none || optional == none { return 12 }
    make p = pair(4)
    p = flip(p.pin)
    if p.left != 5 || p.right != 4 { return 10 }
    if choose(true) != "a" || choose(false) != "b" { return 11 }
    return 0
}
'''.replace('RUNTIME', str(ROOT / 'library/runtime/pkg.quip')).replace('make identity', many + 'make identity'))
    base = [str(COMPILER), 'compile', str(source), '-o', str(work / 'program'),
            '--no-progress', '--hide-warnings']

    def compile(*flags):
        result = subprocess.run([*base, *flags], cwd=work, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    raw = compile('--emit-llvm-raw')
    assert 'mul ' not in function(raw, 'dead_'), function(raw, 'dead_')
    assert 'sdiv ' in function(raw, 'keep_division_'), function(raw, 'keep_division_')
    constant = function(raw, 'constant_branch_')
    assert 'br ' not in constant and 'alloca' not in constant and 'add ' not in constant, constant
    for name in ['loop_', 'join_']:
        body = function(raw, name)
        assert 'phi ' in body and not re.search(r'\b(alloca|load|store)\b', body), body
    assert 'br ' not in function(raw, 'chain_'), function(raw, 'chain_')
    pointer = function(raw, 'read_')
    assert pointer.count('load ') == 1 and 'alloca' not in pointer, pointer
    aggregate = function(raw, 'pair_')
    assert not any(op in aggregate for op in ['alloca', '@llvm.memset', '@llvm.memmove']), aggregate
    assert function(raw, 'pair_value_').count('alloca') == 1, function(raw, 'pair_value_')
    assert '@llvm.memmove' not in function(raw, 'pair_value_'), function(raw, 'pair_value_')
    assert function(raw, 'choose_').count('@__quip_str_drop') <= 2, function(raw, 'choose_')

    for optimization in [[], ['-O2'], ['-O3'], ['--release']]:
        compile(*optimization)
        result = subprocess.run([str(work / 'program')], cwd=work, capture_output=True, text=True)
        assert result.returncode == 0, (optimization, result.returncode, result.stdout, result.stderr)
    (work / 'program').unlink()
    optimized = compile('--emit-llvm-optimized', '-O2')
    assert 'target datalayout' in optimized and 'define ' in optimized, optimized
    assert not (work / 'program').exists(), 'optimized dump unexpectedly linked a program'

    combined = compile('--emit-mir', '--emit-llvm-optimized', '-O2')
    assert 'fn main(' in combined and 'target datalayout' in combined, combined
    raw_again = compile('--emit-llvm-optimized', '--emit-llvm-raw')
    assert 'target datalayout' not in raw_again, raw_again

    # Diagnostics must still inspect the original MIR before unreachable pruning.
    source.write_text('''@pub @entry
make main () -> int {
    make n: int
    if false { return n }
    return 0
}
''')
    rejected = subprocess.run([*base, '--emit-llvm'], cwd=work, capture_output=True, text=True)
    assert rejected.returncode != 0, 'optimization hid an uninitialized read'
print('MIR optimization checks passed')
