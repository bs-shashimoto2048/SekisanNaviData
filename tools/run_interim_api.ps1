<#
暫定・隔離運用: 推論 API(inference_server/server.py。検証専用)を、この PC の loopback で手動起動して稼働させ続ける。
設計: docs/inference_api_interim_operation.md §3(暫定運用)。本番サーバーではない。

- 手動実行専用(自動起動・タスク登録はしない)。起動後は停止せず稼働し続ける(停止は Ctrl+C か、プロセスの終了)。
- 出力先・server.yaml・ログは、リポジトリ配下の ./output_interim の内側だけ。UNC・ネットワークドライブ・リポジトリ外・./output_interim の外・
  シンボリックリンク/ジャンクション等(外へ出る経路になり得る)を経由する場所は拒否する。
- 待ち受けは 127.0.0.1 のみ(server.py 側でも loopback 以外は拒否される)。--allow-unc・--parent-pid は使わない。
- ポートは必須(-Port)。値はここに固定しない。
- バックエンドの既定は実モデル(sahi)。偽の検出結果を返す fake は、検証専用で、-Backend fake を明示したときだけ(既定・設定・環境変数では選ばれない)。
- 既に server.yaml がある場合は起動しない(稼働中の API の接続先を上書きしないため)。稼働中でないと確認できたとき(異常終了の残りなど)だけ -ReplaceStale。
- -DryRun: 検証だけして終了する(サーバーは起動しない・通信しない・何も作らない)。
- ログ(<出力先>\logs\api_*.log)は UTF-8。標準エラーの出力もログに残す。

例(PowerShell。リポジトリのフォルダで):
  .\tools\run_interim_api.ps1 -Port <1024-65535 の固定ポート> -DryRun     # まず検証だけ
  .\tools\run_interim_api.ps1 -Port <...> -ModelDir <model.yaml と best.pt のフォルダ> -PythonExe <推論用 venv の python>   # 既定は実モデル(sahi)
  .\tools\run_interim_api.ps1 -Port <...> -Backend fake                  # 検証専用: 偽の検出結果(モデル不要)。明示したときだけ。通常運用では使わない
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][int]$Port,
    [string]$OutputRoot = "output_interim",
    [ValidateSet("fake", "sahi")][string]$Backend = "sahi",
    [string]$ModelDir = "",
    [string]$PythonExe = "inference_server\.venv\Scripts\python.exe",
    [string]$FakeScript = "inference_server\fake_example.json",
    [string]$InstanceId = "",
    [switch]$ReplaceStale,
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$isolatedName = "output_interim"

function Fail([string]$msg) { Write-Host "NG: $msg"; exit 2 }

function Resolve-Local([string]$p) {
    if ([System.IO.Path]::IsPathRooted($p)) { return [System.IO.Path]::GetFullPath($p) }
    return [System.IO.Path]::GetFullPath((Join-Path $repo $p))
}

# UNC・ネットワークドライブは使わない(存在確認などで共有へ触れる前に拒否する)
function Assert-Local([string]$label, [string]$raw, [string]$full) {
    if ($raw.StartsWith("\\") -or $raw.StartsWith("//") -or $full.StartsWith("\\")) { Fail "$label に UNC パスは使えません" }
    $drive = [System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot($full))
    if ($drive.DriveType -eq [System.IO.DriveType]::Network) { Fail "$label にネットワークドライブは使えません" }
}

