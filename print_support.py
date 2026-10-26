"""IJCAD 用の印刷スクリプト(WIRING.SCR)の作成。

社内共通パッケージ bs_pkg の design_mod.output_print_script と同じ -PLOT の手順を、このリポジトリ側で管理する版
(bs_pkg は Git 管理外のため、印刷設定の変更をここで追跡できるようにした)。

変更点は「印刷オフセット」だけ: 従来の固定値 `5,5`(用紙の左下から 5mm)だと、図面と用紙の縦横比の差が
すべて右(または上)の余白になって偏る。`C`(Center the plot=用紙の中央に配置)にすると、
印刷範囲(Limits)が用紙の中央に置かれ、左右・上下の余白が等しくなる。
尺度は従来どおり `F`(Fit)で、印刷範囲(Limits)・用紙(ISO A3)も変えていないので、PDF 上の図枠の大きさ(縮尺)は変わらない。
`close y` は維持する(`close n` や引数なしの `close` にしない)。
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Optional

PLOT_OFFSET = "C"  # 用紙の中央に配置(旧: "5,5")


def build_script_lines(dwg_path: str, pdf_path: str, plot_offset: str = PLOT_OFFSET) -> list[str]:
    """1 図面分の IJCAD スクリプト行(-PLOT の対話入力順は従来と同じ)。"""
    return [
        f"open {dwg_path}",
        "layer F *HOJYO,*MPRINT,*CMNTH,REVISION*",
        "",
        "-PLOT",
        "Y",                      # 詳細設定
        "Model",                  # レイアウト
        "DWG To PDF",             # 出力デバイス
        "ISO A3 (420.00 x 297.00 MM)",  # 用紙
        "M",                      # 単位: mm
        "L",                      # 用紙の向き: 横
        "N",                      # 上下反転なし
        "L",                      # 印刷範囲: Limits
        "F",                      # 尺度: Fit
        plot_offset,              # 印刷オフセット: 中央(旧 5,5)
        "Y",                      # 印刷スタイルを使用
        "thickness.ctb",
        "Y",                      # 線の太さを使用
        "A",                      # 表示どおり
        pdf_path,                 # 出力ファイルパス
        "N",                      # ページ設定を保存しない
        "Y",                      # 印刷を実行
        "close y",                # 図面を保存せず閉じる(忘れないこと)
    ]


def output_print_script(page_list, dir_target: str, dir_product: str, plot_offset: str = PLOT_OFFSET) -> str:
    """WIRING.SCR(shift-jis)を dir_product に作成して、そのパスを返す。出力 PDF は `{ページ}.pdf`(従来どおり)。

    設計図面が読めない場合は FileNotFoundError(従来の sys.exit() ではなく、呼び出し側が製番の失敗として扱えるようにした)。
    """
    script = os.path.join(dir_product, "WIRING.SCR")
    lines: list[str] = []
    for page in page_list:
        name = str(page)
        dwg_path = os.path.join(dir_target, name + ".dwg")
        if not os.access(dwg_path, os.R_OK):
            raise FileNotFoundError(f"印刷図面が存在しません: {dwg_path}")
        lines += build_script_lines(dwg_path, os.path.join(dir_product, name + ".pdf"), plot_offset)
    lines.append("quit y")
    with open(script, "w", encoding="shift-jis") as f:
        f.write("\n".join(lines) + "\n")
    return script


# ##############################################################################
# IJCAD の起動と、ハング(PDF が出ず応答もしない)時の終了(この呼び出しが起動したプロセスだけ)
# ##############################################################################
# 背景(A1AY4693 の失敗): bs_pkg の design_mod.print_dwg_for_pdf は、待ち時間が満了(TimeoutExpired)しても例外を握りつぶすだけで、IJCAD を終了させない
# (起動して process.wait(起動時間 + 枚数 × 1 枚あたりの時間) するだけ。終了コードも標準出力も見ない)。図面(mk32)を開いたまま応答しなくなった IJCAD が残り、
# 設計側の図面を開いたままにしていた。bs_pkg は Git 管理外の共通パッケージなので変更せず、同じ起動方法(設定・コマンド)をこのリポジトリ側に置いた。
#
# 「生きている」ことは異常の根拠にしない。通常の実行でも、待ち時間(35 秒など)を毎回超えて IJCAD が残っていた(チャンク間隔が常に約 55 秒)。
# PDF は待ち満了より前に書き終わっている(5 枚が約 11 秒の間に出る)ので、生存だけで失敗・終了にすると、正常な印刷まで失敗にしてしまう。
# 区別する 3 つの事実:
#   ① プロセスの終了(正常ならスクリプトの最後の `quit y` で終了するが、終了までの時間は未確認)
#   ② 期待する PDF が、すべて有効に出力されたか(有効 = ある・空でない・先頭が %PDF-。書き込み中を避けるため、2 秒間サイズが変わらないこと)
#   ③ IJCAD の応答状態(その PID のトップレベルウィンドウが、すべて「応答なし」= IsHungAppWindow か。ウィンドウが無ければ不明)
# 判定:
#   - プロセスが終了 → 正常(COMPLETED)。
#   - 生きていても、期待する PDF がすべて有効 → 正常(PDFS_READY)。プロセスは終了しない(従来どおり残る場合がある)。
#   - PDF が不足し、予定の待ち(従来の待ち時間)を過ぎ、かつ IJCAD の全ウィンドウが応答なしのまま 15 秒続き、その間に PDF が 1 枚も増えない → ハング。
#     起動した IJCAD だけを、照合 → 通常終了の要求 → 猶予 → 強制終了の順で終了し、失敗(TIMEOUT_*)にする。
#   - PDF が不足していても、IJCAD が応答中(または応答状態が不明)なら、終了しない。監視の最長時間(従来の待ち + 従来の流れが印刷後に待つ秒数)まで待ち、
#     過ぎたら PENDING を返す。呼び出し側は従来どおり、待機と PDF の検査(再確認つき)に進み、欠けていれば「成果物が不足」で失敗にする(IJCAD は終了しない)。
# 記録は固定の文言だけ(ページ名・パス・製番・例外メッセージ・コマンドライン・標準出力/標準エラーを含めない)。
# 標準出力・標準エラーは収集しない: IJCAD はスクリプトの図面パスや設計側のパスを出力し得て、ログ・履歴・画面へ漏らさずに扱える保証がなく、
# パイプを接続すると応答しない IJCAD が詰まる恐れもあるため。終了コードだけ(数値)を結果に持つ。
COMPLETED, PDFS_READY, PENDING, NOT_RUN = "completed", "pdfs_ready", "pending", "not_run"
TIMEOUT_CLOSED, TIMEOUT_KILLED, TIMEOUT_UNVERIFIED, TIMEOUT_ALIVE = "timeout_closed", "timeout_killed", "timeout_unverified", "timeout_alive"
PRINT_SETTLE_PER_SHEET = 4      # 従来の流れが印刷後に待つ秒数(1 枚あたり。product_flow の cad_mod.countdown(枚数 × 4))。監視の最長時間に含める
POLL_SECONDS = 1.0              # 終了・PDF・応答状態の確認間隔
PDF_STABLE_SECONDS = 2.0        # 期待する PDF が揃ってから、サイズが変わらないことを確認する秒数(書き込み中の判定を避ける)
HANG_CONFIRM_SECONDS = 15.0     # 応答なしが、この秒数続き、その間に PDF が増えなければ、ハングと判定する(重い図面での一時的な無応答を除く)
CLOSE_GRACE = 10                # 通常終了の要求から、強制終了へ移るまでの猶予[秒]
KILL_GRACE = 5                  # 強制終了の後、終了を確認するまでの待ち[秒]
WM_CLOSE = 0x0010


@dataclass(frozen=True)
class PrintOutcome:
    """印刷(IJCAD)の呼び出し結果。timed_out(ハングと判定して終了処理をした)のとき、呼び出し側は後続へ進まず、製番を失敗にする。"""
    status: str
    exit_code: Optional[int] = None

    @property
    def timed_out(self) -> bool:
        return self.status in (TIMEOUT_CLOSED, TIMEOUT_KILLED, TIMEOUT_UNVERIFIED, TIMEOUT_ALIVE)


_TIMEOUT_DETAIL = {
    TIMEOUT_CLOSED: "通常終了を要求して終了しました",
    TIMEOUT_KILLED: "強制終了しました",
    TIMEOUT_UNVERIFIED: "起動した IJCAD と照合できないため終了していません",
    TIMEOUT_ALIVE: "終了を確認できません",
}


def timeout_reason(stage: str, outcome: PrintOutcome) -> str:
    """ハング(PDF が出ず、応答もしない)の失敗理由(画面・履歴用の固定形式。製番・ページ名・パス・例外メッセージを含めない。「 / 」は履歴の連結と衝突するので使わない)。"""
    return f"[Step.2] {stage}の IJCAD が応答せず PDF が出力されませんでした({_TIMEOUT_DETAIL[outcome.status]})(未完了)"


class WindowsProcessControl:
    """Windows の API(ctypes)による、プロセスの照合・応答状態の確認・通常終了の要求。テストが差し替える。取得できないものは None / 0 を返す。"""

    def identity(self, pid: int):
        """(作成時刻[FILETIME 整数], 実行ファイル名の小文字) か、取得できなければ None。"""
        if os.name != "nt":
            return None
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        handle = k32.OpenProcess(0x1000, False, int(pid))      # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            c, e, k, u = (wintypes.FILETIME() for _ in range(4))
            if not k32.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                return None
            created = (c.dwHighDateTime << 32) | c.dwLowDateTime
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return None
            return created, os.path.basename(buf.value).lower()
        finally:
            k32.CloseHandle(handle)

    def _windows(self, pid: int) -> list:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        found = []
        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def each(hwnd, _lparam):
            owner = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == int(pid):
                found.append(hwnd)
            return True
        user32.EnumWindows(proc(each), 0)
        return found

    def responding(self, pid: int):
        """その PID のトップレベルウィンドウがすべて「応答なし」なら False、1 つでも応答していれば True、ウィンドウが無い・取得できなければ None。"""
        if os.name != "nt":
            return None
        import ctypes
        hwnds = self._windows(pid)
        if not hwnds:
            return None
        hung = [bool(ctypes.windll.user32.IsHungAppWindow(h)) for h in hwnds]
        return not all(hung)

    def request_close(self, pid: int) -> int:
        """その PID が持つトップレベルウィンドウへ WM_CLOSE(通常終了の要求)を送る。送った数を返す。"""
        if os.name != "nt":
            return 0
        import ctypes
        user32 = ctypes.windll.user32
        return sum(1 for h in self._windows(pid) if user32.PostMessageW(h, WM_CLOSE, 0, 0))


def _expected_exe_name(command: str) -> str:
    """起動コマンド(実行ファイル + 引数)の先頭の実行ファイル名(小文字)。引用符つきのパス・空白を含むパスの両方を扱う。"""
    text = command.strip()
    if text.startswith('"'):
        exe = text[1:].split('"', 1)[0]
    else:
        low = text.lower()
        exe = text[:low.index(".exe") + 4] if ".exe" in low else text.split(" ", 1)[0]
    return os.path.basename(exe.replace("\\", "/")).lower()


def _terminate_hung(proc, launched, expected_exe: str, control, log: Callable[[str], None],
                    close_grace: float, kill_grace: float) -> str:
    """ハングと判定した IJCAD を終了する(この呼び出しが起動した proc だけ)。結果の status を返す。"""
    def same_process() -> bool:
        now = control.identity(proc.pid)
        return launched is not None and now is not None and now == launched and now[1] == expected_exe

    if proc.poll() is not None:                                   # 判定の直後に、自分で終了していた
        log(" └> [印刷] 判定の直後に IJCAD が終了していました")
        return TIMEOUT_CLOSED
    if not same_process():
        log(" └> [印刷] 起動した IJCAD と照合できないため、終了しません")
        return TIMEOUT_UNVERIFIED
    sent = control.request_close(proc.pid)
    log(f" └> [印刷] 通常終了を要求しました(対象ウィンドウ {sent} 件)")
    try:
        proc.wait(close_grace)
        log(" └> [印刷] 通常終了を確認しました")
        return TIMEOUT_CLOSED
    except subprocess.TimeoutExpired:
        pass
    if not same_process():
        log(" └> [印刷] 猶予後に、起動した IJCAD と照合できないため、強制終了しません")
        return TIMEOUT_UNVERIFIED
    log(" └> [印刷] 猶予内に終了しないため、起動した IJCAD を強制終了します")
    try:
        proc.kill()
    except Exception as e:  # noqa: BLE001 - 例外メッセージは記録しない(型名だけ)
        log(f" └> [印刷] 強制終了を要求できませんでした({type(e).__name__})")
    try:
        proc.wait(kill_grace)
        log(" └> [印刷] 強制終了を確認しました")
        return TIMEOUT_KILLED
    except subprocess.TimeoutExpired:
        log(" └> [印刷] 強制終了後も終了を確認できません")
        return TIMEOUT_ALIVE


def _load_ijcad_settings(sheets_nmb: int):
    """bs_pkg.design_mod.print_dwg_for_pdf と同じ設定・同じコマンドの組み立て。(実行ファイル, 起動時間, 1 枚あたりの時間, config_loader) を返す。"""
    from bs_pkg import config_loader, design_mod
    config = config_loader.load_yaml(design_mod.YAML_FILE_PATH)
    cad_ini = config_loader.load_ini(config["cad_setting"])
    cad = cad_ini["Enviroment"]["ACAD"]
    if cad.strip() != "" and not cad.endswith(".exe"):
        cad = cad + ".exe"
    return cad, config["launch_time"], config["print_time"], config_loader


def _default_pdf_valid(path: str) -> bool:
    try:
        if not os.path.isfile(path) or os.path.getsize(path) <= 0:
            return False
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def print_dwg_for_pdf(script_path: str, sheets_nmb: int, expected_pdfs=(), *, log: Optional[Callable[[str], None]] = None,
                      popen=subprocess.Popen, control=None, clock=time.monotonic, pdf_valid: Callable[[str], bool] = _default_pdf_valid,
                      close_grace: float = CLOSE_GRACE, kill_grace: float = KILL_GRACE) -> PrintOutcome:
    """IJCAD をバッチ(/b)で起動して印刷スクリプトを実行し、終了・PDF・応答状態を見守る(判定はこのブロックの上の説明)。
    起動できない・設定を読めない場合は、従来の design_mod.print_dwg_for_pdf と同じく例外にせず NOT_RUN を返す(後続の PDF 検査が欠落として失敗にする)。
    expected_pdfs: このスクリプトが出力する PDF のパス(空なら PDF での判定をしない = ハング判定もしない)。"""
    log = log or (lambda _msg: None)
    control = control or WindowsProcessControl()
    expected = [str(p) for p in expected_pdfs]
    try:
        cad, launch_time, print_time, config_loader = _load_ijcad_settings(sheets_nmb)
        command = config_loader.convert_win_path(cad) + " /b " + config_loader.convert_win_path(script_path)
        planned = launch_time + sheets_nmb * print_time             # 従来の待ち時間(予定の完了時刻)
        monitor_limit = planned + sheets_nmb * PRINT_SETTLE_PER_SHEET   # 監視の最長時間(従来の流れが PDF の検査に入るまでの時間)
        start = clock()
        proc = popen(command)
    except Exception as e:  # noqa: BLE001 - 従来どおり握りつぶす(メッセージにはパスが入り得るので、型名だけ記録する)
        log(f" └> [印刷] IJCAD を起動できませんでした({type(e).__name__})")
        return PrintOutcome(NOT_RUN)
    launched = control.identity(proc.pid)
    exe_name = _expected_exe_name(command)
    valid_count = 0
    progress_at = start
    ready_sig, ready_since = None, None
    hung_since = None
    while True:
        try:
            code = proc.wait(POLL_SECONDS)
            return PrintOutcome(COMPLETED, code)
        except subprocess.TimeoutExpired:
            pass
        now = clock()
        elapsed = now - start
        if expected:
            ok = [p for p in expected if pdf_valid(p)]
            if len(ok) > valid_count:
                valid_count, progress_at, hung_since = len(ok), now, None
            if len(ok) == len(expected):
                sig = tuple(os.path.getsize(p) for p in expected)
                if sig != ready_sig:
                    ready_sig, ready_since = sig, now
                elif now - ready_since >= PDF_STABLE_SECONDS:
                    log(" └> [印刷] 期待する PDF がすべて出力されました(IJCAD は終了していませんが、終了しません)")
                    return PrintOutcome(PDFS_READY)
            else:
                ready_sig, ready_since = None, None
                if elapsed >= planned:                               # 予定の完了時刻を過ぎても PDF が揃わないときだけ、応答状態を見る
                    if control.responding(proc.pid) is False:
                        hung_since = now if hung_since is None else hung_since
                    else:
                        hung_since = None
                    if hung_since is not None and now - hung_since >= HANG_CONFIRM_SECONDS and progress_at <= hung_since:
                        log(f" └> [印刷] PDF が揃わず、IJCAD が {int(now - hung_since)} 秒以上応答しません(出力済み {len(ok)}/{len(expected)} 件)")
                        return PrintOutcome(_terminate_hung(proc, launched, exe_name, control, log, close_grace, kill_grace))
        if elapsed >= monitor_limit:
            break
    if expected:
        log(f" └> [印刷] 監視時間({int(monitor_limit)} 秒)内に PDF が揃いませんでしたが、IJCAD は応答中(または状態不明)のため終了しません。PDF の検査に進みます")
    return PrintOutcome(PENDING)
