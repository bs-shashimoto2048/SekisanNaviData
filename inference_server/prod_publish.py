"""本番推論 API の接続設定(共有 server.yaml)の公開。旧サーバー(起動のたびに無条件で上書き)の代わりの、明示的で安全な更新。

方針(docs/production_inference_api.md §5):
  - **既定では書かない。** 書くのは、起動時に `--publish-config` を明示したときだけ。`check` サブコマンドは計画(変更するキー)を出すだけで書かない。
  - 既存の server.yaml が**稼働中、または稼働しているか判定できない**接続先を指しているときは、常に拒否する(`--replace-stale` でも不可)。
    「古い(使われていない)」と判定するのは、接続先が**接続を明示的に拒否した**(TCP の接続拒否)ときだけ。
    読み取りタイムアウト・到達不能・名前解決失敗・接続先を読めない・形式が不正、は「判定不能」= 拒否(手動で確認する)。
  - 既存の server.yaml があって**接続拒否(古い)**のときは、`--replace-stale` の明示がない限り拒否する。
  - 既存ファイルは**1 回だけ読み**、そのバイト列をそのまま退避し、同じバイト列を解釈して新しい内容を作る。置換の直前に、内容が変わっていないこと・
    接続先がまだ使われていないことを再確認する。同じフォルダの排他ロックファイルで、同時の公開を防ぐ。
  - 書き込みは同じフォルダの一時ファイル → `os.replace`(原子的。短い再試行つき。失敗時は一時ファイルを残さない)。
  - 既存のキーは**保持**し、変えるのは host の ip・port・pc_ip と、識別用の mode・instance_id・model だけ(proxy・directory・その他の未知キーは既存のまま。
    既存が無いときだけ、設定の proxy を使う)。
  - 計画・結果には、パス・アドレス・ポート・ハッシュ全文を出さない(キー名と状態だけ)。
"""
from __future__ import annotations

import datetime
import hashlib
import ipaddress
import os
import time
import uuid
from dataclasses import dataclass, field

import requests
import yaml

from prod_config import ProdConfig

REPLACE_RETRIES = 5


