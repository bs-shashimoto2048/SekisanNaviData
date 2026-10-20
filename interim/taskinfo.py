# 専用タスクの Task Scheduler の情報(最終起動・次回予定・終了コード・見逃し回数・電源)を、読み取りだけで取得する(画面の「前回の結果」欄用)。
# - タスクの登録・変更・実行・無効化はしない(Get-ScheduledTaskInfo と BatteryStatus の照会だけ)。値は型付きで取るので、OS の表示言語に依存しない。
# - タスク名は、専用名(SekisanNaviData_Interim…)の英数字・アンダースコアだけを、完全一致で受け付ける(通常のタスク名・前後の空白・改行を含む名前は、読み取りの前に拒否する)。
# - 出力は JSON 文字列だけ。呼び出し側(interim/runs.py)が検証して使い、失敗したときは例外メッセージを出さずに「取得できません」とする。
import re
import subprocess
import sys

# 完全一致(fullmatch)で判定する。`$` は末尾の改行の手前にも一致するため、`match` と `$` の組み合わせだと「名前 + 改行」を通してしまう。
_TASK_NAME_OK = re.compile(r"SekisanNaviData_Interim[A-Za-z0-9_]*")

_INFO_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "$i=Get-ScheduledTaskInfo -TaskName '{name}';"
    "$f={{param($d) if($d -and $d.Year -gt 2000){{$d.ToString('yyyy-MM-ddTHH:mm:ss')}}else{{''}}}};"
    "$ac=$null;try{{$b=Get-CimInstance -ClassName BatteryStatus -Namespace root\\wmi -ErrorAction Stop|Select-Object -First 1;if($b){{$ac=[bool]$b.PowerOnline}}}}catch{{}};"
    "[pscustomobject]@{{last=(& $f $i.LastRunTime);next=(& $f $i.NextRunTime);result=[int64]$i.LastTaskResult;missed=[int]$i.NumberOfMissedRuns;ac=$ac}}|ConvertTo-Json -Compress"
)


def default_info_runner(task_name: str) -> str:
    if not isinstance(task_name, str) or not _TASK_NAME_OK.fullmatch(task_name):       # 読み取り(PowerShell の起動)より前に拒否する
        raise ValueError("task name")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _INFO_SCRIPT.format(name=task_name)],
                         capture_output=True, timeout=20, creationflags=flags)
    if res.returncode != 0:
        raise RuntimeError("info")
    return res.stdout.decode("utf-8", "replace")
