# IJCAD の印刷の見守り(print_support.print_dwg_for_pdf)と、ハング時の製番の失敗扱い(pipeline.product_flow)のテスト。
# 背景: bs_pkg の print_dwg_for_pdf は、待ち時間が満了しても IJCAD を終了させず、応答しない IJCAD が図面を開いたまま残った(A1AY4693)。
# 一方、通常の実行でも IJCAD は待ち満了(約 35 秒)を超えて生きていた。そこで「生きている」ことでは判定せず、
# プロセスの終了・期待する PDF の有効性・IJCAD の応答状態を区別する(判定は print_support のコメント)。
# すべてモック・一時フォルダ(IJCAD・実図面・共有先・本番出力・タスクスケジューラ・製番処理には接続しない。実プロセスの終了は行わない。時計も偽物)。
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import print_support as ps  # noqa: E402
from pipeline import product_flow as pf  # noqa: E402
from interim import runs as interim_runs  # noqa: E402
from tests.test_step2_artifacts import Base, PRODUCT  # noqa: E402

CAD = r"C:\Fake IJCAD\gcad.exe"
IDENTITY = (132_000_000_000_000_000, "gcad.exe")
PAGES = ["mk28", "mk29", "mk30", "mk31", "mk32"]
SHEETS = len(PAGES)
PLANNED = 15 + SHEETS * 4                                      # 従来の待ち時間(起動時間 15 + 1 枚 4 秒 × 5)= 35 秒
PACE = PLANNED + SHEETS * ps.PRINT_SETTLE_PER_SHEET            # 従来の「待ち + 呼び出し側の待機」= 55 秒。PDF が揃っても、ここまでは IJCAD の終了を待つ
LIMIT = max(PACE, PLANNED + ps.HANG_CONFIRM_SECONDS + ps.POLL_SECONDS)   # 監視の最長時間 = 55 秒(5 枚)


class FakeConfigLoader:
    @staticmethod
    def convert_win_path(path):
        return path if " " not in path else '"' + path + '"'


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeProc:
    """subprocess.Popen の偽物。wait のたびに偽の時計を進め、その時刻の出来事(PDF の出力・終了)を起こす。"""
    def __init__(self, pid, clock, events, on_tick=None, exit_at=None, exits_on_close=False, exits_on_kill=True):
        self.pid, self.clock, self.events, self.on_tick = pid, clock, events, on_tick
        self.exit_at, self.exits_on_close, self.exits_on_kill = exit_at, exits_on_close, exits_on_kill
        self.returncode, self.closed, self.killed = None, False, False

    def wait(self, timeout=None):
        self.clock.t += timeout
        self.events.append(("wait", self.pid, timeout, self.clock.t))
        if self.on_tick:
            self.on_tick(self.clock.t)
        if (self.exit_at is not None and self.clock.t >= self.exit_at) or (self.closed and self.exits_on_close) or (self.killed and self.exits_on_kill):
            self.returncode = 0
            return 0
        raise subprocess.TimeoutExpired("gcad", timeout)

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.events.append(("kill", self.pid, self.clock.t))


class FakeControl:
    """プロセスの照合・応答状態・通常終了の要求の偽物。responding は時刻の関数(True / False / None)。"""
    def __init__(self, identities, events, clock, responding=lambda t: True):
        self.identities, self.events, self.clock, self.resp = list(identities), events, clock, responding
        self.proc = None

    def identity(self, pid):
        self.events.append(("identity", pid))
        return self.identities.pop(0) if len(self.identities) > 1 else self.identities[0]

    def responding(self, pid):
        self.events.append(("responding", pid, self.clock.t))
        return self.resp(self.clock.t)

    def request_close(self, pid):
        self.events.append(("request_close", pid, self.clock.t))
        self.proc.closed = True
        return 2


