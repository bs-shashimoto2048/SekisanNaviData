# 設計データのページ情報 `.db` から、製番単位の design.csv を作る。
#
# 仕様(確定分):
# - 対象は設計製番フォルダ直下の `*.db`(拡張子の大文字小文字は問わない)すべて。ファイル名が数字だけかどうかでは除外しない
#   (英字混じり・記号始まり・行数の少ない .db も対象)。元ファイルは読むだけ。
#   例外は Windows の画像キャッシュ `Thumbs.db` だけ(ファイル名が大文字小文字を問わず一致するものを除外。ページ情報ではない)。
# - .db は CP932 のテキストで、1 行 = 1 つの値。1〜24 行目を、次の列順で横に展開し、1 ファイルにつき 1 行を出す。
#   値は意味を変える加工をしない(空白・全角・記号はそのまま)。取り除くのは改行だけ。
# - 行数の扱い: 24 行に満たなくても失敗にしない。存在する行をその行番号の列に入れ、残りの列は空欄にして 24 列にそろえる。
#   24 行を超える分は読まない(先頭 24 行だけ使う)。空行は行番号を詰めず、その行の空欄として保持する。
# - 列名(見出し行)は DESIGN_COLUMNS のとおり。`blank` は見出しとして文字どおり出し、「年」「月」「日」は重複したまま。
# - 行の順番は、4 行目(ページ番号)の値で `0, 1, 2, 3, 01, 02, 03` を優先し、残りは次の規則で後ろに並べる(いつ実行しても同じ並び)。
#     優先 7 値 → 数字だけの値(数値順) → その他の値(文字列順) → 4 行目が無い/空のもの。同順位はファイル名順。
# - .db が 1 つも無い・読めない/CP932 でない・CSV に書けない文字がある → DesignDataError(部分的な design.csv は作らない)。
#   呼び出し側(Step.1)が製番の失敗として記録し、参照元を整えて再実行できるようにする。
# - 出力は一時ファイル経由(os.replace)で、途中失敗で壊れた CSV を残さない。CP932・BOMなし。
import csv
import logging
import os
import tempfile
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger(__name__)

DESIGN_CSV_NAME = "design.csv"
# 見出し行(提示済みの design.csv の表記どおり)。i 番目の列は .db の (i+1) 行目の値。
DESIGN_COLUMNS: List[str] = [
    "blank", "整理番号", "製番", "ページ番号", "blank", "図名", "図面尺度１", "図面尺度２",
    "承認設計", "年", "月", "日", "承認照査", "年", "月", "日",
    "製作設計", "年", "月", "日", "製作照査", "年", "月", "日",
]
LINE_COUNT = len(DESIGN_COLUMNS)   # 24
PAGE_COLUMN_INDEX = 3              # 4 列目(.db の 4 行目)= ページ番号
PAGE_PRIORITY = ["0", "1", "2", "3", "01", "02", "03"]
EXCLUDED_DB_NAME = "thumbs.db"     # Windows の画像キャッシュ(ページ情報ではない)。小文字で比較する


class DesignDataError(Exception):
    """製番の design.csv を作れない。メッセージは製番の失敗理由(表示・記録用)。"""


def find_page_db_files(design_dir) -> List[Path]:
    """設計製番フォルダ直下の *.db(大文字小文字を問わない)を名前順に返す。フォルダが無ければ空。
    Windows の画像キャッシュ `Thumbs.db`(ファイル名が大文字小文字を問わず一致するもの)だけは、ページ情報ではないので除く。"""
    folder = Path(design_dir)
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.iterdir()
                   if p.is_file() and p.suffix.lower() == ".db" and p.name.lower() != EXCLUDED_DB_NAME),
                  key=lambda q: q.name)


def read_page_db(path) -> List[str]:
    """1 つの .db の 1〜24 行目を、24 個の値のリストで返す。24 行に満たない分は空欄、超える分は捨てる。
    読めない/CP932 でないときは DesignDataError。改行以外は加工しない。"""
    path = Path(path)
    try:
        text = path.read_bytes().decode("cp932")
    except (OSError, UnicodeDecodeError) as e:
        raise DesignDataError(f"{path.name}: 読み込めません({type(e).__name__})") from e
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":       # 最終行の改行による空要素
        lines.pop()
    lines = lines[:LINE_COUNT]          # 空行は詰めずに保持し、先頭 24 行だけを使う
    return lines + [""] * (LINE_COUNT - len(lines))


