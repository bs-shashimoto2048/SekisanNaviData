# pipeline.product_flow(S2)のテスト: Step の順序・各 Step の終わり方・結果の記録・失敗時の後始末。
# 外部接続なし: ホスト・設計サーバー・IJCAD・推論 API は mock、フォルダは一時ディレクトリ、shutil.rmtree は記録用モック。
# (全体を通した成功・失敗の流れは test_main_integration / test_output_contract が見ている)
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bs_pkg import image_mod  # noqa: E402
import importlib  # noqa: E402
from pipeline import product_flow as pf  # noqa: E402

# cad_data 配下は窓口(cad_data_process)経由が原則のため、design.csv を作る補助だけ importlib で直接読む(構造テストの直 import 禁止ルールに合わせる)
write_design_csv = importlib.import_module("cad_data.design_page_db").write_design_csv

STEPS = ["check_product", "step1_design_data", "step2_inference_images", "step3_frame_scale",
         "step4_detect_objects", "step5_aggregate"]


class FlowBase(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="product_flow_")
        self.addCleanup(td.cleanup)
        self.tmp = Path(td.name).resolve()
        self.output_dir = self.tmp / "output"
        self.dir_product = self.output_dir / "A1BN0001"
        self.output_dir.mkdir()
        self.logger = mock.Mock(name="logger")
        self.rmtree = mock.Mock(name="shutil.rmtree")  # 実削除しない
        p = mock.patch.object(pf.shutil, "rmtree", self.rmtree)
        p.start()
        self.addCleanup(p.stop)

    def ctx(self, **kw):
        cfg = dict(product="A1BN0001", output_dir=str(self.output_dir), dir_product=str(self.dir_product), logger=self.logger,
                   config_data={"data_extension": {"original_draw": ".png"}, "api": {"dir_venv": "x", "yaml_file": "y"}},
                   target_initial="A1", box_types=("OS",), converter_path="conv", converter_version="v",
                   converter_extension=".dwg", converted_extension=".dxf")
        cfg.update(kw)
        return pf.ProductContext(**cfg)

    def new_results(self):
        return {"completed": [], "failed": {}, "skipped": {}}

    def logged(self, level):
        return [str(c.args[0]) for c in getattr(self.logger, level).call_args_list]


class RecordResultTest(FlowBase):
    def test_each_status_goes_to_its_own_bucket(self):
        r = self.new_results()
        pf.record_result(r, "P1", pf.ProductResult(pf.COMPLETED))
        pf.record_result(r, "P2", pf.ProductResult(pf.FAILED, "理由A"))
        pf.record_result(r, "P3", pf.ProductResult(pf.SKIPPED, "理由B"))
        self.assertEqual(r, {"completed": ["P1"], "failed": {"P2": "理由A"}, "skipped": {"P3": "理由B"}})


class SafeRmtreeTest(FlowBase):
    def test_only_under_output_root(self):
        self.dir_product.mkdir()
        pf.safe_rmtree(str(self.dir_product), str(self.output_dir), ignore_errors=True)
        self.rmtree.assert_called_once_with(str(self.dir_product), ignore_errors=True)

    def test_refuses_root_itself_outside_and_parent_escape(self):
        for target in (self.output_dir, self.tmp, self.output_dir / ".." / "x", Path(self.tmp.anchor)):
            with self.assertRaises(RuntimeError, msg=str(target)):
                pf.safe_rmtree(str(target), str(self.output_dir))
        self.assertEqual(self.rmtree.call_count, 0)


class ProcessOrderTest(FlowBase):
    def patch_steps(self, outcomes):
        calls = []
        for name in STEPS:
            def step(ctx, _n=name):
                calls.append(_n)
                return outcomes.get(_n)
            p = mock.patch.object(pf, name, step)
            p.start()
            self.addCleanup(p.stop)
        return calls

    def test_all_steps_in_order_then_completed(self):
        calls = self.patch_steps({})
        self.assertEqual(pf.process_product(self.ctx()), pf.ProductResult(pf.COMPLETED))
        self.assertEqual(calls, STEPS)

    def test_stops_at_first_result(self):
        stop = pf.ProductResult(pf.FAILED, "x", cleanup=True)
        calls = self.patch_steps({"step3_frame_scale": stop})
        self.assertIs(pf.process_product(self.ctx()), stop)
        self.assertEqual(calls, STEPS[:4])   # step4 以降は実行しない


