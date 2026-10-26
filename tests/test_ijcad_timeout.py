# IJCAD の印刷が時間内に終了しないときの、起動プロセスの終了(print_support.print_dwg_for_pdf)と、製番の失敗扱い(pipeline.product_flow)のテスト。
# 背景: bs_pkg の print_dwg_for_pdf は、待ち時間が満了しても IJCAD を終了させず、応答しない IJCAD が図面を開いたまま残った(A1AY4693)。
# すべてモック・一時フォルダ(IJCAD・実図面・共有先・本番出力・タスクスケジューラ・製番処理には接続しない。実プロセスの終了は行わない)。
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import print_support as ps  # noqa: E402
from pipeline import product_flow as pf  # noqa: E402
from interim import runs as interim_runs  # noqa: E402
from tests.test_step2_artifacts import Base, PRODUCT  # noqa: E402

SECRET_DIR = r"C:\Secret Share\A1ZZ9999\drawings"           # ログ・履歴・画面へ出てはいけない(パス・製番・ページ名)
SECRET_PAGES = ["mk32", "zz77"]
CAD = r"C:\Fake IJCAD\gcad.exe"
IDENTITY = (132_000_000_000_000_000, "gcad.exe")


class FakeConfigLoader:
    @staticmethod
    def convert_win_path(path):
        return path if " " not in path else '"' + path + '"'


class FakeProc:
    """subprocess.Popen の偽物。wait の結果を順に指定する(数値 = その終了コードで終了 / "timeout" = 時間切れ)。"""
    def __init__(self, pid, waits, events):
        self.pid, self.waits, self.events = pid, list(waits), events
        self.returncode = None

    def wait(self, timeout=None):
        self.events.append(("wait", self.pid, timeout))
        step = self.waits.pop(0) if self.waits else "timeout"
        if step == "timeout":
            raise subprocess.TimeoutExpired("gcad", timeout)
        self.returncode = step
        return step

    def poll(self):
        return self.returncode

    def kill(self):
        self.events.append(("kill", self.pid))


class FakeControl:
    """プロセスの照合と通常終了の要求の偽物。identity は呼ばれるたびに、指定した値を順に返す(最後の値を使い回す)。"""
    def __init__(self, identities, events):
        self.identities, self.events = list(identities), events

    def identity(self, pid):
        self.events.append(("identity", pid))
        return self.identities.pop(0) if len(self.identities) > 1 else self.identities[0]

    def request_close(self, pid):
        self.events.append(("request_close", pid))
        return 2


