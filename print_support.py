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
# 共通製作仕様書は、図面ページの印刷(WIRING.SCR・`{ページ}.pdf`・製番フォルダ直下)と、スクリプトも出力先も共有しない。
# 以前は、仕様書の PDF を一度ページ「1」の仮名 `1.pdf` に出して改名していたため、仕様書の IJCAD が見守りの上限(35 秒)を過ぎて後から PDF を出すと、
# 改名の確認に間に合わず、後続のページ「1」の印刷と同じ名前を取り合って上書きし得た(InterimOps の実行で、仕様書が作られなかった製番がある)。
# いまは、専用の作業フォルダ(製番フォルダ直下の SPEC_WORK_DIR)に専用のスクリプトと PDF を置く。IJCAD は製番フォルダ直下へ何も書かない。
# IJCAD に渡す PDF 名は ASCII の固定名 SPEC_WORK_PDF_NAME(日本語名をスクリプト経由で渡さない)。最終名 SPEC_PDF_NAME(`0.pdf`)へは、
# PDF の完成を安全に確認できたときだけ、Python 側で移す(finalize_spec_pdf)。`0.pdf` は図面ページ「0」の PDF と同名なので、
# ページ「0」が印刷対象に含まれる場合・製番フォルダ直下に既に `0.pdf` がある場合は、移さない(図面ページの PDF を保護し、作業フォルダに残す)。
SPEC_SCRIPT_NAME = "SPEC_SHEET.SCR"
SPEC_WORK_PDF_NAME = "spec_sheet.pdf"
SPEC_PDF_NAME = "0.pdf"
SPEC_WORK_DIR = "spec_sheet_work"


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