class PrintWatchTest(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="ijcad_timeout_")
        self.addCleanup(td.cleanup)
        self.dir = Path(td.name).resolve()
        self.pdfs = [self.dir / f"{p}.pdf" for p in PAGES]

    COMPLETE = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\nstartxref\n0\n%%EOF\n"
    PARTIAL = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n"                 # 書きかけ(末尾の xref・trailer・%%EOF が無い)

    def write(self, n, partial_last=False):
        for i, p in enumerate(self.pdfs[:n]):
            p.write_bytes(self.PARTIAL if (partial_last and i == n - 1) else self.COMPLETE)

    def run_print(self, schedule=None, exit_at=None, responding=lambda t: True, identities=(IDENTITY,), pid=4242,
                  exits_on_close=False, exits_on_kill=True, popen_error=None, expected=True, pdf_valid=None,
                  sheets=SHEETS, partial_window=None, exit_flag_at=None):
        """schedule: {経過秒: その時点までに出力済みの PDF 枚数}。partial_window: (開始, 終了)の間は、最後の PDF が書きかけ(末尾なし)。
        exit_flag_at: この時刻に、wait の結果とは無関係にプロセスが終了済みになる(終了処理の直前の競合)。"""
        self.events, self.logs, self.clock = [], [], Clock()
        schedule = schedule or {}

        def on_tick(t):
            done = [n for at, n in sorted(schedule.items()) if t >= at]
            if done:
                self.write(done[-1], partial_last=bool(partial_window and partial_window[0] <= t < partial_window[1]))
            if exit_flag_at is not None and t >= exit_flag_at:
                self.control.proc.returncode = 0

        def popen(command):
            self.events.append(("popen", command))
            if popen_error:
                raise popen_error
            proc = FakeProc(pid, self.clock, self.events, on_tick, exit_at, exits_on_close, exits_on_kill)
            self.control.proc = proc
            return proc
        self.control = FakeControl(identities, self.events, self.clock, responding)
        kwargs = {"pdf_valid": pdf_valid} if pdf_valid else {}
        with mock.patch.object(ps, "_load_ijcad_settings", return_value=(CAD, 15, 4, FakeConfigLoader)):
            return ps.print_dwg_for_pdf(str(self.dir / "WIRING.SCR"), sheets, [str(p) for p in self.pdfs[:sheets]] if expected else [],
                                        log=self.logs.append, popen=popen, control=self.control, clock=self.clock, **kwargs)

    def kinds(self):
        return [e[0] for e in self.events]

    def times(self, kind):
        return [e[-1] for e in self.events if e[0] == kind]

    # ---- 正常 ----
    def test_process_exits_within_the_time_is_a_success_and_nothing_else_is_touched(self):
        out = self.run_print(exit_at=3)
        self.assertEqual(out, ps.PrintOutcome(ps.COMPLETED, 0))
        self.assertFalse(out.timed_out)
        for kind in ("request_close", "kill", "responding"):
            self.assertNotIn(kind, self.kinds())
        self.assertEqual(self.logs, [])

    def test_alive_process_with_all_valid_pdfs_is_a_success_and_never_terminated(self):
        """生きたままでも(待ち満了より前に PDF が揃っていれば)正常。終了要求・強制終了は一度も呼ばれない。"""
        out = self.run_print(schedule={20: SHEETS})                       # IJCAD は終了しない(exit_at なし)
        self.assertEqual(out.status, ps.PDFS_READY)
        self.assertFalse(out.timed_out)
        for kind in ("request_close", "kill"):
            self.assertNotIn(kind, self.kinds())
        self.assertEqual(self.clock.t, PACE)                              # ペース(従来の待ち + 待機 = 55 秒)までは、IJCAD の終了を待つ(次の IJCAD と重ねない)
        self.assertFalse(out.left_unresponsive)

    def test_all_valid_pdfs_with_an_unresponsive_ijcad_is_a_success_left_running_with_a_warning(self):
        """PDF が完成していれば、IJCAD が応答なしでも製番は失敗にせず、終了もしない。残っていることだけを固定の文言で警告する。"""
        out = self.run_print(schedule={20: SHEETS}, responding=lambda t: False)
        self.assertEqual(out.status, ps.PDFS_READY)
        self.assertTrue(out.left_unresponsive)
        self.assertFalse(out.timed_out)
        for kind in ("request_close", "kill"):
            self.assertNotIn(kind, self.kinds())
        self.assertIn("応答しないまま残っています", "\n".join(self.logs))

    def test_a_responding_or_unknown_leftover_is_not_reported_as_unresponsive(self):
        for responding in (lambda t: True, lambda t: None):
            out = self.run_print(schedule={20: SHEETS}, responding=responding)
            self.assertEqual(out, ps.PrintOutcome(ps.PDFS_READY))

    def test_process_that_exits_after_the_pdfs_are_ready_is_reported_as_completed_at_its_exit(self):
        out = self.run_print(schedule={20: SHEETS}, exit_at=40)
        self.assertEqual(out, ps.PrintOutcome(ps.COMPLETED, 0))
        self.assertEqual(self.clock.t, 40)                                # 終了を待ってから返す(早く返して次の IJCAD と重ねない)

    def test_a_paused_partial_pdf_is_not_complete_even_if_its_size_is_unchanged_for_a_long_time(self):
        """書き込みが一時停止(サイズ不変が長く続く)しても、末尾(%%EOF)が無い間は完成と見なさない。再開して完成すれば、その後に正常。"""
        out = self.run_print(schedule={10: SHEETS}, partial_window=(10, 42))       # 10〜42 秒は最後の PDF が書きかけ
        self.assertEqual(out.status, ps.PDFS_READY)
        self.assertEqual(self.clock.t, PACE)
        self.run_print(schedule={10: SHEETS}, partial_window=(10, 100))             # 最後まで書きかけ
        self.assertNotIn("request_close", self.kinds())                             # 応答中なので終了もしない
        self.assertEqual(self.clock.t, LIMIT)

    def test_a_paused_partial_pdf_with_a_hung_ijcad_is_a_hang_not_a_success(self):
        out = self.run_print(schedule={10: SHEETS}, partial_window=(10, 100), responding=lambda t: False)
        self.assertEqual(out.status, ps.TIMEOUT_KILLED)

    def test_completing_at_the_moment_the_hang_would_be_declared_is_not_a_hang(self):
        """ハング判定の境界(応答なし 15 秒の時刻)に PDF が揃った場合は、揃った方を優先する(終了しない)。"""
        out = self.run_print(schedule={15: SHEETS - 1, int(PLANNED + ps.HANG_CONFIRM_SECONDS): SHEETS}, responding=lambda t: False)
        self.assertEqual(out.status, ps.PDFS_READY)
        for kind in ("request_close", "kill"):
            self.assertNotIn(kind, self.kinds())

    def test_process_exit_just_before_the_termination_is_not_killed(self):
        out = self.run_print(schedule={15: SHEETS - 1}, responding=lambda t: False, exit_flag_at=int(PLANNED + ps.HANG_CONFIRM_SECONDS))
        self.assertTrue(out.timed_out)
        for kind in ("request_close", "kill"):
            self.assertNotIn(kind, self.kinds())

    def test_a_single_sheet_print_can_also_detect_a_hang_within_the_monitoring_time(self):
        """1 枚(共通製作仕様書)でも、予定 + 応答なし 15 秒を確認できる最長時間がある。"""
        out = self.run_print(sheets=1, responding=lambda t: False)
        self.assertEqual(out.status, ps.TIMEOUT_KILLED)
        self.assertGreaterEqual(self.times("request_close")[0], 15 + 4 + ps.HANG_CONFIRM_SECONDS)

    def test_a_responsiveness_probe_error_is_treated_as_unknown_and_never_terminates(self):
        def boom(t):
            raise OSError(str(self.dir) + r"\secret")
        out = self.run_print(schedule={15: SHEETS - 1}, responding=boom)
        self.assertEqual(out.status, ps.PENDING)
        self.assertNotIn("request_close", self.kinds())
        text = "\n".join(self.logs)
        self.assertIn("OSError", text)
        self.assertNotIn("secret", text)

    def test_the_total_time_budget_is_not_longer_than_the_old_flow(self):
        """従来: 待ち 35 → 呼び出し側の待機 20 → 検査の再確認 20 = 75 秒。見守り(最長 55 秒)+ 検査の再確認 20 秒で、同じ上限。"""
        old_total = PLANNED + SHEETS * 4 + pf.ARTIFACT_GRACE_PRINT
        self.assertEqual(LIMIT + pf.ARTIFACT_GRACE_PRINT, old_total)
        self.run_print(schedule={15: SHEETS - 1})
        self.assertEqual(self.clock.t + pf.ARTIFACT_GRACE_PRINT, old_total)

    def test_pdfs_must_stay_unchanged_before_they_count_as_ready(self):
        """書き込み中(サイズが変わっている間)は、揃ったとは見なさない。"""
        sizes = iter(range(100, 1000))

        def growing(path):
            Path(path).write_bytes(b"%PDF-1.4\n" + b"x" * next(sizes))
            return True
        out = self.run_print(schedule={10: SHEETS}, pdf_valid=growing)      # 毎回サイズが変わる → 安定しない
        self.assertEqual(out.status, ps.PENDING)
        self.assertGreaterEqual(self.clock.t, LIMIT)

    def test_launch_command_and_the_monitoring_time_follow_the_existing_flow(self):
        self.run_print(exit_at=3)
        popen = [e for e in self.events if e[0] == "popen"][0]
        self.assertEqual(popen[1], FakeConfigLoader.convert_win_path(CAD) + " /b " + FakeConfigLoader.convert_win_path(str(self.dir / "WIRING.SCR")))       # design_mod と同じ組み立て(/b + スクリプト)
        self.run_print()                                                    # PDF なし・応答中 → 最長まで見守る
        self.assertEqual((self.clock.t, LIMIT), (LIMIT, 55))

    # ---- PDF 不足 + 応答あり/不明 ----
    def test_missing_pdf_but_responding_is_never_terminated_and_returns_pending_after_the_limit(self):
        out = self.run_print(schedule={15: SHEETS - 1}, responding=lambda t: True)
        self.assertEqual(out.status, ps.PENDING)
        self.assertFalse(out.timed_out)                                   # ここでは失敗にしない(続く既存の検査が「成果物が不足」で失敗にする)
        for kind in ("request_close", "kill"):
            self.assertNotIn(kind, self.kinds())
        self.assertGreaterEqual(self.clock.t, LIMIT)                      # 既存の待ち時間いっぱいまで待ってから返す
        self.assertIn("終了しません", "\n".join(self.logs))

    def test_missing_pdf_with_unknown_responsiveness_is_not_terminated(self):
        out = self.run_print(schedule={15: SHEETS - 1}, responding=lambda t: None)
        self.assertEqual(out.status, ps.PENDING)
        self.assertNotIn("request_close", self.kinds())

    def test_responsiveness_is_not_consulted_before_the_planned_time(self):
        self.run_print(schedule={15: SHEETS - 1}, responding=lambda t: False)
        self.assertTrue(all(t >= PLANNED for t in self.times("responding")))

    def test_a_busy_but_working_ijcad_is_not_terminated_while_pdfs_keep_appearing(self):
        """応答なしでも、PDF が増えている間は、重い処理で動いていると見なして終了しない。"""
        out = self.run_print(schedule={30: 1, 38: 2, 44: 3, 50: 4}, responding=lambda t: False)
        self.assertNotIn("request_close", self.kinds())
        self.assertNotIn("kill", self.kinds())
        self.assertEqual(out.status, ps.PENDING)

    def test_a_brief_unresponsive_moment_is_not_a_hang(self):
        out = self.run_print(schedule={15: SHEETS - 1}, responding=lambda t: not (36 <= t < 44))      # 8 秒だけ応答なし
        self.assertEqual(out.status, ps.PENDING)
        self.assertNotIn("request_close", self.kinds())

    # ---- ハング(PDF 不足 + 応答なしが続く) ----
    def test_hang_closes_only_the_launched_process_gracefully_first(self):
        out = self.run_print(schedule={15: SHEETS - 1}, responding=lambda t: False, exits_on_close=True)
        self.assertEqual(out.status, ps.TIMEOUT_CLOSED)
        self.assertTrue(out.timed_out)
        self.assertEqual([e[1] for e in self.events if e[0] == "request_close"], [4242])
        self.assertNotIn("kill", self.kinds())
        self.assertGreaterEqual(self.times("request_close")[0], PLANNED + ps.HANG_CONFIRM_SECONDS - 1)    # 予定を過ぎ、15 秒続いてから

    def test_hang_force_kill_only_after_the_close_grace_expires(self):
        out = self.run_print(schedule={15: SHEETS - 1}, responding=lambda t: False)
        self.assertEqual(out.status, ps.TIMEOUT_KILLED)
        order = [e[0] for e in self.events if e[0] in ("request_close", "kill")]
        self.assertEqual(order, ["request_close", "kill"])
        close_at, kill_at = self.times("request_close")[0], self.times("kill")[0]
        self.assertEqual(kill_at - close_at, ps.CLOSE_GRACE)

    def test_hang_where_kill_does_not_end_the_process_is_reported(self):
        out = self.run_print(schedule={15: SHEETS - 1}, responding=lambda t: False, exits_on_kill=False)
        self.assertEqual(out.status, ps.TIMEOUT_ALIVE)
        self.assertTrue(out.timed_out)

    def test_only_the_launched_pid_is_targeted(self):
        self.run_print(schedule={15: SHEETS - 1}, responding=lambda t: False, pid=777)
        pids = {e[1] for e in self.events if e[0] in ("request_close", "kill", "identity", "responding")}
        self.assertEqual(pids, {777})                                       # 起動した IJCAD の PID だけ(名前で探して終了する処理は無い)

    def test_a_pid_reused_by_another_process_is_not_terminated(self):
        reused = (IDENTITY[0] + 1, "gcad.exe")                              # 作成時刻が違う(PID の再利用)
        out = self.run_print(schedule={15: SHEETS - 1}, responding=lambda t: False, identities=[IDENTITY, reused])
        self.assertEqual(out.status, ps.TIMEOUT_UNVERIFIED)
        for kind in ("request_close", "kill"):
            self.assertNotIn(kind, self.kinds())

    def test_another_executable_is_not_terminated(self):
        out = self.run_print(schedule={15: SHEETS - 1}, responding=lambda t: False, identities=[IDENTITY, (IDENTITY[0], "notepad.exe")])
        self.assertEqual(out.status, ps.TIMEOUT_UNVERIFIED)
        self.assertNotIn("kill", self.kinds())

    def test_unreadable_identity_is_not_terminated(self):
        out = self.run_print(schedule={15: SHEETS - 1}, responding=lambda t: False, identities=[None])
        self.assertEqual(out.status, ps.TIMEOUT_UNVERIFIED)
        self.assertNotIn("request_close", self.kinds())

    def test_identity_lost_after_the_grace_blocks_the_force_kill(self):
        out = self.run_print(schedule={15: SHEETS - 1}, responding=lambda t: False, identities=[IDENTITY, IDENTITY, (IDENTITY[0] + 5, "gcad.exe")])
        self.assertEqual(out.status, ps.TIMEOUT_UNVERIFIED)
        self.assertIn("request_close", self.kinds())
        self.assertNotIn("kill", self.kinds())

    # ---- 起動失敗・非漏えい ----
    def test_launch_failure_is_swallowed_like_before_without_leaking_the_message(self):
        out = self.run_print(popen_error=OSError(str(self.dir) + r"\secret.dwg の起動に失敗"))
        self.assertEqual(out.status, ps.NOT_RUN)
        self.assertFalse(out.timed_out)                                      # 従来どおり。後続の PDF 検査が欠落として失敗にする
        text = "\n".join(self.logs)
        self.assertIn("OSError", text)
        self.assertNotIn("secret", text)
        self.assertNotIn(str(self.dir), text)

    def test_logs_never_contain_paths_page_names_or_process_output(self):
        for kwargs in (dict(schedule={15: SHEETS - 1}, responding=lambda t: False, exits_on_close=True),
                       dict(schedule={15: SHEETS - 1}, responding=lambda t: False),
                       dict(schedule={15: SHEETS - 1}, responding=lambda t: True),
                       dict(schedule={20: SHEETS})):
            self.run_print(**kwargs)
            text = "\n".join(self.logs)
            self.assertTrue(text)
            for forbidden in [str(self.dir), self.dir.name, "WIRING", CAD, *PAGES, ".pdf", "stdout", "stderr"]:
                self.assertNotIn(forbidden, text)

    def test_without_expected_pdfs_no_hang_is_ever_declared(self):
        out = self.run_print(expected=False, responding=lambda t: False)
        self.assertEqual(out.status, ps.PENDING)
        for kind in ("request_close", "kill"):
            self.assertNotIn(kind, self.kinds())

    def test_expected_exe_name_handles_quoted_and_unquoted_commands(self):
        self.assertEqual(ps._expected_exe_name('"C:\\Fake IJCAD\\GCAD.exe" /b "x y.scr"'), "gcad.exe")
        self.assertEqual(ps._expected_exe_name("C:\\Fake IJCAD\\gcad.exe /b x.scr"), "gcad.exe")


