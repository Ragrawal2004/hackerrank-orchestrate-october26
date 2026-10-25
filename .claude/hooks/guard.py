#!/usr/bin/env python3
"""PreToolUse guards for this repo. Exit 0 = allow, exit 2 = block (stderr goes to Claude).

    guard.py commit-gate   Bash `git commit` must have a clean stage and green tests.
    guard.py dataset       Write/Edit/MultiEdit/Bash may not write to or delete dataset/.
    guard.py env-guard     Read/Edit/Write/MultiEdit/Bash may not read or edit .env (templates are fine).
    guard.py git-add       `git add .` / `-A` / `--all` are refused; stage files by name.
    guard.py git-destructive  `git push --force/-f`, `git reset --hard`, `git clean -f` are refused.
    guard.py branch-gate   `git commit` is refused while on main or master.

commit-gate blocks when the staged files (names only; contents and .env are never read)
include .env, anything under runs/, .venv/ or __pycache__/, or when
`python3 -m unittest discover -s code/tests -t code` fails.

dataset parses shell commands statically. It is a guardrail, not a sandbox: variable
indirection, `eval`, or a script file that writes to dataset/ cannot be seen from here.
Malformed hook input fails open; a test runner that cannot launch fails closed.
"""
import fnmatch
import json
import os
import re
import shlex
import string
import subprocess
import sys

TEST_CMD = ["python3", "-m", "unittest", "discover", "-s", "code/tests", "-t", "code"]
TEST_TIMEOUT = 1200
CASE_INSENSITIVE_FS = sys.platform in ("darwin", "win32")

# --- shell parsing -----------------------------------------------------------------

