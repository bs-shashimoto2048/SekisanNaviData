# 実行管理(運用画面の「4. 製番を指定して実行」と、定期実行の入口)。
# 処理そのものは既存の app.main() をそのまま呼ぶ(処理ロジックを別実装しない)。ここは「いつ・どれを・二重起動なしで呼ぶか」と、
# 結果を履歴に残すことだけを受け持つ。失敗した製番を成功扱いにしない(app.main の results をそのまま状態にする)。
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

from ops import com as com_mod
from ops.batch_results import DEADLINE_MARK, RESULT_FAILED, RESULT_NOT_STARTED, results_from, run_level_results
from ops.history import COMPLETED, FAILED, KIND_MANUAL, KIND_RUN_NOW, KIND_SCHEDULED, KIND_SCHEDULED_CHECK, RUNNING, History, sanitize_reason
from ops.host_targets import TargetError, TargetPlan
from ops.settings_store import SettingsError, SettingsStore, validate_product_no

LOCK_FILE = "ops_run.local.lock"
SKIPPED = "スキップ"
ALL_PRODUCTS = "(通常処理)"
DRY_RUN_LABEL = "(検証のみ)"
SYSTEM_EXIT_REASON = "処理が途中で終了しました(SystemExit)"
DRY_RUN_OK_REASON = "検証のみ(dry-run): 起動・ロック・COM・設定の読込を確認しました。製番処理は実行していません"
NO_TARGET_LABEL = "(対象なし)"
RESOLVING_LABEL = "(対象を取得中)"
BUSY_REASON = "実行中のため開始しませんでした(二重起動の防止)"
UNRESOLVED_OUTPUT_REASON = "出力先を確認できないため、実行しませんでした(出力先の確認手段がありません)。製番フォルダの作成・印刷・API の呼び出しは行っていません。"


class Busy(RuntimeError):
    """実行中のため開始できない(二重起動の防止)。"""


def _try_lock(fd: int) -> bool:
    """ファイルの先頭 1 バイトに、排他ロックを非ブロッキングで試みる。取れれば True。OS のロックなので、持ち主のプロセスが
    終了(異常終了を含む)すれば自動で解放される(古いロック・PID の使い回しの問題が起きない)。別のハンドル(同じプロセス内を含む)とも競合する。"""
    os.lseek(fd, 0, os.SEEK_SET)
    if sys.platform == "win32":
        import msvcrt
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    if sys.platform == "win32":
        import msvcrt
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


class RunLock:
    """プロセスをまたぐ排他(画面からの実行どうし、および画面とタスクスケジューラからの定期実行の二重起動を防ぐ)。
    ロックファイル(中身は使わない)を OS のファイルロックで排他する。取得できなければ Busy。"""

    RETRIES = 5          # 状態の確認(is_held)が一瞬ロックを取る間と重なっても、すぐ Busy にしないための再試行
    RETRY_WAIT = 0.05

    def __init__(self, state_dir):
        self.path = Path(state_dir) / LOCK_FILE
        self._fd: Optional[int] = None

    def _open(self) -> int:
        return os.open(str(self.path), os.O_RDWR | os.O_CREAT)

    def is_held(self) -> bool:
        """誰か(このプロセス・別プロセス)がロックを持っているか。確認のために一瞬ロックを取って、すぐ放す。"""
        if self._fd is not None:
            return True
        fd = self._open()
        try:
            if _try_lock(fd):
                _unlock(fd)
                return False
            return True
        finally:
            os.close(fd)

    def acquire(self, tag: str = "") -> None:
        if self._fd is not None:
            raise Busy("実行中です。完了してから開始してください。")
        for i in range(self.RETRIES):
            fd = self._open()
            if _try_lock(fd):
                self._fd = fd
                return
            os.close(fd)
            if i + 1 < self.RETRIES:
                time.sleep(self.RETRY_WAIT)
        raise Busy("実行中です。完了してから開始してください。")

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                _unlock(fd)
            finally:
                os.close(fd)