class TimeoutReasonTest(unittest.TestCase):
    ALL = [ps.TIMEOUT_CLOSED, ps.TIMEOUT_KILLED, ps.TIMEOUT_UNVERIFIED, ps.TIMEOUT_ALIVE]
    FORMAT = re.compile(r"^\[Step\.2\] (印刷\(PDF\)|共通製作仕様書の印刷)の IJCAD が応答せず PDF が出力されませんでした\(.+\)\(未完了\)$")

    def test_reason_is_a_fixed_format_without_identifying_information(self):
        for status in self.ALL:
            reason = ps.timeout_reason(pf.STAGE_PRINT, ps.PrintOutcome(status))
            self.assertRegex(reason, self.FORMAT)
            self.assertNotIn("/", reason)                                    # 履歴の連結区切り(" / ")と衝突させない
            for forbidden in ["\\", PRODUCT, "mk", "Error", "stderr"]:
                self.assertNotIn(forbidden, reason)

    def test_each_outcome_has_a_distinct_reason(self):
        reasons = {ps.timeout_reason(pf.STAGE_PRINT, ps.PrintOutcome(s)) for s in self.ALL}
        self.assertEqual(len(reasons), 4)

    def test_the_screen_classifies_it_as_its_own_fixed_label(self):
        for status in self.ALL:
            for stage in (pf.STAGE_PRINT, pf.STAGE_SPEC_PRINT):
                label = interim_runs.classify_failure(ps.timeout_reason(stage, ps.PrintOutcome(status)))
                self.assertEqual(label, f"Step.2 の IJCAD が応答せず PDF が出ない({stage})")
        self.assertEqual(interim_runs.classify_failure("[Step.2] 印刷(PDF)の成果物が不足しています: 対象 5 件中 1 件(欠落 1 件・空または不正 0 件)(未完了)"),
                         "Step.2 の成果物が不足(印刷(PDF))")   # 既存の分類は変わらない

    def test_non_hang_outcomes_are_not_timeouts(self):
        for status in (ps.COMPLETED, ps.PDFS_READY, ps.PENDING, ps.NOT_RUN):
            self.assertFalse(ps.PrintOutcome(status).timed_out)