def output_print_script(page_list, dir_target: str, dir_product: str, plot_offset: str = PLOT_OFFSET, *,
                        script_name: str = "WIRING.SCR", pdf_name: Optional[Callable[[str], str]] = None) -> str:
    """WIRING.SCR(shift-jis)を dir_product に作成して、そのパスを返す。出力 PDF は `{ページ}.pdf`(従来どおり)。
    script_name・pdf_name(ページ → PDF のファイル名)は、図面ページの印刷と名前も出力先も共有しない印刷(共通製作仕様書)だけが指定する。

    設計図面が読めない場合は FileNotFoundError(従来の sys.exit() ではなく、呼び出し側が製番の失敗として扱えるようにした)。
    """
    script = os.path.join(dir_product, script_name)
    lines: list[str] = []
    for page in page_list:
        name = str(page)
        dwg_path = os.path.join(dir_target, name + ".dwg")
        if not os.access(dwg_path, os.R_OK):
            raise FileNotFoundError(f"印刷図面が存在しません: {dwg_path}")
        pdf_file = pdf_name(name) if pdf_name else name + ".pdf"
        lines += build_script_lines(dwg_path, os.path.join(dir_product, pdf_file), plot_offset)
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
#   ② 期待する PDF が、すべて「完成」しているか。完成 = ある・空でない・先頭が %PDF-・末尾 1 KiB に %%EOF がある(pdf_is_complete)・サイズが 2 秒間変わらない。
#      PDF は末尾(xref・trailer・%%EOF)を最後に書くので、書きかけ・一時停止中のファイルは %%EOF が無く、完成と見なされない
#      (隔離出力の PDF 3,554 件すべてが、先頭 %PDF- と末尾の %%EOF を持つことを確認済み)。2 秒の安定は、末尾が書かれた直後の書き込み残りを避ける補助。
#      印刷スクリプトの完了の合図(専用ファイルなど)は追加していない: スクリプトに LISP 等を足して合図を出す方法は、実際の IJCAD で動作を確認できず、
#      印刷スクリプトそのものを変えると正常印刷を壊す恐れがあるため。信頼できる合図が無い限界は、末尾の %%EOF + 安定 + (ページの順次印刷で後のページが出れば前のページは終わっている)
#      という「出力側の証拠」で補う。それでも、完成した PDF が後から書き換えられる場合は検知できない(未確認)。
#   ③ IJCAD の応答状態(その PID のトップレベルウィンドウが、すべて「応答なし」= IsHungAppWindow か。ウィンドウが無い・取得できなければ不明)
# 時間(従来の流れと同じ上限。従来: 待ち 35 秒 → 呼び出し側の待機 20 秒 → 検査の再確認 20 秒):
#   予定 planned = 起動時間 + 枚数 × 1 枚あたりの時間(従来の待ち)。ペース pace = planned + 枚数 × 4(従来の待ち + 呼び出し側の待機)。
#   この呼び出しが、従来の「待ち + 待機」を担う(呼び出し側の待機は無くした)。したがって、判定までの上限は従来と同じ 75 秒(5 枚)= ペース 55 秒 + 検査の再確認 20 秒。
#   ・PDF が揃っても、ペースまでは IJCAD の終了を待つ(次の IJCAD を、前の IJCAD の終了前に起動し重ねないため。従来と同じ間隔)。
#   ・ハングの確認(応答なし 15 秒)ができるよう、監視の最長時間は max(ペース, 予定 + 15 秒 + 1 秒)(1 枚の共通製作仕様書でも確認できる)。
# 判定(ペースまたは最長時間に達したとき):
#   - プロセスが終了 → 正常(COMPLETED)。
#   - 生きていても、PDF がすべて完成 → 正常(PDFS_READY)。**終了しない**。ただし、そのとき IJCAD が応答なしなら、残っていることを固定の文言で警告する
#     (left_unresponsive)。正常な IJCAD の残存(応答中)と、完了後のハング残存(応答なし)を区別して記録するが、終了しない方針で確定(PDF は完成しており、
#     製番は失敗にしない。方針は確定済み: 警告のみで、終了しない)。
#   - PDF が不足し、予定を過ぎ、全ウィンドウが応答なしのまま 15 秒続き、その間に PDF が 1 枚も増えない → ハング。
#     起動した IJCAD だけを、照合 → 通常終了の要求 → 猶予 → 強制終了の順で終了し、失敗(TIMEOUT_*)にする。
#   - PDF が不足していても、IJCAD が応答中(または応答状態が不明)なら、終了しない。最長時間まで待ち、PENDING を返す。
#     呼び出し側は従来どおり PDF の検査(再確認つき)に進み、欠けていれば「成果物が不足」で失敗にする(IJCAD は終了しない)。
# 記録は固定の文言だけ(ページ名・パス・製番・例外メッセージ・コマンドライン・標準出力/標準エラーを含めない)。
# 標準出力・標準エラーは収集しない: IJCAD はスクリプトの図面パスや設計側のパスを出力し得て、ログ・履歴・画面へ漏らさずに扱える保証がなく、
# パイプを接続すると応答しない IJCAD が詰まる恐れもあるため。終了コードだけ(数値)を結果に持つ。
COMPLETED, PDFS_READY, PENDING, NOT_RUN = "completed", "pdfs_ready", "pending", "not_run"
TIMEOUT_CLOSED, TIMEOUT_KILLED, TIMEOUT_UNVERIFIED, TIMEOUT_ALIVE = "timeout_closed", "timeout_killed", "timeout_unverified", "timeout_alive"
PRINT_SETTLE_PER_SHEET = 4      # 従来の流れが印刷後に待っていた秒数(1 枚あたり)。ペースに含める(従来は product_flow の cad_mod.countdown(枚数 × 4))
POLL_SECONDS = 1.0              # 終了・PDF・応答状態の確認間隔
PDF_STABLE_SECONDS = 2.0        # 期待する PDF が揃ってから、サイズが変わらないことを確認する秒数
HANG_CONFIRM_SECONDS = 15.0     # 応答なしが、この秒数続き、その間に PDF が増えなければ、ハングと判定する(重い図面での一時的な無応答を除く)
PDF_TAIL_BYTES = 1024           # %%EOF を探す末尾の範囲
CLOSE_GRACE = 10                # 通常終了の要求から、強制終了へ移るまでの猶予[秒]
KILL_GRACE = 5                  # 強制終了の後、終了を確認するまでの待ち[秒]
WM_CLOSE = 0x0010


@dataclass(frozen=True)
class PrintOutcome:
    """印刷(IJCAD)の呼び出し結果。timed_out(ハングと判定して終了処理をした)のとき、呼び出し側は後続へ進まず、製番を失敗にする。
    left_unresponsive: PDF は完成しているが、IJCAD が応答しないまま残っている(失敗にはしない。警告のみ)。"""
    status: str
    exit_code: Optional[int] = None
    left_unresponsive: bool = False

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


