# 設計データのページ番号 `.db` から、製番単位の design.csv を作る。
#
# 仕様(確定分):
# - 対象は設計製番フォルダ直下の `<ページ番号>.db`(拡張子の大文字小文字は問わない。ファイル名が数字だけの .db)。元ファイルは読むだけ。
#   数字以外の名前の .db(Windows の Thumbs.db など)はページ番号 .db ではないので読まない(情報ログに残す)。
# - .db は CP932 のテキストで、1 行 = 1 つの値。1〜24 行目を、次の列順で横に展開し、1 ファイルにつき 1 行を出す。
#   値は意味を変える加工をしない(空白・全角・記号はそのまま)。取り除くのは改行だけ。25 行目以降は読まない。
# - 列名(見出し行)は DESIGN_COLUMNS のとおり。`blank` は見出しとして文字どおり出し、「年」「月」「日」は重複したまま。
# - 行の順番は、4 行目(ページ番号)の値で `0, 1, 2, 3, 01, 02, 03` を優先する。該当しないページは後ろに、数字だけの値を数値順、
#   その他を文字列順で並べ、同順位はファイル名で決める(いつ実行しても同じ並び)。
# - .db が無い・24 行に満たない・読めない/CP932 でない・CSV に書けない文字がある → DesignDataError(部分的な design.csv は作らない)。
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


class DesignDataError(Exception):
    """製番の design.csv を作れない。メッセージは製番の失敗理由(表示・記録用)。"""


def find_page_db_files(design_dir, log=None) -> List[Path]:
    """設計製番フォルダ直下の、ファイル名が数字だけの *.db(大文字小文字を問わない)を名前順に返す。フォルダが無ければ空。"""
    log = log or logger
    folder = Path(design_dir)
    if not folder.is_dir():
        return []
    files = []
    for p in sorted(folder.iterdir(), key=lambda q: q.name):
        if p.is_file() and p.suffix.lower() == ".db":
            if p.stem.isdigit():
                files.append(p)
            else:
                log.info(f"design: ページ番号ではない .db を読みません: {p.name}")
    return files


def read_page_db(path) -> List[str]:
    """1 つの .db の 1〜24 行目を値のリストで返す。読めない/行が足りないときは DesignDataError。改行以外は加工しない。"""
    path = Path(path)
    try:
        text = path.read_bytes().decode("cp932")
    except (OSError, UnicodeDecodeError) as e:
        raise DesignDataError(f"{path.name}: 読み込めません({type(e).__name__})") from e
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":       # 最終行の改行による空要素
        lines.pop()
    if len(lines) < LINE_COUNT:
        raise DesignDataError(f"{path.name}: 行が足りません({len(lines)} 行。{LINE_COUNT} 行が必要です)")
    return lines[:LINE_COUNT]


def _page_order_key(page: str, filename: str) -> Tuple:
    if page in PAGE_PRIORITY:
        return (0, PAGE_PRIORITY.index(page), 0, "", filename)
    if page.isdigit():
        return (1, 0, int(page), page, filename)
    return (2, 0, 0, page, filename)


def build_design_rows(design_dir, log=None) -> List[List[str]]:
    """全ページ .db を読んで、ページ番号の値の順に並べた行(24 値)のリストを返す。失敗は DesignDataError。"""
    log = log or logger
    files = find_page_db_files(design_dir, log)
    if not files:
        reason = "設計製番フォルダにページ番号の .db がありません(design.csv を作れません)"
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


def export_design_csv(design_dir, product_dir, log=None) -> str:
    """設計製番フォルダのページ番号 .db から design.csv を作り、作ったパスを返す。作れないときは DesignDataError。"""
    log = log or logger
    rows = build_design_rows(design_dir, log)
    try:
        return write_design_csv(rows, product_dir)
    except Exception as e:  # noqa: BLE001 - CP932 に書けない文字・ディスク障害なども製番の失敗として扱う
        log.error(f"design.csv の書き出しに失敗しました: {type(e).__name__}: {e}")
        raise DesignDataError(f"design.csv の書き出しに失敗しました({type(e).__name__})") from e
