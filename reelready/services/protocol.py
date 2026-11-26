"""The ``reelready://`` link handler that Windows runs when Play / Open folder is clicked.

ReelReady runs in Docker and cannot start programs on the host, so the settings page offers a
.reg file that registers ``reelready://play/<movie id>`` and ``reelready://folder/<movie id>``
for the current Windows user. The registered command runs a short PowerShell script that asks
ReelReady where the movie is and which player to use, then opens it. Nothing stays running.

The link text never becomes code: the script reads the movie id from its own command line
with a digits-only pattern, and the link is appended after a trailing ``#`` comment.
"""

VIDEO_EXTENSIONS = (
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".mts",
    ".webm", ".flv", ".mpg", ".mpeg", ".rmvb", ".iso",
)

REG_KEY = r"HKEY_CURRENT_USER\Software\Classes\reelready"
CONHOST = r"C:\Windows\System32\conhost.exe"
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

# One line, single quotes only: it is embedded in a double-quoted registry command, and the
# shell would expand percent signs, so the script uses neither.
_SCRIPT = [
    "$ErrorActionPreference='Stop'",
    "$shell=New-Object -ComObject WScript.Shell",
    "try {"
    " $m=[regex]::Matches([Environment]::CommandLine,'reelready:/{0,2}(play|folder)/([0-9]{1,9})');"
    " if($m.Count -eq 0){throw '链接格式不正确'};"
    " $g=$m[$m.Count-1].Groups; $action=$g[1].Value; $id=$g[2].Value;"
    " $wc=New-Object Net.WebClient; $wc.Proxy=$null; $wc.Encoding=[Text.Encoding]::UTF8;"
    " try { $info=$wc.DownloadString('__SERVER__/playback/movies/'+$id) | ConvertFrom-Json }"
    " catch { throw '连接不上 ReelReady（__SERVER__），请确认它正在运行' };"
    " if(-not $info.path){throw [string]$info.message};"
    " $p=[string]$info.path;"
    " if(-not (Test-Path -LiteralPath $p)){throw ('这台电脑上找不到：'+$p+'。请检查设置里的路径映射')};"
    " if($action -eq 'folder'){"
    "  if(Test-Path -LiteralPath $p -PathType Leaf){Start-Process explorer.exe -ArgumentList ('/select,'+[char]34+$p+[char]34)}"
    "  else {Start-Process explorer.exe -ArgumentList ([char]34+$p+[char]34)};"
    "  exit 0 };"
    " if($info.state -ne 'ready'){throw [string]$info.message};"
    " $exts=@(__EXTENSIONS__);"
    " if(Test-Path -LiteralPath $p -PathType Leaf){$v=Get-Item -LiteralPath $p}"
    " else {"
    "  $all=@(Get-ChildItem -LiteralPath $p -Recurse -File | Where-Object {$exts -contains $_.Extension.ToLower()});"
    "  $main=@($all | Where-Object {$_.Name -notmatch 'sample'}); if($main.Count -eq 0){$main=$all};"
    "  $v=$main | Sort-Object Length -Descending | Select-Object -First 1 };"
    " if(-not $v -or ($exts -notcontains $v.Extension.ToLower())){throw '没有找到视频文件'};"
    " $kind=[string]$info.player; $exe=([string]$info.player_path).Trim([char]34,' ');"
    " if(-not $exe -and $kind -eq 'custom'){throw '选择了自定义程序，但没有填写播放器程序路径'};"
    " if(-not $exe -and $kind -ne 'system'){"
    "  $c=@();"
    "  if($kind -eq 'potplayer'){"
    "   foreach($k in 'HKCU:\\Software\\DAUM\\PotPlayer64','HKCU:\\Software\\DAUM\\PotPlayer'){$c+=(Get-ItemProperty -LiteralPath $k -ErrorAction SilentlyContinue).ProgramPath};"
    "   $c+=($env:ProgramFiles+'\\DAUM\\PotPlayer\\PotPlayerMini64.exe'),(${env:ProgramFiles(x86)}+'\\DAUM\\PotPlayer\\PotPlayerMini.exe'),'PotPlayerMini64.exe','PotPlayerMini.exe'};"
    "  if($kind -eq 'vlc'){"
    "   foreach($k in 'HKLM:\\SOFTWARE\\VideoLAN\\VLC','HKLM:\\SOFTWARE\\WOW6432Node\\VideoLAN\\VLC'){$c+=(Get-ItemProperty -LiteralPath $k -ErrorAction SilentlyContinue).'(default)'};"
    "   $c+=($env:ProgramFiles+'\\VideoLAN\\VLC\\vlc.exe'),(${env:ProgramFiles(x86)}+'\\VideoLAN\\VLC\\vlc.exe'),'vlc.exe'};"
    "  if($kind -eq 'mpv'){$c+=($env:ProgramFiles+'\\mpv\\mpv.exe'),($env:LOCALAPPDATA+'\\Programs\\mpv\\mpv.exe'),'mpv.exe'};"
    "  foreach($x in $c){"
    "   if(-not $x){continue};"
    "   if(Test-Path -LiteralPath $x -PathType Leaf){$exe=$x;break};"
    "   $cmd=Get-Command $x -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1;"
    "   if($cmd){$exe=$cmd.Source;break} };"
    "  if(-not $exe){throw ('没有找到 '+$kind+'，请在设置里填写播放器程序路径')} };"
    " if($exe){"
    "  if(-not (Test-Path -LiteralPath $exe -PathType Leaf)){throw ('找不到播放器程序：'+$exe)};"
    "  Start-Process -FilePath $exe -ArgumentList ([char]34+$v.FullName+[char]34) }"
    " else {Invoke-Item -LiteralPath $v.FullName}"
    " } catch { [void]$shell.Popup(('打开失败：'+$_.Exception.Message),0,'ReelReady',48) }",
]


