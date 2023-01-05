from . import utils, generation as gen
from .model import CodeT5ForRealm, CodeT5Large
from .reporters import RepairReporter
from .config import MetaConfig, RepairConfig
from .generation_defs import SynthesisSuccessful
from .jdt_lsp import JdtLspAnalyzer, Message
from .lsp.text import TextFile
from .lsp import spec
from .d4j import Change, Defects4J, Bug
from multiprocessing import Pipe
from multiprocessing.connection import Connection
from typing import cast, List, NamedTuple, Optional, Dict, Tuple, Callable
from pathlib import Path
import time
import numpy
import torch
import random
import regex as re
import shutil

DATA_DIR = Path(".lsp_data")


def wait_until_all_analyzers_free(
    realm_conns: List[Connection],
    max_waiting_time: float = 20,
    free_check_time: float = 1.0,
):
    batch_is_free = [False] * len(realm_conns)
    start_time = time.perf_counter()
    print("Waiting until all analyzers are free...")
    while time.perf_counter() < start_time + max_waiting_time:
        for idx, connection in enumerate(realm_conns):
            if not batch_is_free[idx]:
                connection.send(
                    Message(True, JdtLspAnalyzer.is_free.__name__, free_check_time)
                )

        for idx, connection in enumerate(realm_conns):
            if not batch_is_free[idx]:
                is_free = connection.recv()
                batch_is_free[idx] = is_free
        print("Elapsed:", time.perf_counter() - start_time)
        if all(batch_is_free):
            print("All analyzers are free:", time.perf_counter() - start_time)
            break


def remove_buggy_hunk(text_file: TextFile, change: Change) -> Tuple[str, str]:
    """Modifies `text_file` and returns the prefix and the suffix"""
    start = change.start - 1
    end = start + len(change.removed_lines)
    start_pos = text_file.refine_index(start, 0)
    end_pos = text_file.refine_index(end, 0)

    start_index = text_file.form_index(start, 0)
    end_index = text_file.form_index(end, 0)

    prefix_start = 0
    suffix_end = len(text_file.content)
    prefix = text_file.content[prefix_start:start_index]
    suffix = "\n" + text_file.content[end_index:suffix_end]

    # prefix(\n)
    # insertion(\n)
    # <cursor:infill>
    # (\n)suffix
    text_file.change(
        [
            cast(
                spec.EntireDocumentChange,
                {"text": "\n", "range": {"start": start_pos, "end": end_pos}},
            )
        ]
    )

    text_file.move_cursor(start_index)
    assert prefix.endswith("\n")
    assert text_file.content[text_file.cursor - 1] == "\n"
    assert text_file.content[text_file.cursor] == "\n"

    return prefix, suffix


