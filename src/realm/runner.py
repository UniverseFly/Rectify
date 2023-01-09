import functools
import subprocess
import time
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path
from typing import Iterator, cast

import javalang
import regex as re
from joblib import Parallel, delayed

from . import utils
from .config import MetaConfig, RepairConfig, ValidationConfig
from .d4j import Defects4J
from .lsp import TextFile
from .repair import Repairer
from .report import Report
from .results import (
    AvgFilePatch,
    AvgPatch,
    AvgSynthesisResult,
    BuggyHunk,
    GenerationDatapoint,
    HunkRepairResult,
    Outcome,
    PatchValidationResult,
    RepairResult,
    RepairTransformedResult,
    ValidationDatapoint,
    ValidationResult,
)


@dataclass(frozen=True)
class Runner:
    """A `Report` converter"""

    report: Report

    @staticmethod
    def create(root: Path, config: MetaConfig, repair_config: RepairConfig) -> "Runner":
        root.mkdir()
        print("Metadata will be saved to", root)
        return Runner(Report(root, config, RepairResult({}, repair_config), None, None))

    @staticmethod
    def load(root: Path) -> "Runner":
        print(f"Loading data from {root}")
        return Runner(Report.load(root))

    def repair(self, repairer: Repairer):
        utils.disable_tokenizer_parallel()
        repairer.repair(self.report)

    def transform(self):
        """Incremental data transformation"""
        report = self.report
        assert isinstance(report.repair_result, RepairResult)
        # a_results = report.analysis_result.results
        # for repair_idx, result in enumerate(report.repair_result.results):
        # if repair_idx == len(a_results):
        #     a_results.append(RepairAnalysisResult({}, {}))
        if report.transformed_result is None:
            report.transformed_result = RepairTransformedResult({}, {})
        result_dict = report.transformed_result.result_dict
        all_appeared = report.transformed_result.all_appeared
        for bug_id, files in report.repair_result.result_dict.items():
            patches = result_dict.setdefault(bug_id, [])
            appeared = all_appeared.setdefault(bug_id, set())
            for patch_idx, patch in enumerate(iter_files(files)):
                if patch_idx < len(patches):
                    continue
                if any(
                    hunk.result.hunk is None for file in patch for hunk in file.hunks
                ):
                    is_duplicate = False
                else:
                    concat_hunk_str = "".join(
                        cast(str, hunk_patch.result.hunk)
                        for file_patch in patch
                        for hunk_patch in file_patch.hunks
                    )
                    if concat_hunk_str in appeared:
                        is_duplicate = True
                    else:
                        appeared.add(concat_hunk_str)
                        is_duplicate = False
                patches.append(AvgPatch(patch, is_duplicate))
            result_dict[bug_id] = patches
        # a_results.append(RepairAnalysisResult(result_dict))
        # report.analysis_result = RepairAnalysisResults(a_results)
        report.save()

    def transform_if_not_performed(self):
        if self.report.transformed_result is None:
            print("Doing data transformation first...")
            self.transform()
            print("Done")

    def validate(self, config: ValidationConfig):
        self.transform_if_not_performed()
        """Recoverable validation"""
        bug_pattern = re.compile(config.bug_pattern)
        report = self.report
        d4j = report.get_d4j()
        assert report.transformed_result is not None
        if report.validation_result is None:
            report.validation_result = ValidationResult([], {})
        report.validation_result.validation_configs.append(config)
        val_config_idx = len(report.validation_result.validation_configs) - 1
        transformed = report.transformed_result
        validation_result = report.validation_result
        validation_result_dict = validation_result.result_dict
        transformed_result_dict = transformed.result_dict
        unvalidated_analysis_results: list[list[tuple[str, int, AvgPatch]]] = []
        for bug_id, patches in transformed_result_dict.items():
            if bug_pattern.fullmatch(bug_id) is None:
                continue
            validated_patches = validation_result_dict.setdefault(bug_id, {})
            unvalidated_analysis_results.append([])
            for patch_idx, patch in enumerate(patches):
                if (
                    not patch.is_duplicate
                    and not patch.is_broken
                    and patch_idx not in validated_patches
                ):
                    unvalidated_analysis_results[-1].append((bug_id, patch_idx, patch))
        # Validate n_cores bugs with different bug_ids in parallel
        for zipped_result in zip_longest(*unvalidated_analysis_results):
            zipped_results = filter(lambda r: r is not None, zipped_result)
            for result_batch in utils.chunked(config.n_cores, zipped_results):
                assert len(result_batch) == len(set(r[0] for r in result_batch))
                val_results: list[PatchValidationResult] = Parallel(
                    n_jobs=len(result_batch)
                )(
                    delayed(validate_patch)(d4j, bug_id, avg_patch)
                    for (bug_id, _, avg_patch) in result_batch
                )
                for (bug_id, val_idx, _), val_result in zip(result_batch, val_results):
                    assert bug_id in validation_result_dict
                    assert val_idx not in validation_result_dict[bug_id]
                    validation_result_dict[bug_id][val_idx] = (
                        val_config_idx,
                        val_result,
                    )
                report.save()

    def evaluate_generation(self) -> dict[str, list[GenerationDatapoint]]:
        assert self.report.repair_result is not None
        self.transform_if_not_performed()
        transformed_result = self.report.transformed_result
        assert transformed_result is not None
        transformed_result_dict = transformed_result.result_dict
        result: dict[str, list[GenerationDatapoint]] = {}
        for bug_id, patches in transformed_result_dict.items():
            mapped_generation_datapoints = map(
                lambda patch: GenerationDatapoint(
                    gen_time=patch.total_gen_time,
                    n_total=1,
                    n_unique=utils.binary(lambda patch: not patch.is_duplicate, patch),
                    n_unfinished=utils.binary(
                        lambda patch: not patch.is_unfinished, patch
                    ),
                    n_pruned=utils.binary(lambda patch: not patch.is_pruned, patch),
                ),
                patches,
            )
            datapoints = functools.reduce(
                lambda points, point: points + [points[-1] + point],
                mapped_generation_datapoints,
                [GenerationDatapoint.zero()],
            )
            result[bug_id] = datapoints
        return result


