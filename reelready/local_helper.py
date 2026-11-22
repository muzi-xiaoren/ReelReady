"""ReelReady 本地助手：在这台电脑上打开已下载的电影或所在文件夹。

ReelReady 运行在 Docker 里时无法启动电脑上的播放器，网页上的「播放」「打开文件夹」
按钮会请求本助手代为打开。运行方式（需要 Python 3.10+，只用标准库）：

    python reelready-helper.py

想开机自动运行且不显示窗口，可以用 pythonw 运行，并把快捷方式放进「启动」文件夹
（Win+R 输入 shell:startup）。

助手只监听本机 127.0.0.1，并且只接受带正确口令、来自 ReelReady 网页的请求；
要打开的文件路径和播放器由 ReelReady 服务端决定，只会打开视频文件或其所在文件夹。
"""

import argparse
import hmac
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

# ReelReady fills these in when the script is downloaded from the settings page.
SERVER = "http://localhost:8765"
TOKEN = ""
PORT = 8766

VERSION = 1
VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".mts",
    ".webm", ".flv", ".mpg", ".mpeg", ".rmvb", ".iso",
}
PLAYERS = {
    "potplayer": [
        r"%ProgramFiles%\DAUM\PotPlayer\PotPlayerMini64.exe",
        r"%ProgramFiles(x86)%\DAUM\PotPlayer\PotPlayerMini.exe",
        r"%ProgramFiles%\PotPlayer\PotPlayerMini64.exe",
        "PotPlayerMini64", "PotPlayerMini",
    ],
    "vlc": [
        r"%ProgramFiles%\VideoLAN\VLC\vlc.exe",
        r"%ProgramFiles(x86)%\VideoLAN\VLC\vlc.exe",
        "/Applications/VLC.app/Contents/MacOS/VLC",
        "vlc",
    ],
    "mpv": [
        r"%ProgramFiles%\mpv\mpv.exe",
        r"%LOCALAPPDATA%\Programs\mpv\mpv.exe",
        "mpv",
    ],
}
PLAYER_NAMES = {"potplayer": "PotPlayer", "vlc": "VLC", "mpv": "mpv"}


class HelperError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def allowed_origins(server: str) -> set[str]:
    parts = urlsplit(server)
    origins = {f"{parts.scheme}://{parts.netloc}"}
    port = f":{parts.port}" if parts.port else ""
    for host in ("localhost", "127.0.0.1", "[::1]"):
        origins.add(f"{parts.scheme}://{host}{port}")
    return origins


def find_player(kind: str, custom_path: str) -> str:
    custom_path = custom_path.strip().strip('"')
    if custom_path:
        if not Path(custom_path).is_file():
            raise HelperError(f"找不到播放器程序：{custom_path}")
        return custom_path
    if kind == "custom":
        raise HelperError("选择了「自定义程序」，但没有填写播放器程序路径")
    for candidate in PLAYERS.get(kind, []):
        expanded = os.path.expandvars(candidate)
        if "%" not in expanded and Path(expanded).is_file():
            return expanded
        found = shutil.which(candidate)
        if found:
            return found
    raise HelperError(f"没有找到 {PLAYER_NAMES.get(kind, kind)}，请在设置中填写播放器程序路径")


def pick_video(path: Path) -> Path:
    if path.is_file():
        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            raise HelperError(f"不是视频文件：{path.name}")
        return path
    videos = [item for item in path.rglob("*") if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS]
    if not videos:
        raise HelperError("文件夹里没有找到视频文件")
    main = [item for item in videos if "sample" not in item.name.lower()] or videos
    return max(main, key=lambda item: item.stat().st_size)


def open_folder(path: Path) -> None:
    if sys.platform == "win32":
        if path.is_file():
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(str(path))  # noqa: S606 - a folder, opened in Explorer
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)] if path.is_file() else ["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path if path.is_dir() else path.parent)])


def play(video: Path, player: str, player_path: str) -> str:
    if player == "system" and not player_path.strip():
        if sys.platform == "win32":
            os.startfile(str(video))  # noqa: S606 - extension checked by pick_video
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(video)])
        return "系统默认播放器"
    executable = find_player(player, player_path)
    subprocess.Popen([executable, str(video)])
    return Path(executable).stem


