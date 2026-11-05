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
from ops.history import COMPLETED, FAILED, KIND_MANUAL, KIND_SCHEDULED, KIND_SCHEDULED_CHECK, RUNNING, History, sanitize_reason
from ops.scheduled_plan import (DEADLINE_MARK, RESULT_FAILED, RESULT_NOT_STARTED, ScheduledPlan, build_plan, default_is_done, failure_streaks,
                                results_from, run_level_results)
from ops.settings_store import OpsSettings, SettingsError, SettingsStore, validate_product_no

LOCK_FILE = "ops_run.local.lock"
SKIPPED = "スキップ"
ALL_PRODUCTS = "(通常処理)"
DRY_RUN_LABEL = "(検証のみ)"
SYSTEM_EXIT_REASON = "処理が途中で終了しました(SystemExit)"
DRY_RUN_OK_REASON = "検証のみ(dry-run): 起動・ロック・COM・設定の読込を確認しました。製番処理は実行していません"
NO_TARGET_LABEL = "(対象なし)"
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
                 output_dir_resolver: Optional[Callable[[str], str]] = None, is_done: Optional[Callable[[str, str], bool]] = None):
        # 定期実行(run_scheduled / run_scheduled_dry_run)だけが使う:
        # - output_dir_resolver(root): 運用設定の出力先 root(空なら設定どおり)から、製番フォルダの親(実効の出力先)を返す。処理済みの除外に使う。
        #   None のときは、定期実行は出力先を確認できないため開始しない(副作用なしで拒否する)。
        # - is_done(output_dir, product): 処理済みの判定(省略時は製番処理と同じ規則)。
        self.output_dir_resolver = output_dir_resolver
        self._is_done = is_done or default_is_done
        self.refusal: Optional[str] = None     # 定期実行を、副作用なしで拒否した理由(履歴には残さない。run-scheduled が標準エラーに出す)
        self.last_plan: Optional[ScheduledPlan] = None   # 直近の定期実行(dry-run を含む)で確定した対象
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

    # ---- 定期実行 ----
    def _make_plan(self, settings: OpsSettings, root: str) -> ScheduledPlan:
        """今回の対象の確定。処理済み(出力先)と、直近の失敗・スキップの連続(履歴)を読んで並べ替える。副作用なし。"""
        output_dir = self.output_dir_resolver(root)
        streaks = failure_streaks(self.history.runs(limit=None), settings.scheduled_products)
        return build_plan(settings, output_dir, self._is_done, streaks=streaks)

    def scheduled_preflight(self) -> Tuple[OpsSettings, str, ScheduledPlan]:
        """定期実行(本番・dry-run 共通)の検査と、今回の対象の確定。履歴・ロック・フォルダ作成・印刷・API 呼び出しより前に行う(副作用なし)。
        順序: 運用設定の読込(不正なら拒否)→ 実効出力先の照合(手動実行と同じ OutputGuard)→ 起動時のコード版の照合 → 今回の対象の確定。
        どれかが通らなければ SettingsError(固定の理由)。"""
        settings = self.store.load()
        root = settings.output_root
        if self.output_guard is not None:
            self.output_guard(root, "ops_setting")          # 出力先が本番(設定 data.output の下の storage/output)と違えば拒否
        if self.code_guard is not None:
            self.code_guard()                               # 起動後にコミットが変わっていれば拒否
        if self.output_dir_resolver is None:
            raise SettingsError(UNRESOLVED_OUTPUT_REASON)   # 実効の出力先を確認できなければ、処理済みの判定ができないため始めない
        try:
            plan = self._make_plan(settings, root)
        except SettingsError:
            raise
        except Exception as e:  # noqa: BLE001 - 理由は固定文(例外メッセージ・パスは出さない)
            raise SettingsError(f"定期実行の対象を確定できないため、実行しませんでした({type(e).__name__})。"
                                "製番フォルダの作成・印刷・API の呼び出しは行っていません。") from None
        self.last_plan = plan
        return settings, root, plan

    def _replan_under_lock(self, settings: OpsSettings, root: str) -> Optional[ScheduledPlan]:
        """ロック取得後、実処理の直前に対象を作り直す(検査から、ここまでの間に、並行処理で処理済みになった製番が、上限の枠を無駄に使わないように)。
        作り直せなければ None(呼び出し側は、何も処理せずに拒否する)。"""
        try:
            plan = self._make_plan(settings, root)
        except Exception as e:  # noqa: BLE001
            self.refusal = (f"ロック取得後に対象を確定し直せないため、実行しませんでした({type(e).__name__})。"
                            "製番フォルダの作成・印刷・API の呼び出しは行っていません。")
            return None
        self.last_plan = plan
        return plan

    @staticmethod
    def _deferred_of(plan: ScheduledPlan):
        return [{"product": p, "streak": n} for p, n in plan.deferred]

    def run_scheduled_targeted(self) -> str:
        """定期実行(運用画面 ops_console のタスクスケジューラから起動)。設定に明示された製番リストだけを、件数・時間の上限内で処理する(出図リストからは追加しない)。
        検査(設定・出力先・コード版)に通らなければ、履歴・ロック・フォルダ作成・印刷・API 呼び出しより前に拒否する(self.refusal に理由)。
        失敗・スキップが続く製番は後ろに回し、ロック取得後に処理済み判定をやり直してから、確定した対象を履歴に記録して処理する。結果の状態を返す。"""
        self.refusal = None
        self.last_plan = None
        try:
            settings, root, plan = self.scheduled_preflight()
        except SettingsError as e:
            self.refusal = str(e)
            return FAILED
        if not plan.targets:                                # リスト未設定・空・全件処理済み: 何も処理せず、記録だけ残す(ロックは取らない)
            run_id = self.history.start(KIND_SCHEDULED, NO_TARGET_LABEL, plan.summary(), targets=[], deferred=self._deferred_of(plan))
            self.history.finish(run_id, SKIPPED, plan.summary())
            return SKIPPED
        try:
            self.lock.acquire("scheduled")
        except Busy:
            rid = self.history.start(KIND_SCHEDULED, ",".join(plan.targets), targets=list(plan.targets))
            self.history.finish(rid, SKIPPED, "実行中のため開始しませんでした(二重起動の防止)")
            return SKIPPED
        try:
            plan = self._replan_under_lock(settings, root)    # 実処理の直前に、処理済み判定をやり直す
            if plan is None:
                return FAILED
            if not plan.targets:                             # 並行処理で、対象がすべて処理済みになった
                run_id = self.history.start(KIND_SCHEDULED, NO_TARGET_LABEL, plan.summary(), targets=[], deferred=self._deferred_of(plan))
                self.history.finish(run_id, SKIPPED, plan.summary())
                return SKIPPED
            label = ",".join(plan.targets)
            run_id = self.history.start(KIND_SCHEDULED, label, plan.summary(), targets=list(plan.targets),
                                        deferred=self._deferred_of(plan))   # 今回の対象を、実行前に記録する
            self.current = {"id": run_id, "kind": KIND_SCHEDULED, "product": label}
            deadline = time.monotonic() + plan.max_minutes * 60
            return self._execute(run_id, list(plan.targets), root, label, stop_after=deadline, batch=True)
        finally:
            self.current = None
            self.lock.release()

    def run_scheduled_targeted_dry_run(self) -> str:
        """run-scheduled --dry-run(タスクの起動経路の検証用)。本番と同じ検査(設定・出力先・コード版)と対象の確定(並べ替え・ロック後の再確認を含む)を行い、
        その結果を履歴(種別「定期(検証)」)に残す。ロック取得・COM の初期化と終了だけを行い、main_func(app.main)は呼ばない。
        製番・出力先・設定値は履歴の理由に記録しない(対象一覧は targets、後ろに回した製番は deferred に残す)。検査に通らなければ、履歴・ロックより前に拒否する。"""
        self.refusal = None
        self.last_plan = None
        try:
            settings, root, plan = self.scheduled_preflight()
        except SettingsError as e:
            self.refusal = str(e)
            return FAILED
        try:
            self.lock.acquire("scheduled-dry-run")
        except Busy:
            rid = self.history.start(KIND_SCHEDULED_CHECK, DRY_RUN_LABEL, targets=list(plan.targets))
            self.history.finish(rid, SKIPPED, "実行中のため開始しませんでした(二重起動の防止)")
            return SKIPPED
        try:
            plan = self._replan_under_lock(settings, root)    # 本番と同じく、ロック取得後に対象を確定し直す
            if plan is None:
                return FAILED
            run_id = self.history.start(KIND_SCHEDULED_CHECK, DRY_RUN_LABEL, plan.summary(), targets=list(plan.targets),
                                        deferred=self._deferred_of(plan))
            self.current = {"id": run_id, "kind": KIND_SCHEDULED_CHECK, "product": DRY_RUN_LABEL}
            try:
                self.com_init()
            except Exception as e:  # noqa: BLE001
                self.history.finish(run_id, FAILED, f"COM を初期化できませんでした({type(e).__name__})")
                return FAILED
            try:
                self.store.load()                       # 読めることだけ確認する(値は使わない・記録しない)
                status, reason = COMPLETED, f"{DRY_RUN_OK_REASON}。{plan.summary()}"
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

    # ---- 従来の定期実行(全件。暫定運用 interim/runs.py が使う。ops_console の定期実行は上の run_scheduled_targeted) ----
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