def _page_order_key(page: str, filename: str) -> Tuple:
    if page in PAGE_PRIORITY:
        return (0, PAGE_PRIORITY.index(page), 0, "", filename)
    if page.isdigit():
        return (1, 0, int(page), page, filename)
    if page == "":                      # 4 行目が無い/空: 最後。ファイル名で安定させる
        return (3, 0, 0, "", filename)
    return (2, 0, 0, page, filename)


def build_design_rows(design_dir, log=None) -> List[List[str]]:
    """全 .db を読んで、ページ番号の値の順に並べた行(24 値)のリストを返す。失敗は DesignDataError。"""
    log = log or logger
    files = find_page_db_files(design_dir)
    if not files:
        reason = "設計製番フォルダに .db がありません(design.csv を作れません)"
        log.warning(f"design: {reason}")
        raise DesignDataError(reason)
    entries = []
    for f in files:
        try:
            values = read_page_db(f)
        except DesignDataError as e:
            log.warning(f"design: 読み取りに失敗したため design.csv を作成しません(部分的な CSV は出しません): {e}")
            raise
        entries.append((values[PAGE_COLUMN_INDEX], f.name, values))
    entries.sort(key=lambda e: _page_order_key(e[0], e[1]))
    return [values for _, _, values in entries]


def write_design_csv(rows: List[List[str]], product_dir) -> str:
    """製番フォルダ直下へ design.csv を CP932・BOMなしで書く。一時ファイル経由で、失敗しても壊れたファイルを残さない。"""
    dest = os.path.join(str(product_dir), DESIGN_CSV_NAME)
    fd, tmp = tempfile.mkstemp(prefix=".design_", suffix=".tmp", dir=str(product_dir))
    try:
        with os.fdopen(fd, "w", encoding="cp932", newline="") as f:
            writer = csv.writer(f, lineterminator="\r\n")
            writer.writerow(DESIGN_COLUMNS)
            writer.writerows(rows)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return dest


def read_design_page_ids(design_csv_path) -> List[str]:
    """作成済みの design.csv の「ページ番号」列を、行の順に返す(値は加工しない。空欄の行は空文字のまま)。

    印刷対象を決める入力のため、安全側に倒す: ファイルが無い/読めない/CP932 でない/見出し行が想定と違う/
    列が足りない行がある/データ行が 1 つも無い、のいずれでも DesignDataError(呼び出し側は製番の失敗にする)。"""
    path = Path(design_csv_path)
    try:
        text = path.read_bytes().decode("cp932")
    except (OSError, UnicodeDecodeError) as e:
        raise DesignDataError(f"{path.name} を読み取れません({type(e).__name__})") from e
    try:
        rows = list(csv.reader(text.splitlines()))
    except csv.Error as e:
        raise DesignDataError(f"{path.name} の形式が不正です({type(e).__name__})") from e
    if not rows or rows[0] != DESIGN_COLUMNS:
        raise DesignDataError(f"{path.name} の見出し行が想定と異なります")
    body = [r for r in rows[1:] if r]  # 完全な空行だけは読み飛ばす
    if not body:
        raise DesignDataError(f"{path.name} にデータ行がありません")
    if any(len(r) != LINE_COUNT for r in body):
        raise DesignDataError(f"{path.name} に列数の異なる行があります")
    return [r[PAGE_COLUMN_INDEX] for r in body]


def export_design_csv(design_dir, product_dir, log=None) -> str:
    """設計製番フォルダの .db から design.csv を作り、作ったパスを返す。作れないときは DesignDataError。"""
    log = log or logger
    rows = build_design_rows(design_dir, log)
    try:
        return write_design_csv(rows, product_dir)
    except Exception as e:  # noqa: BLE001 - CP932 に書けない文字・ディスク障害なども製番の失敗として扱う
        log.error(f"design.csv の書き出しに失敗しました: {type(e).__name__}: {e}")
        raise DesignDataError(f"design.csv の書き出しに失敗しました({type(e).__name__})") from e
