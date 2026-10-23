# 印刷・変換・ファイル補助: 印刷対象ページの選別、5 枚ずつの分割、待機、DWG → DXF 変換、DWG コピー、図面使用中の判定。
# 分割前の cad_data_process.py から関数本体を変更せずに移した(S5)。外部からは `import cad_data_process as cad_mod` で使う。
import msvcrt
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Iterator, List

# ファイル有無チェック
def file_exists(directory: str, filename: str) -> bool:
    """
    指定ディレクトリ内に指定ファイルが存在するかチェック。

    Args:
        directory (str): チェックするディレクトリパス
        filename (str): チェックするファイル名

    Returns:
        bool: ファイルが存在すれば True、存在しなければ False
    """
    file_path = os.path.join(directory, filename)
    return os.path.exists(file_path)

# 分割リスト作成
def chunk_list(lst: List, chunk_size: int = 5) -> Iterator[List]:
    """
    指定リストを指定サイズごとに分割して順に返すジェネレーター関数

    Args:
        lst (List): 分割したいリスト
        chunk_size (int, optional): 分割サイズ（デフォルトは5）

    Yields:
        List: 分割されたリストのスライス
    """
    for i in range(0, len(lst), chunk_size):
        yield lst[i:i + chunk_size]

# 処理時間カウントダウン
def countdown(seconds: int):
    """
    指定秒数カウントダウンして、同じ行に残り時間を表示します。

    Args:
        seconds (int): 待機時間（秒）
    """
    for i in range(seconds, 0, -1):
        print(f"\r待機中: {i}秒", end="")
        time.sleep(1)

# 変換処理(.dxf->.dwg) ※このソフトはディレクトリ単位でしか処理できない_2025/11/27
def convert_dwg2dxf(
    input_dir: str,
    output_dir: str,
    converter_path: str = r"C:/Program Files/ODA/ODAFileConverter 26.10.0/ODAFileConverter.exe",
    version: str = "ACAD2013",
    ) -> tuple[str, int, int]:
    """_summary_

    Args:
        converter_path (_type_, optional): 変換ソフトパス
        input_dir (str): DWG格納ディレクトリパス
        output_dir (str): DXF格納ディレクトリパス
        version (str, optional): _description_. Defaults to "ACAD2013".

    Raises:
        RuntimeError: _description_

    Returns:
        tuple[str, int, int]: _description_

    Examples:
        >>> convert_dwg2dxf(
            "C:/Program Files/ODA/ODAFileConverter 26.10.0/ODAFileConverter.exe",
            "<DESIGN_PRODUCT_DIR>",
            "<OUTPUT_DIR>/dxf",
            )

    Note:
        変換ソフト の Windows のプリインストール先は

        >>> "C:/Program Files/ODA/ODAFileConverter 26.10.0/ODAFileConverter.exe"
    """

    # ODAFileConverterコマンド
    cmd = [
        converter_path,  # 実行ファイルパス
        input_dir,       # 入力フォルダ（dwg_path から自動取得）
        output_dir,      # 出力フォルダ
        version,         # DXFバージョン
        "DXF",           # 出力形式
        "0",             # サブフォルダ再帰しない
        ""               # 対象DWGパス
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        print(result.stdout)
        print(result.stderr)

    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"DWG→DXF変換に失敗しました:\n{e.stderr}")

# ファイルコピー
def copy_files_from_list(
    src_dir: str,
    dst_dir: str,
    file_names: list,
    extension: str
) -> None:
    """
    指定ファイル名リストを元に、入力ディレクトリから出力ディレクトリへコピーする。

    Parameters:
        src_dir (str):入力元ディレクトリパス
        dst_dir (str):出力先ディレクトリパス
        file_names (list):コピー対象のファイル名リスト（拡張子なし）
        extension (str):ファイルの拡張子（例: ".dwg", ".txt"）

    Examples:

    >>> copy_files_from_list(
        "<DESIGN_PRODUCT_DIR>",
        "<OUTPUT_DIR>/dwg",
        "["21","22","23","24","25",]",
        ".dwg"
        )
        return なし
    """

    src_dir = Path(src_dir)
    dst_dir = Path(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)

    for fname in file_names:
        src_file = src_dir / f"{fname}{extension}"
        dst_file = dst_dir / f"{fname}{extension}"

        if src_file.exists():
            shutil.copy2(src_file, dst_file)
            print(f"Copied: {src_file} -> {dst_file}")
        else:
            print(f"File not found, skipped: {src_file}")

