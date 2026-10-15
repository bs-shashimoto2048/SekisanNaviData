# パスの安全判定(隔離領域の内側か・リンク/ジャンクション/UNC/ネットワークドライブを経由しないか)。標準ライブラリのみ。
# tools/smoke_interim_api.py(スモーク)と interim/runs.py(製番処理の隔離出力先の検証)が共有する。実体解決しない「表記上の位置」で判定し、
# そこに至る既存の各階層に再解析ポイントがあれば拒否する(実体解決後に内側に見えても、外へ出る経路を作らない)。
import os
import stat
from pathlib import Path


def _norm(p) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def is_under(path, root) -> bool:
    """実体解決後に、path が root の配下(root 自身は不可)か。"""
    p, r = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(root))
    try:
        return os.path.commonpath([p, r]) == r and p != r
    except ValueError:                                  # 別ドライブ等
        return False


def lexically_within(path, root, allow_equal: bool = True) -> bool:
    """表記上(.. を畳んだ後)、path が root の内側か。実体解決はしない。"""
    p, r = _norm(path), _norm(root)
    try:
        if os.path.commonpath([p, r]) != r:
            return False
    except ValueError:
        return False
    return allow_equal or p != r


def is_link(path) -> bool:
    """シンボリックリンク・ジャンクション等の再解析ポイント(Windows の FILE_ATTRIBUTE_REPARSE_POINT)か。存在しなければ False。"""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def has_link_in_path(path, base) -> bool:
    """base から path までの、既存の各階層(path 自身を含む)に再解析ポイントがあるか。path は base の内側であること。"""
    rel = os.path.relpath(os.path.normpath(str(path)), os.path.normpath(str(base)))
    cur = os.path.normpath(str(base))
    for part in Path(rel).parts:
        if part in ("", "."):
            continue
        cur = os.path.join(cur, part)
        if is_link(cur):
            return True
    return False


def is_network_drive(path) -> bool:
    """Windows のネットワークドライブ(DRIVE_REMOTE)か。UNC もここで True になる。Windows 以外は False。"""
    if os.name != "nt":
        return False
    import ctypes
    anchor = os.path.splitdrive(os.path.realpath(path))[0]
    if anchor.startswith("\\\\"):
        return True
    return ctypes.windll.kernel32.GetDriveTypeW(anchor + "\\") == 4


def _is_unc_text(text: str) -> bool:
    return str(text).replace("/", "\\").startswith("\\\\")
