"""Cloud entry point: Discord only; no Minecraft listener or Bun required."""
import os
import runpy
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ['CLOUD_MODE'] = '1'
import sys
sys.path.insert(0, str(ROOT / 'Discord-GameBot'))
os.chdir(ROOT / 'Discord-GameBot')
from dotenv import load_dotenv
load_dotenv(ROOT / 'Discord-GameBot' / '.env')
import asyncio
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s:%(name)s:%(message)s')
from runtime_probe import install
probe = install()
try:
    revision = subprocess.run(
        ['git', 'rev-parse', '--short', 'HEAD'], cwd=ROOT, capture_output=True,
        text=True, check=True, timeout=3, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    ).stdout.strip()
except (OSError, subprocess.SubprocessError):
    revision = 'unavailable'
logging.getLogger('deployment').info('GameBot revision: %s', revision)
try:
    from player_backup import bootstrap
    asyncio.run(bootstrap())
    runpy.run_path(str(ROOT / 'Discord-GameBot' / 'bot.py'), run_name='__main__')
except BaseException as exc:
    logging.getLogger('runtime_probe').error('Cloud entry exited (boot=%s, error_type=%s)', probe.boot, type(exc).__name__)
    raise
else:
    logging.getLogger('runtime_probe').warning('Cloud entry returned normally (boot=%s)', probe.boot)
finally:
    probe.stop_event.set()
