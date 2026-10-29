"""本番推論 API(prod_server.py)の設定の読み込みと検証。標準ライブラリ + PyYAML のみ。ネットワーク・ファイルへの書き込みはしない。

設計方針(docs/production_inference_api.md):
  - 安全に関わる値(待受アドレス・ポート・許可するクライアント・出力ルート・server.yaml の場所・モデルの期待ハッシュ)は
    **すべて設定ファイルに明示させる**。既定値は持たない(書き忘れは起動拒否)。設定ファイルは Git 管理外(`*.local.yaml`)。
  - 待受は IPv4 アドレス 1 つの明示だけ。全インターフェース(0.0.0.0 / ::)・ホスト名・IPv6 は拒否する。
  - 許可するクライアントは IP / CIDR の明示リスト。全許可(/0)と広すぎる範囲(IPv4 で /24 より広い)は拒否する。
  - 例外メッセージ・ログに、設定値(パス・アドレス・ハッシュの全文)を出さない。ConfigError のメッセージは固定文 + キー名だけ。
"""
from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

MIN_IPV4_PREFIX = 24                       # 許可クライアントの範囲の下限(/24 より広い範囲は拒否)
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
MODES = ("production",)


class ConfigError(ValueError):
    """設定の不備。メッセージは固定文とキー名だけ(値は含めない)。"""


@dataclass(frozen=True)
class ModelExpect:
    id: str
    dir: str
    weights: str
    yaml_name: str
    weights_sha256: str
    weights_size: int
    yaml_sha256: str


@dataclass(frozen=True)
class ProdConfig:
    host: str
    port: int
    allowed_clients: tuple                    # ipaddress.IPv4Network のタプル
    output_root: str                          # /predict が受け付ける work_dir の親(製番フォルダの 1 つ上)
    server_yaml: str                          # 公開する接続設定(共有)のパス。ファイル名は server.yaml 固定
    backup_dir: str                           # 上書き前の退避先
    proxy: dict = field(default_factory=dict)  # 既存の server.yaml に proxy が無いときだけ使う(任意)
    model: ModelExpect | None = None
    token_file: str | None = None             # 任意。指定時は /predict に Bearer トークンを要求する(クライアントが対応するまで未使用)
    timeout_probe_sec: float = 3.0            # 既存 server.yaml の接続先を確認する /health の待ち時間


def _req(d: dict, key: str, typ, section: str):
    if not isinstance(d, dict) or key not in d or d[key] is None:
        raise ConfigError(f"設定項目がありません: {section}.{key}")
    v = d[key]
    if typ is int and isinstance(v, bool):
        raise ConfigError(f"設定項目の型が不正です: {section}.{key}")
    if not isinstance(v, typ):
        raise ConfigError(f"設定項目の型が不正です: {section}.{key}")
    return v


def validate_bind_host(host: str) -> str:
    """待受アドレス: 単一の IPv4 アドレス(明示)だけ。0.0.0.0・IPv6・ホスト名・マルチキャスト・リンクローカルは拒否。"""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise ConfigError("待受アドレスは IPv4 アドレスで明示してください(ホスト名は不可): bind.host") from None
    if ip.version != 4:
        raise ConfigError("待受アドレスは IPv4 のみ指定できます: bind.host")
    if ip.is_unspecified or ip.is_multicast or ip.is_link_local or str(ip) == "255.255.255.255":
        raise ConfigError("全インターフェース・マルチキャスト・リンクローカルでは待ち受けできません: bind.host")
    return str(ip)


def validate_port(port) -> int:
    if not isinstance(port, int) or isinstance(port, bool) or not (1024 <= port <= 65535):
        raise ConfigError("ポートは 1024〜65535 の固定値で指定してください(0 や自動採番は不可): bind.port")
    return port


def parse_allowed_clients(items) -> tuple:
    if not isinstance(items, (list, tuple)) or not items:
        raise ConfigError("許可するクライアントを 1 つ以上、明示してください: allowed_clients")
    nets = []
    for raw in items:
        try:
            net = ipaddress.ip_network(str(raw), strict=True)    # 192.0.2.10/24 のようなホストビット付きは拒否(意図より広がるのを防ぐ)
        except ValueError:
            raise ConfigError("許可するクライアントの形式が不正です(IPv4 または CIDR。ホストビットは 0 に): allowed_clients") from None
        if net.version != 4:
            raise ConfigError("許可するクライアントは IPv4 のみ指定できます: allowed_clients")
        if net.prefixlen < MIN_IPV4_PREFIX:
            raise ConfigError(f"許可するクライアントの範囲が広すぎます(/{MIN_IPV4_PREFIX} より広い範囲は不可): allowed_clients")
        nets.append(net)
    return tuple(nets)


def _is_abs(p: str) -> bool:
    return os.path.isabs(p) or p.startswith("\\\\") or p.startswith("//")


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p.replace("/", "\\"))) if os.name == "nt" else os.path.normpath(p)