def fetch_movie(server: str, token: str, movie_id: int) -> dict:
    request = urllib.request.Request(
        f"{server.rstrip('/')}/playback/movies/{movie_id}",
        headers={"X-ReelReady-Token": token, "Accept": "application/json"},
    )
    # Ignore system proxies: ReelReady is normally reached on this computer.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise HelperError("口令不匹配，请从 ReelReady 设置页重新下载本地助手", 502) from exc
        raise HelperError(f"ReelReady 返回 HTTP {exc.code}", 502) from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise HelperError(f"本地助手连接不上 ReelReady（{server}）", 502) from exc


def open_movie(server: str, token: str, movie_id: int, action: str) -> str:
    info = fetch_movie(server, token, movie_id)
    state = info.get("state")
    if state in ("error", "missing", "not_downloaded", "missing_files"):
        raise HelperError(info.get("message") or "找不到这部电影的文件", 409)
    path = Path(info.get("path") or "")
    if not info.get("path") or not path.exists():
        raise HelperError(f"这台电脑上找不到：{info.get('path') or '（无路径）'}。请检查设置里的路径映射", 404)
    if action == "folder":
        open_folder(path)
        return "已打开所在文件夹"
    if state != "ready":
        raise HelperError(info.get("message") or "还没有下载完成", 409)
    video = pick_video(path)
    used = play(video, info.get("player") or "system", info.get("player_path") or "")
    return f"已用{used}打开 {video.name}"


class Handler(BaseHTTPRequestHandler):
    server_version = "ReelReadyHelper/1"
    config: dict = {}

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - signature from the base class
        if sys.stderr is not None:  # pythonw runs without a console
            sys.stderr.write("[ReelReady 助手] " + (format % args) + "\n")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin in self.config["origins"]

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        origin = self.headers.get("Origin")
        if origin and origin in self.config["origins"]:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        if not self._origin_ok():
            self._send(403, {"ok": False, "error": "不接受来自这个网页的请求"})
            return False
        token = self.headers.get("X-ReelReady-Token", "")
        if not (self.config["token"] and hmac.compare_digest(token, self.config["token"])):
            self._send(403, {"ok": False, "error": "口令不匹配，请从 ReelReady 设置页重新下载本地助手"})
            return False
        return True

    def do_OPTIONS(self) -> None:  # noqa: N802 - http.server naming
        if not self._origin_ok() or not self.headers.get("Origin"):
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-ReelReady-Token")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/ping":
            self._send(404, {"ok": False, "error": "not found"})
            return
        if self._authorized():
            self._send(200, {"ok": True, "version": VERSION, "server": self.config["server"]})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/open":
            self._send(404, {"ok": False, "error": "not found"})
            return
        if not self._authorized():
            return
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 4096)
            body = json.loads(self.rfile.read(length) or b"{}")
            movie_id = int(body.get("movie_id"))
            action = body.get("action")
            if action not in ("play", "folder"):
                raise HelperError("未知操作")
            message = open_movie(self.config["server"], self.config["token"], movie_id, action)
        except HelperError as exc:
            self._send(exc.status, {"ok": False, "error": str(exc)})
            return
        except (TypeError, ValueError):
            self._send(400, {"ok": False, "error": "请求格式不正确"})
            return
        except OSError as exc:
            self._send(500, {"ok": False, "error": f"打开失败：{exc}"})
            return
        self._send(200, {"ok": True, "message": message})


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="ReelReady 本地助手")
    parser.add_argument("--server", default=SERVER, help="ReelReady 网页地址")
    parser.add_argument("--token", default=TOKEN, help="设置页显示的助手口令")
    parser.add_argument("--port", type=int, default=PORT, help="监听端口")
    args = parser.parse_args(argv)
    if not args.token:
        parser.error("缺少口令：请从 ReelReady「设置 → 本地播放」下载助手，或用 --token 指定")
    server = args.server.rstrip("/")
    Handler.config = {"server": server, "token": args.token, "origins": allowed_origins(server)}
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"ReelReady 本地助手已启动：127.0.0.1:{args.port}，连接 {server}。关闭此窗口即停止。", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
