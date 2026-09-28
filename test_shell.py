"""Tests for shell.py.

    python3 -m unittest -v

The parser is tested directly; everything else runs the real shell as a
child process and feeds it commands on stdin, the same way a script would.
"""
import os
import subprocess
import sys
import tempfile
import unittest

import shell

HERE = os.path.dirname(os.path.abspath(__file__))
SHELL = os.path.join(HERE, "shell.py")


def run(script, cwd=None, env=None):
    """Run script through the shell and return (stdout, stderr, exit code)."""
    proc = subprocess.run(
        [sys.executable, SHELL], input=script, capture_output=True,
        text=True, cwd=cwd, env=env, timeout=20,
    )
    return proc.stdout, proc.stderr, proc.returncode


class ParseTests(unittest.TestCase):
    def test_simple_command(self):
        pipeline, bg = shell.parse("ls -l /tmp")
        self.assertEqual([c.argv for c in pipeline], [["ls", "-l", "/tmp"]])
        self.assertFalse(bg)

    def test_empty_and_comment_lines(self):
        self.assertEqual(shell.parse("   "), ([], False))
        self.assertEqual(shell.parse("# just a comment"), ([], False))

    def test_quotes_keep_spaces(self):
        pipeline, _ = shell.parse("echo 'hello   world' \"a b\"")
        self.assertEqual(pipeline[0].argv, ["echo", "hello   world", "a b"])

    def test_pipeline_and_redirects_without_spaces(self):
        pipeline, bg = shell.parse("sort<in.txt|uniq -c>>out.txt &")
        self.assertTrue(bg)
        self.assertEqual(len(pipeline), 2)
        self.assertEqual(pipeline[0].argv, ["sort"])
        self.assertEqual(pipeline[0].stdin, "in.txt")
        self.assertEqual(pipeline[1].argv, ["uniq", "-c"])
        self.assertEqual(pipeline[1].stdout, "out.txt")
        self.assertTrue(pipeline[1].append)

    def test_variables_expand(self):
        os.environ["SHELL_TEST_VAR"] = "value"
        pipeline, _ = shell.parse("echo $SHELL_TEST_VAR")
        self.assertEqual(pipeline[0].argv, ["echo", "value"])

    def test_syntax_errors(self):
        for bad in ["| wc", "ls |", "ls >", "ls > | wc", "&", "ls & wc", "echo 'open"]:
            with self.subTest(line=bad):
                with self.assertRaises(shell.ParseError):
                    shell.parse(bad)


class ShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_runs_program_from_path(self):
        out, _, code = run("echo hello\n")
        self.assertEqual(out, "hello\n")
        self.assertEqual(code, 0)

    def test_runs_program_by_path(self):
        out, _, _ = run("/bin/echo direct\n")
        self.assertEqual(out, "direct\n")

    def test_command_not_found(self):
        out, err, _ = run("definitely-not-a-command-xyz\n")
        self.assertIn("definitely-not-a-command-xyz: command not found", err)
        self.assertIn("Program terminated: exit code 127.", out)

    def test_nonzero_exit_is_reported(self):
        out, _, _ = run("false\n")
        self.assertIn("Program terminated: exit code 1.", out)

    def test_output_redirect_and_append(self):
        run("echo one > out.txt\necho two >> out.txt\n", cwd=self.dir)
        with open(os.path.join(self.dir, "out.txt")) as f:
            self.assertEqual(f.read(), "one\ntwo\n")
        run("echo fresh > out.txt\n", cwd=self.dir)
        with open(os.path.join(self.dir, "out.txt")) as f:
            self.assertEqual(f.read(), "fresh\n")

    def test_input_redirect(self):
        with open(os.path.join(self.dir, "in.txt"), "w") as f:
            f.write("banana\napple\ncherry\n")
        out, _, _ = run("sort < in.txt\n", cwd=self.dir)
        self.assertEqual(out, "apple\nbanana\ncherry\n")

    def test_missing_input_file(self):
        out, err, _ = run("cat < nope.txt\n", cwd=self.dir)
        self.assertIn("nope.txt: No such file or directory", err)
        self.assertIn("exit code 1", out)

    def test_pipeline(self):
        out, _, _ = run("printf 'b\\na\\nb\\nc\\n' | sort | uniq | wc -l\n")
        self.assertEqual(out.strip(), "3")

    def test_pipeline_with_redirects_on_both_ends(self):
        with open(os.path.join(self.dir, "words.txt"), "w") as f:
            f.write("dog\ncat\ndog\n")
        run("sort < words.txt | uniq -c > counts.txt\n", cwd=self.dir)
        with open(os.path.join(self.dir, "counts.txt")) as f:
            lines = [line.split() for line in f]
        self.assertEqual(lines, [["1", "cat"], ["2", "dog"]])

    def test_cd_changes_directory(self):
        out, _, _ = run(f"cd {self.dir}\npwd\n")
        self.assertEqual(os.path.realpath(out.strip()), os.path.realpath(self.dir))

    def test_cd_without_argument_goes_home(self):
        env = dict(os.environ, HOME=self.dir)
        out, _, _ = run("cd /\ncd\npwd\n", env=env)
        self.assertEqual(os.path.realpath(out.strip()), os.path.realpath(self.dir))

    def test_cd_to_missing_directory(self):
        _, err, _ = run("cd /no/such/dir\n")
        self.assertIn("cd: /no/such/dir: No such file or directory", err)

    def test_exit_with_code(self):
        out, _, code = run("exit 7\necho never\n")
        self.assertEqual(code, 7)
        self.assertNotIn("never", out)

    def test_background_job_does_not_block(self):
        out, _, _ = run("sleep 1 &\necho right away\n")
        self.assertRegex(out, r"^\[\d+\]\n")
        self.assertIn("right away", out)

    def test_background_job_completion_is_reported(self):
        out, _, _ = run("true &\nsleep 0.3\necho next\n")
        self.assertRegex(out, r"\[\d+\] done \(exit 0\): true")

    def test_script_file_argument(self):
        script = os.path.join(self.dir, "script.sh")
        with open(script, "w") as f:
            f.write("echo from script\n")
        proc = subprocess.run([sys.executable, SHELL, script],
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.stdout, "from script\n")


if __name__ == "__main__":
    unittest.main()