# ファイル名取得
def listing_files(folder_path: str, extension: str = "dwg") -> list[str]:

    """指定フォルダ内の指定拡張子ファイルをソート済みでリストで取得"""
    if not os.path.exists(folder_path):
        raise FileNotFoundError(f"フォルダが存在しません: {folder_path}")

    files_list = [f for f in os.listdir(folder_path) if f.lower().endswith(f".{extension}")]
    files_list.sort()  # ファイル名順にソート
    return files_list

# 図面使用中(ロックファイル)確認
def is_file_in_use(path):
    try:
        with open(path, "a") as f:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        return False
    except OSError:
        return True

# ----------------------------------------------------------------------------
# 印刷対象ページの選別
# ----------------------------------------------------------------------------
EXCLUDED_PAGE_PREFIX = "x"  # 先頭がこの文字(大文字小文字は区別しない)の図面は、印刷・画像化・推論・データ作成のすべてから除外する

def is_excluded_page(name) -> bool:
    """図面名(ページ名。拡張子の有無は問わない)の先頭が X/x なら True。Windows のファイル名は大文字小文字を区別しないため両方を除外する。"""
    return Path(str(name)).stem.lower().startswith(EXCLUDED_PAGE_PREFIX)

PRINT_PAGE_ID_MAX_LENGTH = 6  # design.csv の「ページ番号」がこの文字数を超える図面は印刷対象にしない

def is_printable_page_id(page_id) -> bool:
    """design.csv の「ページ番号」が印刷対象の条件を満たすか。
    条件: 前後の空白を除いて空でなく、6 文字以内で、先頭が X/x でないこと(先頭ゼロなど文字列の中身は加工しない)。"""
    s = str(page_id).strip()
    return s != "" and len(s) <= PRINT_PAGE_ID_MAX_LENGTH and not s.casefold().startswith(EXCLUDED_PAGE_PREFIX)

def printable_page_keys(design_page_ids) -> frozenset:
    """design.csv の「ページ番号」の一覧から、印刷対象の条件を満たすものの照合キー(前後の空白を除き、大文字小文字を区別しない形)を返す。
    先頭ゼロは保持する(`01` と `1` は別のページ)。"""
    return frozenset(str(p).strip().casefold() for p in design_page_ids if is_printable_page_id(p))

def page_key(name) -> str:
    """ページ名(図面名・.baninf の PAGE。拡張子は付けない)を、printable_page_keys と照合するためのキーにする。"""
    return str(name).strip().casefold()

def list_print_pages(directory: str, file_ext: str, design_page_ids) -> List[str]:
    """設計製番フォルダ直下の図面(file_ext)のうち、design.csv の「ページ番号」が印刷対象の条件を満たすものに対応する
    ページ名(拡張子なし。図面ファイル名の綴りのまま)を、従来のページ並び(文字列の昇順)で返す。

    design.csv に無い図面、ページ番号が 7 文字以上の図面、X/x 始まりの図面は印刷しない。照合は大文字小文字を区別しない。
    `design_page_ids` は必須(読み取れない場合に、条件なしで全図面を印刷してしまわないため)。
    出力 PDF 名は従来どおり `{ページ名}.pdf`。ファイルを開かず、一覧だけを読む。
    """
    ext = file_ext.lower()
    allowed = printable_page_keys(design_page_ids)
    pages = {p.stem for p in Path(directory).iterdir() if p.is_file() and p.suffix.lower() == ext}
    return sorted(p for p in pages if page_key(p) in allowed)
