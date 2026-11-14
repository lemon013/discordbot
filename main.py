from __future__ import annotations

"""Start and supervise Minecraft Discord Bridge + Discord GameBot on Windows."""

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parent
BRIDGE_DIR = ROOT / "Minecraft-Discord-Bridge"
GAMEBOT_DIR = ROOT / "Discord-GameBot"
BRIDGE_PORTS = (3063, 23191)


def say(message: str) -> None:
    print(message, flush=True)


def port_is_in_use(port: int) -> bool:
    """Return True when a local process is already listening on this TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            return sock.connect_ex(("127.0.0.1", port)) == 0
        except OSError:
            return False


def find_python() -> list[str]:
    """Return a command that can create the project's virtual environment."""
    if os.name == "nt":
        py_launcher = shutil.which("py")
        if py_launcher:
            return [py_launcher, "-3"]
    return [sys.executable]


def ensure_gamebot_environment() -> Path | None:
    requirements = GAMEBOT_DIR / "requirements.txt"
    example_env = GAMEBOT_DIR / ".env.example"
    env_file = GAMEBOT_DIR / ".env"
    venv_python = GAMEBOT_DIR / ".venv" / "Scripts" / "python.exe"

    if not GAMEBOT_DIR.is_dir():
        say(f"[ERROR] GameBot folder not found: {GAMEBOT_DIR}")
        return None
    if not requirements.is_file():
        say("[ERROR] Discord-GameBot/requirements.txt が見つかりません。")
        return None

    if not env_file.exists():
        if not example_env.is_file():
            say("[ERROR] .env と .env.example が見つかりません。")
            return None
        shutil.copy2(example_env, env_file)
        say("[SETUP] Discord-GameBot/.env を作成しました。Botトークンを設定してください。")
        if os.name == "nt":
            try:
                subprocess.Popen(["notepad.exe", str(env_file)])
            except OSError:
                pass
        input(".env に DISCORD_BOT_TOKEN を設定して保存したら Enter を押してください... ")

    if not venv_python.is_file():
        say("[SETUP] Python仮想環境を作成しています...")
        command = find_python() + ["-m", "venv", str(GAMEBOT_DIR / ".venv")]
        try:
            result = subprocess.run(command, cwd=GAMEBOT_DIR, check=False)
        except OSError as exc:
            say(f"[ERROR] Pythonを起動できません: {exc}")
            return None
        if result.returncode != 0 or not venv_python.is_file():
            say("[ERROR] Python仮想環境の作成に失敗しました。Python 3をインストールしてください。")
            return None

    say("[SETUP] Pythonライブラリを確認・インストールしています...")
    result = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "-r", str(requirements)],
        cwd=GAMEBOT_DIR,
        check=False,
    )
    if result.returncode != 0:
        say("[ERROR] GameBotのライブラリをインストールできませんでした。")
        return None
    return venv_python


def pump_output(name: str, stream: TextIO) -> None:
    try:
        for line in iter(stream.readline, ""):
            print(f"[{name}] {line.rstrip()}", flush=True)
    finally:
        stream.close()


def start_process(name: str, command: list[str], cwd: Path, env: dict[str, str]) -> subprocess.Popen[str] | None:
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=None,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
    except OSError as exc:
        say(f"[ERROR] {name} を起動できません: {exc}")
        return None

    assert process.stdout is not None
    threading.Thread(target=pump_output, args=(name, process.stdout), daemon=True).start()
    say(f"[START] {name} を起動しました (PID: {process.pid})")
    return process


def stop_process(name: str, process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    say(f"[STOP] {name} を終了しています...")
    try:
        if os.name == "nt":
            process.send_signal(getattr(signal_module(), "CTRL_BREAK_EVENT"))
        else:
            process.terminate()
        process.wait(timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        if os.name == "nt":
            # Terminate only this process and its descendants, never unrelated PIDs.
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def signal_module():
    # Imported lazily so the module remains easy to lint on non-Windows systems.
    import signal
    return signal


def main() -> int:
    if not BRIDGE_DIR.is_dir():
        say("[ERROR] Minecraft-Discord-Bridge フォルダーが見つかりません。")
        return 1
    updater = BRIDGE_DIR / "updater.exe"
    bridge_entry = BRIDGE_DIR / "app" / "discord-mcbe.js"
    if not updater.is_file() or not bridge_entry.is_file():
        say("[ERROR] Bridgeの起動ファイルが不足しています (updater.exe / app/discord-mcbe.js)。")
        return 1

    say("=" * 62)
    say(" Minecraft Discord GameBot - main.py")
    say(" Bridge と Discord GameBot を1つの画面で起動します")
    say(" 終了するときは Ctrl+C を押してください")
    say("=" * 62)

    children: list[tuple[str, subprocess.Popen[str]]] = []
    try:
        busy_ports = [port for port in BRIDGE_PORTS if port_is_in_use(port)]
        if busy_ports:
            say(f"[WARN] Bridgeのポート {', '.join(map(str, busy_ports))} が既に使用中です。")
            say("[WARN] 二重起動を避けるためBridgeは新規起動しません。既存Bridgeを確認してください。")
        else:
            bridge_env = os.environ.copy()
            bridge_env["BUN_BE_BUN"] = "1"
            try:
                version_result = subprocess.run(
                    [str(updater), "--version"],
                    cwd=BRIDGE_DIR,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                    check=False,
                )
                if version_result.returncode == 0:
                    bridge_env["LAUNCHER_VERSION"] = version_result.stdout.strip().splitlines()[0]
            except (OSError, subprocess.SubprocessError):
                pass
            bridge = start_process(
                "Bridge",
                [str(updater), "run", "app\\discord-mcbe.js"],
                BRIDGE_DIR,
                bridge_env,
            )
            if bridge:
                children.append(("Bridge", bridge))
                time.sleep(1.5)
                if bridge.poll() is not None:
                    say("[ERROR] Bridgeが起動直後に終了しました。上のBridgeログを確認してください。")

        venv_python = ensure_gamebot_environment()
        if venv_python is None:
            raise RuntimeError("GameBotの準備に失敗しました")

        gamebot = start_process(
            "GameBot",
            [str(venv_python), "-u", "bot.py"],
            GAMEBOT_DIR,
            os.environ.copy(),
        )
        if gamebot:
            children.append(("GameBot", gamebot))

        if not children:
            say("[ERROR] 起動できたプロセスがありません。")
            return 1

        say("[INFO] 起動監視中です。終了するには Ctrl+C を押してください。")
        reported_exits: set[str] = set()
        while True:
            time.sleep(1)
            running = 0
            for name, process in children:
                code = process.poll()
                if code is None:
                    running += 1
                elif name not in reported_exits:
                    reported_exits.add(name)
                    say(f"[WARN] {name} が終了しました (終了コード: {code})。")
            if running == 0:
                say("[INFO] 起動していたすべてのプログラムが終了しました。")
                break
    except KeyboardInterrupt:
        say("\n[INFO] 終了要求を受け取りました。")
    except (RuntimeError, OSError) as exc:
        say(f"[ERROR] {exc}")
        return 1
    finally:
        for name, process in reversed(children):
            stop_process(name, process)
        say("[INFO] すべての起動プロセスを終了しました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
