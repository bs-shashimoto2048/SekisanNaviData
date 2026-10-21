# Step.2 の成果物の検査(pipeline.product_flow の check_stage_artifacts と、_print_drawings / _make_inference_images の各段階の直後の検査)のテスト。
# 背景: 印刷(IJCAD)・DXF 変換(ODA)・PDF→JPEG→PNG(bs_pkg.image_mod は例外を握りつぶす)は、成果物ができなくても正常終了することがあり、
# 欠落が Step.3 で初めて FileNotFoundError になっていた(Issue #20)。ここでは、各段階の直後に成果物を検査し、欠けていればその段階で失敗にすることを確かめる。
# すべてモック・一時フォルダ(実 DB・共有データ・CAD・IJCAD・推論 API・タスクスケジューラには接続しない。shutil.rmtree は記録用、または一時フォルダ内だけ)。
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import product_flow as pf  # noqa: E402

PRODUCT = "A1BN0001"
REASON_FORMAT = re.compile(r"^\[Step\.2\] (印刷\(PDF\)|DXF 変換|JPEG 変換|PNG 前処理)の成果物が不足しています: 対象 \d+ 件中 \d+ 件\(欠落 \d+ 件・空または不正 \d+ 件\)\(未完了\)$")


def write_pdf(path):
    Path(path).write_bytes(b"%PDF-1.4\n%%EOF\n")


def write_dxf(path):
    Path(path).write_bytes(b"0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nEOF\n")


def write_image(path, fmt):
    Image.new("RGB", (6, 4), (255, 255, 255)).save(path, format=fmt)


class Base(unittest.TestCase):
    PRINT_PAGES = ["10", "11", "12", "13", "14", "15", "16"]          # 印刷対象(7 枚 → 5 枚 + 2 枚の塊)。推論対象でない PDF だけのページを含む
    INFER_PAGES = ["10", "11", "12"]                                   # 推論対象(DXF・JPEG・PNG が必要なのは、このページだけ)

    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="step2_artifacts_")
        self.addCleanup(td.cleanup)
        self.tmp = Path(td.name).resolve()
        self.output_dir = self.tmp / "output"
        self.dir_product = self.output_dir / PRODUCT
        self.dwg_dir = self.dir_product / "dwg"
        self.dwg_dir.mkdir(parents=True)
        self.logger = mock.Mock(name="logger")
        self.sleeps = []
        for patcher in (mock.patch.object(pf, "_artifact_sleep", side_effect=lambda s: self.sleeps.append(s)),):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.calls = []                                                    # 外部処理の呼び出し順(後続へ進んでいないことの確認用)
        # 各段階が「成果物を作らない」ページ(段階ごとに指定)と、「作るが空/壊れている」ページ
        self.skip = {"print": set(), "dxf": set(), "jpeg": set(), "png": set()}
        self.broken = {"print": set(), "dxf": set(), "jpeg": set(), "png": set()}

    def ctx(self, **kw):
        cfg = dict(product=PRODUCT, output_dir=str(self.output_dir), dir_product=str(self.dir_product), logger=self.logger,
                   config_data={"data_extension": {"original_draw": ".png"}, "api": {"dir_venv": "x", "yaml_file": "y"}},
                   target_initial="A1", box_types=("OS",), converter_path="conv", converter_version="v",
                   converter_extension=".dwg", converted_extension=".dxf",
                   dir_design_product=str(self.tmp / "design"), dwg_copy_dir=str(self.dwg_dir),
                   baninf_pages=list(self.INFER_PAGES), print_pages=list(self.PRINT_PAGES))
        cfg.update(kw)
        return pf.ProductContext(**cfg)

    # ---- 外部処理の偽物(成果物を作る/作らない/壊す) ----
    def make(self, kind, page, writer):
        if page in self.skip[kind]:
            return
        if page in self.broken[kind]:
            (self.dir_product / (page + writer["ext"])).write_bytes(b"")           # 空ファイル
            return
        writer["fn"](self.dir_product / (page + writer["ext"]))

    def fake_print_script(self, chunk, *_a, **_k):
        self.calls.append(("print_script", list(chunk)))
        for p in chunk:
            self.make("print", p, {"ext": ".pdf", "fn": write_pdf})
        return "s.scr"

    def fake_convert(self, *_a, **_k):
        self.calls.append(("convert_dwg2dxf",))
        for p in self.INFER_PAGES:
            self.make("dxf", p, {"ext": ".dxf", "fn": write_dxf})

    def fake_pdf_to_images(self, *_a, **_k):
        self.calls.append(("pdf_to_images",))
        for p in self.INFER_PAGES:
            self.make("jpeg", p, {"ext": ".jpeg", "fn": lambda path: write_image(path, "JPEG")})

    def fake_process(self, *_a, **_k):
        self.calls.append(("process_images_from_jpeg",))
        for p in self.INFER_PAGES:
            self.make("png", p, {"ext": ".png", "fn": lambda path: write_image(path, "PNG")})

    def patches(self):
        return [mock.patch.object(pf.cad_mod, "is_file_in_use", return_value=False),
                mock.patch.object(pf.print_support, "output_print_script", side_effect=self.fake_print_script),
                mock.patch.object(pf.design_mod, "print_dwg_for_pdf"),
                mock.patch.object(pf.cad_mod, "countdown"),
                mock.patch.object(pf.cad_mod, "copy_files_from_list", side_effect=lambda *a, **k: self.calls.append(("copy_dwg",))),
                mock.patch.object(pf.cad_mod, "convert_dwg2dxf", side_effect=self.fake_convert),
                mock.patch.object(pf.image_mod, "pdf_to_images", side_effect=self.fake_pdf_to_images),
                mock.patch.object(pf.image_mod, "process_images_from_jpeg", side_effect=self.fake_process),
                mock.patch.object(pf, "_jpeg_extension", return_value="jpeg")]

    def run_step2(self, ctx=None):
        ctx = ctx or self.ctx()
        started = []
        try:
            for p in self.patches():
                p.start()
                started.append(p)
            with mock.patch.object(pf, "_select_pages", return_value=None):
                return pf.step2_inference_images(ctx)
        finally:
            for p in started:
                p.stop()

    def names(self):
        return [c[0] for c in self.calls]