class FlowTest(Base):
    def patches(self):
        """Base の外部処理のモックのうち、印刷の呼び出しだけを、呼び出し結果(self.outcomes を順に返す)に置き換える。
        同じ属性を二重に差し替えると、Base.run_step2 の停止順(開始順)で元に戻らず、他のテストへ漏れるため、置き換える。"""
        results = iter(self.outcomes)
        self.print_calls = []

        def fake_print(*a, **k):
            self.print_calls.append((a, k))
            return next(results)
        base = [p for p in super().patches() if getattr(p, "attribute", None) != "print_dwg_for_pdf"]
        return base + [mock.patch.object(pf.print_support, "print_dwg_for_pdf", side_effect=fake_print)]

    def run_with_print(self, outcomes, runner):
        self.outcomes = outcomes
        return runner()

    def test_hang_fails_the_product_and_nothing_later_runs(self):
        out = ps.PrintOutcome(ps.TIMEOUT_KILLED)
        res = self.run_with_print([out], self.run_step2)
        self.assertEqual((res.status, res.cleanup), (pf.FAILED, True))
        self.assertEqual(res.reason, ps.timeout_reason(pf.STAGE_PRINT, out))
        self.assertEqual(self.names(), ["print_script"])                     # 次の塊・DWG コピー・DXF 変換・推論用画像へ進まない
        self.assertEqual(self.sleeps, [])                                    # 欠落の再確認(待機)にも入らない

    def test_the_expected_pdf_paths_of_the_chunk_are_passed_to_the_watcher(self):
        self.run_with_print([ps.PrintOutcome(ps.COMPLETED, 0)] * 2, self.run_step2)
        first = self.print_calls[0]
        self.assertEqual(first[0][1], 5)
        self.assertEqual(first[0][2], [str(self.dir_product / f"{p}.pdf") for p in self.PRINT_PAGES[:5]])
        self.assertIs(first[1]["pdf_valid"], ps.pdf_is_complete)

    def test_hang_on_the_second_chunk_stops_after_it(self):
        ok = ps.PrintOutcome(ps.COMPLETED, 0)
        res = self.run_with_print([ok, ps.PrintOutcome(ps.TIMEOUT_CLOSED)], self.run_step2)
        self.assertEqual(res.status, pf.FAILED)
        self.assertEqual(self.names(), ["print_script", "print_script"])
        self.assertEqual(res.reason, ps.timeout_reason(pf.STAGE_PRINT, ps.PrintOutcome(ps.TIMEOUT_CLOSED)))

    def test_alive_ijcad_with_all_pdfs_present_proceeds_as_normal(self):
        """プロセスが生きたまま(PDFS_READY)でも、PDF が揃っていれば、従来どおり次へ進む。"""
        ready = ps.PrintOutcome(ps.PDFS_READY)
        self.assertIsNone(self.run_with_print([ready, ready], self.run_step2))
        self.assertEqual(self.names(), ["print_script", "print_script", "copy_dwg", "convert_dwg2dxf", "pdf_to_images", "process_images_from_jpeg"])

    def test_pending_with_a_missing_pdf_fails_through_the_existing_artifact_check_not_the_hang_reason(self):
        """IJCAD が応答中のまま PDF が欠けた(PENDING)場合は、終了せず、既存の検査が「成果物が不足」で失敗にする。"""
        self.skip["print"] = {"12"}
        res = self.run_with_print([ps.PrintOutcome(ps.PENDING)], self.run_step2)
        self.assertRegex(res.reason, r"^\[Step\.2\] 印刷\(PDF\)の成果物が不足しています: 対象 5 件中 1 件")
        self.assertNotIn("応答せず", res.reason)
        self.assertEqual(self.names(), ["print_script"])

    def test_launch_failure_still_falls_through_to_the_pdf_check(self):
        self.skip["print"] = set(self.PRINT_PAGES)
        res = self.run_with_print([ps.PrintOutcome(ps.NOT_RUN)], self.run_step2)
        self.assertRegex(res.reason, r"^\[Step\.2\] 印刷\(PDF\)の成果物が不足しています")

    def test_the_failure_is_cleaned_up_only_for_the_target_product_folder(self):
        sibling = self.output_dir / "A1BN9999"
        sibling.mkdir()
        (sibling / "keep.txt").write_text("keep", encoding="utf-8")
        outside = self.tmp / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        results = {"completed": [], "failed": {}, "skipped": {}}

        def runner():
            started = []
            try:
                for p in self.patches():
                    p.start()
                    started.append(p)
                with mock.patch.object(pf, "process_product", lambda c: pf.step2_inference_images(c)), \
                        mock.patch.object(pf, "_select_pages", return_value=None):
                    pf.run_product(self.ctx(), results)                      # 実の shutil.rmtree(一時フォルダの中だけ)
            finally:
                for p in reversed(started):
                    p.stop()
        self.run_with_print([ps.PrintOutcome(ps.TIMEOUT_KILLED)], runner)
        self.assertEqual(list(results["failed"]), [PRODUCT])
        self.assertEqual(results["failed"][PRODUCT], ps.timeout_reason(pf.STAGE_PRINT, ps.PrintOutcome(ps.TIMEOUT_KILLED)))
        self.assertFalse(self.dir_product.exists())
        self.assertTrue((sibling / "keep.txt").is_file())
        self.assertTrue(outside.is_file())

    def test_history_reason_and_logs_do_not_leak_page_names_or_paths(self):
        res = self.run_with_print([ps.PrintOutcome(ps.TIMEOUT_KILLED)], self.run_step2)
        for forbidden in [str(self.tmp), PRODUCT, *self.PRINT_PAGES, "\\"]:
            self.assertNotIn(forbidden, res.reason)
        warn = " ".join(str(c.args[0]) for c in self.logger.warning.call_args_list)
        self.assertNotIn(str(self.tmp), warn)


class PdfCompleteTest(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="pdf_complete_")
        self.addCleanup(td.cleanup)
        self.dir = Path(td.name)

    def put(self, name, data):
        path = self.dir / name
        path.write_bytes(data)
        return str(path)

    def test_complete_pdf(self):
        self.assertTrue(ps.pdf_is_complete(self.put("a.pdf", PrintWatchTest.COMPLETE)))

    def test_incomplete_or_invalid_files_are_not_complete(self):
        self.assertFalse(ps.pdf_is_complete(str(self.dir / "missing.pdf")))                 # 無い
        self.assertFalse(ps.pdf_is_complete(self.put("empty.pdf", b"")))                    # 空
        self.assertFalse(ps.pdf_is_complete(self.put("partial.pdf", PrintWatchTest.PARTIAL)))   # 書きかけ(末尾なし)
        self.assertFalse(ps.pdf_is_complete(self.put("junk.pdf", b"hello%%EOF\n")))         # 先頭が %PDF- でない
        self.assertFalse(ps.pdf_is_complete(str(self.dir)))                                  # フォルダ

    def test_the_trailer_must_be_near_the_end_of_the_file(self):
        body = b"%PDF-1.4\n%%EOF\n" + b"x" * (ps.PDF_TAIL_BYTES + 100)                      # %%EOF が先頭側にあるだけ(末尾 1 KiB の外)
        self.assertFalse(ps.pdf_is_complete(self.put("early.pdf", body)))
        self.assertTrue(ps.pdf_is_complete(self.put("late.pdf", b"%PDF-1.4\n" + b"x" * 5000 + b"\nstartxref\n0\n%%EOF\r\n")))

    def test_the_default_validator_is_the_trailer_aware_check(self):
        import inspect
        self.assertIs(inspect.signature(ps.print_dwg_for_pdf).parameters["pdf_valid"].default, ps.pdf_is_complete)