OPERATOR_CHARS = set(";()<>|&\n")
HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)(\w+)\1[^\n]*\n(.*?)\n[ \t]*\2[ \t]*(?:\n|$)", re.S)
SHELLS = {"bash", "sh", "zsh", "dash"}
SCRIPTERS = {"python", "python3", "node", "perl", "ruby"}
SCRIPTERS_RE = re.compile(r"python[\d.]*|node|perl|ruby")
WRAPPERS = {"sudo", "doas", "command", "nohup", "time", "nice", "exec", "builtin", "env", "stdbuf", "timeout"}
WRAPPER_VALUE_OPTS = {"-u", "-g", "-n", "-C", "-p", "-h", "-U", "-s"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
GIT_COMMIT_VALUE_FLAGS = "mFcCtu"


def tokenize(command):
    command = command.replace("`", "\n")
    lex = shlex.shlex(command, posix=True, punctuation_chars=";()<>|&\n")
    lex.whitespace = " \t\r"
    lex.commenters = ""
    lex.wordchars += "".join(c for c in string.printable if not c.isspace() and c not in ";()<>|&'\"\\")
    return list(lex)


def is_operator(tok):
    return bool(tok) and set(tok) <= OPERATOR_CHARS


def split_segments(command):
    """Simple commands as dicts: words, redirect targets, heredoc flag. Raises ValueError."""
    segs, cur = [], {"words": [], "redirs": [], "inredirs": [], "heredoc": False}
    toks = tokenize(command)
    i = 0
    while i < len(toks):
        t = toks[i]
        if is_operator(t) and "<" not in t and ">" not in t:
            if cur["words"] or cur["redirs"] or cur["inredirs"]:
                segs.append(cur)
            cur = {"words": [], "redirs": [], "inredirs": [], "heredoc": False}
        elif is_operator(t) and ">" in t:
            if i + 1 < len(toks) and not is_operator(toks[i + 1]):
                cur["redirs"].append(toks[i + 1])
                i += 1
        elif is_operator(t):
            cur["heredoc"] = cur["heredoc"] or t.startswith("<<")
            if i + 1 < len(toks) and not is_operator(toks[i + 1]):
                if t.endswith("<") and not t.endswith("<<"):
                    cur["inredirs"].append(toks[i + 1])
                i += 1
        else:
            cur["words"].append(t)
        i += 1
    if cur["words"] or cur["redirs"] or cur["inredirs"]:
        segs.append(cur)
    return segs


def extract_heredocs(command):
    """(command with heredoc bodies removed, [(program, body)] for script/shell heredocs)."""
    bodies = []

    def pull(m):
        before = command[: m.start()]
        head = before.splitlines()[-1] if before.strip() else ""
        words = strip_wrappers(head.split())
        prog = os.path.basename(words[0]) if words else ""
        if prog in SHELLS or prog in SCRIPTERS or re.fullmatch(r"python[\d.]*", prog):
            bodies.append((prog, m.group(3)))
        return " " + m.group(0).split("\n", 1)[0] + "\n"

    return HEREDOC.sub(pull, command), bodies


def strip_wrappers(words):
    words = list(words)
    while words:
        base = os.path.basename(words[0])
        if ASSIGNMENT.match(words[0]):
            words.pop(0)
        elif base in WRAPPERS:
            words.pop(0)
            while words and (words[0].startswith("-") or ASSIGNMENT.match(words[0])):
                opt = words.pop(0)
                if opt in WRAPPER_VALUE_OPTS and words:
                    words.pop(0)
            if base == "timeout" and words:
                words.pop(0)
        else:
            break
    return words


def git_subcommand(args):
    """(global options dict-ish, subcommand, rest) for the words after `git`."""
    args = list(args)
    chdir = None
    while args and args[0].startswith("-"):
        opt = args.pop(0)
        if opt in GIT_VALUE_OPTS and args:
            val = args.pop(0)
            if opt == "-C":
                chdir = val
    if not args:
        return chdir, None, []
    return chdir, args[0], args[1:]


# --- commit gate --------------------------------------------------------------------


def is_git_commit(command):
    """None if no `git commit` in the command, else {"all": bool, "dir": -C value or None}."""
    try:
        segs = split_segments(command)
    except ValueError:
        return None
    for seg in segs:
        words = strip_wrappers(seg["words"])
        if not words or os.path.basename(words[0]) != "git":
            continue
        chdir, sub, rest = git_subcommand(words[1:])
        if sub != "commit" or "--dry-run" in rest:
            continue
        take_all = False
        for w in rest:
            if w == "--":
                break
            if w == "--all":
                take_all = True
            elif w.startswith("-") and not w.startswith("--"):
                for ch in w[1:]:
                    if ch == "a":
                        take_all = True
                    if ch == "a" or ch in GIT_COMMIT_VALUE_FLAGS:
                        break
        return {"all": take_all, "dir": chdir}
    return None


ENV_TEMPLATES = (".env.example", ".env.sample", ".env.template")


def is_env_name(name):
    """True for .env and .env.<suffix> (secrets); the committed templates are not."""
    return bool(re.match(r"^\.env(\..+)?$", name)) and name not in ENV_TEMPLATES


def forbidden_staged(paths):
    bad = []
    for p in paths:
        parts = p.replace("\\", "/").split("/")
        name, dirs = parts[-1], parts[:-1]
        if is_env_name(name) or any(d in ("runs", ".venv", "__pycache__") for d in dirs):
            bad.append(p)
    return bad


def default_git_names(root, args):
    out = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=True).stdout
    return [n for n in out.split("\0") if n]


def default_run_tests(root):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(TEST_CMD, cwd=root, capture_output=True, text=True, timeout=TEST_TIMEOUT, env=env)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def tail(text, n=40):
    return "\n".join(text.strip().splitlines()[-n:])


def commit_gate(event, root, git_names=None, run_tests=None):
    if event.get("tool_name") != "Bash":
        return 0, ""
    info = is_git_commit((event.get("tool_input") or {}).get("command", ""))
    if info is None:
        return 0, ""
    git_names = git_names or default_git_names
    run_tests = run_tests or default_run_tests
    where = root
    if info["dir"]:
        where = os.path.join(event.get("cwd") or root, os.path.expanduser(info["dir"]))
    live = "--diff-filter=ACMRTUXB"
    try:
        names = git_names(where, ["diff", "--cached", "--name-only", "-z", live])
        if info["all"]:
            names += git_names(where, ["diff", "--name-only", "-z", live])
    except (OSError, subprocess.SubprocessError) as e:
        return 2, f"BLOCKED by commit gate: could not list staged files ({e})."
    bad = forbidden_staged(names)
    if bad:
        listing = "\n".join(f"  - {p}" for p in sorted(set(bad)))
        return 2, (
            "BLOCKED by commit gate: these files must not be committed "
            "(.env, runs/, .venv/, __pycache__/):\n"
            f"{listing}\n"
            "Unstage them with: git restore --staged <path>   (and keep them in .gitignore)."
        )
    try:
        rc, output = run_tests(root)
    except (OSError, subprocess.SubprocessError) as e:
        return 2, f"BLOCKED by commit gate: could not run the test suite ({e})."
    if rc != 0:
        return 2, (
            "BLOCKED by commit gate: tests failed "
            f"({' '.join(TEST_CMD)}). Fix them, then commit again.\n{tail(output)}"
        )
    return 0, ""