class SuccessPathTest(Base):
    def test_all_artifacts_present_proceeds_exactly_as_before(self):
        self.assertIsNone(self.run_step2())
        self.assertEqual(self.names(), ["print_script", "print_script", "copy_dwg", "convert_dwg2dxf", "pdf_to_images", "process_images_from_jpeg"])
        self.assertEqual([c[1] for c in self.calls if c[0] == "print_script"], [self.PRINT_PAGES[:5], self.PRINT_PAGES[5:]])
        self.assertEqual(self.sleeps, [])                                   # 揃っていれば待たない

    def test_pdf_only_pages_need_only_the_pdf_not_dxf_jpeg_png(self):
        """推論対象でないページ(PDF だけ作るページ)は、DXF・JPEG・PNG を必須扱いしない。"""
        self.assertIsNone(self.run_step2())
        for p in set(self.PRINT_PAGES) - set(self.INFER_PAGES):
            self.assertTrue((self.dir_product / f"{p}.pdf").is_file())
            for ext in (".dxf", ".jpeg", ".png"):
                self.assertFalse((self.dir_product / f"{p}{ext}").exists(), p + ext)

    def test_stage_checks_use_the_contract_names(self):
        """検査する名前: 印刷 <ページ>.pdf / DXF <ページ><convert_aft> / JPEG <ページ>.<image_mod の拡張子> / PNG <ページ><original_draw>。"""
        seen = []
        real = pf.check_stage_artifacts

        def spy(ctx, stage, pages, name_of, valid, grace=0):
            seen.append((stage, [name_of(p) for p in pages], grace))
            return real(ctx, stage, pages, name_of, valid, grace)
        with mock.patch.object(pf, "check_stage_artifacts", side_effect=spy):
            self.assertIsNone(self.run_step2(self.ctx(converted_extension=".dxf")))
        by_stage = {s: (n, g) for s, n, g in seen}
        self.assertEqual(by_stage[pf.STAGE_DXF][0], ["10.dxf", "11.dxf", "12.dxf"])
        self.assertEqual(by_stage[pf.STAGE_JPEG][0], ["10.jpeg", "11.jpeg", "12.jpeg"])
        self.assertEqual(by_stage[pf.STAGE_PNG][0], ["10.png", "11.png", "12.png"])
        printed = [n for s, names, g in seen if s == pf.STAGE_PRINT for n in names]
        self.assertEqual(printed, [f"{p}.pdf" for p in self.PRINT_PAGES])
        self.assertEqual((by_stage[pf.STAGE_DXF][1], by_stage[pf.STAGE_JPEG][1], by_stage[pf.STAGE_PNG][1]), (pf.ARTIFACT_GRACE_DXF, 0, 0))