class SpecSheetFlowTest(Base):
    """Step.1 の共通製作仕様書の印刷(pf._print_spec_sheet)と、Step.2 の図面印刷の最後の最終名への移動(pf._finalize_spec_sheet)。
    IJCAD には ASCII の固定名(spec_sheet.pdf)を専用の作業フォルダで渡し、最終名 0.pdf へは、完成を確認でき、かつ図面ページの PDF と衝突しないときだけ Python 側で移す。"""
    SPEC_PDF = ps.SPEC_PDF_NAME                       # 最終名(0.pdf)
    WORK_NAME = ps.SPEC_WORK_PDF_NAME                 # IJCAD に渡す作業用の名前(ASCII)
    PAGES = ["10", "11", "12", "13", "14", "15", "16"]

    def setUp(self):
        super().setUp()
        self.spec_dir = self.tmp / "serial" / "AY0001S0"                      # 設計側の共通製作仕様書(1.dwg)。実際の output_print_script がここを読む
        self.spec_dir.mkdir(parents=True)
        (self.spec_dir / "1.dwg").write_bytes(b"dummy")
        self.design = self.tmp / "design"
        self.design.mkdir(exist_ok=True)
        for page in ("0", "1", "2"):
            (self.design / f"{page}.dwg").write_bytes(b"dummy")               # 図面ページ(印刷対象)
        self.dir_product.mkdir(exist_ok=True)
        self.work_dir = self.dir_product / ps.SPEC_WORK_DIR
        self.work_pdf = self.work_dir / self.WORK_NAME
        self.spec_ctx = self.ctx(serial="AY0001", dir_design_serial=str(self.tmp / "serial"))

    # ---- 補助 ----
    def script_lines(self, path):
        return Path(path).read_bytes().decode("shift-jis").replace("\r\n", "\n").split("\n")

    def pdf_targets(self, path):
        return [l for l in self.script_lines(path) if l.lower().endswith(".pdf")]

    def warnings(self):
        return [c.args[0] for c in self.logger.warning.call_args_list]

    def run_spec(self, outcome, on_print=None):
        """print_support.print_dwg_for_pdf だけを偽物にする(output_print_script は実物)。on_print(script, expected) で、IJCAD の出力を真似る。"""
        seen = {}

        def fake_print(script, sheets, expected, **kwargs):
            seen.update(script=script, sheets=sheets, expected=list(expected), kwargs=kwargs, stale=self.work_pdf.exists())
            if on_print:
                on_print(script, expected)
            return outcome
        with mock.patch.object(pf.print_support, "print_dwg_for_pdf", side_effect=fake_print), \
                mock.patch.object(pf.cad_mod, "countdown"):
            return pf._print_spec_sheet(self.spec_ctx, str(self.dir_product)), seen

    def write_like_ijcad(self, script, expected=None):
        """印刷スクリプトの PDF 出力先へ、完成した仕様書の PDF を書く(IJCAD の出力の真似)。"""
        Path(self.pdf_targets(script)[-1]).write_bytes(PrintWatchTest.COMPLETE + b"spec-sheet")

    SPEC_CONTENT = PrintWatchTest.COMPLETE + b"spec-sheet"

    def page_bytes(self, page):
        return PrintWatchTest.COMPLETE + f"page-{page}".encode()

    def run_step2(self, pages):
        """Step.2 の図面印刷(塊ごとの印刷・成果物検査・最後の仕様書の移動)。IJCAD は偽物で、ページ PDF を内容つきで書く。"""
        self.spec_ctx.print_pages = list(pages)
        self.script_calls = []

        def fake_script(chunk, dir_target, dir_product, *a, pdf_name=None, **k):
            self.script_calls.append((list(chunk), pdf_name))
            for page in chunk:
                name = pdf_name(page) if pdf_name else f"{page}.pdf"
                (self.dir_product / name).write_bytes(self.page_bytes(page))
            return "s.scr"
        started = []
        try:
            for patcher in self.patches():
                if getattr(patcher, "attribute", None) not in ("print_dwg_for_pdf", "countdown", "output_print_script"):
                    patcher.start()
                    started.append(patcher)
            with mock.patch.object(pf.print_support, "output_print_script", side_effect=fake_script), \
                    mock.patch.object(pf.print_support, "print_dwg_for_pdf", side_effect=lambda *a, **k: ps.PrintOutcome(ps.COMPLETED, 0)), \
                    mock.patch.object(pf.cad_mod, "countdown"), mock.patch.object(pf, "_select_pages", return_value=None):
                return pf.step2_inference_images(self.spec_ctx)
        finally:
            for patcher in reversed(started):
                patcher.stop()

    def page_file(self, page):
        return self.dir_product / pf.page_pdf_name(self.spec_ctx, page)

    def assert_pages(self, pages):
        for page in pages:
            self.assertEqual(self.page_file(page).read_bytes(), self.page_bytes(page), page)

    def assert_fixed_warning(self, text, part):
        self.assertIn(part, text)
        for forbidden in [str(self.tmp), PRODUCT, "AY0001", ps.SPEC_SCRIPT_NAME, self.WORK_NAME, "ページ「", "\\", "secret"]:
            self.assertNotIn(forbidden, text)

    # ---- 1. 専用のスクリプト・出力先・ASCII の作業用 PDF 名 ----
    def test_the_spec_script_and_output_are_dedicated_and_ascii(self):
        res, seen = self.run_spec(ps.PrintOutcome(ps.PENDING))
        self.assertIsNone(res)
        self.assertEqual(Path(seen["script"]), self.work_dir / ps.SPEC_SCRIPT_NAME)       # 専用の作業フォルダの、専用のスクリプト名
        self.assertNotEqual(ps.SPEC_SCRIPT_NAME.upper(), "WIRING.SCR")
        self.assertEqual([Path(l[5:]).name for l in self.script_lines(seen["script"]) if l.startswith("open ")], ["1.dwg"])
        self.assertEqual([Path(x) for x in self.pdf_targets(seen["script"])], [self.work_pdf])
        self.assertTrue(self.WORK_NAME.isascii() and self.WORK_NAME.lower().endswith(".pdf"))   # IJCAD に渡す名前は ASCII の固定名
        self.assertEqual(seen["expected"], [str(self.work_pdf)])                           # 見守る PDF も作業フォルダのもの
        self.assertEqual(seen["sheets"], 1)
        self.assertEqual([p.name for p in self.dir_product.iterdir() if p.name != "dwg"], [ps.SPEC_WORK_DIR])   # 製番フォルダ直下には、IJCAD が何も書かない

    def test_the_final_names_are_not_page_numbers_and_never_the_plain_0_pdf(self):
        self.assertEqual(self.SPEC_PDF, "0_S0.pdf")                                        # 共通製作仕様書
        self.assertEqual(ps.CHECK_SHEET_PDF_NAME, "0_A1.pdf")                              # 設計作図完了シート
        names = {self.SPEC_PDF, ps.CHECK_SHEET_PDF_NAME}
        self.assertEqual(len(names), 2)
        self.assertFalse(names & {"0.pdf", self.WORK_NAME})
        for name in names:
            self.assertFalse(Path(name).stem.isdigit())                                    # 数値の図面ページ番号として読まれない

    def test_the_page_script_is_unchanged(self):
        """図面ページ(ページ「0」「1」を含む)の印刷スクリプトは、従来どおり WIRING.SCR と `{ページ名}.pdf`(製番フォルダ直下)。"""
        path = ps.output_print_script(["0", "1", "2"], str(self.design), str(self.dir_product))
        self.assertEqual(Path(path), self.dir_product / "WIRING.SCR")
        self.assertEqual([Path(x) for x in self.pdf_targets(path)], [self.dir_product / f"{p}.pdf" for p in ("0", "1", "2")])

    def test_the_two_scripts_do_not_share_a_file_or_a_folder(self):
        _, seen = self.run_spec(ps.PrintOutcome(ps.PENDING))
        page = ps.output_print_script(["0", "1"], str(self.design), str(self.dir_product))
        self.assertNotEqual(Path(seen["script"]), Path(page))
        self.assertNotEqual(Path(seen["script"]).parent, Path(page).parent)
        self.assertNotEqual(Path(self.pdf_targets(seen["script"])[0]).parent, Path(self.pdf_targets(page)[0]).parent)

    def test_the_spec_print_alone_does_not_touch_the_product_folder(self):
        """仕様書は図面ページより先に印刷する。この時点では、仕様書の PDF は作業フォルダにあり、製番フォルダ直下(最終名・ページ PDF の場所)には何も作らない。"""
        res, _ = self.run_spec(ps.PrintOutcome(ps.COMPLETED, 0), on_print=self.write_like_ijcad)
        self.assertIsNone(res)
        self.assertEqual(self.work_pdf.read_bytes(), self.SPEC_CONTENT)
        self.assertFalse((self.dir_product / self.SPEC_PDF).exists())
        self.assertEqual(self.spec_ctx.spec_work_pdf, str(self.work_pdf))
        self.assertTrue(self.spec_ctx.spec_settled)

    # ---- 2. 通常時: 仕様書が 0.pdf になる(ページ「0」が無いとき) ----
    def check_normal(self, status):
        res, _ = self.run_spec(ps.PrintOutcome(status, 0 if status == ps.COMPLETED else None), on_print=self.write_like_ijcad)
        self.assertIsNone(res)
        self.assertIsNone(self.run_step2(self.PAGES))
        self.assertEqual((self.dir_product / self.SPEC_PDF).read_bytes(), self.SPEC_CONTENT)   # 仕様書が 0.pdf
        self.assertFalse(self.work_dir.exists())                                           # 作業フォルダ(スクリプト含む)は片付く
        self.assertIsNone(self.spec_ctx.spec_work_pdf)
        self.assert_pages(self.PAGES)                                                      # 図面ページの PDF は従来どおり
        self.assertNotIn(ps.PDF_STABLE_SECONDS, self.sleeps)                               # 終了済み・安定確認済みなら、追加の待ちを足さない
        self.assertEqual(self.warnings(), [])

    def test_normal_exit_moves_the_spec_pdf_to_0_pdf(self):
        self.check_normal(ps.COMPLETED)

    def test_pdfs_ready_moves_the_spec_pdf_to_0_pdf(self):
        self.check_normal(ps.PDFS_READY)

    # ---- 3. 設計作図完了シート(ページ「0」)は 0_A1.pdf。仕様書の 0_S0.pdf と、図面ページ・通常の 0.pdf は別名 ----
    def test_page_zero_is_written_as_0_A1_and_the_spec_as_0_S0_without_any_conflict(self):
        res, _ = self.run_spec(ps.PrintOutcome(ps.COMPLETED, 0), on_print=self.write_like_ijcad)
        self.assertIsNone(res)
        self.assertIsNone(self.run_step2(["0"] + self.PAGES))
        self.assertEqual((self.dir_product / "0_S0.pdf").read_bytes(), self.SPEC_CONTENT)  # 共通製作仕様書
        self.assertEqual((self.dir_product / "0_A1.pdf").read_bytes(), self.page_bytes("0"))   # 設計作図完了シート
        self.assertFalse((self.dir_product / "0.pdf").exists())                            # 通常の 0.pdf は作られない(別名)
        self.assert_pages(self.PAGES)                                                      # 通常のページは `{ページ}.pdf` のまま
        self.assertEqual(self.warnings(), [])

    def test_only_the_chunk_with_page_zero_gets_an_output_name(self):
        """0 を含む塊だけ pdf_name を渡す。ほかの塊は従来どおり指定なし。"""
        self.assertIsNone(self.run_step2(["0", "1", "2", "3", "4", "10", "11"]))
        (chunk_a, name_a), (chunk_b, name_b) = self.script_calls
        self.assertEqual((chunk_a, chunk_b), (["0", "1", "2", "3", "4"], ["10", "11"]))
        self.assertIsNotNone(name_a)
        self.assertIsNone(name_b)
        self.assertEqual([name_a(p) for p in chunk_a], ["0_A1.pdf", "1.pdf", "2.pdf", "3.pdf", "4.pdf"])

    def test_the_real_page_script_prints_page_zero_to_0_A1(self):
        for page in ("0", "1"):
            (self.design / f"{page}.dwg").write_bytes(b"dummy")
        path = ps.output_print_script(["0", "1"], str(self.design), str(self.dir_product), pdf_name=lambda p: pf.page_pdf_name(self.spec_ctx, p))
        self.assertEqual(Path(path), self.dir_product / "WIRING.SCR")
        self.assertEqual([Path(x) for x in self.pdf_targets(path)], [self.dir_product / "0_A1.pdf", self.dir_product / "1.pdf"])

    def test_page_zero_is_always_0_A1_even_as_an_inference_page(self):
        """設計作図完了シートは、推論対象かどうかにかかわらず、常に 0_A1.pdf。推論用の 0.pdf は別に(完成した 0_A1.pdf のコピーとして)用意する。"""
        self.spec_ctx.baninf_pages = ["0", "1"]
        self.assertEqual(pf.page_pdf_name(self.spec_ctx, "0"), "0_A1.pdf")
        self.assertEqual(pf.page_pdf_name(self.spec_ctx, "1"), "1.pdf")
        self.spec_ctx.baninf_pages = ["1"]
        self.assertEqual(pf.page_pdf_name(self.spec_ctx, "0"), "0_A1.pdf")
        self.assertEqual(pf.page_pdf_name(self.spec_ctx, 0), "0_A1.pdf")
        self.assertEqual(pf.page_pdf_name(self.spec_ctx, "01"), "01.pdf")                  # 先頭ゼロの別のページは対象外

    def test_an_existing_0_pdf_is_left_alone_and_not_mistaken(self):
        """既存の 0.pdf(以前の出力・通常のページ)は、触れない・上書きしない。仕様書・完了シートは別名で出る。"""
        (self.dir_product / "0.pdf").write_bytes(b"legacy page zero")
        self.run_spec(ps.PrintOutcome(ps.COMPLETED, 0), on_print=self.write_like_ijcad)
        self.assertIsNone(self.run_step2(["0"] + self.PAGES))
        self.assertEqual((self.dir_product / "0.pdf").read_bytes(), b"legacy page zero")
        self.assertEqual((self.dir_product / "0_S0.pdf").read_bytes(), self.SPEC_CONTENT)
        self.assertEqual((self.dir_product / "0_A1.pdf").read_bytes(), self.page_bytes("0"))

    def test_an_existing_0_S0_is_never_overwritten(self):
        self.run_spec(ps.PrintOutcome(ps.COMPLETED, 0), on_print=self.write_like_ijcad)
        (self.dir_product / self.SPEC_PDF).write_bytes(b"existing spec")
        self.assertIsNone(self.run_step2(self.PAGES))
        self.assertEqual((self.dir_product / self.SPEC_PDF).read_bytes(), b"existing spec")
        self.assertEqual(self.work_pdf.read_bytes(), self.SPEC_CONTENT)                    # 仕様書は作業フォルダに残る
        self.assertEqual(self.spec_ctx.spec_work_pdf, str(self.work_pdf))
        self.assert_fixed_warning(self.warnings()[-1], "同名のファイルが既にあるため、最終名へ移していません")

    def test_a_0_S0_created_while_waiting_for_stability_is_not_overwritten(self):
        _, seen = self.run_spec(ps.PrintOutcome(ps.PENDING))
        self.write_like_ijcad(seen["script"])

        def appears(_seconds):
            (self.dir_product / self.SPEC_PDF).write_bytes(b"created meanwhile")
        with mock.patch.object(pf, "_artifact_sleep", side_effect=appears):
            pf._finalize_spec_sheet(self.spec_ctx, str(self.dir_product))
        self.assertEqual((self.dir_product / self.SPEC_PDF).read_bytes(), b"created meanwhile")
        self.assertTrue(self.work_pdf.exists())
        self.assert_fixed_warning(self.warnings()[-1], "同名のファイルが既にあるため、最終名へ移していません")

    # ---- 3b. ページ「0」が推論対象: 0_A1.pdf(完了シート)と、推論用の 0.pdf(完成した 0_A1.pdf のコピー)の両方 ----
    def inference_zero(self):
        self.spec_ctx.baninf_pages = ["0", "10", "11", "12"]
        self.INFER_PAGES = ["0", "10", "11", "12"]                                       # 後続の段階(DXF・画像)の偽物も、ページ「0」ぶんの成果物を作る

    def test_inference_page_zero_makes_0_A1_and_the_inference_0_pdf_with_the_same_content(self):
        self.inference_zero()
        self.run_spec(ps.PrintOutcome(ps.COMPLETED, 0), on_print=self.write_like_ijcad)
        self.assertIsNone(self.run_step2(["0"] + self.PAGES))
        check = self.dir_product / "0_A1.pdf"
        infer = self.dir_product / "0.pdf"
        self.assertEqual(check.read_bytes(), self.page_bytes("0"))                         # 設計作図完了シート(成果物)
        self.assertEqual(infer.read_bytes(), self.page_bytes("0"))                         # 推論用の入力(同じ図面のコピー)
        self.assertEqual((self.dir_product / "0_S0.pdf").read_bytes(), self.SPEC_CONTENT)  # 共通製作仕様書は別のまま
        self.assertNotEqual(self.dir_product / "0_S0.pdf", check)
        self.assert_pages(self.PAGES)                                                      # 通常のページは `{ページ}.pdf` のまま
        self.assertFalse((self.dir_product / "0.pdf.part").exists())                       # 一時ファイルは残らない
        self.assertEqual(self.warnings(), [])

    def test_inference_zero_pdf_is_prepared_before_the_later_stages_need_it(self):
        """画像変換(推論)が 0.pdf を開く時点で、0.pdf が既にある。用意できなければ後続へ進まない。"""
        self.inference_zero()
        self.run_spec(ps.PrintOutcome(ps.COMPLETED, 0), on_print=self.write_like_ijcad)
        seen = {}
        original = self.fake_pdf_to_images

        def spy(dir_product, pages):                                                     # 画像変換(bs_pkg)の呼び出しの時点を見る
            seen["zero_exists"] = (Path(dir_product) / "0.pdf").is_file()
            seen["pages"] = list(pages)
            return original(dir_product, pages)
        self.fake_pdf_to_images = spy                                                    # Base の差し替え点(run_step2 が参照する)
        self.assertIsNone(self.run_step2(["0"] + self.PAGES))
        self.assertTrue(seen.get("zero_exists"))
        self.assertIn("0", seen["pages"])

    def test_a_stale_0_A1_blocks_the_print_and_is_never_overwritten_or_used(self):
        self.inference_zero()
        self.run_spec(ps.PrintOutcome(ps.COMPLETED, 0), on_print=self.write_like_ijcad)
        (self.dir_product / "0_A1.pdf").write_bytes(b"stale check sheet")
        res = self.run_step2(["0"] + self.PAGES)
        self.assertEqual((res.status, res.cleanup), (pf.FAILED, True))
        self.assertEqual(res.reason, "[Step.2] 設計作図完了シートの出力先に既存の PDF があるため、印刷しません(未完了)")
        self.assertEqual((self.dir_product / "0_A1.pdf").read_bytes(), b"stale check sheet")   # 既存は変更しない
        self.assertFalse((self.dir_product / "0.pdf").exists())                             # 古い PDF から推論用を作らない
        self.assertEqual(self.script_calls, [])                                             # 印刷(スクリプト作成)に進まない
        for forbidden in [str(self.tmp), PRODUCT, "AY0001", "\\"]:
            self.assertNotIn(forbidden, res.reason)

    def test_a_stale_inference_0_pdf_blocks_the_print_too(self):
        self.inference_zero()
        self.run_spec(ps.PrintOutcome(ps.COMPLETED, 0), on_print=self.write_like_ijcad)
        (self.dir_product / "0.pdf").write_bytes(b"stale inference input")
        res = self.run_step2(["0"] + self.PAGES)
        self.assertEqual(res.status, pf.FAILED)
        self.assertEqual((self.dir_product / "0.pdf").read_bytes(), b"stale inference input")
        self.assertFalse((self.dir_product / "0_A1.pdf").exists())
        self.assertEqual(self.script_calls, [])

    def test_an_existing_plain_0_pdf_does_not_block_when_page_zero_is_not_an_inference_page(self):
        """推論対象でないとき、既存の通常の 0.pdf は、0_A1.pdf とは別のファイルとして触れない(印刷は止まらない)。"""
        (self.dir_product / "0.pdf").write_bytes(b"legacy plain zero")
        self.run_spec(ps.PrintOutcome(ps.COMPLETED, 0), on_print=self.write_like_ijcad)
        self.assertIsNone(self.run_step2(["0"] + self.PAGES))
        self.assertEqual((self.dir_product / "0.pdf").read_bytes(), b"legacy plain zero")
        self.assertEqual((self.dir_product / "0_A1.pdf").read_bytes(), self.page_bytes("0"))

    def test_a_failed_copy_leaves_no_partial_file_and_fails_with_a_fixed_reason(self):
        self.inference_zero()
        self.run_spec(ps.PrintOutcome(ps.COMPLETED, 0), on_print=self.write_like_ijcad)
        with mock.patch.object(pf.os, "rename", side_effect=PermissionError("secret C:/real/path")):
            res = self.run_step2(["0"] + self.PAGES)
        self.assertEqual((res.status, res.cleanup), (pf.FAILED, True))
        self.assertEqual(res.reason, "[Step.2] 推論用の PDF を用意できません(未完了)")
        self.assertFalse((self.dir_product / "0.pdf").exists())
        self.assertFalse((self.dir_product / "0.pdf.part").exists())                        # 不完全なコピーを残さない
        self.assertEqual((self.dir_product / "0_A1.pdf").read_bytes(), self.page_bytes("0"))   # 完了シートは無事
        self.assertTrue(any("PermissionError" in w for w in self.warnings()))               # 型名だけ
        self.assertFalse(any("secret" in w or "real" in w for w in self.warnings()))
        self.assertFalse(any("secret" in res.reason or "real" in res.reason for _ in [0]))

    def test_the_inference_copy_is_not_made_from_an_unfinished_0_A1(self):
        self.inference_zero()
        with mock.patch.object(pf.print_support, "pdf_is_complete", return_value=False):
            res = pf._ensure_inference_zero_pdf(self.spec_ctx)
        self.assertEqual(res.status, pf.FAILED)
        self.assertFalse((self.dir_product / "0.pdf").exists())

    def test_non_inference_page_zero_makes_no_inference_copy(self):
        self.run_spec(ps.PrintOutcome(ps.COMPLETED, 0), on_print=self.write_like_ijcad)
        self.assertIsNone(self.run_step2(["0"] + self.PAGES))
        self.assertTrue((self.dir_product / "0_A1.pdf").is_file())
        self.assertFalse((self.dir_product / "0.pdf").exists())                             # 推論に要らないので、作らない

    def test_without_page_zero_only_the_spec_sheet_is_added(self):
        self.run_spec(ps.PrintOutcome(ps.COMPLETED, 0), on_print=self.write_like_ijcad)
        self.assertIsNone(self.run_step2(self.PAGES))
        self.assertTrue((self.dir_product / "0_S0.pdf").is_file())
        self.assertFalse((self.dir_product / "0_A1.pdf").exists())                         # 無いファイルは作らない・別のファイルで代用しない
        self.assertFalse((self.dir_product / "0.pdf").exists())

    # ---- 4. PENDING: 失敗にせず・終了せず、遅れて完成すれば最後に 0_S0.pdf へ移す。確認できなければ移さず固定の警告 ----
    def test_pending_neither_fails_nor_terminates(self):
        res, _ = self.run_spec(ps.PrintOutcome(ps.PENDING))
        self.assertIsNone(res)                                                             # 従来どおり「あれば焼く」(失敗にしない)
        self.assertFalse(ps.PrintOutcome(ps.PENDING).timed_out)
        self.assertEqual(self.spec_ctx.spec_work_pdf, str(self.work_pdf))
        self.assertFalse(self.spec_ctx.spec_settled)

    def test_a_late_pdf_after_pending_is_moved_at_the_end_without_touching_pages(self):
        _, seen = self.run_spec(ps.PrintOutcome(ps.PENDING))
        self.write_like_ijcad(seen["script"])                                              # 遅れて完成(出力先は作業フォルダ)
        self.assertIsNone(self.run_step2(self.PAGES))
        self.assertEqual((self.dir_product / self.SPEC_PDF).read_bytes(), self.SPEC_CONTENT)
        self.assertIn(ps.PDF_STABLE_SECONDS, self.sleeps)                                  # PENDING は、サイズの安定を確認してから
        self.assert_pages(self.PAGES)
        self.assertFalse(self.work_dir.exists())
        self.assertEqual(self.warnings(), [])

    def prepare_pending(self):
        self.run_spec(ps.PrintOutcome(ps.PENDING))
        for page in self.PAGES:
            (self.dir_product / f"{page}.pdf").write_bytes(self.page_bytes(page))
        self.spec_ctx.print_pages = list(self.PAGES)

    def check_not_moved(self, pdf_exists=True):
        self.assertFalse((self.dir_product / self.SPEC_PDF).exists())
        self.assertEqual(self.work_pdf.exists(), pdf_exists)                               # 出力済みなら、作業フォルダに残す(消さない)
        self.assertEqual(self.spec_ctx.spec_work_pdf, str(self.work_pdf))
        self.assert_pages(self.PAGES)
        self.assert_fixed_warning(self.warnings()[-1], "完成を確認できなかったため、最終名へ移していません")

    def finalize(self):
        pf._finalize_spec_sheet(self.spec_ctx, str(self.dir_product))

    def test_a_pdf_that_never_appears_is_a_fixed_warning_only(self):
        self.prepare_pending()
        self.finalize()
        self.check_not_moved(pdf_exists=False)

    def test_an_unfinished_pdf_is_not_moved(self):
        self.prepare_pending()
        self.work_dir.mkdir(exist_ok=True)
        self.work_pdf.write_bytes(PrintWatchTest.PARTIAL)                                  # 書きかけ(末尾の %%EOF が無い)
        self.finalize()
        self.check_not_moved()

    def test_a_pdf_still_growing_is_not_moved(self):
        self.prepare_pending()
        self.work_dir.mkdir(exist_ok=True)
        self.work_pdf.write_bytes(PrintWatchTest.COMPLETE)

        def grow(_seconds):
            with open(self.work_pdf, "ab") as f:
                f.write(b"\n% more")
        with mock.patch.object(pf, "_artifact_sleep", side_effect=grow):
            self.finalize()
        self.check_not_moved()

    def test_a_pdf_still_held_by_ijcad_is_not_moved_and_the_message_is_not_logged(self):
        self.prepare_pending()
        self.work_dir.mkdir(exist_ok=True)
        self.work_pdf.write_bytes(PrintWatchTest.COMPLETE)
        with mock.patch.object(ps.os, "rename", side_effect=PermissionError("secret C:/real/path")):
            self.finalize()
        self.check_not_moved()
        self.assertTrue(any("PermissionError" in w for w in self.warnings()))              # 型名だけ
        self.assertFalse(any("secret" in w or "real" in w for w in self.warnings()))

    # ---- 5. 回帰: 図面ページの印刷・方針(a)・ハング時の終了条件・待機時間 ----
    def test_the_page_prints_still_use_the_page_names_and_the_original_script(self):
        """Step.2 の塊の印刷(PDF だけのページ・推論対象ページを含む)は、スクリプト名・PDF 名の指定なしで、従来の引数のまま。"""
        scripts = []

        def fake_script(chunk, dir_target, dir_product, *a, **k):
            scripts.append((list(chunk), dir_product, k))
            for page in chunk:
                (self.dir_product / f"{page}.pdf").write_bytes(PrintWatchTest.COMPLETE)
            return "s.scr"
        started = []
        try:
            for patcher in self.patches():
                if getattr(patcher, "attribute", None) not in ("print_dwg_for_pdf", "countdown", "output_print_script"):
                    patcher.start()
                    started.append(patcher)
            results = iter([ps.PrintOutcome(ps.COMPLETED, 0)] * 2)
            with mock.patch.object(pf.print_support, "output_print_script", side_effect=fake_script), \
                    mock.patch.object(pf.print_support, "print_dwg_for_pdf", side_effect=lambda *a, **k: next(results)) as prt, \
                    mock.patch.object(pf.cad_mod, "countdown"), mock.patch.object(pf, "_select_pages", return_value=None):
                pf.step2_inference_images(self.spec_ctx)
        finally:
            for patcher in reversed(started):
                patcher.stop()
        self.assertEqual([s[0] for s in scripts], [self.PRINT_PAGES[:5], self.PRINT_PAGES[5:]])
        self.assertTrue(all(s[2] == {} for s in scripts))                                  # 専用名・PDF 名の指定は、図面ページには渡さない
        self.assertTrue(all(s[1] == str(self.dir_product) for s in scripts))               # 出力先は従来どおり製番フォルダ
        self.assertEqual([c.args[2] for c in prt.call_args_list],
                         [[str(self.dir_product / f"{p}.pdf") for p in chunk] for chunk in (self.PRINT_PAGES[:5], self.PRINT_PAGES[5:])])

    def test_the_print_call_arguments_and_waits_are_unchanged(self):
        _, seen = self.run_spec(ps.PrintOutcome(ps.COMPLETED, 0))
        self.assertEqual(seen["sheets"], 1)                                                 # 1 枚(待ち = 起動時間 + 1 × 1 枚あたり・監視の最長時間も従来どおり)
        self.assertIs(seen["kwargs"]["pdf_valid"], ps.pdf_is_complete)
        self.assertIs(seen["kwargs"]["log"], self.logger.warning)
        self.assertEqual(set(seen["kwargs"]), {"log", "pdf_valid"})                         # close_grace・kill_grace などの時間も既定のまま

    def test_hang_on_the_spec_sheet_still_fails_the_product_with_the_fixed_reason(self):
        out = ps.PrintOutcome(ps.TIMEOUT_KILLED)
        res, _ = self.run_spec(out)
        self.assertEqual((res.status, res.cleanup), (pf.FAILED, True))
        self.assertEqual(res.reason, ps.timeout_reason(pf.STAGE_SPEC_PRINT, out))
        self.assertRegex(res.reason, TimeoutReasonTest.FORMAT)
        for forbidden in [str(self.tmp), PRODUCT, "AY0001", "0.pdf", self.WORK_NAME, ps.SPEC_SCRIPT_NAME, "\\"]:
            self.assertNotIn(forbidden, res.reason)
        self.assertIsNone(self.spec_ctx.spec_work_pdf)                                      # ハング(終了処理済み)では、移動の対象にしない

    def test_an_unresponsive_leftover_after_complete_pdfs_stays_warning_only(self):
        for outcome in (ps.PrintOutcome(ps.PDFS_READY), ps.PrintOutcome(ps.PDFS_READY, left_unresponsive=True)):
            self.assertIsNone(pf.print_timeout_result(pf.STAGE_SPEC_PRINT, outcome))        # 方針(a): 失敗にも終了にもしない

    def test_a_stale_work_pdf_is_removed_before_printing(self):
        """以前の実行の残りがあると、見守りが即座に「完成」と見なす。印刷の前に作業フォルダの PDF だけを消す(図面ページの PDF には触れない)。"""
        self.work_dir.mkdir()
        self.work_pdf.write_bytes(PrintWatchTest.COMPLETE + b"stale")
        (self.dir_product / "1.pdf").write_bytes(self.page_bytes("1"))
        _, seen = self.run_spec(ps.PrintOutcome(ps.PENDING))
        self.assertFalse(seen["stale"])
        self.assertEqual((self.dir_product / "1.pdf").read_bytes(), self.page_bytes("1"))

    def test_no_spec_dwg_skips_the_print_as_before(self):
        (self.spec_dir / "1.dwg").unlink()
        with mock.patch.object(pf.print_support, "print_dwg_for_pdf") as prt:
            self.assertIsNone(pf._print_spec_sheet(self.spec_ctx, str(self.dir_product)))
        prt.assert_not_called()
        self.assertFalse(self.work_dir.exists())
        self.assertIsNone(self.spec_ctx.spec_work_pdf)
        before = len(self.warnings())                                                       # 「見つかりませんでした」の既存の警告だけ
        pf._finalize_spec_sheet(self.spec_ctx, str(self.dir_product))                       # 何も無ければ、何もしない
        self.assertEqual(len(self.warnings()), before)


