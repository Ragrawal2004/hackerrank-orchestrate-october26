import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import REPO_ROOT

GUARD_PATH = REPO_ROOT / ".claude" / "hooks" / "guard.py"
_spec = importlib.util.spec_from_file_location("guard", GUARD_PATH)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

ROOT = "/proj"


def bash(command, cwd=ROOT):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


def tool(name, path, cwd=ROOT, key="file_path"):
    return {"tool_name": name, "tool_input": {key: path}, "cwd": cwd}


class IsGitCommitTests(unittest.TestCase):
    def test_plain_commit(self):
        self.assertIsNotNone(guard.is_git_commit('git commit -m "x"'))

    def test_not_a_commit(self):
        for cmd in ["git status", "git log --oneline", "ls", "git commit-tree abc"]:
            self.assertIsNone(guard.is_git_commit(cmd), cmd)

    def test_quoted_mention_is_ignored(self):
        self.assertIsNone(guard.is_git_commit('echo "git commit"'))
        self.assertIsNone(guard.is_git_commit("grep 'git commit' README.md"))

    def test_global_options_before_subcommand(self):
        self.assertIsNotNone(guard.is_git_commit("git -C . commit -m x"))
        self.assertIsNotNone(guard.is_git_commit("git -c user.name=a commit -m x"))

    def test_chained(self):
        self.assertIsNotNone(guard.is_git_commit("git add -A && git commit -m x"))
        self.assertIsNotNone(guard.is_git_commit("ls; git commit -m x"))
        self.assertIsNotNone(guard.is_git_commit("ls\ngit commit -m x"))

    def test_dry_run_is_not_gated(self):
        self.assertIsNone(guard.is_git_commit("git commit --dry-run"))

    def test_all_flag_detected(self):
        self.assertTrue(guard.is_git_commit("git commit -am x")["all"])
        self.assertTrue(guard.is_git_commit("git commit -a -m x")["all"])
        self.assertTrue(guard.is_git_commit("git commit --all -m x")["all"])
        self.assertFalse(guard.is_git_commit("git commit -m x")["all"])
        self.assertFalse(guard.is_git_commit('git commit -m "use -a flag"')["all"])


class ForbiddenStagedTests(unittest.TestCase):
    def test_env_files(self):
        self.assertEqual(guard.forbidden_staged([".env"]), [".env"])
        self.assertEqual(guard.forbidden_staged(["code/.env"]), ["code/.env"])
        self.assertEqual(guard.forbidden_staged([".env.local"]), [".env.local"])

    def test_env_templates_allowed(self):
        self.assertEqual(guard.forbidden_staged([".env.example", ".env.sample"]), [])

    def test_runs_dir(self):
        self.assertEqual(guard.forbidden_staged(["runs/a.json"]), ["runs/a.json"])
        self.assertEqual(guard.forbidden_staged(["code/runs/x/y.csv"]), ["code/runs/x/y.csv"])

    def test_venv_and_pycache(self):
        self.assertEqual(guard.forbidden_staged([".venv/bin/python"]), [".venv/bin/python"])
        got = guard.forbidden_staged(["code/ghost/__pycache__/a.cpython-314.pyc"])
        self.assertEqual(len(got), 1)

    def test_lookalikes_allowed(self):
        ok = ["code/runs.py", "code/ghost/environment.py", "docs/venv.md", "README.md", "code/tests/test_guard.py"]
        self.assertEqual(guard.forbidden_staged(ok), [])


