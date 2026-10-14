# 設計データの `*.boxspec` から、製番ごとの材料一覧 material.csv を作る。
#
# 仕様(確定分):
# - 対象は設計フォルダ(.baninf と同じ場所)直下の `*.boxspec`(拡張子の大文字小文字は問わない)。元ファイルは読むだけ。
# - ファイル名(拡張子を除く)をページ番号 PAGE とする(例: 301.boxspec -> 301)。
# - CP932。1 行 = `|` 区切りの要素の並びで、各要素は最初の `$` でキーと値に分ける。値は加工しない。
#   行の先頭要素は `シンボル名$<名前>`。
# - `BSP-STEELA-*` のシンボルから構成部の材料(STEEL-* の 9 キー)を取り出す。`BSP-CABTYPEA` の CABTYPE は材料行に関連づける。
# - 1 行 = ページ × 材料シンボル × 構成部。
# - 既知の構成部キーが行に無ければ MATERIAL_SPEC は「未記載」。明示値「無し」はそのまま保持して区別する。
#   キーはあるが値が空の場合も、記載が無いので「未記載」にする(「無し」とは別。実データに多い)。
# - 未知の STEEL-* キーは捨てずに行として残し(COMPONENT_NAME は空)、ログに記録する。
# - 不完全な一覧を完全な一覧として出さない: いずれかのファイルが読めない/解析できないときは material.csv を出さずログに残す。
# - material.csv が作れない(.boxspec が無い・解析失敗・材料行が無い・書き出し失敗)ときは MaterialDataError を送出する。
#   呼び出し側(Step.1)が製番の失敗として記録する。material.csv が無い状態を成功扱いで通過させない(参照元を整えて再実行する)。
# - 出力は一時ファイル経由(os.replace)で、途中失敗で半端な material.csv を残さない。CP932・BOMなし。
import csv
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MATERIAL_CSV_NAME = "material.csv"
MATERIAL_COLUMNS = ["PAGE", "SOURCE_FILE", "SYMBOL_NAME", "CABTYPE", "COMPONENT_CODE", "COMPONENT_NAME", "MATERIAL_SPEC"]

NOT_DESCRIBED = "未記載"   # 構成部のキーが無い/値が空
NOTHING = "無し"           # 明示値(そのまま保持)

# 既知の構成部(出力順)
COMPONENTS: List[Tuple[str, str]] = [
    ("STEEL-BASE", "基礎ベース"),
    ("STEEL-SIDE", "側板"),
    ("STEEL-ROOF", "屋根・天井板"),
    ("STEEL-PANEL", "パネル（背面板）"),
    ("STEEL-DIVIDE", "仕切り板"),
    ("STEEL-FRAME", "組枠"),
    ("STEEL-BOTTOM", "底板"),
    ("STEEL-INSIDEPANEL", "補助・内部パネル"),
    ("STEEL-DOOR", "扉板"),
]
KNOWN_CODES = {code for code, _ in COMPONENTS}

SYMBOL_KEY = "シンボル名"
STEEL_SYMBOL_PREFIX = "BSP-STEELA-"
CABTYPE_SYMBOL = "BSP-CABTYPEA"
CABTYPE_KEY = "CABTYPE"


class BoxspecError(Exception):
    """1 つの .boxspec を読めない/解析できない。"""


class MaterialDataError(Exception):
    """製番の material.csv を作れない。メッセージは製番の失敗理由(表示・記録用)。"""


def find_boxspec_files(design_dir) -> List[Path]:
    """設計フォルダ直下の *.boxspec(大文字小文字を問わない)をページ順に返す。フォルダが無ければ空。"""
    folder = Path(design_dir)
    if not folder.is_dir():
        return []
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".boxspec"]
    return sorted(files, key=lambda p: _page_sort_key(p.stem))


def _page_sort_key(page: str):
    # 数字だけのページは数値順、それ以外は後ろに文字列順(同順位は元の文字列で安定させる)
    return (0, int(page), page) if page.isdigit() else (1, 0, page)


def _parse_line(line: str, source: str, lineno: int) -> Optional[List[Tuple[str, str]]]:
    """1 行を (キー, 値) のリストにする。空行は None。`$` を含まない要素があれば BoxspecError。"""
    text = line.strip()
    if not text:
        return None
    pairs = []
    for element in text.split("|"):
        if element == "":       # 行末の `|` による空要素
            continue
        if "$" not in element:
            raise BoxspecError(f"{source}: {lineno}行目に `$` の無い要素があります")
        key, value = element.split("$", 1)       # 値の中の `$` はそのまま値に残す
        pairs.append((key, value))
    return pairs