# $base から $full までの既存の各階層(自身を含む)に、シンボリックリンク・ジャンクション等の再解析ポイントがあるか
function Test-ReparseInPath([string]$base, [string]$full) {
    $rel = $full.Substring($base.Length).Trim([char]92)
    $cur = $base
    foreach ($part in ($rel -split '\\')) {
        if (-not $part) { continue }
        $cur = Join-Path $cur $part
        if (Test-Path -LiteralPath $cur) {
            if ((Get-Item -LiteralPath $cur -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) { return $true }
        }
    }
    return $false
}

function Test-WithinIsolated([string]$full, [string]$isolated) {
    return $full.Equals($isolated, [System.StringComparison]::OrdinalIgnoreCase) -or
           $full.StartsWith($isolated.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase)
}

# --- 検証(何も書かない) ---
try {
    if ($Port -lt 1024 -or $Port -gt 65535) { Fail "ポートは 1024〜65535 の固定値で指定してください" }
    $isolated = Join-Path $repo $isolatedName
    if ($OutputRoot.StartsWith("\\") -or $OutputRoot.StartsWith("//")) { Fail "UNC パスは使えません(隔離出力先はこの PC のローカルフォルダだけ)" }
    $root = Resolve-Local $OutputRoot
    if (-not (Test-WithinIsolated $root $isolated)) { Fail "隔離出力先は、リポジトリ直下の $isolatedName の内側だけ指定できます" }
    Assert-Local "隔離出力先" $OutputRoot $root
    if (Test-ReparseInPath $repo $root) { Fail "隔離出力先に至る階層に、シンボリックリンク・ジャンクション等があります(外へ出る経路になり得るため拒否)" }

    $py = Resolve-Local $PythonExe
    Assert-Local "-PythonExe" $PythonExe $py
    if ((Split-Path -Leaf $py) -notlike "python*.exe") { Fail "-PythonExe は python*.exe を指定してください" }
    if (-not (Test-Path -LiteralPath $py -PathType Leaf)) { Fail "推論用の python が見つかりません(-PythonExe。既定は inference_server\.venv)" }
    $server = Join-Path $repo "inference_server\server.py"
    if (-not (Test-Path -LiteralPath $server -PathType Leaf)) { Fail "inference_server\server.py が見つかりません" }

    $cfgOut = Join-Path $root "api\server.yaml"
    $logDir = Join-Path $root "logs"
    # instance_id: 呼び出し元(interim_console.py など)が所有を確認できるよう、32 桁の 16 進数で指定できる(省略時は自動生成)
    if ($InstanceId -and $InstanceId -cnotmatch "^[0-9a-f]{32}$") { Fail "-InstanceId は 32 桁の 16 進数(小文字)で指定してください" }
    $instanceId = if ($InstanceId) { $InstanceId } else { [guid]::NewGuid().ToString("N") }
    $serverArgs = @($server, "--output-root", $root, "--port", "$Port", "--config-out", $cfgOut, "--backend", $Backend,
                    "--instance-id", $instanceId)
    if ($Backend -eq "sahi") {
        if (-not $ModelDir) { Fail "-Backend sahi には -ModelDir が必要です" }
        $md = Resolve-Local $ModelDir
        Assert-Local "-ModelDir" $ModelDir $md
        $serverArgs += @("--model-dir", $md)
    } else {
        $fs = Resolve-Local $FakeScript
        Assert-Local "-FakeScript" $FakeScript $fs
        if (-not (Test-Path -LiteralPath $fs -PathType Leaf)) { Fail "-FakeScript が見つかりません" }
        $serverArgs += @("--fake-script", $fs)
    }

    if ((Test-Path -LiteralPath $cfgOut) -and -not $ReplaceStale) {
        Fail "既に server.yaml があります(稼働中の API、または異常終了の残り)。稼働中でないと確認できたときだけ -ReplaceStale を付けてください"
    }
} catch {
    Fail "入力を検証できませんでした($($_.Exception.GetType().Name))"
}

Write-Host "検証 OK: backend=$Backend$(if ($Backend -eq 'fake') { '(検証専用・偽の検出結果)' }) / 出力先=<リポジトリ>\$($root.Substring($repo.Length).TrimStart([char]92)) / ポート=指定済み / loopback のみ"
if ($DryRun) { Write-Host "(DryRun: サーバーは起動しません)"; exit 0 }

# --- 起動(手動。稼働し続ける) ---
New-Item -ItemType Directory -Force -Path $root, (Split-Path $cfgOut), $logDir | Out-Null
$log = Join-Path $logDir ("api_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
Write-Host "API を起動します(停止: Ctrl+C)。ログ: <出力先>\logs\$(Split-Path -Leaf $log)"

$utf8 = [System.Text.UTF8Encoding]::new($false)
$prevEncoding = [Console]::OutputEncoding
$prevPyIo = $env:PYTHONIOENCODING
$prevPyBytecode = $env:PYTHONDONTWRITEBYTECODE
$code = 1
try {
    [Console]::OutputEncoding = $utf8                  # python の出力(UTF-8)を文字化けさせずに受ける
    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $ErrorActionPreference = "Continue"                # 標準エラーの 1 行で、PowerShell 5.1 が例外にして止まらないようにする
    & $py @serverArgs 2>&1 | ForEach-Object {
        $line = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { "$_" }
        [System.IO.File]::AppendAllText($log, $line + "`r`n", $utf8)
        $line
    }
    $code = $LASTEXITCODE
} finally {
    $ErrorActionPreference = "Stop"
    [Console]::OutputEncoding = $prevEncoding
    $env:PYTHONIOENCODING = $prevPyIo
    $env:PYTHONDONTWRITEBYTECODE = $prevPyBytecode
    # 自分が起動したサーバーの server.yaml(instance_id が一致するもの)だけを、強制終了などで残っていれば消す
    try {
        if ((Test-Path -LiteralPath $cfgOut) -and ((Get-Content -LiteralPath $cfgOut -Raw -Encoding UTF8) -match "instance_id:\s*$instanceId")) {
            Remove-Item -LiteralPath $cfgOut -Force
        }
    } catch { }
}
exit $code