def validate_output_root(p: str) -> str:
    """製番フォルダの親。絶対パスで、ドライブ直下・共有直下は不可(共有の 1 つ下以降のフォルダを明示させる)。"""
    if not isinstance(p, str) or not p.strip() or "\x00" in p or not _is_abs(p):
        raise ConfigError("出力ルートは絶対パスで指定してください: output_root")
    n = _norm(p).rstrip("\\/")
    if n.startswith("\\\\"):
        parts = [x for x in n.split("\\") if x]
        if len(parts) < 3:                                  # \\host\share だけ(共有直下)は不可
            raise ConfigError("出力ルートは共有の直下ではなく、その下のフォルダを指定してください: output_root")
    else:
        drive, rest = os.path.splitdrive(n)
        if not [x for x in re.split(r"[\\/]+", rest) if x]:
            raise ConfigError("出力ルートはドライブ直下ではなく、その下のフォルダを指定してください: output_root")
    if ".." in re.split(r"[\\/]+", p):
        raise ConfigError("出力ルートに .. は使えません: output_root")
    return p


def validate_server_yaml_path(p: str) -> str:
    if not isinstance(p, str) or not _is_abs(p) or "\x00" in p:
        raise ConfigError("公開する接続設定のパスは絶対パスで指定してください: publish.server_yaml")
    if os.path.basename(p.replace("\\", "/")) != "server.yaml":
        raise ConfigError("公開する接続設定のファイル名は server.yaml のみ指定できます: publish.server_yaml")
    if ".." in re.split(r"[\\/]+", p):
        raise ConfigError("公開する接続設定のパスに .. は使えません: publish.server_yaml")
    return p


def _model(d: dict) -> ModelExpect:
    mid = _req(d, "id", str, "model")
    if not _ID_RE.fullmatch(mid):
        raise ConfigError("モデル ID の形式が不正です: model.id")
    mdir = _req(d, "dir", str, "model")
    if not _is_abs(mdir):
        raise ConfigError("モデルのフォルダは絶対パスで指定してください: model.dir")
    weights = _req(d, "weights", str, "model")
    yml = _req(d, "yaml", str, "model")
    for key, v in (("weights", weights), ("yaml", yml)):
        if not _FILENAME_RE.fullmatch(v) or v in (".", ".."):
            raise ConfigError(f"ファイル名だけを指定してください(パスは不可): model.{key}")
    exp = _req(d, "expected", dict, "model")
    ws = _req(exp, "weights_sha256", str, "model.expected").lower()
    ys = _req(exp, "yaml_sha256", str, "model.expected").lower()
    size = _req(exp, "weights_size", int, "model.expected")
    if not _SHA256_RE.fullmatch(ws) or not _SHA256_RE.fullmatch(ys):
        raise ConfigError("期待ハッシュは SHA-256(64 桁の 16 進)で指定してください: model.expected")
    if size <= 0:
        raise ConfigError("期待サイズは正の整数で指定してください: model.expected.weights_size")
    return ModelExpect(mid, mdir, weights, yml, ws, size, ys)


def parse_config(data: dict) -> ProdConfig:
    if not isinstance(data, dict):
        raise ConfigError("設定ファイルの形式が不正です。")
    mode = data.get("mode")
    if mode not in MODES:
        raise ConfigError("mode: production を明示してください。")
    bind = _req(data, "bind", dict, "(root)")
    host = validate_bind_host(str(_req(bind, "host", str, "bind")))
    port = validate_port(_req(bind, "port", int, "bind"))
    allowed = parse_allowed_clients(data.get("allowed_clients"))
    out_root = validate_output_root(_req(data, "output_root", str, "(root)"))
    pub = _req(data, "publish", dict, "(root)")
    server_yaml = validate_server_yaml_path(_req(pub, "server_yaml", str, "publish"))
    backup_dir = _req(pub, "backup_dir", str, "publish")
    if not _is_abs(backup_dir):
        raise ConfigError("退避先は絶対パスで指定してください: publish.backup_dir")
    proxy = pub.get("proxy") or {}
    if proxy:
        if not isinstance(proxy, dict) or not isinstance(proxy.get("host"), str) or not isinstance(proxy.get("port"), int):
            raise ConfigError("proxy は host(文字列)と port(整数)で指定してください: publish.proxy")
        proxy = {"host": proxy["host"], "port": proxy["port"]}
    model = _model(_req(data, "model", dict, "(root)"))
    token_file = None
    auth = data.get("auth")
    if auth:
        token_file = _req(auth, "token_file", str, "auth")
        if not _is_abs(token_file):
            raise ConfigError("トークンファイルは絶対パスで指定してください: auth.token_file")
    probe = data.get("timeout_probe_sec", 3.0)
    if not isinstance(probe, (int, float)) or isinstance(probe, bool) or not (0.5 <= float(probe) <= 30):
        raise ConfigError("timeout_probe_sec は 0.5〜30 の数値で指定してください。")
    return ProdConfig(host=host, port=port, allowed_clients=allowed, output_root=out_root, server_yaml=server_yaml,
                      backup_dir=backup_dir, proxy=proxy, model=model, token_file=token_file, timeout_probe_sec=float(probe))


def load_config(path: str) -> ProdConfig:
    p = Path(path)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except OSError:
        raise ConfigError("設定ファイルを読み込めません。") from None
    except yaml.YAMLError:
        raise ConfigError("設定ファイルの YAML が不正です。") from None
    return parse_config(data)


def client_allowed(cfg: ProdConfig, remote_addr: str | None) -> bool:
    """接続元の IP が許可リストに含まれるか。解釈できない(IPv6・不明)ときは拒否。"""
    try:
        ip = ipaddress.ip_address(remote_addr or "")
    except ValueError:
        return False
    if ip.version != 4:
        return False
    return any(ip in net for net in cfg.allowed_clients)