class Repairer:
    def __init__(
        self,
        config: MetaConfig,
        model: CodeT5ForRealm,
        d4j: Defects4J,
        reporter: RepairReporter,
        active_connection_analyzer_pairs: List[Tuple[Connection, JdtLspAnalyzer]],
    ) -> None:
        self.config = config
        self.model = model
        self.d4j = d4j
        self.reporter = reporter
        self.active_connection_analyzer_pairs = active_connection_analyzer_pairs

    @staticmethod
    def init(config: MetaConfig, report_dir: Path, pre_allocate: bool) -> "Repairer":
        if DATA_DIR.exists():
            shutil.rmtree(DATA_DIR)
        reporter = RepairReporter.create(report_dir, config)
        model = CodeT5Large.init().to(utils.DEVICE)  # type: ignore # noqa
        d4j = Defects4J(config.d4j_home, config.d4j_checkout_root)
        if pre_allocate:
            print("Doing pre-allocation..")
            model.pre_allocate()
            print("Done.")
        return Repairer(config, model, d4j, reporter, [])

    def server_cmd_maker(self) -> list[str]:
        # IMPORTANT: -data dir should be DIFFERENT for different analyzers!!!
        return self.config.language_server_cmd + [
            "-data",
            str(DATA_DIR / str(next(utils.COUNTER))),
        ]

    def fix_seed(self):
        torch.manual_seed(self.config.seed)
        numpy.random.seed(self.config.seed)
        random.seed(self.config.seed)

    def repair(self, config: RepairConfig, hunk_only: bool = True):
        self.fix_seed()
        self.reporter.dump_repair_config(config)
        pattern = re.compile(config.bug_pattern)
        bugs_considered = self.d4j.single_hunk_bugs if hunk_only else self.d4j.all_bugs
        bugs_to_repair = {
            bug_id: bug
            for bug_id, bug in bugs_considered.items()
            if re.fullmatch(pattern, bug_id)
            # Unicode error
            and bug_id != "Lang-25"
        }
        for bug_id, bug in bugs_to_repair.items():
            gen.CHART_11 = bug_id == "Chart-11"
            self.repair_bug(config, bug_id, bug)

    def clean_up(self):
        for connection, _ in self.active_connection_analyzer_pairs:
            connection.send(None)
        for _, analyzer in self.active_connection_analyzer_pairs:
            analyzer.join()
        self.active_connection_analyzer_pairs.clear()

    def repair_bug(self, config: RepairConfig, bug_id: str, bug: Bug):
        try:
            self.repair_bug_no_cleanup(config, bug_id, bug)
        finally:
            self.clean_up()
            print("Cleaned up")

    def repair_bug_no_cleanup(self, config: RepairConfig, bug_id: str, bug: Bug):
        print("Repair", bug_id)
        self.d4j.checkout_buggy(bug_id, bug.proj_path)
        if not config.method.is_plain():
            connection_pairs = cast(
                list[tuple[Connection, Connection]],
                [Pipe(duplex=True) for _ in range(config.batch_size)],
            )
            connection_analyzer_pairs = [
                (
                    client_conn,
                    JdtLspAnalyzer(
                        analyzer_conn,
                        self.server_cmd_maker(),
                        Path(bug.proj_path),
                        self.model,
                        str(self.config.java8_home),
                    ),
                )
                for analyzer_conn, client_conn in connection_pairs
            ]
            connections = [connection for connection, _ in connection_analyzer_pairs]
            for connection, analyzer in connection_analyzer_pairs:
                analyzer.start()
                self.active_connection_analyzer_pairs.append((connection, analyzer))
            for connection in connections:
                connection.send(Message(False, JdtLspAnalyzer.init.__name__))
        else:
            meaning_less = utils.Meaningless
            connection_analyzer_pairs = cast(
                list[tuple[Connection, JdtLspAnalyzer]],
                [(meaning_less, meaning_less)] * config.batch_size,
            )
            connections = cast(list[Connection], [meaning_less] * config.batch_size)

        # Buggy text files
        buggy_text_files = [
            TextFile(Path(bug.proj_path) / buggy_file.path)
            for buggy_file in bug.buggy_files
        ]

        # Initialize each buggy file for LSP
        if not config.method.is_plain():
            assert isinstance(connections, list)
            for connection in connections:
                for buggy_text_file in buggy_text_files:
                    connection.send(
                        Message(False, JdtLspAnalyzer.open.__name__, buggy_text_file)
                    )
            wait_until_all_analyzers_free(connections)

        # Ready to repair
        for buggy_file_idx, buggy_file in enumerate(bug.buggy_files):
            text_file = buggy_text_files[buggy_file_idx].copy()

            print(len(buggy_file.changes))
            print(buggy_file.path)
            print(
                [(c.start, len(c.removed_lines)) for c in reversed(buggy_file.changes)]
            )

            for (change_idx, change) in enumerate(reversed(buggy_file.changes)):
                hunk_id = (buggy_file_idx, change_idx)
                prefix, suffix = remove_buggy_hunk(text_file, change)
                lm_context = gen.LMContext(
                    self.model, prefix, suffix, config.lm_inference_config
                )
                synthesizer = gen.Synthesizer(
                    lm_context, connections, text_file, config.method
                )
                for idx in range(config.n_samples):
                    print("Hunk index:", hunk_id)
                    print("Repair index:", idx)

                    synthesis_result_batch = synthesizer.synthesize()
                    assert len(synthesis_result_batch.results) == config.batch_size
                    for result in synthesis_result_batch.results:
                        print(
                            success.hunk
                            if (success := result.successful_result) is not None
                            else (result.is_pruned_halfway, result.is_unfinished)
                        )
                    print("Time cost:", synthesis_result_batch.time_cost)
                    buggy_file_path = Path(bug.proj_path) / buggy_file.path
                    assert buggy_file_path.exists()
                    self.reporter.add(
                        bug_id, hunk_id, synthesis_result_batch, buggy_file_path
                    )
                    self.reporter.save()
                    # WARNING: Timeout error, if happend, indicates the TIMEOUT_THRESHOULD is too small (unlikely)
                    # or a fatal implementation error!!
                    # except TimeoutError:
                    #     self.reporter.report_timeout_error()