# --- dataset protector --------------------------------------------------------------

FILE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}

REMOVE_OR_MUTATE = {
    "rm", "rmdir", "unlink", "shred", "truncate", "touch", "chmod", "chown", "chgrp",
    "mkdir", "mkfifo", "mknod", "tee", "patch", "mv",
}
COPY_LIKE = {"cp", "rsync", "install", "scp", "ditto", "ln"}
GIT_REWRITERS = {"checkout", "restore", "clean", "rm", "mv"}
FIND_WRITE_ACTIONS = {"-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir"}
IN_PLACE = re.compile(r"^-[A-Za-z]*i|^--in-place")
SCRIPT_WRITE = re.compile(
    r"open\(\s*[^,)]+,\s*(?:mode\s*=\s*)?['\"][rwxabt+]*[wax+][rwxabt+]*['\"]"
    r"|\.open\(\s*(?:mode\s*=\s*)?['\"][rwxabt+]*[wax+]"
    r"|(?<!stdout)(?<!stderr)\.write(?:_text|_bytes)?\("
    r"|os\.(?:remove|unlink|rename|replace|rmdir|removedirs|truncate)\("
    r"|shutil\.(?:rmtree|move|copy\w*)\("
    r"|\.(?:unlink|rmdir|rename|touch|mkdir)\("
    r"|\.to_(?:csv|json|parquet|excel)\("
)
XARGS_VALUE_OPTS = {"-I", "-n", "-P", "-L", "-s", "-d", "-E", "-J"}