def parse_boxspec(path, log=None) -> List[dict]:
    """1 つの .boxspec から材料行(辞書)を作る。読めない/解析できない場合は BoxspecError。"""
    log = log or logger
    path = Path(path)
    source, page = path.name, path.stem
    try:
        text = path.read_bytes().decode("cp932")
    except (OSError, UnicodeDecodeError) as e:
        raise BoxspecError(f"{source}: 読み込めません({type(e).__name__})") from e

    steel_symbols: List[Tuple[str, Dict[str, str], List[str]]] = []   # (シンボル名, {コード: 値}, 出現順のコード)
    cabtypes: List[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        pairs = _parse_line(line, source, lineno)
        if not pairs:
            continue
        if pairs[0][0] != SYMBOL_KEY:
            log.info(f"boxspec: {source} {lineno}行目: シンボル名で始まらない行を無視します")
            continue
        symbol = pairs[0][1]
        if symbol.startswith(STEEL_SYMBOL_PREFIX):
            values: Dict[str, str] = {}
            order: List[str] = []
            for key, value in pairs[1:]:
                if not key.startswith("STEEL-"):
                    continue
                if key in values:
                    raise BoxspecError(f"{source}: {lineno}行目に重複した {key} があります")
                values[key] = value
                order.append(key)
            steel_symbols.append((symbol, values, order))
        elif symbol == CABTYPE_SYMBOL:
            for key, value in pairs[1:]:
                if key == CABTYPE_KEY:
                    cabtypes.append(value)
        else:
            log.info(f"boxspec: {source} {lineno}行目: 材料と関係しないシンボル {symbol} を無視します")

    cabtype = _resolve_cabtype(source, cabtypes, log)

    rows: List[dict] = []
    for symbol, values, order in steel_symbols:
        for code, name in COMPONENTS:
            value = values.get(code, "")
            rows.append(_row(page, source, symbol, cabtype, code, name, value if value != "" else NOT_DESCRIBED))
        for code in order:
            if code not in KNOWN_CODES:
                log.warning(f"boxspec: {source}: 未対応の構成部キー {code}(値は保持します)")
                value = values[code]
                rows.append(_row(page, source, symbol, cabtype, code, "", value if value != "" else NOT_DESCRIBED))
    return rows


def _resolve_cabtype(source: str, cabtypes: List[str], log) -> str:
    """材料行に関連づける CABTYPE。1 種類だけならそれを使う。無ければ空。
    複数の値があって対応関係が決められないときは推測せず、空にして警告する(実例を確認してから扱いを決める)。"""
    distinct = list(dict.fromkeys(cabtypes))
    if not distinct:
        return ""
    if len(distinct) == 1:
        return distinct[0]
    log.warning(f"boxspec: {source}: CABTYPE が複数({len(distinct)} 種類)あり材料行との対応が決められないため、CABTYPE は空にします")
    return ""


def _row(page, source, symbol, cabtype, code, name, spec) -> dict:
    return {"PAGE": page, "SOURCE_FILE": source, "SYMBOL_NAME": symbol, "CABTYPE": cabtype,
            "COMPONENT_CODE": code, "COMPONENT_NAME": name, "MATERIAL_SPEC": spec}


def build_material_rows(design_dir, log=None) -> List[dict]:
    """設計フォルダの全 .boxspec をまとめた行。.boxspec が無い・1 つでも読めない/解析できない・材料行が無いときは MaterialDataError
    (不完全な一覧を出さないため。理由はログにも残す)。"""
    log = log or logger
    files = find_boxspec_files(design_dir)
    if not files:
        reason = "設計フォルダに *.boxspec がありません(材料情報を作れません)"
        log.warning(f"boxspec: {reason}")
        raise MaterialDataError(reason)
    rows: List[dict] = []
    for f in files:
        try:
            rows.extend(parse_boxspec(f, log))
        except BoxspecError as e:
            log.warning(f"boxspec の解析に失敗したため material.csv を作成しません(部分的な一覧は出しません): {e}")
            raise MaterialDataError(f"boxspec の解析に失敗しました: {e}") from e
    if not rows:
        reason = "*.boxspec に材料シンボル(BSP-STEELA-*)がなく、材料行が 1 件もありません"
        log.warning(f"boxspec: {reason}")
        raise MaterialDataError(reason)
    return rows


def write_material_csv(rows: List[dict], product_dir) -> str:
    """製番フォルダ直下へ material.csv を CP932・BOMなしで書く。一時ファイル経由で、失敗しても半端なファイルを残さない。"""
    dest = os.path.join(str(product_dir), MATERIAL_CSV_NAME)
    fd, tmp = tempfile.mkstemp(prefix=".material_", suffix=".tmp", dir=str(product_dir))
    try:
        with os.fdopen(fd, "w", encoding="cp932", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=MATERIAL_COLUMNS, lineterminator="\r\n")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return dest


def export_material_csv(design_dir, product_dir, log=None) -> str:
    """設計フォルダの .boxspec から material.csv を作り、作ったパスを返す。
    作れないときは MaterialDataError(.boxspec 無し・解析失敗・材料行無し・書き出し失敗)。部分的な material.csv は残さない。
    製番フォルダは毎回新規に作られるため、古い material.csv は残らない。"""
    log = log or logger
    rows = build_material_rows(design_dir, log)
    try:
        return write_material_csv(rows, product_dir)
    except Exception as e:  # noqa: BLE001 - 書き出し失敗も製番の失敗として扱う
        log.error(f"material.csv の書き出しに失敗しました: {type(e).__name__}: {e}")
        raise MaterialDataError(f"material.csv の書き出しに失敗しました: {type(e).__name__}") from e