def validate_patch(
    d4j: Defects4J, bug_id: str, patch: AvgPatch
) -> PatchValidationResult:
    start_time = time.perf_counter()
    assert not patch.is_duplicate

    def cost():
        return time.perf_counter() - start_time

    patch_files: list[TextFile] = []
    for patch_file in patch.file_patches:
        patch_text_file = patch_file.compute_patch()
        assert patch_text_file is not None
        assert patch_text_file.path.exists()
        patch_files.append(patch_text_file)
    assert len(set(p.path for p in patch_files)) == 1
    # Checkout the fixed version and then apply patches b/c we do not consider test file changes
    d4j.checkout(bug_id, buggy=False)
    for patch_text_file in patch_files:
        try:
            javalang.parse.parse(patch_text_file.content)
        except (
            javalang.parser.JavaSyntaxError,
            javalang.tokenizer.LexerError,
        ) as e:
            return PatchValidationResult(Outcome.ParseError, cost(), "", str(e))
        except Exception as e:
            # TODO: add an InternalError record in `Report`
            with open("unexpected_exception", "a") as f:
                f.write(str(type(e)))
                f.write("\n")
                f.write(str(e))
        patch_text_file.write()
    comp_success, comp_stdout, comp_stderr = d4j.compile(bug_id)
    if not comp_success:
        return PatchValidationResult(
            Outcome.CompilationError, cost(), comp_stdout, comp_stderr
        )
    try:
        val_success, val_stdout, val_stderr = d4j.test(bug_id, timeout=180)
        return PatchValidationResult(
            Outcome.Success if val_success else Outcome.TestingError,
            cost(),
            comp_stdout + utils.RULE + val_stdout,
            comp_stderr + utils.RULE + val_stderr,
        )
    except subprocess.TimeoutExpired:
        return PatchValidationResult(
            Outcome.TestingError,
            cost(),
            comp_stdout + utils.RULE + "Timeout",
            comp_stderr + utils.RULE + "Timeout",
        )


_AvgResult = tuple[AvgSynthesisResult, BuggyHunk]


def iter_files(
    file_results: list[list[HunkRepairResult]],
) -> Iterator[list[AvgFilePatch]]:
    # For one bug
    # items = list(hunk_dict.items())
    # items.sort(key=lambda kv: kv[0])
    groups: list[list[list[_AvgResult]]] = []
    # TODO: maybe take a look at `itertools.groupby`
    # last_f: int | None = None
    for hunk_results in file_results:
        groups.append([])
        group = groups[-1]
        for hunk_result in hunk_results:
            avg_results = [
                (avg_result, hunk_result.buggy_hunk)
                for tagged_result in hunk_result.results
                for avg_result in tagged_result.synthesis_result_batch.to_average_results()
            ]
            # assert len(avg_results) > 0
            # assert f_idx == len(groups)
            # if last_f is None or f_idx != last_f:
            #     group: list[list[_AvgResult]] = []
            #     groups.append(group)
            # else:
            #     group = groups[-1]
            # assert h_idx == len(group)
            group.append(avg_results)
    for group in groups:
        assert len(group) > 0
        assert len(set(len(data) for data in group)) == 1
        assert len(set(t[1].file.path for data in group for t in data)) == 1

    for file_groups in zip(*(zip(*group) for group in groups)):
        assert len(file_groups) > 0
        file_patches: list[AvgFilePatch] = []
        for file_group in file_groups:
            hunks: list[AvgSynthesisResult] = []
            buggy_hunk_indices: list[tuple[int, int]] = []
            assert len(file_group) > 0
            bug: TextFile | None = None
            for avg_result, buggy_hunk in file_group:
                bug = buggy_hunk.file
                buggy_hunk_indices.append((buggy_hunk.start, buggy_hunk.end))
                hunks.append(avg_result)
            assert bug is not None
            file_patches.append(AvgFilePatch(hunks, bug, buggy_hunk_indices))
        yield file_patches