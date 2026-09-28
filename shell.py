#!/usr/bin/env python3
"""A small Unix shell built directly on POSIX system calls.

All process control and I/O go through the os module (fork, execve, pipe,
dup2, waitpid, read, write). Nothing uses subprocess, input(), print() or
Python file objects, so every step of running a command is visible here.

Supports:
    ls -l                   run a program found on $PATH (or by path)
    ls > out.txt            redirect output (>> appends)
    sort < in.txt           redirect input
    ls | grep py | wc -l    pipelines of any length
    sleep 5 &               run in the background
    cd DIR, exit [N], jobs  built-ins
    $PS1                    custom prompt (default "$ ")
"""
import os
import shlex
import signal
import sys

OPERATORS = {"|", "<", ">", ">>", "&"}


# ---------------------------------------------------------------- I/O helpers

def write(fd, text):
    """Write all of text to fd; os.write may write fewer bytes than asked."""
    data = text.encode()
    while data:
        data = data[os.write(fd, data):]


def error(message):
    write(2, message + "\n")


class LineReader:
    """Read lines from a file descriptor using os.read only."""

    def __init__(self, fd=0):
        self.fd = fd
        self.buffer = b""

    def readline(self):
        """Return the next line without its newline, or None at end of input."""
        while b"\n" not in self.buffer:
            chunk = os.read(self.fd, 4096)
            if not chunk:
                if self.buffer:
                    line, self.buffer = self.buffer, b""
                    return line.decode(errors="replace")
                return None
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return line.decode(errors="replace")


# -------------------------------------------------------------------- parsing

class ParseError(Exception):
    pass


class Command:
    """One program in a pipeline, with its own redirections."""

    def __init__(self):
        self.argv = []
        self.stdin = None       # path for <
        self.stdout = None      # path for > or >>
        self.append = False     # True for >>

    def __repr__(self):
        return f"Command({self.argv!r}, stdin={self.stdin!r}, stdout={self.stdout!r})"


def tokenize(line):
    """Split a line into words and operators, honoring quotes.

    Operators need no surrounding spaces: "ls>out|wc" works.
    """
    lexer = shlex.shlex(line, posix=True, punctuation_chars="|<>&")
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        return list(lexer)
    except ValueError as exc:            # unbalanced quotes
        raise ParseError(str(exc)) from None


def expand(word):
    """Expand ~ and $VARS in a word."""
    return os.path.expandvars(os.path.expanduser(word))


def parse(line):
    """Parse a line into (pipeline, background).

    pipeline is a list of Command objects joined by |. An empty line gives [].
    """
    tokens = tokenize(line)
    if not tokens:
        return [], False

    background = False
    if tokens[-1] == "&":
        background = True
        tokens.pop()
        if not tokens:
            raise ParseError("syntax error near '&'")

    pipeline = [Command()]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        current = pipeline[-1]
        if tok == "|":
            if not current.argv:
                raise ParseError("syntax error near '|'")
            pipeline.append(Command())
        elif tok in ("<", ">", ">>"):
            if i + 1 >= len(tokens) or tokens[i + 1] in OPERATORS:
                raise ParseError(f"syntax error: '{tok}' needs a file name")
            target = expand(tokens[i + 1])
            if tok == "<":
                current.stdin = target
            else:
                current.stdout = target
                current.append = tok == ">>"
            i += 1
        elif tok == "&":
            raise ParseError("'&' is only allowed at the end of a line")
        elif tok in OPERATORS:
            raise ParseError(f"syntax error near '{tok}'")
        else:
            current.argv.append(expand(tok))
        i += 1

    if not pipeline[-1].argv:
        raise ParseError("syntax error: missing command")
    return pipeline, background


# ------------------------------------------------------------------ execution

def find_program(name):
    """Return the path to run for name, searching $PATH when it has no slash."""
    if "/" in name:
        return name if os.access(name, os.X_OK) else None
    for directory in os.environ.get("PATH", "").split(":"):
        candidate = os.path.join(directory or ".", name)
        if os.access(candidate, os.X_OK) and not os.path.isdir(candidate):
            return candidate
    return None


def redirect(path, target_fd, flags):
    """Open path and move it onto target_fd (0 or 1)."""
    fd = os.open(path, flags, 0o644)
    os.dup2(fd, target_fd)
    os.close(fd)