class RunManager:
    def __init__(self, state_dir, store: SettingsStore, history: History, main_func: Callable,
                 config_path: Optional[str] = None,
                 com_init: Callable = com_mod.co_initialize, com_uninit: Callable = com_mod.co_uninitialize,
                 output_guard: Optional[Callable] = None, code_guard: Optional[Callable] = None,
                 target_resolver: Optional[Callable[[str], TargetPlan]] = None):
        # 定期実行・今すぐ実行(run_scheduled_host / start_run_now / run_scheduled_host_dry_run)だけが使う:
        # - target_resolver(root): 運用設定の出力先 root(空なら設定どおり)から、今回の対象(TargetPlan)を確定する。ホスト DB を読み取り専用で
        #   照会し、出力済み(未完了マーカーなし)を除く(ops/host_targets.py)。失敗は TargetError。None のときは、対象を確定できないため開始しない。
        self.target_resolver = target_resolver
        self.refusal: Optional[str] = None     # 定期実行・今すぐ実行が、処理を始めずに失敗にした理由(履歴にも残る。run-scheduled が標準エラーに出す)
        self.last_plan: Optional[TargetPlan] = None   # 直近の定期実行(dry-run を含む)・今すぐ実行で確定した対象
        # code_guard(): 手動実行の開始前に、起動後のコード更新(古いコードのままの常駐プロセス)を検出する(更新されていれば SettingsError)。None なら確認しない(従来どおり。暫定運用など)
        self.code_guard = code_guard
        # output_guard(root, source): 手動実行の出力先を、実行を始める前に確認する(違えば SettingsError)。None なら確認しない(従来どおり。暫定運用など)
        self.output_guard = output_guard
        self.com_init = com_init              # app.main(ホスト DB は COM)を呼ぶスレッドで、呼ぶ前に初期化する
        self.com_uninit = com_uninit
        self.store = store
        self.history = history
        self.main_func = main_func            # app.main と同じ呼び出し形: (config_path=, products=, output_root=) -> results
        self.config_path = config_path
        self.lock = RunLock(state_dir)
        self.current: Optional[dict] = None   # 実行中の {id, kind, product}(このプロセスで開始したもの)

    # ---- 状態 ----
    def status(self) -> dict:
        return {"running": self.lock.is_held(), "current": self.current}

    # ---- 実行 ----
    def _call_main(self, products, output_root, stop_after=None):
        kwargs = {"config_path": self.config_path}
        if products is not None:
            kwargs["products"] = products
        if output_root:
            kwargs["output_root"] = output_root
        if stop_after is not None:
            kwargs["stop_after"] = stop_after         # 時間上限(time.monotonic の時刻)。app.main が、新しい製番を始める前に確認する
        return self.main_func(**kwargs)

    def _execute(self, run_id: str, products, output_root, product_label: str, stop_after=None, batch: bool = False):
        """app.main を呼び、結果を履歴の状態にする。ここで例外を握って失敗として記録する。"""
        try:
            self.com_init()
        except Exception as e:  # noqa: BLE001 - 初期化できないときは app.main を実行しない
            status = FAILED
            reason = f"COM を初期化できないため実行しませんでした({type(e).__name__})"
            self._finish(run_id, status, reason, run_level_results(products, RESULT_NOT_STARTED, "COM を初期化できませんでした") if batch else None)
            return status
        results = None                  # 定期実行(batch)だけ、製番ごとの結果を履歴に残す(完了・失敗・スキップ・未開始を区別する)
        try:
            res = self._call_main(products, output_root, stop_after)
            status, reason = self._summarize(res, product_label, batch)
            if batch:
                results = results_from(res, products)
        except Exception as e:  # noqa: BLE001 - どんな失敗も「完了」にしない
            status, reason = FAILED, f"{type(e).__name__}: {e}"
            if batch:
                results = run_level_results(products, RESULT_FAILED, "実行全体の失敗")
        except SystemExit:  # 下位モジュールの sys.exit()。履歴を「実行中」のまま残さない。終了コード・メッセージは記録しない(固定の理由)
            status, reason = FAILED, SYSTEM_EXIT_REASON
            if batch:
                results = run_level_results(products, RESULT_FAILED, "実行全体の失敗")
        finally:
            try:
                self.com_uninit()      # 初期化したのと同じスレッドで必ず終了する
            except Exception:  # noqa: BLE001 - 終了処理の失敗で結果の記録を失わない
                pass
        self._finish(run_id, status, reason, results)
        return status

    def _finish(self, run_id: str, status: str, reason: str, results=None):
        """履歴に終了を記録する。製番ごとの結果(results)は定期実行(batch)だけ。results が無いときは、従来どおりの呼び出し
        (暫定運用 interim は、finish が results を受け取らない自前の履歴を渡すため、引数を増やさない)。"""
        if results is None:
            self.history.finish(run_id, status, reason)
        else:
            self.history.finish(run_id, status, reason, results=results)

    @staticmethod
    def _summarize(res: dict, product_label: str, batch: bool = False) -> Tuple[str, str]:
        failed, skipped, done = res.get("failed") or {}, res.get("skipped") or {}, res.get("completed") or []
        if failed:
            detail = " / ".join(f"{p}: {r}" for p, r in list(failed.items())[:5])
            return FAILED, f"完了 {len(done)} 件・失敗 {len(failed)} 件・スキップ {len(skipped)} 件 — {detail}"
        if product_label != ALL_PRODUCTS and not batch:
            if done:
                return COMPLETED, ""
            return SKIPPED, " / ".join(f"{p}: {r}" for p, r in list(skipped.items())[:5]) or "処理されませんでした"
        by_time = sum(1 for r in skipped.values() if DEADLINE_MARK in str(r))
        extra = f"(うち {by_time} 件は時間上限のため開始せず)" if by_time else ""
        return COMPLETED, f"完了 {len(done)} 件・スキップ {len(skipped)} 件{extra}"

    def start_manual(self, product, output_root: Optional[str] = None) -> Tuple[str, threading.Thread]:
        """指定した 1 製番を、別スレッドで 1 回処理する。二重起動は Busy。入力が不正なら SettingsError。"""
        product = validate_product_no(product)
        if self.code_guard is not None:         # 履歴・ロック・スレッド・フォルダ作成・印刷・API 呼び出しより前に、起動時のコードのままか確認する
            self.code_guard()
        from_field = bool((output_root or "").strip())
        root = (output_root or "").strip() or self.store.load().output_root
        if root:
            from ops.settings_store import validate_output_root
            root = validate_output_root(root)
        if self.output_guard is not None:       # 製番フォルダの作成・印刷・API 呼び出しの前(ロック・履歴・スレッドの前)に、出力先を確認する
            self.output_guard(root, "field" if from_field else "ops_setting")
        self.lock.acquire("manual")
        try:
            run_id = self.history.start(KIND_MANUAL, product)
        except BaseException:
            self.lock.release()
            raise
        self.current = {"id": run_id, "kind": KIND_MANUAL, "product": product}

        def work():
            try:
                self._execute(run_id, [product], root, product)
            finally:
                self.current = None
                self.lock.release()

        t = threading.Thread(target=work, name="ops-run", daemon=True)
        t.start()
        return run_id, t

    # ---- 定期実行・今すぐ実行(同じ対象取得・同じ検査・同じ処理・同じ実行ロック。違うのは履歴の種別と起動の経路だけ) ----
    def _preflight(self):
        """全体の安全条件(運用設定の読込 → 実効出力先の照合 → 起動時のコード版の照合)。通らなければ SettingsError(固定の理由)。
        (推論 API の事前確認は app.main が、製番処理の前に 1 度だけ行い、使えなければ製番処理を始めずに「失敗」を返す。COM の初期化は呼び出し側)"""
        settings = self.store.load()
        root = settings.output_root
        if self.output_guard is not None:
            self.output_guard(root, "ops_setting")          # 出力先が本番(設定 data.output の下の storage/output)と違えば拒否
        if self.code_guard is not None:
            self.code_guard()                               # 起動後にコミットが変わっていれば拒否
        if self.target_resolver is None:
            raise SettingsError(UNRESOLVED_OUTPUT_REASON)   # 対象(と実効の出力先)を確認できなければ、処理済みの判定ができないため始めない
        return root

    def _run_host_batch(self, run_id: str, dry_run: bool = False) -> str:
        """実行ロックを持ち、履歴(「実行中」)を開始した状態で呼ぶ。定期実行・今すぐ実行の本体(処理ロジックはここと app.main だけ)。
        1. 全体の安全条件(設定・出力先・コード版・COM)→ 2. ホストを読み取り専用で再照会して今回の対象を確定 → 3. 対象を履歴に記録 →
        4. 取得した対象を、打ち切らずすべて app.main で処理 → 5. 結果(製番ごと)を記録。
        全体の条件を満たさない・ホスト一覧を取得できないときは、製番処理を始めず、0 件成功にもせず「失敗」と理由を履歴に残す。
        製番ごとの通常例外・SystemExit は app.main(pipeline.product_flow.run_product)がその製番の失敗にして残りを続け、
        全件を試した後に失敗があれば、ここで実行全体を「失敗」にする。COM の初期化・終了は、このスレッドで対にする。"""
        self.refusal = None
        self.last_plan = None
        try:
            root = self._preflight()
        except SettingsError as e:
            return self._refuse(run_id, str(e))
        except Exception as e:  # noqa: BLE001 - 理由は固定文(パス・例外メッセージは出さない)
            return self._refuse(run_id, f"全体の事前確認で失敗したため、実行しませんでした({type(e).__name__})。"
                                        "製番フォルダの作成・印刷・API の呼び出しは行っていません。")
        try:
            self.com_init()                                 # ホスト DB は COM(ADODB)。対象の確定(照会)も、このスレッドで初期化してから行う
        except Exception as e:  # noqa: BLE001
            return self._refuse(run_id, f"COM を初期化できないため実行しませんでした({type(e).__name__})")
        try:
            try:
                plan = self.target_resolver(root)           # 押下(起動)の時点で、ホストを再照会して確定する(前回の対象・固定の一覧は使わない)
            except TargetError as e:
                return self._refuse(run_id, str(e))
            except Exception as e:  # noqa: BLE001
                return self._refuse(run_id, f"対象を確定できないため、実行しませんでした({type(e).__name__})。"
                                            "製番フォルダの作成・印刷・API の呼び出しは行っていません。")
            self.last_plan = plan
            if dry_run:
                self.history.update(run_id, reason=plan.summary(), targets=list(plan.targets))
                self.history.finish(run_id, COMPLETED, f"{DRY_RUN_OK_REASON}。{plan.summary()}")
                return COMPLETED
            if not plan.targets:                             # 出図なし・すべて処理済み。0 件成功ではなく「スキップ(対象なし)」と区別して記録する
                self.history.update(run_id, reason=plan.summary(), targets=[], product=NO_TARGET_LABEL)
                self.history.finish(run_id, SKIPPED, plan.summary())
                return SKIPPED
            targets = list(plan.targets)
            label = ",".join(targets)
            self.history.update(run_id, reason=plan.summary(), targets=targets, product=label)    # 実行前に、今回の対象を記録する
            if self.current is not None:
                self.current["product"] = label
            try:
                res = self._call_main(targets, root)         # 時間・件数の上限は付けない(stop_after なし)
                status, reason = self._summarize(res, label, True)
                results = results_from(res, targets)
            except Exception as e:  # noqa: BLE001 - どんな失敗も「完了」にしない
                status, reason = FAILED, f"{type(e).__name__}: {e}"
                results = run_level_results(targets, RESULT_FAILED, "実行全体の失敗")
            except SystemExit:  # app.main の外側で出た sys.exit()。履歴を「実行中」のまま残さない
                status, reason = FAILED, SYSTEM_EXIT_REASON
                results = run_level_results(targets, RESULT_FAILED, "実行全体の失敗")
            self._finish(run_id, status, reason, results)
            return status
        finally:
            try:
                self.com_uninit()                            # 初期化したのと同じスレッドで必ず終了する
            except Exception:  # noqa: BLE001 - 終了処理の失敗で結果の記録を失わない
                pass

    def _refuse(self, run_id: str, reason: str) -> str:
        """処理を始めずに、全体の失敗として履歴に残す(0 件成功にしない)。"""
        self.refusal = reason
        self.history.finish(run_id, FAILED, reason)
        return FAILED

    def _begin(self, kind: str, tag: str) -> str:
        """ロックを取り、履歴を開始する(取れなければ Busy)。成功したら current を設定して run_id を返す。呼び出し側が release する。"""
        self.lock.acquire(tag)
        try:
            run_id = self.history.start(kind, RESOLVING_LABEL)
        except BaseException:
            self.lock.release()
            raise
        self.current = {"id": run_id, "kind": kind, "product": RESOLVING_LABEL}
        return run_id

    def run_scheduled_host(self) -> str:
        """定期実行(タスクスケジューラから起動する run-scheduled)。実行のたびにホストから対象を取得して処理する。結果の状態を返す。
        実行中なら(ロックが取れなければ)、履歴に「スキップ」を残して開始しない。"""
        try:
            run_id = self._begin(KIND_SCHEDULED, "scheduled")
        except Busy:
            rid = self.history.start(KIND_SCHEDULED, RESOLVING_LABEL)
            self.history.finish(rid, SKIPPED, BUSY_REASON)
            return SKIPPED
        try:
            return self._run_host_batch(run_id)
        finally:
            self.current = None
            self.lock.release()

    def run_scheduled_host_dry_run(self) -> str:
        """run-scheduled --dry-run(タスクの起動経路の検証用)。本番と同じ検査・同じ対象の確定(ホストの読み取り専用の照会)を行い、
        結果を履歴(種別「定期(検証)」)に残す。製番処理(app.main)は呼ばない。"""
        try:
            run_id = self._begin(KIND_SCHEDULED_CHECK, "scheduled-dry-run")
        except Busy:
            rid = self.history.start(KIND_SCHEDULED_CHECK, DRY_RUN_LABEL)
            self.history.finish(rid, SKIPPED, BUSY_REASON)
            return SKIPPED
        try:
            self.history.update(run_id, product=DRY_RUN_LABEL)
            return self._run_host_batch(run_id, dry_run=True)
        finally:
            self.current = None
            self.lock.release()

    def start_run_now(self) -> Tuple[str, threading.Thread]:
        """運用画面の「今すぐ実行」。定期実行と同じ本体(_run_host_batch)・同じロックで、別スレッドで 1 回だけ実行する。
        他の実行(手動・定期・今すぐ実行・検証)が動作中なら Busy(履歴は作らない)。タスクスケジューラの登録・有効/無効・時刻には触れない。"""
        run_id = self._begin(KIND_RUN_NOW, "run-now")

        def work():
            try:
                self._run_host_batch(run_id)
            except BaseException:  # noqa: BLE001 - 履歴を「実行中」のまま残さない(想定外の例外・SystemExit)
                try:
                    self.history.finish(run_id, FAILED, SYSTEM_EXIT_REASON)
                except Exception:  # noqa: BLE001
                    pass
            finally:
                self.current = None
                self.lock.release()

        t = threading.Thread(target=work, name="ops-run-now", daemon=True)
        t.start()
        return run_id, t

    # ---- 従来の定期実行(全件。暫定運用 interim/runs.py が使う。ops_console の定期実行は上の run_scheduled_host) ----
    def run_scheduled(self) -> str:
        """定期実行(タスクスケジューラから起動)。通常処理をそのまま 1 回実行する。結果の状態を返す。"""
        try:
            self.lock.acquire("scheduled")
        except Busy:
            rid = self.history.start(KIND_SCHEDULED, ALL_PRODUCTS)
            self.history.finish(rid, SKIPPED, "実行中のため開始しませんでした(二重起動の防止)")
            return SKIPPED
        try:
            root = self.store.load().output_root
            run_id = self.history.start(KIND_SCHEDULED, ALL_PRODUCTS)
            self.current = {"id": run_id, "kind": KIND_SCHEDULED, "product": ALL_PRODUCTS}
            return self._execute(run_id, None, root, ALL_PRODUCTS)
        finally:
            self.current = None
            self.lock.release()

    def run_scheduled_dry_run(self) -> str:
        """run-scheduled --dry-run(タスクの起動経路の検証用)。ロック取得・COM の初期化と終了・ローカル設定の読込・履歴の記録だけを行い、
        main_func(app.main)は呼ばない。履歴は種別「定期(検証)」。製番・出力先・設定値は記録しない。結果の状態を返す。"""
        try:
            self.lock.acquire("scheduled-dry-run")
        except Busy:
            rid = self.history.start(KIND_SCHEDULED_CHECK, DRY_RUN_LABEL)
            self.history.finish(rid, SKIPPED, "実行中のため開始しませんでした(二重起動の防止)")
            return SKIPPED
        try:
            run_id = self.history.start(KIND_SCHEDULED_CHECK, DRY_RUN_LABEL)
            self.current = {"id": run_id, "kind": KIND_SCHEDULED_CHECK, "product": DRY_RUN_LABEL}
            try:
                self.com_init()
            except Exception as e:  # noqa: BLE001
                self.history.finish(run_id, FAILED, f"COM を初期化できませんでした({type(e).__name__})")
                return FAILED
            try:
                self.store.load()                       # 読めることだけ確認する(値は使わない・記録しない)
                status, reason = COMPLETED, DRY_RUN_OK_REASON
            except Exception as e:  # noqa: BLE001
                status, reason = FAILED, f"ローカル設定を読み込めませんでした({type(e).__name__})"
            finally:
                try:
                    self.com_uninit()
                except Exception:  # noqa: BLE001 - 終了処理の失敗で結果の記録を失わない
                    pass
            self.history.finish(run_id, status, reason)
            return status
        finally:
            self.current = None
            self.lock.release()
