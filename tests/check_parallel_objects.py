#!/usr/bin/env python3
"""Compare serial and parallel object generation and exercise worker failure."""
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPILER = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / 'target/debug/quipc'

with tempfile.TemporaryDirectory(prefix='quip-parallel-') as directory:
    work = pathlib.Path(directory)
    (work / 'left.quip').write_text('@pub\nmake left () -> int { return 19 }\n')
    (work / 'right.quip').write_text('@pub\nmake right () -> int { return 23 }\n')
    source = work / 'main.quip'
    source.write_text('''@pure @const {
    make left = @import "left.quip"
    make right = @import "right.quip"
}
@pub @entry
make main () -> int { return left::left() + right::right() }
''')
    output = work / 'program'
    intermediates = work / 'target/debug/intermediate/targets' / str(output).lstrip('/')

    def compile(jobs, *options):
        return subprocess.run([str(COMPILER), 'compile', str(source), '-o', str(output),
                               '--no-progress', '--hide-warnings', '--jobs', str(jobs), *options], cwd=work,
                              text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    for optimization in [[], ['-O2']]:
        serial = compile(1, '--keep-llvm', *optimization)
        assert serial.returncode == 0, serial.stdout
        objects = {p.name: p.read_bytes() for p in (intermediates / 'objects').glob('*.o')}
        ir = {p.name: p.read_bytes() for p in (intermediates / '.llvm').glob('*.ll')}
        assert len(objects) == 3, objects.keys()
        for jobs in [2, 4, 256]:
            parallel = compile(jobs, '--keep-llvm', *optimization)
            assert parallel.returncode == 0, parallel.stdout
            assert objects == {p.name: p.read_bytes() for p in (intermediates / 'objects').glob('*.o')}
            assert ir == {p.name: p.read_bytes() for p in (intermediates / '.llvm').glob('*.ll')}
            assert subprocess.run([str(output)]).returncode == 42

    benchmark = compile(2, '--benchmark')
    assert benchmark.returncode == 0 and 'llvm_wait' in benchmark.stdout, benchmark.stdout
    serial_dump = compile(1, '--emit-llvm')
    dump = compile(2, '--emit-llvm')
    assert serial_dump.returncode == dump.returncode == 0, dump.stdout
    assert 'define ' in dump.stdout and serial_dump.stdout == dump.stdout, dump.stdout

    # An emission error must prevent linking, even with a stale object available.
    output.unlink()
    blocked = intermediates / 'objects/unit_0.o'
    blocked.unlink()
    blocked.mkdir()
    failed = compile(2)
    assert failed.returncode != 0 and 'LLVM object worker failed for' in failed.stdout, failed.stdout
    assert not output.exists(), 'linked after worker failure'
    blocked.rmdir()
    recovered = compile(2)
    assert recovered.returncode == 0, recovered.stdout
    assert subprocess.run([str(output)]).returncode == 42

    # Dependencies can finish codegen even if analysis later rejects the entry.
    valid = source.read_text()
    source.write_text(valid.replace('right::right()', 'missing_function()'))
    output.unlink()
    for obj in (intermediates / 'objects').glob('*.o'):
        obj.unlink()
    rejected = compile(2)
    assert rejected.returncode != 0 and not output.exists(), rejected.stdout
    assert any((intermediates / 'objects').glob('*.o')), 'dependencies were never dispatched'
    source.write_text(valid)

    # Exercise build.quip -> Build options -> compiler subprocess -> worker pool.
    for jobs in [1, 2]:
        (work / 'build.quip').write_text('''@pure @const { make std = @import "std" }
@pub @entry
make main (argc: int, argv: ptr<char>[]) -> int {
    _ = argc
    make b = std::Build.from_args(argv) catch { return 1 }
    b.options.jobs = JOBS
    b.options.benchmark = true
    make exe = std::Executable.default()
    exe.name = "configured"
    exe.entry_file = std::io::Path.create("main.quip")
    b.add_executable(exe)
    b.proceed() catch { return 1 }
    return 0
}
'''.replace('JOBS', str(jobs)))
        built = subprocess.run([str(COMPILER), 'build', '--no-progress', '--hide-warnings',
                                '--alias=("std","' + str(ROOT / 'library/std/pkg.quip') + '")'],
                               cwd=work, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert built.returncode == 0, built.stdout
        assert ('llvm_wait' in built.stdout) == (jobs > 1), built.stdout
        assert subprocess.run([str(work / 'target/debug/configured')]).returncode == 42

    # An old driver passes the build protocol without --jobs. Its compiler
    # must never receive that flag, even when build.quip requests concurrency.
    runner = work / 'build-runner'
    built_runner = subprocess.run([str(COMPILER), 'compile', str(work / 'build.quip'),
                                   '-o', str(runner), '--no-progress', '--hide-warnings',
                                   '--alias=("std","' + str(ROOT / 'library/std/pkg.quip') + '")'],
                                  cwd=work, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert built_runner.returncode == 0, built_runner.stdout
    fake = work / 'old-compiler'
    fake.write_text('#!/bin/sh\nfor arg in "$@"; do\n'
                    '  if [ "$arg" = "--jobs" ]; then exit 99; fi\ndone\nexit 0\n')
    fake.chmod(0o755)
    legacy = subprocess.run([str(runner), '--quip-build-args', str(fake), '',
                             '--no-progress', '--'], cwd=work, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert legacy.returncode == 0, legacy.stdout

    for invalid in ['0', '257', '-1', 'abc', '999999999999999999999999']:
        result = compile(invalid)
        assert result.returncode != 0 and '--jobs requires' in result.stdout, result.stdout

print('Parallel object generation checks passed')