class StageFailureTest(Base):
    def assert_fails(self, result, stage, bad, total, missing, invalid):
        self.assertIsNotNone(result)
        self.assertEqual((result.status, result.cleanup), (pf.FAILED, True))
        self.assertRegex(result.reason, REASON_FORMAT)
        self.assertEqual(result.reason, f"[Step.2] {stage}の成果物が不足しています: 対象 {total} 件中 {bad} 件(欠落 {missing} 件・空または不正 {invalid} 件)(未完了)")
        self.assertNotIn("/", result.reason)                                 # 履歴の連結区切り(" / ")を含めない

    def test_missing_pdf_fails_right_after_printing_and_nothing_later_runs(self):
        self.skip["print"] = {"12"}
        res = self.run_step2()
        self.assert_fails(res, pf.STAGE_PRINT, 1, 5, 1, 0)                  # 最初の塊(5 枚)で止まる
        self.assertEqual(self.names(), ["print_script"])                    # 次の塊の印刷・DWG コピー・変換・推論用画像へ進まない
        self.assertEqual(len(self.sleeps), pf.ARTIFACT_GRACE_PRINT)         # 欠けているときだけ、再確認で待つ(モック。実際には待たない)

    def test_missing_pdf_of_a_pdf_only_page_also_fails(self):
        self.skip["print"] = {"16"}                                         # 2 枚目の塊(PDF だけのページ)の欠落
        res = self.run_step2()
        self.assert_fails(res, pf.STAGE_PRINT, 1, 2, 1, 0)
        self.assertEqual(self.names(), ["print_script", "print_script"])

    def test_empty_or_non_pdf_file_is_invalid(self):
        self.broken["print"] = {"11"}                                       # 空ファイル
        res = self.run_step2()
        self.assert_fails(res, pf.STAGE_PRINT, 1, 5, 0, 1)
        (self.dir_product / "11.pdf").write_bytes(b"not a pdf")             # 空ではないが PDF でない内容(10.pdf は正常)
        res2 = pf.check_stage_artifacts(self.ctx(), pf.STAGE_PRINT, ["10", "11"], lambda p: f"{p}.pdf", pf._is_valid_pdf)
        self.assert_fails(res2, pf.STAGE_PRINT, 1, 2, 0, 1)

    def test_pdf_written_late_is_accepted_within_the_grace_period(self):
        """印刷の PDF が、再確認の途中で書き終わった場合は、失敗にしない(欠けているときだけ待つ)。"""
        written = []

        def late_sleep(seconds):
            self.sleeps.append(seconds)
            if len(self.sleeps) == 3 and not written:
                write_pdf(self.dir_product / "12.pdf")
                written.append(1)
        self.skip["print"] = {"12"}
        with mock.patch.object(pf, "_artifact_sleep", side_effect=late_sleep):
            self.assertIsNone(self.run_step2())
        self.assertEqual(len(self.sleeps), 3)

    def test_converter_success_without_dxf_fails_right_after_conversion(self):
        self.skip["dxf"] = {"11", "12"}
        res = self.run_step2()
        self.assert_fails(res, pf.STAGE_DXF, 2, 3, 2, 0)
        self.assertNotIn("pdf_to_images", self.names())                     # 後続の画像作成へ進まない
        self.assertNotIn("process_images_from_jpeg", self.names())
        self.assertEqual(self.names()[-1], "convert_dwg2dxf")

    def test_swallowed_jpeg_failure_fails_right_after_jpeg_conversion(self):
        """image_mod.pdf_to_images は失敗を握りつぶして正常終了する。その場合も、JPEG の直後に失敗にする。"""
        self.skip["jpeg"] = {"10"}
        res = self.run_step2()
        self.assert_fails(res, pf.STAGE_JPEG, 1, 3, 1, 0)
        self.assertEqual(self.names()[-1], "pdf_to_images")
        self.assertNotIn("process_images_from_jpeg", self.names())          # PNG 前処理へ進まない

    def test_unreadable_jpeg_is_invalid_and_zero_byte_png_is_invalid(self):
        self.broken["jpeg"] = {"11"}
        res = self.run_step2()
        self.assert_fails(res, pf.STAGE_JPEG, 1, 3, 0, 1)
        self.broken["jpeg"] = set()
        self.calls.clear()
        self.broken["png"] = {"12"}
        res2 = self.run_step2()
        self.assert_fails(res2, pf.STAGE_PNG, 1, 3, 0, 1)
        self.assertEqual(self.names()[-1], "process_images_from_jpeg")

    def test_corrupt_image_bytes_are_invalid_not_an_exception(self):
        (self.dir_product / "10.jpeg").write_bytes(b"\xff\xd8garbage")
        ctx = self.ctx()
        res = pf.check_stage_artifacts(ctx, pf.STAGE_JPEG, ["10"], lambda p: f"{p}.jpeg", pf._is_valid_image)
        self.assert_fails(res, pf.STAGE_JPEG, 1, 1, 0, 1)

    def test_png_missing_is_caught_at_step2_instead_of_step3_file_not_found(self):
        """今回の事象: PNG が欠けたまま Step.3 に進み、frame_scale で FileNotFoundError。今後は、PNG 前処理の直後に、段階と件数つきで失敗する。"""
        self.skip["png"] = {"12"}
        res = self.run_step2()
        self.assert_fails(res, pf.STAGE_PNG, 1, 3, 1, 0)

    def test_exit_code_zero_everywhere_but_one_missing_page_in_the_middle(self):
        for kind, stage in (("dxf", pf.STAGE_DXF), ("jpeg", pf.STAGE_JPEG), ("png", pf.STAGE_PNG)):
            self.setUp()
            self.skip[kind] = {"11"}
            res = self.run_step2()
            self.assert_fails(res, stage, 1, 3, 1, 0)


