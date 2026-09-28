# Shell

A small Unix shell written from scratch in Python, using POSIX system calls directly: `fork`, `execve`, `pipe`, `dup2`, `waitpid`, `open`, `read` and `write` from the `os` module. It doesn't use `subprocess`, `input()`, `print()` or Python file objects, so every step of running a command is out in the open in [`shell.py`](shell.py).

```text
$ ls | grep py | wc -l
       2
$ sort < names.txt | uniq -c > counts.txt
$ sleep 30 &
[48213]
$ cd ~/projects
$ nosuchcommand
nosuchcommand: command not found
Program terminated: exit code 127.
```

## Run it

Needs Python 3.9 or newer on macOS or Linux (`fork` doesn't exist on Windows; use WSL there). No packages to install.

```bash
python3 shell.py              # interactive
python3 shell.py script.sh    # run the commands in a file
PS1="player1> " python3 shell.py
```

Leave with `exit` or Ctrl-D.

## What it supports

| Feature | Example | Notes |
|---|---|---|
| Programs on `$PATH` | `ls -la` | Searches each `$PATH` directory, like a real shell. |
| Programs by path | `./a.out`, `/bin/echo hi` | |
| Output redirect | `ls > files.txt`, `date >> log.txt` | `>` overwrites, `>>` appends. |
| Input redirect | `sort < names.txt` | |
| Pipelines | `cat log \| grep ERR \| wc -l` | Any number of stages. Redirects work on the ends. |
| Background jobs | `sleep 30 &` | Prints the pid, then reports `[pid] done` at a later prompt. |
| Quotes | `echo 'a  b' "c d"` | Operators need no spaces: `ls>out` works. |
| Variables and `~` | `echo $HOME`, `cd ~/src` | |
| Comments | `ls  # list files` | |
| Built-ins | `cd [dir]`, `exit [code]`, `jobs` | `cd` with no argument goes to `$HOME`. |
| Prompt | `PS1="> "` | Taken from the `PS1` environment variable; default `$ `. |

When a program fails, the shell prints `Program terminated: exit code N.` Ctrl-C stops the running program but not the shell. At the prompt, Ctrl-C just clears the line.

## How it works

1. **Read.** `LineReader` pulls bytes from stdin with `os.read` and splits them into lines itself, because `input()` isn't allowed.
2. **Parse.** `shlex` splits the line into words and the operators `| < > >> &`. `parse()` turns them into a list of `Command`s (one per pipeline stage), each with its own `argv` and redirect targets.
3. **Built-ins.** `cd` and `exit` must run inside the shell process itself, because a child process changing directory or exiting wouldn't affect the shell.
4. **Launch.** For an N-stage pipeline, the shell makes N-1 pipes, then forks once per stage. Each child `dup2`s its pipe ends and redirect files onto fd 0 and 1, closes every other pipe end, and `execve`s the program. The parent closes all its pipe ends too. Otherwise a reader like `wc` would never see end-of-file and would hang forever.
5. **Wait.** Foreground pipelines are `waitpid`ed. Background ones are recorded and reaped with `WNOHANG` before each prompt.

## Tests

```bash
python3 -m unittest -v
```

The parser is tested directly. Everything else runs the real shell as a child process and feeds it commands on stdin: pipes, redirects, `cd`, exit codes, background jobs and scripts.