class PublishRefused(RuntimeError):
    """公開を拒否した。code は固定の識別子(CLI の終了理由・テスト用)。メッセージは固定文。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class PublishPlan:
    existing: str                         # absent / unreadable / present
    probe: str                            # none / live-other / live-self / dead / unknown
    action: str                           # create / replace / refuse
    refuse_code: str | None = None
    changes: list = field(default_factory=list)   # [(キー, added|changed|unchanged|removed)] 値は含めない

    def public(self) -> dict:
        return {"existing": self.existing, "probe": self.probe, "action": self.action,
                "refuse_code": self.refuse_code, "changes": [{"key": k, "status": s} for k, s in self.changes]}


def _parse(raw: bytes):
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except (UnicodeDecodeError, yaml.YAMLError):
        return None


def read_existing_bytes(path: str):
    """(状態, バイト列)。ファイルが**無い**ことが確認できたときだけ absent。それ以外の読み取り失敗(共有の切断・権限など)は unreadable。"""
    try:
        with open(path, "rb") as f:
            return "present", f.read()
    except FileNotFoundError:
        return "absent", None
    except OSError:
        return "unreadable", None


def read_existing(path: str):
    """(状態, データ)。状態は absent / unreadable / present(YAML の辞書として読めたときだけ)。"""
    state, raw = read_existing_bytes(path)
    if state != "present":
        return state, None
    data = _parse(raw)
    return ("present", data) if data is not None else ("unreadable", None)


def _target(data: dict):
    """既存 server.yaml の接続先(IPv4 リテラルと数値ポート)。読めなければ None(= 判定不能)。ホスト名・文字列は信用しない。"""
    try:
        ip = ipaddress.ip_address(str(data["host"]["pc_ip"]))
        port = data["host"]["port"]
    except Exception:  # noqa: BLE001
        return None
    if ip.version != 4 or isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        return None
    return str(ip), port


def _is_refused(e: BaseException) -> bool:
    """TCP の接続拒否(待ち受けるプロセスが無い)か。原因の連鎖をたどる。"""
    seen, stack = set(), [e]
    while stack and len(seen) < 16:
        x = stack.pop()
        if x is None or id(x) in seen:
            continue
        seen.add(id(x))
        if isinstance(x, ConnectionRefusedError) or getattr(x, "winerror", None) == 10061:
            return True
        stack.extend([getattr(x, "__cause__", None), getattr(x, "__context__", None), getattr(x, "reason", None)])
        stack.extend(a for a in getattr(x, "args", ()) if isinstance(a, BaseException))
    return False


def probe_existing(data: dict, our_instance_id: str, timeout: float, *, get=requests.get) -> str:
    """既存 server.yaml の接続先に /health を 1 回だけ送る(プロキシなし・リダイレクト不追跡)。
    live-self: 自分の instance_id / live-other: 応答がある(自分以外・形式不明を含む) / dead: **接続拒否だけ** / unknown: 上記以外
    (タイムアウト・到達不能・接続先を読めない)。値は返さない。"""
    t = _target(data)
    if t is None:
        return "unknown"
    try:
        res = get(f"http://{t[0]}:{t[1]}/health", proxies={"http": None, "https": None}, timeout=timeout, allow_redirects=False)
    except requests.exceptions.RequestException as e:
        return "dead" if _is_refused(e) else "unknown"
    try:
        body = res.json()
        if isinstance(body, dict) and body.get("instance_id") == our_instance_id:
            return "live-self"
    except ValueError:
        pass
    return "live-other"


def build_content(cfg: ProdConfig, instance_id: str, model_public: dict, existing: dict | None) -> dict:
    """既存の内容を土台にして、変えるキーだけを上書きする(未知キー・proxy・directory は既存のまま)。"""
    content = dict(existing or {})
    host = dict(content.get("host") or {})
    host.update({"ip": cfg.host, "port": cfg.port, "pc_ip": cfg.host})
    content["host"] = host
    if not content.get("proxy") and cfg.proxy:
        content["proxy"] = dict(cfg.proxy)
    content["mode"] = "production"
    content["instance_id"] = instance_id
    content["model"] = model_public
    return content


def _flat(d, prefix=""):
    out = {}
    for k, v in (d or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flat(v, key + "."))
        else:
            out[key] = v
    return out


def diff_keys(old: dict | None, new: dict) -> list:
    a, b = _flat(old), _flat(new)
    rows = []
    for k in sorted(set(a) | set(b)):
        if k not in a:
            rows.append((k, "added"))
        elif k not in b:
            rows.append((k, "removed"))
        else:
            rows.append((k, "unchanged" if a[k] == b[k] else "changed"))
    return rows


_MESSAGES = {
    "existing_unreadable": "既存の server.yaml を読めない(共有に接続できない・権限・形式の不備)ため、上書きしません。手動で確認してください。",
    "live_other_instance": "既存の server.yaml の接続先が稼働中です。上書きしません。",
    "status_unknown": "既存の server.yaml の接続先が稼働中かどうか判定できません(タイムアウト・到達不能など)。上書きしません。手動で確認してください。",
    "stale_needs_replace_flag": "既存の server.yaml の接続先は接続を拒否しました(古い設定)。上書きするには --replace-stale を明示してください。",
    "changed_during_publish": "公開の途中で既存の server.yaml が変更されました。上書きしません。",
    "publish_in_progress": "別の公開処理が進行中です(ロックファイルがあります)。上書きしません。",
    "backup_exists": "同名の退避ファイルが既にあります。上書きしません。",
    "backup_mismatch": "退避ファイルの内容が一致しません。上書きしません。",
    "verify_failed": "書き込み後の確認に失敗しました。",
    "write_failed": "接続設定を書き込めませんでした。",
}


def _decide(state, data, pr, replace_stale):
    """(action, refuse_code)。"""
    if state == "absent":
        return "create", None
    if state == "unreadable":
        return "refuse", "existing_unreadable"
    if pr == "live-other":
        return "refuse", "live_other_instance"
    if pr == "unknown":
        return "refuse", "status_unknown"
    if pr == "live-self":
        return "replace", None
    return ("replace", None) if replace_stale else ("refuse", "stale_needs_replace_flag")


def plan_publish(cfg: ProdConfig, instance_id: str, model_public: dict, *, replace_stale: bool = False,
                 probe=None) -> PublishPlan:
    probe = probe or probe_existing                    # 呼び出し時に解決する(テストが差し替えられるように)
    state, data = read_existing(cfg.server_yaml)
    pr = probe(data, instance_id, cfg.timeout_probe_sec) if state == "present" else "none"
    action, code = _decide(state, data, pr, replace_stale)
    changes = diff_keys(data, build_content(cfg, instance_id, model_public, data)) if state in ("present", "absent") else []
    return PublishPlan(state, pr, action, code, changes)


def _replace_with_retry(tmp: str, dst: str) -> None:
    """共有上で読み取り中のクライアントと衝突して PermissionError になることがあるため、短い再試行を行う。"""
    for attempt in range(REPLACE_RETRIES):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            if attempt == REPLACE_RETRIES - 1:
                raise
            time.sleep(0.2 * (attempt + 1))


def _atomic_write(path: str, text: str) -> None:
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp, path)
    finally:
        try:
            os.remove(tmp)                  # 置換できたときは既に無い。失敗したときは残さない
        except OSError:
            pass


def _sha(b: bytes | None) -> str | None:
    return hashlib.sha256(b).hexdigest() if b is not None else None


def publish(cfg: ProdConfig, instance_id: str, model_public: dict, *, replace_stale: bool = False,
            probe=None, now=datetime.datetime.utcnow) -> dict:
    """計画どおりに server.yaml を公開する。拒否・失敗は PublishRefused(code は固定)。戻り値は公開してよい要約(パス・値なし)。"""
    probe = probe or probe_existing
    lock = cfg.server_yaml + ".lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise PublishRefused("publish_in_progress", _MESSAGES["publish_in_progress"]) from None
    except OSError:
        raise PublishRefused("write_failed", _MESSAGES["write_failed"]) from None
    os.close(fd)
    try:
        return _publish_locked(cfg, instance_id, model_public, replace_stale, probe, now)
    except PublishRefused:
        raise
    except OSError:
        raise PublishRefused("write_failed", _MESSAGES["write_failed"]) from None
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def _publish_locked(cfg, instance_id, model_public, replace_stale, probe, now) -> dict:
    state, raw = read_existing_bytes(cfg.server_yaml)           # 既存は 1 回だけ読む(以降はこのバイト列を使う)
    data = _parse(raw) if state == "present" else None
    if state == "present" and data is None:
        state = "unreadable"
    pr = probe(data, instance_id, cfg.timeout_probe_sec) if state == "present" else "none"
    action, code = _decide(state, data, pr, replace_stale)
    if action == "refuse":
        raise PublishRefused(code, _MESSAGES[code])
    changes = diff_keys(data, build_content(cfg, instance_id, model_public, data))
    new = build_content(cfg, instance_id, model_public, data)
    backup_name = None
    if state == "present":
        os.makedirs(cfg.backup_dir, exist_ok=True)
        backup_name = f"server.yaml.bak-{now().strftime('%Y%m%dT%H%M%SZ')}-{instance_id[:8]}"
        dst = os.path.join(cfg.backup_dir, backup_name)
        try:
            with open(dst, "xb") as out:                         # 'x': 同名があれば失敗(上書きしない)
                out.write(raw)
        except FileExistsError:
            raise PublishRefused("backup_exists", _MESSAGES["backup_exists"]) from None
        with open(dst, "rb") as b:
            if b.read() != raw:
                os.remove(dst)
                raise PublishRefused("backup_mismatch", _MESSAGES["backup_mismatch"])
    # 置換の直前の再確認: 既存の内容が変わっていない / 接続先がまだ使われていない
    state2, raw2 = read_existing_bytes(cfg.server_yaml)
    if state2 != state or _sha(raw2) != _sha(raw):
        raise PublishRefused("changed_during_publish", _MESSAGES["changed_during_publish"])
    if state == "present":
        pr2 = probe(data, instance_id, cfg.timeout_probe_sec)
        if pr2 not in ("dead", "live-self"):
            raise PublishRefused("live_other_instance" if pr2 == "live-other" else "status_unknown",
                                 _MESSAGES["live_other_instance" if pr2 == "live-other" else "status_unknown"])
    _atomic_write(cfg.server_yaml, yaml.safe_dump(new, allow_unicode=True, default_flow_style=False, sort_keys=False))
    after_state, after = read_existing(cfg.server_yaml)
    if after_state != "present" or (after or {}).get("instance_id") != instance_id:
        raise PublishRefused("verify_failed", _MESSAGES["verify_failed"])
    return {"action": action, "backup": backup_name, "changes": [{"key": k, "status": s} for k, s in changes]}


def unpublish_if_ours(cfg: ProdConfig, instance_id: str) -> bool:
    """自分の instance_id の server.yaml だけを削除する(別インスタンスのものは触らない)。既定では呼ばない。"""
    state, data = read_existing(cfg.server_yaml)
    if state == "present" and data.get("instance_id") == instance_id:
        os.remove(cfg.server_yaml)
        return True
    return False
