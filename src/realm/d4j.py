import csv
import itertools
import multiprocessing as mp
import os
import subprocess
from functools import partial
from itertools import groupby
from os import PathLike
from pathlib import Path
from typing import Any, Dict, Iterator, NamedTuple, TypeVar

import git
from unidiff import PatchedFile, PatchSet
from unidiff.patch import Line

from realm import utils
from realm.utils import chunked

Metadata = Dict[str, list[Dict[str, str]]]

T = TypeVar("T")


class Change(NamedTuple):
    start: int
    removed_lines: list[str]
    added_lines: list[str]


class BuggyFile(NamedTuple):
    path: str
    changes: list[Change]

    @staticmethod
    def from_patch_file(reversed: bool, patch_file: PatchedFile) -> "BuggyFile":
        changes: list[Change] = []
        lines_iter: Iterator[Line] = (line for hunk in patch_file for line in hunk)

        try:
            last_context = next(lines_iter)
            while True:
                start_line = next(lines_iter)
                if start_line.is_added:
                    assert last_context.is_context
                    added_lines, line_iter = utils.take_while_two(
                        lambda _: True,
                        lambda lhs, rhs: utils.line_consecutive(lhs, rhs),
                        itertools.chain([start_line], lines_iter),
                    )
                    removed_lines: list[Line] = []
                elif start_line.is_removed:
                    assert last_context.is_context
                    removed_lines, line_iter = utils.take_while_two(
                        lambda _: True,
                        lambda lhs, rhs: utils.line_consecutive(lhs, rhs),
                        itertools.chain([start_line], lines_iter),
                    )
                    added_lines, line_iter = utils.take_while_two(
                        lambda elem: elem.is_added,
                        lambda lhs, rhs: utils.line_consecutive(lhs, rhs),
                        line_iter,
                    )
                else:
                    if start_line.is_context:
                        last_context = start_line
                    continue
                if reversed:
                    removed_lines, added_lines = added_lines, removed_lines
                if len(removed_lines) == 0 and len(added_lines) > 0:
                    start = 1 + (
                        last_context.target_line_no
                        if reversed
                        else last_context.source_line_no
                    )
                else:
                    start = (
                        removed_lines[0].target_line_no
                        if reversed
                        else removed_lines[0].source_line_no
                    )
                assert start is not None
                changes.append(
                    Change(
                        start,
                        # Eliminate the '-'/'+'
                        [str(line)[1:] for line in removed_lines],
                        [str(line)[1:] for line in added_lines],
                    )
                )
        except StopIteration:

            def remove_prefix(diff_fname: str) -> str:
                prefixes = ["a/", "b/"]
                prefix = next(filter(diff_fname.startswith, prefixes), "")
                return diff_fname[len(prefix) :]

            return BuggyFile(remove_prefix(patch_file.source_file), changes)


class Bug(NamedTuple):
    buggy_files: list[BuggyFile]
    proj_path: str

    def iter_hunks(self) -> Iterator[tuple[tuple[int, int], BuggyFile, Change]]:
        """Iterate hunks in a reversed way. This is deterministic and each hunk can be assigned an ID"""
        for idx_i, buggy_file in enumerate(self.buggy_files):
            for idx_j, change in enumerate(buggy_file.changes):
                yield ((idx_i, idx_j), buggy_file, change)


BugId = str
BenchmarkMetadata = Dict[BugId, Bug]