class RunProductCleanupTest(FlowBase):
    """後始末と結果記録は run_product の 1 か所だけ。種類ごとに、削除の有無・引数・記録内容を固定する。"""

    def run_with(self, process, create_dir=True):
        if create_dir:
            self.dir_product.mkdir()
        results = self.new_results()
        with mock.patch.object(pf, "process_product", process):
            pf.run_product(self.ctx(), results)
        return results

    def test_completed_does_not_delete(self):
        r = self.run_with(lambda c: pf.ProductResult(pf.COMPLETED))
        self.assertEqual(r["completed"], ["A1BN0001"])
        self.assertEqual(self.rmtree.call_count, 0)

    def test_skipped_without_cleanup_keeps_folder(self):
        r = self.run_with(lambda c: pf.ProductResult(pf.SKIPPED, "処理済み"))
        self.assertEqual(r["skipped"], {"A1BN0001": "処理済み"})
        self.assertEqual(self.rmtree.call_count, 0)

    def test_cleanup_result_deletes_quietly_under_output_root(self):
        r = self.run_with(lambda c: pf.ProductResult(pf.FAILED, "図枠サイズを取得できません(未完了)", cleanup=True))
        self.assertEqual(r["failed"], {"A1BN0001": "図枠サイズを取得できません(未完了)"})
        self.rmtree.assert_called_once_with(str(self.dir_product), ignore_errors=True)

    def test_skipped_with_cleanup_deletes_quietly(self):
        r = self.run_with(lambda c: pf.ProductResult(pf.SKIPPED, "図面使用中(未完了)", cleanup=True))
        self.assertEqual(r["skipped"], {"A1BN0001": "図面使用中(未完了)"})
        self.rmtree.assert_called_once_with(str(self.dir_product), ignore_errors=True)

    def test_exception_is_failure_and_deletes_with_logging(self):
        def boom(ctx):
            raise ValueError("想定外")
        r = self.run_with(boom)
        self.assertEqual(r["failed"], {"A1BN0001": "ValueError: 想定外"})
        self.rmtree.assert_called_once_with(str(self.dir_product))   # 例外時は ignore_errors なし(失敗はログに出す)
        self.assertTrue(any("Directory removed due to product error" in m for m in self.logged("info")))
        self.assertTrue(any("Process Failed for product A1BN0001" in m for m in self.logged("error")))

    def test_exception_reason_is_truncated_to_200_chars(self):
        def boom(ctx):
            raise RuntimeError("x" * 500)
        r = self.run_with(boom)
        self.assertEqual(r["failed"]["A1BN0001"], "RuntimeError: " + "x" * 200)

    def test_exception_without_product_folder_deletes_nothing(self):
        def boom(ctx):
            raise ValueError("フォルダ作成前")
        r = self.run_with(boom, create_dir=False)
        self.assertIn("A1BN0001", r["failed"])
        self.assertEqual(self.rmtree.call_count, 0)

    def test_cleanup_error_is_logged_and_does_not_stop(self):
        self.rmtree.side_effect = OSError("削除できない")

        def boom(ctx):
            raise ValueError("x")
        r = self.run_with(boom)
        self.assertIn("A1BN0001", r["failed"])
        self.assertTrue(any("Directory cleanup error" in m for m in self.logged("error")))

    def test_inference_request_error_logs_server_side_failure(self):
        def boom(ctx):
            raise image_mod.InferenceRequestError("API失敗", ["22"], "INF: a\nERR: status=500")
        r = self.run_with(boom)
        self.assertTrue(r["failed"]["A1BN0001"].startswith("InferenceRequestError"))
        self.assertTrue(any("推論APIの失敗内容(製番 A1BN0001 は未完了)" in m for m in self.logged("error")))

    def test_system_exit_is_failure_cleans_partial_output_and_reraises_without_leaking_details(self):
        def boom(ctx):
            raise SystemExit(r"C:\secret\x password=abc")        # sys.exit() 相当(終了コード・メッセージにパス/秘密が入っていても記録しない)
        self.dir_product.mkdir()
        results = self.new_results()
        with mock.patch.object(pf, "process_product", boom):
            with self.assertRaises(SystemExit):                  # 従来どおり終了させる(CLI の挙動は変えない)
                pf.run_product(self.ctx(), results)
        self.assertEqual(results, {"completed": [], "failed": {"A1BN0001": pf.SYSTEM_EXIT_REASON}, "skipped": {}})
        self.rmtree.assert_called_once_with(str(self.dir_product))          # 部分出力は既存の失敗と同じ後始末で削除
        self.assertFalse(any("secret" in m or "password" in m for m in self.logged("error") + self.logged("info")))

    def test_system_exit_never_deletes_outside_output_root(self):
        outside = self.tmp / "other"
        outside.mkdir()
        results = self.new_results()
        with mock.patch.object(pf, "process_product", lambda c: (_ for _ in ()).throw(SystemExit(1))):
            with self.assertRaises(SystemExit):
                pf.run_product(self.ctx(dir_product=str(outside)), results)
        self.assertEqual(self.rmtree.call_count, 0)
        self.assertTrue(outside.is_dir())
        self.assertIn("A1BN0001", results["failed"])
        self.assertTrue(any("Directory cleanup error" in m for m in self.logged("error")))

    def test_system_exit_without_product_folder_deletes_nothing(self):
        results = self.new_results()
        with mock.patch.object(pf, "process_product", lambda c: (_ for _ in ()).throw(SystemExit())):
            with self.assertRaises(SystemExit):
                pf.run_product(self.ctx(), results)
        self.assertEqual(self.rmtree.call_count, 0)
        self.assertIn("A1BN0001", results["failed"])

    def test_folder_outside_output_root_is_never_deleted(self):
        outside = self.tmp / "other"
        outside.mkdir()
        results = self.new_results()
        ctx = self.ctx(dir_product=str(outside))
        with mock.patch.object(pf, "process_product", lambda c: pf.ProductResult(pf.FAILED, "x", cleanup=True)):
            pf.run_product(ctx, results)
        self.assertEqual(self.rmtree.call_count, 0)
        self.assertTrue(outside.is_dir())
        self.assertIn("RuntimeError", results["failed"]["A1BN0001"])   # 範囲外の削除要求は失敗として記録される