class CommitGateTests(unittest.TestCase):
    def gate(self, command, staged=(), unstaged=(), tests=(0, "OK")):
        calls = {"tests": 0, "git": []}

        def git_names(root, args):
            calls["git"].append(args)
            return list(unstaged if "--cached" not in args else staged)

        def run_tests(root):
            calls["tests"] += 1
            return tests

        code, msg = guard.commit_gate(bash(command), ROOT, git_names=git_names, run_tests=run_tests)
        return code, msg, calls

    def test_non_commit_passes_without_running_anything(self):
        code, _, calls = self.gate("ls -la", staged=[".env"])
        self.assertEqual(code, 0)
        self.assertEqual(calls["tests"], 0)
        self.assertEqual(calls["git"], [])

    def test_non_bash_tool_passes(self):
        code, _ = guard.commit_gate(tool("Write", "git commit"), ROOT, git_names=lambda r, a: [".env"], run_tests=lambda r: (1, ""))
        self.assertEqual(code, 0)

    def test_clean_stage_and_green_tests_allow(self):
        code, _, calls = self.gate("git commit -m x", staged=["code/a.py"])
        self.assertEqual(code, 0)
        self.assertEqual(calls["tests"], 1)

    def test_blocks_each_forbidden_path(self):
        for bad in [".env", "runs/out.json", ".venv/lib/x.py", "code/__pycache__/x.pyc"]:
            code, msg, calls = self.gate("git commit -m x", staged=["code/a.py", bad])
            self.assertEqual(code, 2, bad)
            self.assertIn(bad, msg)
            self.assertEqual(calls["tests"], 0, "tests must not run when the stage is already bad")

    def test_message_explains_the_fix(self):
        _, msg, _ = self.gate("git commit -m x", staged=[".env"])
        self.assertIn("git restore --staged", msg)

    def test_blocks_when_tests_fail(self):
        code, msg, _ = self.gate("git commit -m x", staged=["a.py"], tests=(1, "FAIL: test_foo\nboom"))
        self.assertEqual(code, 2)
        self.assertIn("test_foo", msg)
        self.assertIn("tests failed", msg.lower())

    def test_all_flag_also_inspects_unstaged_tracked_changes(self):
        code, msg, _ = self.gate("git commit -am x", staged=[], unstaged=["runs/x.json"])
        self.assertEqual(code, 2)
        self.assertIn("runs/x.json", msg)

    def test_without_all_flag_unstaged_changes_are_ignored(self):
        code, _, _ = self.gate("git commit -m x", staged=["a.py"], unstaged=["runs/x.json"])
        self.assertEqual(code, 0)

    def test_chained_commit_is_gated(self):
        code, _, _ = self.gate("git add a.py && git commit -m x", staged=[".env"])
        self.assertEqual(code, 2)

    def test_runner_failure_to_launch_blocks(self):
        def boom(root):
            raise OSError("python3 not found")

        code, msg = guard.commit_gate(bash("git commit -m x"), ROOT, git_names=lambda r, a: [], run_tests=boom)
        self.assertEqual(code, 2)
        self.assertIn("python3 not found", msg)