def pdf_is_complete(path: str) -> bool:
    """PDF が完成しているか(ある・空でない・先頭が %PDF-・末尾 1 KiB に %%EOF)。書きかけ・一時停止中のファイルは、末尾が無いので False。"""
    try:
        size = os.path.getsize(path)
        if not os.path.isfile(path) or size <= 0:
            return False
        with open(path, "rb") as f:
            if f.read(5) != b"%PDF-":
                return False
            f.seek(max(0, size - PDF_TAIL_BYTES))
            return b"%%EOF" in f.read()
    except OSError:
        return False


SPEC_MOVED, SPEC_MISSING, SPEC_INCOMPLETE, SPEC_UNCONFIRMED, SPEC_PROTECTED = "moved", "missing", "incomplete", "unconfirmed", "protected"


def finalize_spec_pdf(src: str, dst: str, *, sleep: Callable[[float], None] = time.sleep, valid: Callable[[str], bool] = pdf_is_complete,
                      stable_seconds: float = PDF_STABLE_SECONDS, log: Optional[Callable[[str], None]] = None) -> str:
    """共通製作仕様書の PDF(作業フォルダの src)を、PDF の完成を安全に確認できたときだけ、最終名(dst)へ移す。
    完成 = pdf_is_complete(先頭 %PDF-・末尾に %%EOF)で、stable_seconds の間サイズが変わらず、まだ完成のまま。
    IJCAD がまだ開いている(書き込み中・ロック中)ファイルは、移動(os.rename)が失敗するので、移さない。
    dst が既にあるときは、上書きしない(SPEC_PROTECTED。os.rename は Windows では既存の dst を上書きせずに失敗する。ここでも先に確認する)。
    確認できなければ、何も変えず(src を残す)、結果だけを返す。図面ページの PDF(製番フォルダ直下の `{ページ}.pdf`)には一切触れない。
    log には固定の文言だけを出す(パス・ページ名・例外メッセージは含めない)。"""
    log = log or (lambda _msg: None)
    try:
        if not os.path.exists(src):
            return SPEC_MISSING
        if os.path.lexists(dst):
            return SPEC_PROTECTED
        if not valid(src):
            return SPEC_INCOMPLETE
        size = os.path.getsize(src)
        if stable_seconds > 0:
            sleep(stable_seconds)
        if not valid(src) or os.path.getsize(src) != size:
            return SPEC_INCOMPLETE
        if os.path.lexists(dst):                             # 待っている間に、図面ページの PDF などが同名で作られた
            return SPEC_PROTECTED
        os.rename(src, dst)
    except OSError as e:  # noqa: BLE001 - 例外メッセージは記録しない(型名だけ)。移せなければ src を残す
        log(f" └> [印刷] 共通製作仕様書の PDF を最終名へ移せませんでした({type(e).__name__})")
        return SPEC_UNCONFIRMED
    work_dir = os.path.dirname(src)
    try:
        script = os.path.join(work_dir, SPEC_SCRIPT_NAME)    # 移せたあと、この作業フォルダにある専用のスクリプト(自分が作ったもの)だけを片付ける
        if os.path.isfile(script):
            os.remove(script)
        os.rmdir(work_dir)                                   # 空になったら作業フォルダも片付ける(ほかに残りがあれば、そのまま)
    except OSError:
        pass
    return SPEC_MOVED


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
        try:
            now = control.identity(proc.pid)
        except Exception:  # noqa: BLE001 - 照合できなければ終了しない
            return False
        return launched is not None and now is not None and now == launched and now[1] == expected_exe

    if proc.poll() is not None:                                   # 判定の直後に、自分で終了していた
        log(" └> [印刷] 判定の直後に IJCAD が終了していました")
        return TIMEOUT_CLOSED
    if not same_process():
        log(" └> [印刷] 起動した IJCAD と照合できないため、終了しません")
        return TIMEOUT_UNVERIFIED
    try:
        sent = control.request_close(proc.pid)
    except Exception as e:  # noqa: BLE001 - 例外メッセージは記録しない(型名だけ)
        sent = 0
        log(f" └> [印刷] 通常終了を要求できませんでした({type(e).__name__})")
    else:
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


def _responding(control, pid, log: Callable[[str], None]):
    """応答状態(True / False / None=不明)。取得に失敗したら不明として扱う(終了の根拠にしない)。"""
    try:
        return control.responding(pid)
    except Exception as e:  # noqa: BLE001 - 例外メッセージは記録しない(型名だけ)
        log(f" └> [印刷] IJCAD の応答状態を取得できませんでした({type(e).__name__})")
        return None