class FinalizeSpecPdfTest(unittest.TestCase):
    """print_support.finalize_spec_pdf(単体)。"""
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="spec_finalize_")
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.src = self.work / "src.pdf"
        self.dst = self.root / "dst.pdf"
        self.other = self.root / "other.pdf"                                               # 無関係のファイル(触れないこと)
        self.other.write_bytes(b"other")
        self.sleeps = []
        self.logs = []

    def run_finalize(self, **kw):
        return ps.finalize_spec_pdf(str(self.src), str(self.dst), sleep=self.sleeps.append, log=self.logs.append, **kw)

    def test_moves_only_a_complete_stable_pdf(self):
        self.src.write_bytes(PrintWatchTest.COMPLETE)
        self.assertEqual(self.run_finalize(), ps.SPEC_MOVED)
        self.assertEqual(self.dst.read_bytes(), PrintWatchTest.COMPLETE)
        self.assertFalse(self.src.exists())
        self.assertFalse(self.work.exists())                                               # 空になった作業フォルダは片付く
        self.assertEqual(self.other.read_bytes(), b"other")
        self.assertEqual(self.sleeps, [ps.PDF_STABLE_SECONDS])

    def test_no_wait_when_the_stability_is_already_confirmed(self):
        self.src.write_bytes(PrintWatchTest.COMPLETE)
        self.assertEqual(self.run_finalize(stable_seconds=0.0), ps.SPEC_MOVED)
        self.assertEqual(self.sleeps, [])

    def test_missing_and_unfinished_are_left_alone(self):
        self.assertEqual(self.run_finalize(), ps.SPEC_MISSING)
        self.src.write_bytes(PrintWatchTest.PARTIAL)
        self.assertEqual(self.run_finalize(), ps.SPEC_INCOMPLETE)
        self.assertEqual(self.src.read_bytes(), PrintWatchTest.PARTIAL)
        self.assertFalse(self.dst.exists())

    def test_an_existing_destination_is_never_overwritten(self):
        self.src.write_bytes(PrintWatchTest.COMPLETE)
        self.dst.write_bytes(b"existing")
        self.assertEqual(self.run_finalize(), ps.SPEC_PROTECTED)
        self.assertEqual(self.dst.read_bytes(), b"existing")
        self.assertEqual(self.src.read_bytes(), PrintWatchTest.COMPLETE)                  # 仕様書は残る
        self.assertEqual(self.sleeps, [])                                                  # 待たずに判定

    def test_a_destination_created_while_waiting_is_never_overwritten(self):
        self.src.write_bytes(PrintWatchTest.COMPLETE)

        def appears(_seconds):
            self.dst.write_bytes(b"created meanwhile")
        self.assertEqual(ps.finalize_spec_pdf(str(self.src), str(self.dst), sleep=appears, log=self.logs.append), ps.SPEC_PROTECTED)
        self.assertEqual(self.dst.read_bytes(), b"created meanwhile")
        self.assertTrue(self.src.exists())

    def test_the_other_files_in_the_work_folder_keep_the_folder(self):
        self.src.write_bytes(PrintWatchTest.COMPLETE)
        (self.work / "left.txt").write_text("x")
        self.assertEqual(self.run_finalize(), ps.SPEC_MOVED)
        self.assertTrue((self.work / "left.txt").exists())                                 # 空でないフォルダは消さない