def project_root(event):
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return env
    d = event.get("cwd") or os.getcwd()
    cur = d
    while True:
        if os.path.isdir(os.path.join(cur, ".claude")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return d
        cur = parent


def resolve(path, cwd):
    p = os.path.expandvars(os.path.expanduser(path))
    if not os.path.isabs(p):
        p = os.path.join(cwd, p)
    return os.path.realpath(p)


def in_dataset(path, root, cwd):
    if not path or path.isdigit() or path == "-" or path.startswith("/dev/"):
        return False
    ds = os.path.realpath(os.path.join(root, "dataset"))
    p = resolve(path, cwd)
    if CASE_INSENSITIVE_FS:
        p, ds = p.lower(), ds.lower()
    return p == ds or p.startswith(ds + os.sep)


def operands(args):
    return [a for a in args if not a.startswith("-") or a == "-"]


def copy_destination(prog, args):
    for i, a in enumerate(args):
        if a == "-t" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--target-directory="):
            return a.split("=", 1)[1]
    ops = operands(args)
    return ops[-1] if ops else None


def script_writes_dataset(code):
    return "dataset" in code.lower() and bool(SCRIPT_WRITE.search(code))


def program_reason(prog, args, root, cwd, depth, mentions_dataset, seg):
    """Why this single program invocation writes to dataset/, or None."""
    hit = lambda p: in_dataset(p, root, cwd)
    if prog in REMOVE_OR_MUTATE:
        for p in operands(args):
            if hit(p):
                return f"`{prog}` on {p}"
    elif prog in COPY_LIKE:
        dest = copy_destination(prog, args)
        if dest and hit(dest):
            return f"`{prog}` into {dest}"
        if "--remove-source-files" in args and any(hit(p) for p in operands(args)):
            return f"`{prog} --remove-source-files` from dataset"
    elif prog in ("sed", "perl"):
        if any(IN_PLACE.match(a) for a in args if a.startswith("-")):
            for p in operands(args):
                if hit(p):
                    return f"in-place `{prog}` on {p}"
    elif prog == "dd":
        for a in args:
            if a.startswith("of=") and hit(a[3:]):
                return f"`dd` writing {a[3:]}"
    elif prog == "git":
        chdir, sub, rest = git_subcommand(args)
        gcwd = resolve(chdir, cwd) if chdir else cwd
        if sub in GIT_REWRITERS:
            for p in operands(rest):
                if in_dataset(p, root, gcwd):
                    return f"`git {sub}` on {p}"
    elif prog == "find":
        starts = [p for p in operands(args) if hit(p)]
        if starts:
            if any(a in FIND_WRITE_ACTIONS for a in args):
                return f"`find {starts[0]} ...` with a delete/print-to-file action"
            for i, a in enumerate(args):
                if a in FIND_EXEC and i + 1 < len(args):
                    sub = os.path.basename(args[i + 1])
                    if sub in REMOVE_OR_MUTATE or sub in COPY_LIKE or sub in ("sed", "perl"):
                        return f"`find {starts[0]} {a} {sub}`"
    elif prog == "xargs":
        rest = list(args)
        while rest and rest[0].startswith("-"):
            opt = rest.pop(0)
            if opt in XARGS_VALUE_OPTS and rest:
                rest.pop(0)
        if rest and mentions_dataset:
            sub = os.path.basename(rest[0])
            if sub in REMOVE_OR_MUTATE or (sub in ("sed", "perl") and any(IN_PLACE.match(a) for a in rest[1:])):
                return f"`xargs {sub}` fed from a command that names dataset/"
    elif prog in SHELLS or re.fullmatch(r"python[\d.]*", prog) or prog in SCRIPTERS:
        for i, a in enumerate(args):
            if a == "-c" and i + 1 < len(args):
                code = args[i + 1]
                if prog in SHELLS:
                    r = command_reason(code, root, cwd, depth + 1)
                    if r:
                        return r
                elif script_writes_dataset(code):
                    return f"`{prog} -c` code that writes under dataset/"
    return None


def command_reason(command, root, cwd, depth=0):
    if depth > 3:
        return None
    stripped, script_bodies = extract_heredocs(command)
    for prog, body in script_bodies:
        if prog in SHELLS:
            r = command_reason(body, root, cwd, depth + 1)
            if r:
                return r
        elif script_writes_dataset(body):
            return f"`{prog}` heredoc that writes under dataset/"
    try:
        segs = split_segments(stripped)
    except ValueError:
        return None
    mentions = any(in_dataset(w, root, cwd) for s in segs for w in s["words"] if not w.startswith("-"))
    for seg in segs:
        for target in seg["redirs"]:
            if in_dataset(target, root, cwd):
                return f"redirect to {target}"
        words = strip_wrappers(seg["words"])
        if not words:
            continue
        prog, args = os.path.basename(words[0]), words[1:]
        if prog in ("cd", "pushd"):
            ops = operands(args)
            if ops:
                cwd = resolve(ops[0], cwd)
            continue
        r = program_reason(prog, args, root, cwd, depth, mentions, seg)
        if r:
            return r
    return None


def dataset_check(event, root):
    tool = event.get("tool_name")
    cwd = event.get("cwd") or root
    inp = event.get("tool_input") or {}
    reason = None
    if tool in FILE_TOOLS:
        path = inp.get(FILE_TOOLS[tool])
        if path and in_dataset(path, root, cwd):
            reason = f"{tool} on {path}"
    elif tool == "Bash":
        reason = command_reason(inp.get("command", ""), root, cwd)
    if reason is None:
        return 0, ""
    return 2, (
        f"BLOCKED by dataset protector: {reason}. dataset/ is read-only input data "
        "(AGENTS.md section 6.1); do not modify or delete it. "
        "Reads (cat, ls, grep, sqlite3, cp out of dataset/) are allowed; write results outside dataset/."
    )


# --- .env read guard ----------------------------------------------------------------

ENV_FILE_TOOLS = {"Read": "file_path", "Edit": "file_path", "Write": "file_path",
                  "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
ENV_READERS = {
    "cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg", "ag", "bat", "nl", "tac", "sed",
    "awk", "gawk", "cut", "sort", "uniq", "strings", "xxd", "od", "hexdump", "vi", "vim", "nvim", "nano",
    "view", "emacs", "diff", "cmp", "paste", "tr", "fold", "column", "base64", "open", "source", ".",
}
ENV_COPIERS = {"cp", "mv", "rsync", "scp", "ditto"}
ENV_NAME_IN_CODE = re.compile(r"[\w./~$-]*\.env(?:\.[A-Za-z0-9_-]+)?")
GLOB_CHARS = set("*?[")


def env_word(word):
    """True if a shell word names a protected env file (also globs that could expand to one)."""
    if not word or (word.startswith("-") and "=" not in word) or word.startswith(("--exclude", "--include")):
        return False
    for cand in (word, word.split("=", 1)[-1]):
        name = os.path.basename(cand.rstrip("/"))
        if GLOB_CHARS & set(name):
            if name.startswith(".") and fnmatch.fnmatch(".env", name):
                return True
        elif is_env_name(name):
            return True
    return False


def env_path(path, cwd):
    if not path:
        return False
    if is_env_name(os.path.basename(path)):
        return True
    return is_env_name(os.path.basename(resolve(path, cwd)))


def code_reads_env(code):
    return any(is_env_name(os.path.basename(m)) for m in ENV_NAME_IN_CODE.findall(code))


def recursive_grep_hits_root(prog, args, root, cwd):
    if prog not in ("grep", "egrep", "fgrep"):
        return False
    recursive = any(
        a in ("--recursive", "--dereference-recursive")
        or (a.startswith("-") and not a.startswith("--") and ("r" in a or "R" in a))
        for a in args
    )
    if not recursive or any(a.startswith(("--exclude", "--include")) for a in args):
        return False
    real_root = os.path.realpath(root)
    for p in operands(args)[1:]:
        d = resolve(p, cwd)
        if real_root == d or real_root.startswith(d.rstrip(os.sep) + os.sep):
            return True
    return False


def env_command_reason(command, root, cwd, depth=0):
    if depth > 3:
        return None
    stripped, bodies = extract_heredocs(command)
    for prog, body in bodies:
        if prog in SHELLS:
            r = env_command_reason(body, root, cwd, depth + 1)
            if r:
                return r
        elif code_reads_env(body):
            return f"`{prog}` heredoc that names .env"
    try:
        segs = split_segments(stripped)
    except ValueError:
        return None
    for seg in segs:
        for target in seg["inredirs"]:
            if env_word(target):
                return f"input redirect from {target}"
        words = strip_wrappers(seg["words"])
        if not words:
            continue
        prog, args = os.path.basename(words[0]), words[1:]
        if prog in ("cd", "pushd"):
            ops = operands(args)
            if ops:
                cwd = resolve(ops[0], cwd)
            continue
        if prog == "xargs":
            rest = list(args)
            while rest and rest[0].startswith("-"):
                if rest.pop(0) in XARGS_VALUE_OPTS and rest:
                    rest.pop(0)
            if rest:
                r = env_command_reason(" ".join(shlex.quote(w) for w in rest), root, cwd, depth + 1)
                if r:
                    return r
            continue
        if prog in SHELLS or SCRIPTERS_RE.fullmatch(prog):
            for i, a in enumerate(args):
                if a == "-c" and i + 1 < len(args):
                    if prog in SHELLS:
                        r = env_command_reason(args[i + 1], root, cwd, depth + 1)
                        if r:
                            return r
                    elif code_reads_env(args[i + 1]):
                        return f"`{prog} -c` code that names .env"
            continue
        if prog in ENV_READERS:
            if any(env_word(a) for a in args):
                return f"`{prog}` on a .env file"
        elif prog in ENV_COPIERS:
            ops = operands(args)
            sources = ops if any(a == "-t" or a.startswith("--target-directory") for a in args) else ops[:-1]
            if any(env_word(a) for a in sources):
                return f"`{prog}` from a .env file"
        if recursive_grep_hits_root(prog, args, root, cwd):
            return f"recursive `{prog}` over the project root, which would read .env"
    return None


def env_guard(event, root):
    tool = event.get("tool_name")
    cwd = event.get("cwd") or root
    inp = event.get("tool_input") or {}
    reason = None
    if tool in ENV_FILE_TOOLS:
        path = inp.get(ENV_FILE_TOOLS[tool])
        if env_path(path, cwd):
            reason = f"{tool} on {path}"
    elif tool == "Bash":
        reason = env_command_reason(inp.get("command", ""), root, cwd)
    if reason is None:
        return 0, ""
    return 2, (
        f"BLOCKED by env guard: {reason}. .env files hold secrets and are never read or edited by the agent. "
        "Use .env.example, .env.sample or .env.template for the variable names; the user manages the real .env."
    )


# --- git guards -----------------------------------------------------------------------


def git_invocations(event):
    """Yield (subcommand, flags, operands, chdir) for every git call in a Bash command."""
    if event.get("tool_name") != "Bash":
        return
    try:
        segs = split_segments((event.get("tool_input") or {}).get("command", ""))
    except ValueError:
        return
    for seg in segs:
        words = strip_wrappers(seg["words"])
        if not words or os.path.basename(words[0]) != "git":
            continue
        chdir, sub, rest = git_subcommand(words[1:])
        if sub is None:
            continue
        flags, ops, after_dd = [], [], False
        for w in rest:
            if after_dd:
                ops.append(w)
            elif w == "--":
                after_dd = True
            elif w.startswith("-") and w != "-":
                flags.append(w)
            else:
                ops.append(w)
        yield sub, flags, ops, chdir


def short_flags(flags):
    return "".join(f[1:] for f in flags if not f.startswith("--"))


def git_add_guard(event, root):
    for sub, flags, ops, _ in git_invocations(event):
        if sub != "add" or "--dry-run" in flags or "n" in short_flags(flags):
            continue
        broad = "--all" in flags or "A" in short_flags(flags) or any(o in (".", "./", ":/") for o in ops)
        if broad:
            return 2, (
                "BLOCKED by git guard: `git add .`, `git add -A` and `git add --all` stage everything, "
                "including files that must never be committed (.env, runs/, caches). "
                "Stage by name instead: git add <file> [<file> ...], so name the files you changed."
            )
    return 0, ""


def git_destructive_guard(event, root):
    for sub, flags, ops, _ in git_invocations(event):
        dry = "--dry-run" in flags or "n" in short_flags(flags)
        if sub == "push" and not dry:
            if "--force" in flags or "f" in short_flags(flags) or any(o.startswith("+") for o in ops):
                return 2, ("BLOCKED by git guard: `git push --force` / `-f` / `+refspec` can overwrite remote history. "
                           "Use --force-with-lease only if the user asks for it.")
        elif sub == "reset" and "--hard" in flags:
            return 2, "BLOCKED by git guard: `git reset --hard` discards uncommitted work. Use `git stash` or `git reset --soft/--mixed`."
        elif sub == "clean" and not dry and ("--force" in flags or "f" in short_flags(flags)):
            return 2, "BLOCKED by git guard: `git clean -f` permanently deletes untracked files. Preview with `git clean -n` and ask the user."
    return 0, ""


def default_current_branch(where):
    try:
        out = subprocess.run(["git", "-C", where, "symbolic-ref", "--short", "-q", "HEAD"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def branch_gate(event, root, current_branch=None):
    if event.get("tool_name") != "Bash":
        return 0, ""
    info = is_git_commit((event.get("tool_input") or {}).get("command", ""))
    if info is None:
        return 0, ""
    where = event.get("cwd") or root
    if info["dir"]:
        where = os.path.join(where, os.path.expanduser(info["dir"]))
    branch = (current_branch or default_current_branch)(where)
    if branch in ("main", "master"):
        return 2, (
            f"BLOCKED by branch gate: you are on `{branch}`; commits go on a feature branch. "
            "Create one with: git switch -c <feature/name>   (your changes come along), then commit."
        )
    return 0, ""


# --- entry point --------------------------------------------------------------------


CHECKS = {
    "commit-gate": commit_gate,
    "dataset": dataset_check,
    "env-guard": env_guard,
    "git-add": git_add_guard,
    "git-destructive": git_destructive_guard,
    "branch-gate": branch_gate,
}


def main(argv=None, stdin=None):
    argv = sys.argv[1:] if argv is None else argv
    stdin = stdin or sys.stdin
    if len(argv) != 1 or argv[0] not in CHECKS:
        print(f"usage: guard.py {'|'.join(CHECKS)}  (hook JSON on stdin)", file=sys.stderr)
        return 64
    try:
        event = json.load(stdin)
        if not isinstance(event, dict):
            return 0
    except ValueError:
        return 0
    root = project_root(event)
    code, message = CHECKS[argv[0]](event, root)
    if message:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
