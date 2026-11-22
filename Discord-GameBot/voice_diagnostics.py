"""Read-only runtime measurements; never include environment values or credentials."""
from pathlib import Path
import platform
import shutil


def number(path):
    try:
        value = int(Path(path).read_text(encoding='ascii').strip())
        return value if 0 <= value < 2 ** 60 else None
    except (OSError, ValueError):
        return None


def mib(value):
    return f'{value / 1048576:.1f} MiB' if value is not None else '取得できませんでした'


def snapshot(directory):
    limit = number('/sys/fs/cgroup/memory.max')
    used = number('/sys/fs/cgroup/memory.current')
    if limit is None:
        limit = number('/sys/fs/cgroup/memory/memory.limit_in_bytes')
    if used is None:
        used = number('/sys/fs/cgroup/memory/memory.usage_in_bytes')
    rss = None
    try:
        for line in Path('/proc/self/status').read_text(encoding='ascii').splitlines():
            if line.startswith('VmRSS:'):
                rss = int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        free = shutil.disk_usage(directory).free
    except OSError:
        free = None
    return dict(system=platform.system(), architecture=platform.machine(),
                python=platform.python_version(), limit=limit, used=used, rss=rss, free=free)


def describe(data, configured=False):
    return (f'実行環境：{data["system"]} / {data["architecture"]} / Python {data["python"]}\n'
            f'コンテナのメモリ上限：{mib(data["limit"])}\n'
            f'コンテナの使用メモリ：{mib(data["used"])}\n'
            f'Botプロセスの使用メモリ：{mib(data["rss"])}\n'
            f'ファイルシステムの空き：{mib(data["free"])}\n'
            f'VOICEVOX接続先：{"設定済み" if configured else "未設定"}\n'
            '※コンテナ値はMWSアカウント全体の使用量と一致するとは限りません。\n'
            '※ファイルシステムの空きは、MWSの保存容量枠の残量を表しません。\n'
            '※この診断は音声モデルを起動しません。この結果だけでは読み上げの動作保証はできません。')