def handler_script(server: str) -> str:
    server = server.rstrip("/")
    if any(char in server for char in "'\"%`$") or not server.startswith(("http://", "https://")):
        raise ValueError("unsupported server address")
    extensions = ",".join(f"'{ext}'" for ext in VIDEO_EXTENSIONS)
    script = "; ".join(_SCRIPT).replace("__SERVER__", server).replace("__EXTENSIONS__", extensions)
    assert '"' not in script and "%" not in script
    return script


REG_PS_PATH = "HKCU:\\Software\\Classes\\reelready"


def handler_command() -> str:
    # The script lives in the key's "Script" value so the command stays short; browsers read
    # this command to decide whether a handler exists. conhost --headless keeps PowerShell from
    # flashing a console window. The link (%1) is appended after "#", so PowerShell treats it
    # as a comment.
    return (
        f'"{CONHOST}" --headless "{POWERSHELL}" -NoProfile -NonInteractive -ExecutionPolicy Bypass '
        f"-Command \"Invoke-Expression (Get-ItemProperty -LiteralPath '{REG_PS_PATH}').Script #\" \"%1\""
    )


def _reg_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _reg_file(lines: list[str]) -> bytes:
    # regedit reads Unicode .reg files as UTF-16 LE with a BOM and CRLF line endings.
    return "\r\n".join(["Windows Registry Editor Version 5.00", "", *lines, ""]).encode("utf-16")


def install_reg(server: str) -> bytes:
    return _reg_file([
        f"[-{REG_KEY}]",
        "",
        f"[{REG_KEY}]",
        f"@={_reg_string('URL:ReelReady')}",
        '"URL Protocol"=""',
        f'"Script"={_reg_string(handler_script(server))}',
        "",
        f"[{REG_KEY}\\shell\\open\\command]",
        f"@={_reg_string(handler_command())}",
    ])


def uninstall_reg() -> bytes:
    return _reg_file([f"[-{REG_KEY}]"])