class PrintCallTest(unittest.TestCase):
    def call(self, waits, identities=(IDENTITY,), popen_error=None, sheets=5, pid=4242):
        self.events, self.logs = [], []
        procs = []

        def popen(command):
            self.events.append(("popen", command))
            if popen_error:
                raise popen_error
            procs.append(FakeProc(pid, waits, self.events))
            return procs[-1]
        self.proc_holder = procs
        control = FakeControl(identities, self.events)
        with mock.patch.object(ps, "_load_ijcad_settings", return_value=(CAD, 15, 4, FakeConfigLoader)):
            return ps.print_dwg_for_pdf(SECRET_DIR + r"\WIRING.SCR", sheets, log=self.logs.append, popen=popen, control=control,
                                        close_grace=10, kill_grace=5)

    def kinds(self):
        return [e[0] for e in self.events]

    def test_normal_exit_within_the_time_limit_is_a_success_and_nothing_is_terminated(self):
        out = self.call([0])
        self.assertEqual(out, ps.PrintOutcome(ps.COMPLETED, 0))
        self.assertFalse(out.timed_out)
        self.assertNotIn("request_close", self.kinds())
        self.assertNotIn("kill", self.kinds())
        self.assertEqual(self.logs, [])                                      # 通常時は、従来どおり何も記録しない

    def test_launch_command_and_wait_limit_follow_the_existing_flow(self):
        self.call([0], sheets=5)
        popen = [e for e in self.events if e[0] == "popen"][0]
        self.assertEqual(popen[1], '"' + CAD + '"' + " /b " + '"' + SECRET_DIR + r"\WIRING.SCR" + '"')   # design_mod と同じ組み立て(/b + スクリプト)
        wait = [e for e in self.events if e[0] == "wait"][0]
        # 従来の待ち(15 + 5 × 4 = 35) + 従来の流れが印刷後に待つ秒数(5 × 4) + 欠落の再確認(20)
        self.assertEqual(wait[2], 35 + 5 * ps.PRINT_SETTLE_PER_SHEET + ps.PRINT_EXIT_GRACE)

    def test_timeout_then_graceful_close_does_not_kill(self):
        out = self.call(["timeout", 0])                                      # 規定時間は満了 → 通常終了の要求後に終了
        self.assertEqual(out.status, ps.TIMEOUT_CLOSED)
        self.assertTrue(out.timed_out)
        self.assertIn(("request_close", 4242), self.events)
        self.assertNotIn("kill", self.kinds())

    def test_timeout_then_force_kill_only_after_the_grace_expires(self):
        out = self.call(["timeout", "timeout", 1])                           # 規定時間・通常終了の猶予とも満了 → 強制終了 → 終了を確認
        self.assertEqual(out.status, ps.TIMEOUT_KILLED)
        order = [e for e in self.events if e[0] in ("request_close", "kill")]
        self.assertEqual(order, [("request_close", 4242), ("kill", 4242)])   # 通常終了の要求が先、強制終了は後(猶予の後だけ)
        waits = [e[2] for e in self.events if e[0] == "wait"]
        self.assertEqual(waits[1:], [10, 5])                                 # 猶予 10 秒 → 強制終了後の確認 5 秒

    def test_still_alive_after_kill_is_reported_not_hidden(self):
        out = self.call(["timeout", "timeout", "timeout"])
        self.assertEqual(out.status, ps.TIMEOUT_ALIVE)
        self.assertTrue(out.timed_out)

    def test_only_the_launched_process_is_targeted(self):
        out = self.call(["timeout", "timeout", 1], pid=777)
        self.assertEqual(out.status, ps.TIMEOUT_KILLED)
        pids = {e[1] for e in self.events if e[0] in ("request_close", "kill", "identity")}
        self.assertEqual(pids, {777})                                        # 起動した IJCAD の PID だけ(名前で探して終了する処理は無い)

    def test_a_different_process_with_the_same_pid_is_not_terminated(self):
        """PID が別のプロセス(作成時刻が違う)に再利用されていた場合は、終了要求も強制終了もしない。"""
        reused = (IDENTITY[0] + 1, "gcad.exe")
        out = self.call(["timeout"], identities=[IDENTITY, reused])
        self.assertEqual(out.status, ps.TIMEOUT_UNVERIFIED)
        self.assertNotIn("request_close", self.kinds())
        self.assertNotIn("kill", self.kinds())

    def test_a_process_with_another_executable_is_not_terminated(self):
        other = (IDENTITY[0], "notepad.exe")
        out = self.call(["timeout"], identities=[other])
        self.assertEqual(out.status, ps.TIMEOUT_UNVERIFIED)
        self.assertNotIn("kill", self.kinds())

    def test_unreadable_identity_is_not_terminated(self):
        out = self.call(["timeout"], identities=[None])
        self.assertEqual(out.status, ps.TIMEOUT_UNVERIFIED)
        self.assertNotIn("request_close", self.kinds())

    def test_identity_lost_after_the_grace_blocks_the_force_kill(self):
        out = self.call(["timeout", "timeout"], identities=[IDENTITY, IDENTITY, (IDENTITY[0] + 5, "gcad.exe")])
        self.assertEqual(out.status, ps.TIMEOUT_UNVERIFIED)
        self.assertIn("request_close", self.kinds())
        self.assertNotIn("kill", self.kinds())

    def test_launch_failure_is_swallowed_like_before_without_leaking_the_message(self):
        out = self.call([0], popen_error=OSError(SECRET_DIR + r"\secret.dwg の起動に失敗"))
        self.assertEqual(out.status, ps.NOT_RUN)
        self.assertFalse(out.timed_out)                                      # 従来どおり。後続の PDF 検査が欠落として失敗にする
        text = "\n".join(self.logs)
        self.assertIn("OSError", text)
        self.assertNotIn("secret", text)
        self.assertNotIn(SECRET_DIR, text)

    def test_logs_never_contain_paths_page_names_or_process_output(self):
        for waits in (["timeout", 0], ["timeout", "timeout", 1], ["timeout", "timeout", "timeout"]):
            self.call(waits)
            text = "\n".join(self.logs)
            self.assertTrue(text)                                            # 時間切れ・終了要求・結果は記録される
            for forbidden in [SECRET_DIR, "Secret", "A1ZZ9999", "WIRING", CAD, *SECRET_PAGES, "stdout", "stderr"]:
                self.assertNotIn(forbidden, text)
            self.assertRegex(text, r"規定時間\(\d+ 秒\)内に終了しませんでした")

    def test_expected_exe_name_handles_quoted_and_unquoted_commands(self):
        self.assertEqual(ps._expected_exe_name('"C:\\Fake IJCAD\\GCAD.exe" /b "x y.scr"'), "gcad.exe")
        self.assertEqual(ps._expected_exe_name("C:\\Fake IJCAD\\gcad.exe /b x.scr"), "gcad.exe")