def print_dwg_for_pdf(script_path: str, sheets_nmb: int, expected_pdfs=(), *, log: Optional[Callable[[str], None]] = None,
                      popen=subprocess.Popen, control=None, clock=time.monotonic, pdf_valid: Callable[[str], bool] = pdf_is_complete,
                      close_grace: float = CLOSE_GRACE, kill_grace: float = KILL_GRACE) -> PrintOutcome:
    """IJCAD をバッチ(/b)で起動して印刷スクリプトを実行し、終了・PDF・応答状態を見守る(判定と時間はこのブロックの上の説明)。
    起動できない・設定を読めない場合は、従来の design_mod.print_dwg_for_pdf と同じく例外にせず NOT_RUN を返す(後続の PDF 検査が欠落として失敗にする)。
    expected_pdfs: このスクリプトが出力する PDF のパス(空なら PDF での判定をしない = ハング判定もしない)。"""
    log = log or (lambda _msg: None)
    control = control or WindowsProcessControl()
    expected = [str(p) for p in expected_pdfs]
    try:
        cad, launch_time, print_time, config_loader = _load_ijcad_settings(sheets_nmb)
        command = config_loader.convert_win_path(cad) + " /b " + config_loader.convert_win_path(script_path)
        planned = launch_time + sheets_nmb * print_time                       # 従来の待ち時間(予定の完了時刻)
        pace = planned + sheets_nmb * PRINT_SETTLE_PER_SHEET                  # 従来の「待ち + 呼び出し側の待機」。PDF が揃っても、ここまでは終了を待つ
        monitor_limit = max(pace, planned + HANG_CONFIRM_SECONDS + POLL_SECONDS)   # ハング(応答なし 15 秒)を確認できる最長時間
        start = clock()
        proc = popen(command)
    except Exception as e:  # noqa: BLE001 - 従来どおり握りつぶす(メッセージにはパスが入り得るので、型名だけ記録する)
        log(f" └> [印刷] IJCAD を起動できませんでした({type(e).__name__})")
        return PrintOutcome(NOT_RUN)
    try:
        launched = control.identity(proc.pid)
    except Exception:  # noqa: BLE001 - 照合できなければ、後で終了しない
        launched = None
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
        all_ready = False
        if expected:
            ok = [p for p in expected if pdf_valid(p)]
            if len(ok) > valid_count:
                valid_count, progress_at, hung_since = len(ok), now, None
            if len(ok) == len(expected):
                try:
                    sig = tuple(os.path.getsize(p) for p in expected)
                except OSError:
                    sig = None
                if sig is None:
                    ready_sig, ready_since = None, None
                elif sig != ready_sig:
                    ready_sig, ready_since = sig, now
                elif now - ready_since >= PDF_STABLE_SECONDS:
                    all_ready = True
            else:
                ready_sig, ready_since = None, None
                if elapsed >= planned:                                         # 予定の完了時刻を過ぎても PDF が揃わないときだけ、応答状態を見る
                    if _responding(control, proc.pid, log) is False:
                        hung_since = now if hung_since is None else hung_since
                    else:
                        hung_since = None
                    if hung_since is not None and now - hung_since >= HANG_CONFIRM_SECONDS and progress_at <= hung_since:
                        log(f" └> [印刷] PDF が揃わず、IJCAD が {int(now - hung_since)} 秒以上応答しません(完成済み {len(ok)}/{len(expected)} 件)")
                        return PrintOutcome(_terminate_hung(proc, launched, exe_name, control, log, close_grace, kill_grace))
        if all_ready and elapsed >= pace:                                      # PDF が完成。ペース(従来の間隔)まで IJCAD の終了を待ったので、正常
            if _responding(control, proc.pid, log) is False:
                log(" └> [印刷] PDF はすべて完成していますが、IJCAD が応答しないまま残っています(終了していません。手動で確認してください)")
                return PrintOutcome(PDFS_READY, left_unresponsive=True)
            log(" └> [印刷] 期待する PDF がすべて完成しました(IJCAD は終了していませんが、終了しません)")
            return PrintOutcome(PDFS_READY)
        if elapsed >= monitor_limit:
            break
    if expected:
        log(f" └> [印刷] 監視時間({int(monitor_limit)} 秒)内に PDF が揃いませんでしたが、IJCAD は応答中(または状態不明)のため終了しません。PDF の検査に進みます")
    return PrintOutcome(PENDING)