class ChunkPacingTest(Base):
    def test_the_chunk_flow_no_longer_waits_the_old_countdown_again(self):
        """従来の「枚数 × 4 秒」の待機は print_support の見守り(ペース)に含めた。呼び出し側で二重に待たない。"""
        countdown = mock.Mock(name="countdown")
        results = iter([ps.PrintOutcome(ps.COMPLETED, 0)] * 2)
        started = []
        try:
            for p in self.patches_without_print():
                p.start()
                started.append(p)
            with mock.patch.object(pf.print_support, "print_dwg_for_pdf", side_effect=lambda *a, **k: next(results)), \
                    mock.patch.object(pf.cad_mod, "countdown", countdown), mock.patch.object(pf, "_select_pages", return_value=None):
                self.assertIsNone(pf.step2_inference_images(self.ctx()))
        finally:
            for p in reversed(started):
                p.stop()
        self.assertNotIn(mock.call(20), countdown.call_args_list)
        self.assertNotIn(mock.call(8), countdown.call_args_list)

    def patches_without_print(self):
        return [p for p in self.patches() if getattr(p, "attribute", None) not in ("print_dwg_for_pdf", "countdown")]

    def test_a_responding_leftover_or_an_unresponsive_leftover_does_not_fail_the_product(self):
        for outcome in (ps.PrintOutcome(ps.PDFS_READY), ps.PrintOutcome(ps.PDFS_READY, left_unresponsive=True)):
            self.assertIsNone(pf.print_timeout_result(pf.STAGE_PRINT, outcome))


if __name__ == "__main__":
    unittest.main()