class ReasonHygieneAndCleanupTest(Base):
    def test_reason_has_no_product_path_exception_or_settings(self):
        self.skip["print"] = {"10", "11"}
        res = self.run_step2()
        for forbidden in (PRODUCT, str(self.tmp), "Error", "Exception", "Traceback", "conv", "dwg", "pdf", "12", "10", "11"):
            self.assertNotIn(forbidden, res.reason.replace("[Step.2]", "").replace("PDF", ""), forbidden)
        self.assertRegex(res.reason, REASON_FORMAT)
        warn = " ".join(str(c.args[0]) for c in self.logger.warning.call_args_list)
        self.assertNotIn(str(self.tmp), warn)                                # ログにも実パスは出さない(ページ名だけ)

    def test_failure_goes_through_run_product_cleanup_limited_to_the_product_folder(self):
        """既存の安全な後始末(出力ルート配下の対象製番フォルダだけ)が使われ、出力ルート外・別の製番フォルダには触れない。"""
        sibling = self.output_dir / "A1BN9999"
        sibling.mkdir()
        (sibling / "keep.txt").write_text("keep", encoding="utf-8")
        outside = self.tmp / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        self.skip["jpeg"] = {"10"}
        results = {"completed": [], "failed": {}, "skipped": {}}
        started = []
        try:
            for p in self.patches():
                p.start()
                started.append(p)
            with mock.patch.object(pf, "process_product", lambda c: pf.step2_inference_images(c)), \
                    mock.patch.object(pf, "_select_pages", return_value=None):
                pf.run_product(self.ctx(), results)                         # 実の shutil.rmtree(一時フォルダの中だけ)
        finally:
            for p in started:
                p.stop()
        self.assertEqual(list(results["failed"]), [PRODUCT])
        self.assertRegex(results["failed"][PRODUCT], REASON_FORMAT)
        self.assertEqual((results["completed"], results["skipped"]), ([], {}))
        self.assertFalse(self.dir_product.exists())                          # 対象製番フォルダだけが消えた
        self.assertTrue((sibling / "keep.txt").is_file())                    # 別の製番フォルダは残る
        self.assertTrue(outside.is_file())                                   # 出力ルートの外は触れない
        self.assertTrue(self.output_dir.is_dir())

    def test_cleanup_uses_safe_rmtree_with_output_root_and_quiet_mode(self):
        rmtree = mock.Mock(name="shutil.rmtree")
        self.skip["print"] = {"10"}
        results = {"completed": [], "failed": {}, "skipped": {}}
        started = []
        try:
            for p in self.patches():
                p.start()
                started.append(p)
            with mock.patch.object(pf, "process_product", lambda c: pf.step2_inference_images(c)), \
                    mock.patch.object(pf, "_select_pages", return_value=None), mock.patch.object(pf.shutil, "rmtree", rmtree):
                pf.run_product(self.ctx(), results)
        finally:
            for p in started:
                p.stop()
        rmtree.assert_called_once_with(str(self.dir_product), ignore_errors=True)
        self.assertTrue(pf.is_under(str(self.dir_product), str(self.output_dir)))


if __name__ == "__main__":
    unittest.main()