class TimeoutReasonTest(unittest.TestCase):
    ALL = [ps.TIMEOUT_CLOSED, ps.TIMEOUT_KILLED, ps.TIMEOUT_UNVERIFIED, ps.TIMEOUT_ALIVE]
    FORMAT = re.compile(r"^\[Step\.2\] (印刷\(PDF\)|共通製作仕様書の印刷)の IJCAD が規定時間内に終了しませんでした\(.+\)\(未完了\)$")

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
                self.assertEqual(label, f"Step.2 の IJCAD が規定時間内に終了しない({stage})")
        self.assertEqual(interim_runs.classify_failure("[Step.2] 印刷(PDF)の成果物が不足しています: 対象 5 件中 1 件(欠落 1 件・空または不正 0 件)(未完了)"),
                         "Step.2 の成果物が不足(印刷(PDF))")   # 既存の分類は変わらない


class FlowTest(Base):
    def patches(self):
        """Base の外部処理のモックのうち、印刷の呼び出しだけを、呼び出し結果(self.outcomes を順に返す)に置き換える。
        同じ属性を二重に差し替えると、Base.run_step2 の停止順(開始順)で元に戻らず、他のテストへ漏れるため、置き換える。"""
        results = iter(self.outcomes)
        base = [p for p in super().patches() if getattr(p, "attribute", None) != "print_dwg_for_pdf"]
        return base + [mock.patch.object(pf.print_support, "print_dwg_for_pdf", side_effect=lambda *a, **k: next(results))]

    def run_with_print(self, outcomes, runner):
        self.outcomes = outcomes
        return runner()

    def test_timeout_fails_the_product_and_nothing_later_runs(self):
        out = ps.PrintOutcome(ps.TIMEOUT_KILLED)
        res = self.run_with_print([out], self.run_step2)
        self.assertEqual((res.status, res.cleanup), (pf.FAILED, True))
        self.assertEqual(res.reason, ps.timeout_reason(pf.STAGE_PRINT, out))
        self.assertEqual(self.names(), ["print_script"])                     # 次の塊・DWG コピー・DXF 変換・推論用画像へ進まない
        self.assertEqual(self.sleeps, [])                                    # 欠落の再確認(待機)にも入らない

    def test_timeout_on_the_second_chunk_stops_after_it(self):
        ok = ps.PrintOutcome(ps.COMPLETED, 0)
        res = self.run_with_print([ok, ps.PrintOutcome(ps.TIMEOUT_CLOSED)], self.run_step2)
        self.assertEqual(res.status, pf.FAILED)
        self.assertEqual(self.names(), ["print_script", "print_script"])
        self.assertEqual(res.reason, ps.timeout_reason(pf.STAGE_PRINT, ps.PrintOutcome(ps.TIMEOUT_CLOSED)))

    def test_no_timeout_proceeds_exactly_as_before(self):
        ok = ps.PrintOutcome(ps.COMPLETED, 0)
        self.assertIsNone(self.run_with_print([ok, ok], self.run_step2))
        self.assertEqual(self.names(), ["print_script", "print_script", "copy_dwg", "convert_dwg2dxf", "pdf_to_images", "process_images_from_jpeg"])

    def test_launch_failure_still_falls_through_to_the_pdf_check(self):
        """起動できなかった(NOT_RUN)場合は従来どおり。PDF が無ければ、成果物の検査が欠落として失敗にする。"""
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
                with mock.patch.object(pf, "process_product", lambda c: pf.step2_inference_images(c)),                         mock.patch.object(pf, "_select_pages", return_value=None):
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
        # 時間切れの失敗では、製番フォルダの実パスをこの段階のログに足さない(印刷の塊のページ一覧は、従来どおりの記録)
        warn = " ".join(str(c.args[0]) for c in self.logger.warning.call_args_list)
        self.assertNotIn(str(self.tmp), warn)


if __name__ == "__main__":
    unittest.main()