class GitIntegrationTests(unittest.TestCase):
    """Real git in a throwaway repo: proves the default staged-file lookup."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.git("init", "-q")
        (self.repo / "keep.txt").write_text("k")
        (self.repo / "runs").mkdir()
        (self.repo / "runs" / "old.json").write_text("{}")
        self.git("add", "-f", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-qm", "init")

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def gate(self, command):
        return guard.commit_gate(bash(command, cwd=str(self.repo)), str(self.repo), run_tests=lambda r: (0, "OK"))

    def test_staged_env_is_found(self):
        (self.repo / ".env").write_text("FAKE=1")
        self.git("add", "-f", ".env")
        code, msg = self.gate("git commit -m x")
        self.assertEqual(code, 2)
        self.assertIn(".env", msg)

    def test_unstaged_env_is_not_a_problem(self):
        (self.repo / ".env").write_text("FAKE=1")
        code, _ = self.gate("git commit -m x")
        self.assertEqual(code, 0)

    def test_staging_a_deletion_of_a_forbidden_path_is_allowed(self):
        self.git("rm", "-q", "--cached", "runs/old.json")
        code, _ = self.gate("git commit -m x")
        self.assertEqual(code, 0)


class DatasetFileToolTests(unittest.TestCase):
    def test_write_edit_multiedit_notebook_blocked(self):
        for name, key in [("Write", "file_path"), ("Edit", "file_path"), ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")]:
            code, msg = guard.dataset_check(tool(name, "/proj/dataset/user_identities.csv", key=key), ROOT)
            self.assertEqual(code, 2, name)
            self.assertIn("dataset", msg)

    def test_relative_and_dotdot_paths_blocked(self):
        self.assertEqual(guard.dataset_check(tool("Write", "dataset/x.csv"), ROOT)[0], 2)
        self.assertEqual(guard.dataset_check(tool("Write", "../dataset/x.csv", cwd="/proj/code"), ROOT)[0], 2)
        self.assertEqual(guard.dataset_check(tool("Edit", "/proj/code/../dataset/x.csv"), ROOT)[0], 2)

    def test_nested_new_file_and_directory_itself_blocked(self):
        self.assertEqual(guard.dataset_check(tool("Write", "/proj/dataset/evidence/new/a.txt"), ROOT)[0], 2)
        self.assertEqual(guard.dataset_check(tool("Write", "/proj/dataset"), ROOT)[0], 2)

    def test_lookalikes_allowed(self):
        for p in ["/proj/dataset_notes/a.md", "/proj/code/dataset.py", "/proj/code/ghost/dataset/x.py", "/proj/output.csv"]:
            self.assertEqual(guard.dataset_check(tool("Write", p), ROOT)[0], 0, p)

    def test_other_tools_pass(self):
        self.assertEqual(guard.dataset_check(tool("Read", "/proj/dataset/x.csv"), ROOT)[0], 0)
        self.assertEqual(guard.dataset_check(tool("Grep", "/proj/dataset/x.csv", key="path"), ROOT)[0], 0)

    def test_symlink_into_dataset_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp).resolve()
            (real / "dataset").mkdir()
            (real / "link").symlink_to(real / "dataset")
            code, _ = guard.dataset_check(tool("Write", str(real / "link" / "f.csv"), cwd=str(real)), str(real))
            self.assertEqual(code, 2)

    @unittest.skipUnless(sys.platform in ("darwin", "win32"), "case-insensitive filesystems only")
    def test_case_variant_blocked_on_case_insensitive_fs(self):
        self.assertEqual(guard.dataset_check(tool("Write", "/proj/Dataset/x.csv"), ROOT)[0], 2)


class DatasetBashBlockedTests(unittest.TestCase):
    def assertBlocked(self, command, cwd=ROOT):
        code, msg = guard.dataset_check(bash(command, cwd=cwd), ROOT)
        self.assertEqual(code, 2, command)
        self.assertIn("dataset", msg)

    def test_redirects(self):
        for c in [
            "echo hi > dataset/a.csv",
            "echo hi >> dataset/a.csv",
            "echo hi >dataset/a.csv",
            "echo hi >| dataset/a.csv",
            "cmd &> dataset/a.log",
            "cmd 2> dataset/err.log",
            "cat x | tee dataset/a.csv",
            "cat x | tee -a dataset/a.csv",
            "dd if=/dev/zero of=dataset/a.bin",
        ]:
            self.assertBlocked(c)

    def test_deletes_and_mutations(self):
        for c in [
            "rm dataset/user_identities.csv",
            "rm -rf dataset",
            "rm -rf dataset/",
            "rm -f dataset/evidence/logs/*.log",
            "rmdir dataset/evidence/images",
            "unlink dataset/a.csv",
            "shred -u dataset/a.csv",
            "truncate -s 0 dataset/a.csv",
            "touch dataset/new.csv",
            "mkdir dataset/new",
            "chmod 777 dataset/a.csv",
            "ln -s /etc/passwd dataset/pw",
            "find dataset -name '*.log' -delete",
            "find dataset -type f -exec rm {} +",
        ]:
            self.assertBlocked(c)

    def test_move_and_copy(self):
        self.assertBlocked("mv dataset/a.csv /tmp/a.csv")
        self.assertBlocked("mv /tmp/a.csv dataset/a.csv")
        self.assertBlocked("cp /tmp/a.csv dataset/a.csv")
        self.assertBlocked("cp -r /tmp/stuff dataset/")
        self.assertBlocked("cp -t dataset /tmp/a.csv")
        self.assertBlocked("rsync -a /tmp/x/ dataset/")
        self.assertBlocked("install -m 644 a dataset/a")

    def test_in_place_editors(self):
        self.assertBlocked("sed -i '' 's/a/b/' dataset/a.csv")
        self.assertBlocked("sed -i.bak 's/a/b/' dataset/a.csv")
        self.assertBlocked("sed -ni 's/a/b/p' dataset/a.csv")
        self.assertBlocked("perl -pi -e 's/a/b/' dataset/a.csv")

    def test_git_commands_that_rewrite_files(self):
        for c in [
            "git checkout -- dataset/a.csv",
            "git checkout HEAD -- dataset",
            "git restore dataset/a.csv",
            "git clean -fd dataset",
            "git rm dataset/a.csv",
            "git rm -r --cached dataset",
            "git mv dataset/a.csv dataset/b.csv",
        ]:
            self.assertBlocked(c)

    def test_chained_and_nested(self):
        self.assertBlocked("ls && rm dataset/a.csv")
        self.assertBlocked("ls; rm dataset/a.csv")
        self.assertBlocked("ls\nrm dataset/a.csv")
        self.assertBlocked("echo $(rm dataset/a.csv)")
        self.assertBlocked("echo `rm dataset/a.csv`")
        self.assertBlocked('bash -c "rm dataset/a.csv"')
        self.assertBlocked("sh -c 'echo x > dataset/a.csv'")
        self.assertBlocked("sudo rm dataset/a.csv")
        self.assertBlocked("FOO=1 rm dataset/a.csv")
        self.assertBlocked("env FOO=1 rm dataset/a.csv")

    def test_cwd_tracking(self):
        self.assertBlocked("cd dataset && rm a.csv")
        self.assertBlocked("rm a.csv", cwd="/proj/dataset")
        self.assertBlocked("echo x > ../dataset/a.csv", cwd="/proj/code")
        self.assertBlocked("rm /proj/dataset/a.csv", cwd="/tmp")

    def test_inline_python_writes(self):
        self.assertBlocked("python3 -c \"open('dataset/a.csv','w').write('x')\"")
        self.assertBlocked("python3 -c \"import os; os.remove('dataset/a.csv')\"")
        self.assertBlocked("python3 -c \"import shutil; shutil.rmtree('dataset')\"")
        self.assertBlocked("python3 - <<'EOF'\nfrom pathlib import Path\nPath('dataset/a.csv').write_text('x')\nEOF")


class DatasetBashAllowedTests(unittest.TestCase):
    def assertAllowed(self, command, cwd=ROOT):
        code, msg = guard.dataset_check(bash(command, cwd=cwd), ROOT)
        self.assertEqual(code, 0, f"{command!r} -> {msg}")

    def test_reads(self):
        for c in [
            "cat dataset/deletion_rules.csv",
            "head -n 3 dataset/deletion_requests.csv",
            "ls -la dataset/evidence",
            "wc -l dataset/*.csv",
            "grep -rn alice dataset/evidence/logs",
            "sqlite3 :memory: '.read dataset/evidence/databases/a.sql'",
            "find dataset -name '*.json'",
            "cat dataset/a.csv | head -2 > /tmp/out.txt",
            "sort < dataset/a.csv > /tmp/sorted.txt",
            "sed -n '1,5p' dataset/a.csv",
            "sed 's/a/b/' dataset/a.csv",
            "git diff -- dataset",
            "git status dataset",
            "git log -- dataset",
            "git checkout -- code/main.py",
            "python3 -c \"print(open('dataset/a.csv').read())\"",
            "python3 -c \"import csv; list(csv.reader(open('dataset/a.csv')))\"",
            "diff dataset/a.csv dataset/b.csv",
        ]:
            self.assertAllowed(c)

    def test_copy_out_of_dataset(self):
        self.assertAllowed("cp dataset/a.csv /tmp/a.csv")
        self.assertAllowed("cp -r dataset/evidence /tmp/ev")
        self.assertAllowed("rsync -a dataset/ /tmp/mirror/")

    def test_writes_elsewhere(self):
        for c in [
            "echo hi > output.csv",
            "rm -rf runs/old",
            "touch code/new.py",
            "mv a.txt b.txt",
            "tee /tmp/x < dataset/a.csv",
            "echo hi > /dev/null",
            "git commit -m 'edit dataset docs'",
            "echo 'rm dataset/a.csv'",
            "grep 'rm dataset' README.md",
        ]:
            self.assertAllowed(c)

    def test_sibling_names(self):
        self.assertAllowed("rm -rf dataset_old")
        self.assertAllowed("touch code/dataset.py")
        self.assertAllowed("rm code/ghost/dataset/x.py")

    def test_malformed_command_is_allowed(self):
        self.assertAllowed("echo 'unterminated")


class MainTests(unittest.TestCase):
    def run_main(self, sub, event, root=ROOT):
        stdin = io.StringIO(event if isinstance(event, str) else json.dumps(event))
        stderr = io.StringIO()
        old = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = root
        real_err, sys.stderr = sys.stderr, stderr
        try:
            code = guard.main([sub], stdin=stdin)
        finally:
            sys.stderr = real_err
            if old is None:
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
            else:
                os.environ["CLAUDE_PROJECT_DIR"] = old
        return code, stderr.getvalue()

    def test_dataset_block_writes_message_to_stderr(self):
        code, err = self.run_main("dataset", tool("Write", "/proj/dataset/a.csv"))
        self.assertEqual(code, 2)
        self.assertIn("dataset", err)

    def test_dataset_allow(self):
        code, err = self.run_main("dataset", bash("cat dataset/a.csv"))
        self.assertEqual((code, err), (0, ""))

    def test_bad_json_fails_open(self):
        for sub in ("dataset", "commit-gate"):
            self.assertEqual(self.run_main(sub, "not json")[0], 0)

    def test_unknown_subcommand_is_an_error(self):
        self.assertNotEqual(self.run_main("bogus", bash("ls"))[0], 0)


class EnvToolTests(unittest.TestCase):
    def check(self, name, path, key="file_path", cwd=ROOT):
        return guard.env_guard(tool(name, path, cwd=cwd, key=key), ROOT)

    def test_read_edit_write_multiedit_notebook_blocked(self):
        for name, key in [("Read", "file_path"), ("Edit", "file_path"), ("Write", "file_path"), ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")]:
            code, msg = self.check(name, "/proj/.env", key=key)
            self.assertEqual(code, 2, name)
            self.assertIn(".env", msg)

    def test_variants_and_locations_blocked(self):
        for p in [".env", "code/.env", "/proj/code/.env", ".env.local", ".env.production", "../.env"]:
            self.assertEqual(self.check("Read", p, cwd="/proj/code" if p.startswith("..") else ROOT)[0], 2, p)

    def test_templates_allowed(self):
        for p in [".env.example", ".env.sample", ".env.template", "code/.env.example"]:
            for name in ("Read", "Edit", "Write"):
                self.assertEqual(self.check(name, p)[0], 0, f"{name} {p}")

    def test_lookalikes_allowed(self):
        for p in ["code/ghost/envfile.py", "docs/env.md", "code/tests/test_envfile.py", "environment.py", ".envrc", "my.env.txt"]:
            self.assertEqual(self.check("Read", p)[0], 0, p)

    def test_symlink_to_env_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp).resolve()
            (real / ".env").write_text("FAKE=1")
            (real / "innocent.txt").symlink_to(real / ".env")
            self.assertEqual(guard.env_guard(tool("Read", str(real / "innocent.txt"), cwd=str(real)), str(real))[0], 2)

    def test_unrelated_tools_pass(self):
        self.assertEqual(self.check("Grep", ".env", key="path")[0], 0)


class EnvBashTests(unittest.TestCase):
    def assertBlocked(self, command, cwd=ROOT):
        code, msg = guard.env_guard(bash(command, cwd=cwd), ROOT)
        self.assertEqual(code, 2, command)
        self.assertIn(".env", msg)

    def assertAllowed(self, command, cwd=ROOT):
        code, msg = guard.env_guard(bash(command, cwd=cwd), ROOT)
        self.assertEqual(code, 0, f"{command!r} -> {msg}")

    def test_named_readers(self):
        for c in [
            "cat .env", "cat code/.env", "cat -n .env", "head -n 3 .env", "tail -f .env", "less .env", "more .env",
            "grep API .env", "grep -r KEY . .env", "egrep KEY .env", "rg KEY .env", "sed -n 1p .env",
            "awk '{print}' .env", "cut -d= -f2 .env", "sort .env", "bat .env", "nl .env", "tac .env",
            "strings .env", "xxd .env", "vim .env", "cp .env /tmp/leak", "diff .env .env.example",
            "cat .env.local", "cat .env.production",
        ]:
            self.assertBlocked(c)

    def test_source_and_dot(self):
        self.assertBlocked("source .env")
        self.assertBlocked(". .env")
        self.assertBlocked("set -a; source .env; set +a")
        self.assertBlocked("set -a && . ./.env")

    def test_input_redirect(self):
        self.assertBlocked("wc -l < .env")
        self.assertBlocked("while read l; do echo $l; done < .env")
        self.assertBlocked("echo $(< .env)")
        self.assertBlocked("tr a b <.env")

    def test_chained_and_wrapped(self):
        self.assertBlocked("ls && cat .env")
        self.assertBlocked("ls; cat .env")
        self.assertBlocked("cat .env | head")
        self.assertBlocked("sudo cat .env")
        self.assertBlocked("env FOO=1 cat .env")
        self.assertBlocked("bash -c 'cat .env'")
        self.assertBlocked("ls | xargs cat .env")
        self.assertBlocked("echo `cat .env`")

    def test_absolute_relative_and_cwd(self):
        self.assertBlocked("cat /proj/.env")
        self.assertBlocked("cat ../.env", cwd="/proj/code")
        self.assertBlocked("cat $PWD/.env")
        self.assertBlocked("cat ./.env")
        self.assertBlocked("cat .e*")
        self.assertBlocked("cd code && cat .env")

    def test_inline_scripts(self):
        self.assertBlocked("python3 -c \"print(open('.env').read())\"")
        self.assertBlocked("python3 -c \"from pathlib import Path; Path('.env').read_text()\"")
        self.assertBlocked("python3 - <<'EOF'\nprint(open('.env').read())\nEOF")

    def test_recursive_grep_over_project_root(self):
        for c in ["grep -r KEY .", "grep -rn KEY /proj", "grep -R KEY ..", "grep --recursive KEY ."]:
            self.assertBlocked(c, cwd=ROOT if "/proj" in c or c.endswith(" .") else "/proj/code")
        self.assertBlocked("grep -r KEY ..", cwd="/proj/code")
        self.assertAllowed("grep -r KEY code")
        self.assertAllowed("grep -rn envfile code/ghost")
        self.assertAllowed("grep -r --exclude='.env*' KEY .")
        self.assertAllowed("grep -r KEY .", cwd="/proj/code/ghost")

    def test_templates_allowed(self):
        for c in ["cat .env.example", "head .env.sample", "grep KEY .env.template", "cp .env.example .env.example.bak",
                  "diff .env.example .env.sample", "source .env.example", "wc -l < .env.example"]:
            self.assertAllowed(c)

    def test_other_commands_allowed(self):
        for c in [
            "ls -la", "cat README.md", "grep -rn envfile code/ghost", "cat code/ghost/envfile.py",
            "git status", "git check-ignore .env", "ls .env", "test -f .env", "touch .env.example",
            "echo '.env' >> .gitignore", "cat .gitignore", "git commit -m 'document .env handling'",
            "echo 'cat .env'", "python3 -m unittest discover -s code/tests -t code",
            "python3 code/main.py --env-file .env",
        ]:
            self.assertAllowed(c)

    def test_malformed_is_allowed(self):
        self.assertAllowed("echo 'unterminated")


class GitAddGuardTests(unittest.TestCase):
    def check(self, command):
        return guard.git_add_guard(bash(command), ROOT)

    def test_blocks_broad_adds(self):
        for c in [
            "git add .", "git add -A", "git add --all", "git add -Av", "git add -fA", "git add ./", "git add :/",
            "git -C . add .", "git add -A -- ", "cd code && git add .", "ls && git add -A", "git add -v .",
            "git add a.py .", "sudo git add .",
        ]:
            code, msg = self.check(c)
            self.assertEqual(code, 2, c)
            self.assertIn("git add", msg)

    def test_message_suggests_naming_files(self):
        self.assertIn("name the files", self.check("git add .")[1])

    def test_allows_explicit_paths(self):
        for c in [
            "git add code/main.py", "git add code/ghost/*.py", "git add -p", "git add -u", "git add .gitignore",
            "git add .claude/hooks/guard.py", "git add ./code/main.py", "git add code/.", "git status", "git commit -m 'git add .'",
            "echo 'git add .'", "grep 'git add -A' README.md", "git add -n code/main.py", "git add --dry-run code/a.py",
        ]:
            self.assertEqual(self.check(c)[0], 0, c)

    def test_other_tools_pass(self):
        self.assertEqual(guard.git_add_guard(tool("Write", "git add ."), ROOT)[0], 0)


class GitDestructiveGuardTests(unittest.TestCase):
    def check(self, command):
        return guard.git_destructive_guard(bash(command), ROOT)

    def test_blocks_force_push(self):
        for c in [
            "git push --force", "git push -f", "git push -f origin feature/agent", "git push origin main --force",
            "git push -fu origin x", "git push origin +feature/agent", "git -C . push --force", "ls && git push -f",
            "git push --force origin HEAD", "git push origin HEAD --force",
        ]:
            code, msg = self.check(c)
            self.assertEqual(code, 2, c)
            self.assertIn("BLOCKED", msg)

    def test_blocks_reset_hard(self):
        for c in ["git reset --hard", "git reset --hard HEAD~1", "git reset HEAD~1 --hard", "git -C . reset --hard origin/main"]:
            self.assertEqual(self.check(c)[0], 2, c)

    def test_blocks_forced_clean(self):
        for c in ["git clean -f", "git clean -fd", "git clean -fdx", "git clean -df", "git clean --force", "git clean -xf -e keep"]:
            self.assertEqual(self.check(c)[0], 2, c)

    def test_allows_safe_variants(self):
        for c in [
            "git push", "git push origin feature/agent", "git push -u origin feature/agent", "git push --force-with-lease",
            "git push --dry-run -f", "git reset", "git reset --soft HEAD~1", "git reset --mixed HEAD~1", "git reset HEAD file.py",
            "git clean -n", "git clean -nd", "git clean --dry-run", "git status", "git commit -m 'git push --force'",
            "echo 'git reset --hard'", "grep -n 'clean -f' README.md", "git fetch --force",
        ]:
            self.assertEqual(self.check(c)[0], 0, c)

    def test_message_names_the_command(self):
        self.assertIn("git reset --hard", self.check("git reset --hard")[1])
        self.assertIn("git push --force", self.check("git push -f")[1])
        self.assertIn("git clean -f", self.check("git clean -fd")[1])


class BranchGateTests(unittest.TestCase):
    def gate(self, command, branch):
        seen = []

        def current_branch(where):
            seen.append(where)
            return branch

        code, msg = guard.branch_gate(bash(command), ROOT, current_branch=current_branch)
        return code, msg, seen

    def test_blocks_commit_on_main_and_master(self):
        for b in ("main", "master"):
            code, msg, _ = self.gate("git commit -m x", b)
            self.assertEqual(code, 2, b)
            self.assertIn(b, msg)
            self.assertIn("git switch -c", msg)

    def test_allows_feature_branches(self):
        for b in ("feature/agent", "main-fix", "maintenance", "fix/master-plan", "mainline"):
            self.assertEqual(self.gate("git commit -m x", b)[0], 0, b)

    def test_detached_head_or_unknown_branch_allowed(self):
        self.assertEqual(self.gate("git commit -m x", None)[0], 0)
        self.assertEqual(self.gate("git commit -m x", "")[0], 0)

    def test_non_commit_never_looks_up_branch(self):
        code, _, seen = self.gate("git status", "main")
        self.assertEqual(code, 0)
        self.assertEqual(seen, [])

    def test_dry_run_and_quoted_mentions_allowed(self):
        self.assertEqual(self.gate("git commit --dry-run", "main")[0], 0)
        self.assertEqual(self.gate("echo 'git commit'", "main")[0], 0)

    def test_chained_and_amend_and_all_blocked(self):
        for c in ["git add a.py && git commit -m x", "git commit --amend --no-edit", "git commit -am x", "git -C . commit -m x"]:
            self.assertEqual(self.gate(c, "main")[0], 2, c)

    def test_other_tools_pass(self):
        code, _ = guard.branch_gate(tool("Write", "x"), ROOT, current_branch=lambda w: "main")
        self.assertEqual(code, 0)

    def test_lookup_uses_the_session_cwd_not_the_project_root(self):
        seen = []
        guard.branch_gate(bash("git commit -m x", cwd="/elsewhere/worktree"), ROOT, current_branch=lambda w: seen.append(w))
        self.assertEqual(seen, ["/elsewhere/worktree"])

    def test_dash_c_directory_is_used_for_lookup(self):
        _, _, seen = self.gate("git -C other commit -m x", "main")
        self.assertTrue(seen[0].endswith("other"))


class BranchGateGitIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def gate(self):
        return guard.branch_gate(bash("git commit -m x", cwd=str(self.repo)), str(self.repo))

    def test_real_repo_on_main_then_feature(self):
        self.git("init", "-q", "-b", "main")
        self.assertEqual(self.gate()[0], 2)
        self.git("switch", "-q", "-c", "feature/x")
        self.assertEqual(self.gate()[0], 0)

    def test_not_a_git_repo_is_allowed(self):
        self.assertEqual(self.gate()[0], 0)


class NewSubcommandMainTests(unittest.TestCase):
    def run_main(self, sub, event):
        real_err, sys.stderr = sys.stderr, io.StringIO()
        old = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = ROOT
        try:
            code = guard.main([sub], stdin=io.StringIO(json.dumps(event)))
            return code, sys.stderr.getvalue()
        finally:
            sys.stderr = real_err
            if old is None:
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
            else:
                os.environ["CLAUDE_PROJECT_DIR"] = old

    def test_each_subcommand_is_wired(self):
        for sub, event in [
            ("env-guard", tool("Read", "/proj/.env")),
            ("git-add", bash("git add .")),
            ("git-destructive", bash("git reset --hard")),
        ]:
            code, err = self.run_main(sub, event)
            self.assertEqual(code, 2, sub)
            self.assertIn("BLOCKED", err)

    def test_branch_gate_subcommand_is_registered(self):
        code, _ = self.run_main("branch-gate", bash("ls"))
        self.assertEqual(code, 0)


class SettingsRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.hooks = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())["hooks"]["PreToolUse"]

    def matcher_for(self, sub):
        found = [h["matcher"] for h in self.hooks for c in h["hooks"] if c["command"].rstrip().endswith(" " + sub)]
        self.assertEqual(len(found), 1, f"{sub} must be registered exactly once")
        return found[0]

    def test_env_guard_matcher(self):
        self.assertEqual(self.matcher_for("env-guard"), "Read|Edit|Write|MultiEdit|Bash")

    def test_git_guards_are_bash_only(self):
        for sub in ("git-add", "git-destructive", "branch-gate"):
            self.assertEqual(self.matcher_for(sub), "Bash", sub)

    def test_existing_hooks_still_registered(self):
        self.assertEqual(self.matcher_for("commit-gate"), "Bash")
        self.assertEqual(self.matcher_for("dataset"), "Write|Edit|MultiEdit|Bash")


class GitignoreTests(unittest.TestCase):
    def test_local_settings_are_ignored(self):
        # read the repo's own .gitignore: `git check-ignore` would also honour a personal global ignore
        lines = {l.strip() for l in (REPO_ROOT / ".gitignore").read_text().splitlines()}
        self.assertTrue(
            lines & {".claude/settings.local.json", "/.claude/settings.local.json", "**/.claude/settings.local.json"},
            ".claude/settings.local.json must be listed in the repo's .gitignore",
        )


if __name__ == "__main__":
    unittest.main()