class CheckProductTest(FlowBase):
    def check(self, prop, **kw):
        with mock.patch.object(pf.host_mod, "get_property_data", return_value=prop):
            return pf.check_product(self.ctx(**kw))

    def test_blank_host_info_is_skipped(self):
        for prop in (("", "OS2K"), ("S001", ""), (None, None)):
            self.assertEqual(self.check(prop), pf.ProductResult(pf.SKIPPED, "ホスト情報を取得できません"), prop)

    def test_not_target_is_skipped(self):
        self.assertEqual(self.check(("S001", "ZZ1")), pf.ProductResult(pf.SKIPPED, "処理対象外"))
        self.assertEqual(self.check(("S001", "OS2K"), target_initial="B2"), pf.ProductResult(pf.SKIPPED, "処理対象外"))

    def test_already_processed_is_skipped_and_not_cleaned(self):
        self.dir_product.mkdir()
        res = self.check(("S001", "OS2K"))
        self.assertEqual(res, pf.ProductResult(pf.SKIPPED, "処理済み"))
        self.assertFalse(res.cleanup)

    def test_target_continues_and_stores_host_info(self):
        ctx = self.ctx()
        with mock.patch.object(pf.host_mod, "get_property_data", return_value=("S001", "OS2K")):
            self.assertIsNone(pf.check_product(ctx))
        self.assertEqual((ctx.serial, ctx.box_type), ("S001", "OS2K"))


