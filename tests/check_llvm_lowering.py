#!/usr/bin/env python3
"""Exercise direct scalar lowering and inspect unoptimized LLVM IR."""
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPILER = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / 'target/debug/quipc'


def function(ir, name):
    match = re.search(r'define [^\n]*@' + name + r'[^\n]*\{\n(.*?)\n}', ir, re.S)
    assert match, (name, ir)
    return match[1]


with tempfile.TemporaryDirectory(prefix='quip-lowering-') as directory:
    work = pathlib.Path(directory)
    (work / 'dep.quip').write_text('''@pub
make twice (n: int) -> int { return n * 2 }
@pub
make nothing () { return }
''')
    cases = {
        'scalar': ('''@pure @const { make dep = @import "dep.quip" }
make fib (n: int) -> int {
    if n < 2 { return n }
    return fib(n - 1) + fib(n - 2)
}
make imported (n: int) -> int { dep::nothing() return dep::twice(n) }
make boolean_copy (a: int, b: int) -> int {
    make value = a < b
    make copy = value
    if !copy { return 0 }
    return 1
}
make general (n: int) -> char {
    make value = n < 0
    make copied = value
    if !copied { return 'x' }
    return 'y'
}
make floating (n: float) -> int { if n < 2.5 { return 1 } return 0 }
@pub @entry
make main () -> int {
    if fib(10) != 55 { return 1 }
    if imported(21) != 42 { return 2 }
    if floating(1.5) != 1 { return 3 }
    if boolean_copy(1, 2) != 1 || boolean_copy(2, 1) != 0 { return 4 }
    return 0
}
''', 0),
        'mutable': ('''make loop (limit: int) -> int {
    make total = 0
    make i = 0
    while i < limit {
        if i % 2 == 0 { total += i } else { total += 1 }
        i++
    }
    return total
}
make mutate (value: ref<int>) { value.* = 19 }
make alias () -> int {
    make value = 7
    mutate(value.pin)
    return value
}
@pub @entry
make main () -> int {
    if loop(10) != 25 { return 1 }
    if alias() != 19 { return 2 }
    return 0
}
''', 0),
        'strings': ('''make equal (a: str, b: str) -> bool { return a == b }
make unequal (a: str, b: str) -> bool { return a != b }
@pub @entry
make main () -> int {
    make value = ""
    make i = 0
    while i < 4 { value = value + "x" i++ }
    if value != "xxxx" { return 1 }
    if value == "xxx" { return 2 }
    if !equal("", "") { return 3 }
    if equal("abcd", "abce") { return 4 }
    if !unequal("x", "xx") { return 5 }
    return 0
}
''', 0),
        'native': ('''@unsafe @external
make abs (n: i32) -> i32
@pub @entry
make main () -> int { @unsafe return abs(-42) - 42 }
''', 0),
        'raw': ('''make write (value: ptr<u8>) -> char {
    make i = 0
    while i < 4 { value.* = 42 i++ }
    return 'x'
}
@pub @entry
make main () -> int {
    make value: u8 = 0
    @unsafe make address: ptr<u8> = value.pin
    write(address)
    @unsafe return value - 42
}
''', 0),
    }

    for name, (source, expected) in cases.items():
        path = work / (name + '.quip')
        if name == 'strings':
            source = '@import "' + str(ROOT / 'library/runtime/pkg.quip') + '"\n' + source
        path.write_text(source)
        output = work / name
        command = [str(COMPILER), 'compile', str(path), '-o', str(output), '--no-progress', '--hide-warnings']
        emitted = subprocess.run([*command, '--emit-llvm'], cwd=work, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert emitted.returncode == 0, (name, emitted.stdout)
        ir = emitted.stdout
        if name == 'scalar':
            for symbol in ['fib_', 'imported_']:
                body = function(ir, symbol)
                assert not re.search(r'\b(alloca|load|store)\b', body), body
                assert 'br label %bb0' not in body, body
            fib = function(ir, 'fib_')
            assert 'icmp slt' in fib and 'br i1' in fib and 'zext i1' not in fib, fib
            assert 'zext i1' not in function(ir, 'floating_'), ir
            assert 'zext i1' not in function(ir, 'boolean_copy_'), ir
            general = function(ir, 'general_')
            assert general.count('alloca') == 1 and 'alloca [24 x i8]' not in general, general
            assert 'zext i1' not in general and 'br label %bb0' not in general, general
            assert '__quip_string_equal' not in ir, ir
        if name == 'mutable':
            assert 'alloca' in function(ir, 'alias_'), ir
            assert '__quip_string_equal' not in ir, ir
        if name == 'strings':
            assert ir.count('define private i64 @__quip_string_equal') == 1, ir
            for body in re.findall(r'define [^\n]*\{\n(.*?)\n}', ir, re.S):
                # All string scratch allocations must stay in entry, even in loops.
                entry, *blocks = re.split(r'\n\w+:[^\n]*\n', body)
                assert not any('alloca [24 x i8]' in block for block in blocks), body
        if name == 'native':
            assert function(ir, '__quip_user_main').count('alloca') == 1, ir
        if name == 'raw':
            assert '__quip_string_equal' not in ir, ir
            raw = function(ir, 'write_')
            assert 'alloca [24 x i8]' not in raw, raw
            assert not any('alloca' in block for block in re.split(r'\n\w+:[^\n]*\n', raw)[1:]), raw
        for optimization in [[], ['-O2']]:
            compiled = subprocess.run([*command, *optimization], cwd=work, text=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            assert compiled.returncode == 0, (name, compiled.stdout)
            ran = subprocess.run([str(output)], capture_output=True, text=True)
            assert ran.returncode == expected, (name, optimization, ran.returncode, ran.stdout, ran.stderr)
        print(name + ': passed')
    # MIR imports the compiler's unit manager, so this planner harness links
    # the same LLVM archives and system libraries as the compiler build.
    llvm_config = os.environ.get('LLVM_CONFIG') or shutil.which('llvm-config')
    if not llvm_config:
        prefix = subprocess.check_output(['brew', '--prefix'], text=True).strip()
        llvm_config = str(pathlib.Path(prefix) / 'opt/llvm@22/bin/llvm-config')
    libdir = pathlib.Path(subprocess.check_output([llvm_config, '--libdir'], text=True).strip())
    archives = subprocess.check_output([llvm_config, '--link-static', '--libnames',
                                       'core', 'passes', 'nativecodegen', 'aarch64', 'x86'], text=True).split()
    links = [str(libdir / name) for name in archives]
    prefix = subprocess.check_output(['brew', '--prefix'], text=True).strip()
    sdk = pathlib.Path(subprocess.check_output(['xcrun', '--sdk', 'macosx', '--show-sdk-path'], text=True).strip())
    links += [str(pathlib.Path(prefix) / 'opt/zstd/lib/libzstd.a')]
    links += [str(sdk / 'usr/lib' / name) for name in ['libc++.tbd', 'libz.tbd', 'libxml2.tbd']]
    command = [str(COMPILER), 'compile', str(ROOT / 'tests/llvm_plans.quip'),
               '-o', str(work / 'plans'), '--no-progress', '--hide-warnings',
               '--alias=("std","' + str(ROOT / 'library/std/pkg.quip') + '")']
    for link in links:
        command += ['--link', link]
    compiled = subprocess.run(command, cwd=work, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert compiled.returncode == 0, compiled.stdout
    planned = subprocess.run([str(work / 'plans')])
    assert planned.returncode == 0, ('planner exit', planned.returncode)
    print('MIR dominance and CFG plans: passed')
print('LLVM lowering checks passed')