class Defects4J:
    def __init__(
        self, d4j_home: Path, d4j_checkout_root: Path, java8_home: Path
    ) -> None:
        self.d4j_home = d4j_home
        self.d4j_checkout_root = d4j_checkout_root
        self.java8_home = java8_home
        assert d4j_home.exists()
        assert self.d4j_executable.exists()
        assert self.java8_home.exists()
        assert d4j_checkout_root.exists()
        self.metadata = self._get_metadata()
        self.all_bugs = self._all_bugs()
        self.single_hunk_bugs = {
            id: bug
            for (id, bug) in self.all_bugs.items()
            if len(bug.buggy_files) == 1 and len(bug.buggy_files[0].changes) == 1
        }
        self.single_line_bugs = {
            id: bug
            for (id, bug) in self.single_hunk_bugs.items()
            if len(bug.buggy_files[0].changes[0].added_lines) == 1
        }

    @staticmethod
    def split_bug_id(bug_id: str) -> tuple[str, str]:
        proj, id_str = bug_id.split("-")
        return proj, id_str

    @staticmethod
    def group_by_project(data_dict: dict[str, T]) -> list[tuple[str, dict[str, T]]]:
        def key_fn(item: tuple[str, Any]) -> str:
            bug_id, _ = item
            return Defects4J.split_bug_id(bug_id)[0]

        data_items = list(data_dict.items())
        data_items.sort(key=key_fn)
        results: list[tuple[str, dict[str, T]]] = []
        for project, group in groupby(data_items, key_fn):
            results.append((project, {bug_id: data for bug_id, data in group}))
        return results

    @staticmethod
    def form_bug_id(proj: str, id_str: str) -> str:
        return proj + "-" + id_str

    @property
    def d4j_executable(self) -> Path:
        return self.d4j_home / "framework" / "bin" / "defects4j"

    def compile(self, bug_id: str) -> tuple[bool, str, str]:
        bug = self.all_bugs[bug_id]
        env = dict(os.environ, JAVA_HOME=str(self.java8_home))
        result = subprocess.run(
            [str(self.d4j_executable), "compile"],
            env=env,
            cwd=bug.proj_path,
            text=True,
            capture_output=True,
        )
        success = result.returncode == 0
        assert "FAIL" not in result.stderr if success else "FAIL" in result.stderr
        return (success, result.stdout, result.stderr)

    def test(self, bug_id: str, timeout: int) -> tuple[bool, str, str]:
        """Test a partial test suite first and then the entire suite"""
        success, stdout, stderr = self.test_with_option(
            bug_id, timeout, entire_test_suite=False
        )
        if not success:
            return success, stdout, stderr
        return self.test_with_option(bug_id, timeout, entire_test_suite=True)

    def test_with_option(
        self, bug_id: str, timeout: int, entire_test_suite: bool
    ) -> tuple[bool, str, str]:
        bug = self.all_bugs[bug_id]
        env = dict(os.environ, JAVA_HOME=str(self.java8_home))
        result = subprocess.run(
            [str(self.d4j_executable), "test"] + ([] if entire_test_suite else ["-r"]),
            env=env,
            cwd=bug.proj_path,
            timeout=timeout,
            text=True,
            capture_output=True,
        )
        failing_tests = Path(bug.proj_path) / "failing_tests"
        assert failing_tests.exists()
        with open(failing_tests) as f:
            success = f.read().strip() == ""

        failing_test_0 = "Failing tests: 0"
        assert (
            result.stdout.startswith(failing_test_0)
            if success
            else not result.stdout.startswith(failing_test_0)
        )
        return success, result.stdout, result.stderr

    def checkout(self, bug_id: str, buggy: bool = True):
        bug_proj_path = self.all_bugs[bug_id].proj_path
        proj, id_str = self.split_bug_id(bug_id)
        repo = git.Repo(bug_proj_path)
        repo.git.execute(["git", "checkout", "HEAD", "-f", "."])
        subprocess.run(
            [
                str(self.d4j_executable),
                "checkout",
                "-p",
                proj,
                f"-v{id_str}{'b' if buggy else 'f'}",
                "-w",
                bug_proj_path,
            ]
        )
        repo.git.execute(["git", "checkout", "HEAD", "-f", "."])
        repo.git.execute(["git", "clean", "-xfd"])
        repo.close()

    def buggy_files(self, bug: dict) -> list[BuggyFile]:
        patch_file = (
            self.d4j_home
            / "framework"
            / "projects"
            / bug["proj"]
            / "patches"
            / f"{bug['bug_id']}.src.patch"
        )
        patch_set = PatchSet.from_filename(patch_file, errors="ignore")
        patch_files: Iterator[PatchedFile] = filter(
            lambda f: f.is_modified_file, patch_set
        )
        return list(map(partial(BuggyFile.from_patch_file, True), patch_files))

    @staticmethod
    def bug_id(bug: dict) -> str:
        return f"{bug['proj']}-{bug['bug_id']}"

    def _all_bugs(self) -> BenchmarkMetadata:
        return {
            self.bug_id(bug): Bug(
                buggy_files=self.buggy_files(bug), proj_path=bug["path"]
            )
            for bug in self.metadata
        }

    def _get_checkout_meta(self, proj: str, bug: Dict[str, str]) -> Dict:
        path = self.d4j_checkout_root / f'{proj}-{bug["bug.id"]}'
        bug_id = bug["bug.id"]
        return {
            "proj": proj,
            "bug_id": bug_id,
            "buggy_commit": bug["revision.id.buggy"],
            "url": bug["report.url"],
            "fixed_commit": bug["revision.id.fixed"],
            "path": str(path.absolute()),
            "cmd": [
                str(self.d4j_executable),
                "checkout",
                "-p",
                proj,
                "-v",
                f"{bug_id}f",
                "-w",
                str(path.absolute()),
            ],
        }

    def _get_all_checkout_meta(self, bugs: Metadata) -> list[Dict[str, str]]:
        return [
            self._get_checkout_meta(proj, bug)
            for proj, proj_bugs in bugs.items()
            for bug in proj_bugs
        ]

    def _get_metadata(self) -> list[Dict[str, str]]:
        all_bugs = self._get_all_bugs()
        data = self._get_all_checkout_meta(all_bugs)
        return data

    def _get_all_bugs(self) -> Metadata:
        def impl():
            proj_dir = self.d4j_home / "framework" / "projects"
            for path_i in proj_dir.iterdir():
                if not path_i.is_dir():
                    continue
                for path_j in path_i.iterdir():
                    if path_j.name == "active-bugs.csv":
                        with open(path_j) as f:
                            dataset = csv.reader(f)
                            keys = next(dataset)
                            kv_list = (zip(keys, values) for values in dataset)
                            bugs = [{k: v for k, v in kv} for kv in kv_list]
                            yield path_i.name, bugs

        return {proj: bugs for proj, bugs in impl()}
