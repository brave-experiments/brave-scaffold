# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Console detail must not change execution or lose diagnostic evidence."""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

import tests.support
from scaffold.common import cli, config, procs
from scaffold.brave.app import run_command
from scaffold.common.results import Result, ScaffoldError


class DurationTests(unittest.TestCase):
    def test_short_long_and_rounded_boundary_durations(self):
        cases = ((0, "0.00s"), (0.31, "0.31s"), (191.29, "3m 11.29s"),
                 (59.999, "1m 0.00s"), (3599.999, "1h 0m 0.00s"),
                 (3723.45, "1h 2m 3.45s"), (90000, "25h 0m 0.00s"))
        for seconds, expected in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(procs.format_duration(seconds), expected)

    def test_phase_timings_use_readable_durations(self):
        console = io.StringIO()
        log = procs.CommandLog(stream=console, verbosity="normal")
        log.timings = [("Package build", 191.29)]
        log.report_timings(252.29)
        self.assertIn("Package build 3m 11.29s; Other 1m 1.00s", console.getvalue())


class VerbosityTests(unittest.TestCase):
    def invoke(self, level, code, use_config=False, **environment):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        path = root / 'config.toml'
        path.write_text('schema_version = 1\n[logging]\nverbosity = "%s"\n' % (level if use_config else 'normal'))
        parsed = cli.Parsed(values={'config': str(path), **({} if use_config else {'verbosity': level})})
        out, err = io.StringIO(), io.StringIO()
        def handler(ctx):
            procs.run_capture([sys.executable, '-c', "import os; print(os.environ['PROBE_PAYLOAD'])"],
                              root, dict(os.environ, PROBE_PAYLOAD="private probe payload"), ctx.log)
            rc = procs.run_streaming([sys.executable, '-c', code], root,
                                    dict(os.environ, **environment), ctx.log)
            return Result(command='example', exit_code=rc, text='Finished')
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = run_command('example', parsed, handler, stdout=out, stderr=err)
        files = list((root / '.bdev' / 'logs').glob('*.log'))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        return rc, out.getvalue(), err.getvalue(), files[0].read_text()

    def test_normal_shows_child_but_hides_probes_and_saves_commands(self):
        rc, out, err, saved = self.invoke('normal', "print('build' + ' output')")
        self.assertEqual(rc, 0)
        self.assertIn('build output', out)
        self.assertNotIn('private probe payload', err)
        self.assertIn('PROBE_PAYLOAD', saved)
        self.assertNotIn('private probe payload', saved)
        self.assertIn('build output', saved)
        self.assertIn('Log:', err)

    def test_verbose_shows_probes(self):
        _, _, err, _ = self.invoke('verbose', "print('build' + ' output')")
        self.assertIn('PROBE_PAYLOAD', err)

    def test_quiet_suppresses_success_but_replays_failure_tail(self):
        _, out, err, saved = self.invoke('quiet', "print('child' + ' output')")
        self.assertNotIn('child output', out + err)
        self.assertIn('child output', saved)
        rc, out, err, saved = self.invoke('quiet', "print('failure' + ' detail'); raise SystemExit(7)")
        self.assertEqual(rc, 7)
        self.assertIn('failure detail', err)
        self.assertNotIn('failure detail', out)

    def test_child_secrets_are_redacted_even_across_writes(self):
        code = "import os; os.write(1,b'abc'); os.write(1,b'def\\nhttps://u:pw@example.com\\n')"
        _, out, err, saved = self.invoke('normal', code, ACCESS_TOKEN='abcdef')
        self.assertNotIn('abcdef', out + err + saved)
        self.assertNotIn('u:pw@', out + saved)
        self.assertIn('***', out)

    def test_verbosity_options_conflict_and_forwarding(self):
        spec = cli.CommandSpec('build', 'Build', forward=True)
        with self.assertRaises(ScaffoldError):
            cli.parse_tokens(spec, ['--quiet', '--verbose'])
        parsed = cli.parse_tokens(spec, ['--verbose', '--', '--quiet'])
        self.assertEqual(parsed.get('verbosity'), 'verbose')
        self.assertEqual(parsed.forwarded, ['--quiet'])
        parsed = cli.parse_leading(spec, ['--quiet', 'run', '--verbose'])
        self.assertEqual(parsed.forwarded, ['run', '--verbose'])

    def test_config_accepts_levels_and_rejects_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            for level in ('quiet', 'normal', 'verbose'):
                path.write_text('schema_version = 1\n[logging]\nverbosity = "%s"\n' % level)
                self.assertEqual(config.load_config(path).verbosity, level)
            path.write_text('schema_version = 1\n[logging]\nverbosity = "loud"\n')
            with self.assertRaises(ScaffoldError):
                config.load_config(path)

    def test_direct_stdout_keeps_binary_bytes_and_partial_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = io.BytesIO()
            stdout = io.TextIOWrapper(raw, encoding="utf-8")
            log = procs.CommandLog(enabled=False, stream=io.StringIO(), verbosity="normal")
            log.open(directory)
            with contextlib.redirect_stdout(stdout):
                rc = procs.run_streaming([sys.executable, '-c', "import os; os.write(1, b'\\xffprompt')"],
                                         directory, os.environ, log, preserve_stdout=True)
            self.assertEqual(rc, 0)
            self.assertEqual(raw.getvalue(), b'\xffprompt')
            log.close()

    def test_json_child_output_never_enters_stdout(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text('schema_version = 1\n[logging]\nverbosity = "quiet"\n')
            parsed = cli.Parsed(values={'config': str(path), 'json': True, 'verbosity': 'normal'})
            out, err = io.StringIO(), io.StringIO()
            def handler(ctx):
                procs.run_streaming([sys.executable, '-c', "print('child-text')"], directory,
                                    os.environ, ctx.log, json_mode=ctx.json_mode)
                return Result(command='example')
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(run_command('example', parsed, handler, stdout=out, stderr=err), 0)
            self.assertEqual(json.loads(out.getvalue())['status'], 'ok')
            self.assertIn('child-text', err.getvalue())

    def test_bytecode_blocks_keep_other_actions_streams_and_unterminated_warnings(self):
        from scaffold.brave.packages import BytecodeOutput
        console = io.StringIO()
        log = procs.CommandLog(verbosity="normal", stream=console)
        output = procs._StreamOutput(log, [], {"API_TOKEN": "private-token"}, "stdout", True,
                                     verbose_output=BytecodeOutput())
        output.receive("stdout", b"[1/4] F ACTION //chrome:empty__bytecode_rewrite(//toolchain)\nstdout:\n")
        output.receive("stderr", b"independent diagnostic\n")
        output.receive("stdout", b"[2/4] F ACTION //chrome:other(//toolchain)\nstdout:\nother output\n")
        output.receive("stdout", b"[3/4] F ACTION //chrome:warning__bytecode_rewrite(//toolchain)\nstdout:\n")
        output.receive("stdout", b"redirecting constructor from upstream/Class to brave/Class\n")
        output.receive("stdout", b"WARNING: private-")
        output.receive("stdout", b"token")
        output.finish()
        self.assertEqual(console.getvalue(), "independent diagnostic\n"
                         "[2/4] F ACTION //chrome:other(//toolchain)\nstdout:\nother output\n"
                         "[3/4] F ACTION //chrome:warning__bytecode_rewrite(//toolchain)\nstdout:\n"
                         "WARNING: ***")

    def test_split_secrets_and_long_lines_never_save_partial_values(self):
        with tempfile.TemporaryDirectory() as directory:
            log = procs.CommandLog(verbosity='quiet', stream=io.StringIO())
            log.open(directory)
            header_size = len(Path(log.path).read_text())
            output = procs._StreamOutput(log, [], {'API_TOKEN': 'sensitive-value'}, 'stdout', False)
            output.receive('stdout', b'sensitive-')
            self.assertNotIn('sensitive-', Path(log.path).read_text())
            output.receive('stdout', b'value\nhttps://user:pass')
            output.receive('stdout', b'word@host/path\n')
            output.receive('stdout', b'x' * 1_048_577)
            output.receive('stdout', b'end\nlast line')
            output.finish()
            log.close()
            saved = Path(log.path).read_text()
            self.assertNotIn('sensitive', saved)
            self.assertNotIn('password', saved)
            self.assertIn('omitted', saved)
            self.assertTrue(saved.endswith('last line'))
            self.assertLess(len(saved) - header_size, 200)

    def test_metal_reuses_only_the_latest_execution_check(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from scaffold.brave.cmd_build import metal_environment
        from scaffold.common.checks import make_check
        good = make_check('metal-toolchain', 'pass', 'Ready', 'mac', xcrun_works=True)
        bad = make_check('metal-toolchain', 'warning', 'Missing', 'mac')
        context = SimpleNamespace(log=procs.CommandLog(enabled=False))
        with patch('scaffold.brave.cmd_build.run_capture', return_value=procs.ProcessResult(0)) as probe:
            self.assertEqual(metal_environment(context, {}, [good]), {})
            probe.assert_not_called()
            self.assertEqual(metal_environment(context, {}, [good, bad]), {})
            self.assertEqual(probe.call_count, 1)

    def test_progress_updates_do_not_flood_redirected_output(self):
        from unittest.mock import patch
        stream = io.StringIO()
        log = procs.CommandLog(stream=stream, verbosity='normal')
        with patch.object(procs.time, 'monotonic', side_effect=[100, 101, 109, 110]):
            for i in range(4):
                log.progress('dependency %d' % i)
        self.assertEqual(stream.getvalue().splitlines(), ['dependency 0', 'dependency 3'])

    def test_file_alone_selects_each_console_level(self):
        for level in ('quiet', 'normal', 'verbose'):
            with self.subTest(level=level):
                rc, out, err, saved = self.invoke(level, "print('from' + ' child')", use_config=True)
                self.assertEqual(rc, 0)
                self.assertEqual('from child' in out, level != 'quiet')
                self.assertEqual('PROBE_PAYLOAD' in err, level == 'verbose')
                self.assertIn('from child', saved)

    def test_phase_times_survive_failure_and_quiet_keeps_them_in_log(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            console = io.StringIO()
            log = procs.CommandLog(stream=console, verbosity='quiet')
            log.open(directory)
            try:
                with patch.object(procs.time, 'monotonic', side_effect=[1.0, 3.5]):
                    with self.assertRaises(ValueError):
                        with log.measure('Source state'):
                            raise ValueError('failed')
                log.report_timings(4.0)
                self.assertEqual(console.getvalue(), '')
                self.assertIn('Source state 2.50s; Other 1.50s', Path(log.path).read_text())
            finally:
                log.close()