class Step2Test(FlowBase):
    def plan_ctx(self, pages, files, design_ids=None, design_csv=True):
        d = self.tmp / "design"
        d.mkdir()
        for f in files:
            (d / f).write_bytes(b"x")
        # 製番フォルダの design.csv(Step.1 が作る)。既定では、ページ名と図面名のすべてを「ページ番号」として載せる
        self.dir_product.mkdir(exist_ok=True)
        if design_csv:
            ids = list(pages) + [Path(f).stem for f in files] if design_ids is None else design_ids
            rows = [["", "S001", "A1BN0001", str(i), "", f"図名{i}"] + [""] * 18 for i in dict.fromkeys(str(x) for x in ids)]
            write_design_csv(rows, self.dir_product)
        return self.ctx(product_df=pd.DataFrame({"PAGE": pages}), dir_design_product=str(d))

    def test_select_pages_ok_stores_plan(self):
        ctx = self.plan_ctx(["21", "X1"], ["21.dwg", "24.dwg", "X1.dwg"])
        self.assertIsNone(pf._select_pages(ctx))
        self.assertEqual((ctx.baninf_pages, ctx.print_pages), (["21"], ["21", "24"]))
        self.assertEqual(list(ctx.product_df["PAGE"]), ["21"])

    def test_select_pages_missing_drawing_fails_with_cleanup(self):
        res = pf._select_pages(self.plan_ctx(["21", "22"], ["21.dwg"]))
        self.assertEqual((res.status, res.cleanup), (pf.FAILED, True))
        self.assertIn("22", res.reason)

    def test_select_pages_no_inference_pages_skips_with_cleanup(self):
        res = pf._select_pages(self.plan_ctx(["X1"], ["24.dwg"]))
        self.assertEqual(res, pf.ProductResult(pf.SKIPPED, "推論対象ページなし", cleanup=True))

    def test_select_pages_applies_the_design_csv_page_id_condition(self):
        # 6文字以内・先頭がX/xでない・design.csv にある図面だけを印刷し、推論・座標計算にも残さない(大文字小文字は区別せず、先頭ゼロは保持)
        files = ["21.dwg", "ABCDEF.dwg", "ABCDEFG.dwg", "X1.dwg", "01.dwg", "1.dwg", "notlisted.dwg"]
        ctx = self.plan_ctx(["21", "ABCDEFG", "X1", "01"], files, design_ids=["21", "abcdef", "ABCDEFG", "X1", "01"])
        self.assertIsNone(pf._select_pages(ctx))
        self.assertEqual(ctx.print_pages, ["01", "21", "ABCDEF"])
        self.assertEqual(ctx.baninf_pages, ["01", "21"])
        self.assertEqual(list(ctx.product_df["PAGE"]), ["21", "01"])
        excluded = [str(c.args[0]) for c in self.logger.info.call_args_list if "excluded_pages" in str(c.args[0])]
        self.assertTrue(excluded and "ABCDEFG" in excluded[0] and "notlisted" in excluded[0] and "'1'" in excluded[0])

    def test_select_pages_fails_safely_when_design_csv_is_missing(self):
        res = pf._select_pages(self.plan_ctx(["21"], ["21.dwg", "22.dwg"], design_csv=False))
        self.assertEqual((res.status, res.cleanup), (pf.FAILED, True))
        self.assertIn("design.csv", res.reason)

    def test_select_pages_fails_safely_when_design_csv_is_unreadable_or_malformed(self):
        for content in (b"\xff\xfe\x00broken", "blank,整理番号\r\n1,2\r\n".encode("cp932"), b""):
            with self.subTest(content=content[:12]):
                self.setUp()
                ctx = self.plan_ctx(["21"], ["21.dwg"], design_csv=False)
                (self.dir_product / "design.csv").write_bytes(content)
                res = pf._select_pages(ctx)
                self.assertEqual((res.status, res.cleanup), (pf.FAILED, True))
                self.assertEqual((ctx.print_pages, ctx.baninf_pages), ([], []))   # 失敗時は何も対象にしない
    def test_print_in_use_skips_before_printing_that_chunk(self):
        ctx = self.plan_ctx(["21"], ["21.dwg", "22.dwg"])
        ctx.print_pages = ["21", "22"]
        with mock.patch.object(pf.cad_mod, "is_file_in_use", return_value=True), \
                mock.patch.object(pf.print_support, "output_print_script") as out, \
                mock.patch.object(pf.design_mod, "print_dwg_for_pdf") as prt, \
                mock.patch.object(pf.cad_mod, "countdown"):
            res = pf._print_drawings(ctx)
        self.assertEqual(res, pf.ProductResult(pf.SKIPPED, "図面使用中(未完了)", cleanup=True))
        self.assertEqual((out.call_count, prt.call_count), (0, 0))

    def test_print_chunks_of_five_when_not_in_use(self):
        pages = [str(i) for i in range(10, 17)]
        ctx = self.plan_ctx(pages, [f"{p}.dwg" for p in pages])
        ctx.print_pages = pages
        self.dir_product.mkdir(exist_ok=True)

        def fake_script(chunk, *_a, **_k):          # 印刷の代わりに、印刷後に PDF ができている状態にする(印刷直後に成果物を検査するため)
            for p in chunk:
                (self.dir_product / f"{p}.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
            return "s.scr"
        with mock.patch.object(pf.cad_mod, "is_file_in_use", return_value=False), \
                mock.patch.object(pf.print_support, "output_print_script", side_effect=fake_script) as out, \
                mock.patch.object(pf.design_mod, "print_dwg_for_pdf") as prt, \
                mock.patch.object(pf.cad_mod, "countdown"):
            self.assertIsNone(pf._print_drawings(ctx))
        self.assertEqual([c.args[0] for c in out.call_args_list], [pages[:5], pages[5:]])
        self.assertEqual([c.args[1] for c in prt.call_args_list], [5, 2])


class Step3Test(FlowBase):
    def test_missing_frame_on_any_page_fails_after_checking_all_pages(self):
        ctx = self.ctx(baninf_pages=["21", "22"], product_df=pd.DataFrame({"PAGE": ["21", "22"]}))
        with mock.patch.object(pf.cad_mod, "get_frame_size", return_value=(0, 0)) as gfs, \
                mock.patch.object(pf.cad_mod, "export_csv") as exp:
            res = pf.step3_frame_scale(ctx)
        self.assertEqual(res, pf.ProductResult(pf.FAILED, "図枠サイズを取得できません(未完了)", cleanup=True))
        self.assertEqual(gfs.call_count, 2)        # 1 ページ目で止めず、全ページを見てから失敗にする(従来どおり)
        self.assertEqual(exp.call_count, 0)        # product_df.csv は上書きしない


class Step4Test(FlowBase):
    def test_server_manager_checks_shared_workdir_and_passes_timeout(self):
        mgr = mock.Mock(base_url="http://127.0.0.1:1")
        ctx = self.ctx(manager=mgr, request_timeout=77.0, baninf_pages=["21"])
        with mock.patch.object(pf.inference_support, "verify_shared_work_dir") as ver, \
                mock.patch.object(pf.image_mod, "slice_prediction_request", return_value="INF: ok") as req, \
                mock.patch.object(pf.est_mod, "create_detected_csv", return_value=(pd.DataFrame(), "d.csv", [])):
            self.assertIsNone(pf.step4_detect_objects(ctx))
        ver.assert_called_once_with("http://127.0.0.1:1", str(self.dir_product))
        self.assertEqual(req.call_args.kwargs, {"use_proxy": True, "timeout": 77.0})

    def test_without_manager_no_timeout_and_proxy_setting_is_passed(self):
        ctx = self.ctx(baninf_pages=["21"])
        ctx.config_data["api"]["use_proxy"] = False
        with mock.patch.object(pf.inference_support, "verify_shared_work_dir") as ver, \
                mock.patch.object(pf.image_mod, "slice_prediction_request", return_value=None) as req, \
                mock.patch.object(pf.est_mod, "create_detected_csv", return_value=(pd.DataFrame(), "d.csv", [])):
            pf.step4_detect_objects(ctx)
        self.assertEqual(ver.call_count, 0)
        self.assertEqual(req.call_args.kwargs, {"use_proxy": False})

    def test_api_error_propagates_as_exception_for_run_product(self):
        ctx = self.ctx(baninf_pages=["21"])
        with mock.patch.object(pf.image_mod, "slice_prediction_request", side_effect=image_mod.InferenceRequestError("NG", ["21"], "")):
            with self.assertRaises(image_mod.InferenceRequestError):
                pf.step4_detect_objects(ctx)


if __name__ == "__main__":
    unittest.main()