def run_child(command, read_fd, write_fd, pipe_fds):
    """In a forked child: wire up stdin/stdout, then exec. Never returns."""
    # The shell ignores Ctrl-C at the prompt; programs it runs should not.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    try:
        if read_fd is not None:
            os.dup2(read_fd, 0)
        if write_fd is not None:
            os.dup2(write_fd, 1)
        for fd in pipe_fds:              # children must not hold pipe ends open
            os.close(fd)
        if command.stdin:
            redirect(command.stdin, 0, os.O_RDONLY)
        if command.stdout:
            mode = os.O_APPEND if command.append else os.O_TRUNC
            redirect(command.stdout, 1, os.O_WRONLY | os.O_CREAT | mode)
    except OSError as exc:
        error(f"{exc.filename}: {exc.strerror}")
        os._exit(1)

    program = find_program(command.argv[0])
    if program is None:
        error(f"{command.argv[0]}: command not found")
        os._exit(127)
    try:
        os.execve(program, command.argv, os.environ)
    except OSError as exc:
        error(f"{command.argv[0]}: {exc.strerror}")
        os._exit(126)


def launch(pipeline):
    """Fork one child per command, connected by pipes. Return their pids."""
    pipes = [os.pipe() for _ in range(len(pipeline) - 1)]
    all_fds = [fd for pair in pipes for fd in pair]
    pids = []
    for index, command in enumerate(pipeline):
        read_fd = pipes[index - 1][0] if index > 0 else None
        write_fd = pipes[index][1] if index < len(pipes) else None
        pid = os.fork()
        if pid == 0:
            run_child(command, read_fd, write_fd, all_fds)
        pids.append(pid)
    for fd in all_fds:                   # otherwise readers never see EOF
        os.close(fd)
    return pids


def exit_code(status):
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return os.WEXITSTATUS(status)


def wait_for(pids):
    """Wait for every pid; return the exit code of the last one."""
    code = 0
    for pid in pids:
        while True:
            try:
                _, status = os.waitpid(pid, 0)
                break
            except KeyboardInterrupt:    # Ctrl-C goes to the child; keep waiting
                continue
            except ChildProcessError:    # already reaped
                status = 0
                break
        code = exit_code(status)
    return code


# ------------------------------------------------------------------- the shell

class Shell:
    def __init__(self, fd=0):
        self.reader = LineReader(fd)
        self.interactive = os.isatty(fd)
        self.jobs = {}                   # pid -> command text, for & jobs
        self.last_status = 0

    def prompt(self):
        return os.environ.get("PS1", "$ ")

    def reap_background(self):
        """Report background jobs that finished since the last prompt."""
        while self.jobs:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                self.jobs.clear()
                return
            if pid == 0:
                return
            text = self.jobs.pop(pid, None)
            if text is not None:
                write(1, f"[{pid}] done (exit {exit_code(status)}): {text}\n")

    # built-ins return True when they handled the command
    def builtin(self, pipeline):
        if len(pipeline) != 1:
            return False
        argv = pipeline[0].argv
        name = argv[0]
        if name == "exit":
            code = self.last_status
            if len(argv) > 1:
                try:
                    code = int(argv[1])
                except ValueError:
                    error(f"exit: {argv[1]}: numeric argument required")
                    code = 2
            raise SystemExit(code)
        if name == "cd":
            target = argv[1] if len(argv) > 1 else os.environ.get("HOME", "/")
            try:
                os.chdir(target)
                self.last_status = 0
            except OSError as exc:
                error(f"cd: {target}: {exc.strerror}")
                self.last_status = 1
            return True
        if name == "jobs":
            for pid, text in self.jobs.items():
                write(1, f"[{pid}] running: {text}\n")
            self.last_status = 0
            return True
        return False

    def run_line(self, line):
        try:
            pipeline, background = parse(line)
        except ParseError as exc:
            error(f"myshell: {exc}")
            self.last_status = 2
            return
        if not pipeline or self.builtin(pipeline):
            return
        pids = launch(pipeline)
        if background:
            self.jobs[pids[-1]] = line.strip().rstrip("&").strip()
            write(1, f"[{pids[-1]}]\n")
            return
        self.last_status = wait_for(pids)
        if self.last_status == 128 + signal.SIGINT:
            write(1, "\n")               # the terminal left the cursor after ^C
        if self.last_status != 0:
            write(1, f"Program terminated: exit code {self.last_status}.\n")

    def loop(self):
        # Ctrl-C at the prompt clears the line instead of killing the shell.
        signal.signal(signal.SIGINT, signal.default_int_handler)
        while True:
            self.reap_background()
            if self.interactive:
                write(1, self.prompt())
            try:
                line = self.reader.readline()
            except KeyboardInterrupt:
                self.reader.buffer = b""
                write(1, "\n")
                continue
            if line is None:             # Ctrl-D / end of script
                if self.interactive:
                    write(1, "\n")
                return self.last_status
            self.run_line(line)


def main():
    fd = 0
    if len(sys.argv) > 1:                # run a script file: shell.py script.sh
        fd = os.open(sys.argv[1], os.O_RDONLY)
    try:
        code = Shell(fd).loop()
    except SystemExit as exc:
        code = exc.code
    os._exit(code if isinstance(code, int) else 0)


if __name__ == "__main__":
    main()
