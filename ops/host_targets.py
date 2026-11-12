# 定期実行と「今すぐ実行」が共有する、今回の対象の確定(同じ関数・同じ条件)。実行のたびにホスト DB を読み取り専用で照会する。
#
# 対象 = 「実行月(当月〜当日)に 3 番図面が出図済み」かつ「製番が設定 `target_initial`(A1)で始まる」製番のうち、
#   - 出力先に製番フォルダが無いもの(未処理)、または
#   - 出力先に製番フォルダがあっても、未完了マーカーがあるもの(再処理)。
# 出力済みで、未完了マーカーが無い製番は除く。固定の製番リスト・件数や時間の上限は使わない(取得した対象をすべて処理する)。
#
# ホストの一覧が取得できない(接続・認証・照会の失敗)ときは、0 件成功にせず `TargetError`(固定の理由)にする。
# bs_pkg.host_mod.create_n3_design_list は、失敗を握りつぶして「出図なし」と同じ空のリストを返すため、そのままは使えない。
# ここでは、同じ接続・同じテーブルの SELECT を、失敗を例外にして実行する(読み取り専用。UPDATE/INSERT/DELETE は発行しない)。
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional, Sequence

HOST_FAILED_REASON = ("ホストの製番一覧を取得できないため、実行しませんでした({kind})。"
                      "製番フォルダの作成・印刷・API の呼び出しは行っていません。")
OUTPUT_DIR_REASON = ("出力先のフォルダを確認できないため、実行しませんでした。"
                     "製番フォルダの作成・印刷・API の呼び出しは行っていません。")


class TargetError(RuntimeError):
    """対象を確定できない。メッセージは固定の理由(履歴・画面に出してよい。パス・接続情報・例外メッセージは含めない)。"""


@dataclass(frozen=True)
class TargetPlan:
    targets: tuple          # 今回処理する製番(この順)
    listed: int             # 当月(〜当日)の 3 番出図済みの製番(頭文字で絞ったあと)の件数

    def summary(self) -> str:
        if not self.targets:
            return f"対象なし(当月の出図済み {self.listed} 件は、すべて処理済み、または出図がありません)"
        return f"対象 {len(self.targets)} 件(当月の出図済み {self.listed} 件、うち処理済み {self.listed - len(self.targets)} 件を除外。未完了マーカーのある製番は再処理)"


def fetch_design3_rows(today: Optional[datetime] = None) -> List[dict]:
    """ホスト DB から、当月の 3 番出図の行([{フィールド名: 値}, …])を読み取り専用で取得する。失敗は例外のまま(握りつぶさない)。
    host_mod.access_db と同じ接続・同じ SQL(`HostAccess.search_design3`)。出図が 1 件も無い月は、正常な空のリスト。"""
    import win32com.client                      # 遅延 import(Windows + pywin32。テストは fetch を差し替える)
    from bs_pkg import config_loader, host_mod

    today = today or datetime.now()
    table, fields = host_mod.get_tables(8)
    key = fields[7]
    sql = (f"SELECT * FROM {table} AS A "
           f"WHERE EXTRACT(MONTH FROM A.{key}) = {int(today.strftime('%m'))} "
           f"AND EXTRACT(YEAR FROM A.{key}) = {int(today.strftime('%Y'))}")
    assert sql.lstrip().upper().startswith("SELECT")          # 読み取り専用であることの確認(更新系は発行しない)
    product_field = fields[0]
    local = config_loader.load_yaml(host_mod.LOCAL_CONFIG_PATH)
    connection = rows = None
    try:
        connection = win32com.client.Dispatch("ADODB.Connection")
        connection.Open("Provider=IBMDA400;Data source=" + local["host_name"] + ";User ID=" + local["user_id"]
                        + ";Password=" + local["password"] + ";")
        rows = win32com.client.Dispatch("ADODB.Recordset")
        rows.Open(sql, connection)
        out = []
        while not rows.EOF:
            value = rows.Fields(product_field).Value
            out.append({product_field: (value or "").strip() if isinstance(value, str) or value is None else str(value).strip()})
            rows.MoveNext()
        return out
    finally:
        for handle in (rows, connection):
            try:
                if handle is not None:
                    handle.Close()
            except Exception:  # noqa: BLE001 - 後始末の失敗で、取得結果・元の例外を隠さない
                pass


def resolve_targets(output_dir: str, target_initial: str, fetch: Callable[[], Sequence[dict]] = fetch_design3_rows,
                    list_missing: Optional[Callable[[str, list], list]] = None,
                    with_incomplete: Optional[Callable[[str, list, list], list]] = None) -> TargetPlan:
    """今回の対象を確定する(副作用なし。出力先の一覧を読むだけ)。ホストの取得に失敗したとき・出力先を読めないときは TargetError。
    list_missing / with_incomplete の既定は、従来の通常処理(app.main)と同じ関数(host_mod.get_missing_folders・
    product_flow.include_incomplete_products)。"""
    if list_missing is None or with_incomplete is None:
        from bs_pkg import host_mod
        from pipeline import product_flow
        list_missing = list_missing or host_mod.get_missing_folders
        with_incomplete = with_incomplete or product_flow.include_incomplete_products
    try:                                                   # 読めないまま続けると、全製番が「未処理」(または 0 件)に見えてしまう(host_mod.get_missing_folders は失敗を空で返す)
        os.listdir(output_dir)
    except OSError:
        raise TargetError(OUTPUT_DIR_REASON) from None
    try:
        rows = list(fetch())
    except Exception as e:  # noqa: BLE001 - 理由は固定文(例外メッセージ・接続情報は出さない)
        raise TargetError(HOST_FAILED_REASON.format(kind=type(e).__name__)) from None
    listed = sorted({p for row in rows for p in row.values() if p and p.startswith(target_initial)})
    try:
        missing = list(list_missing(output_dir, listed))
        targets = list(with_incomplete(output_dir, listed, missing))
    except Exception:  # noqa: BLE001
        raise TargetError(OUTPUT_DIR_REASON) from None
    return TargetPlan(tuple(targets), len(listed))
