"""Cloud entry point: Discord only; no Minecraft listener or Bun required."""
import os
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ['CLOUD_MODE'] = '1'
import sys
sys.path.insert(0, str(ROOT / 'Discord-GameBot'))
os.chdir(ROOT / 'Discord-GameBot')
runpy.run_path(str(ROOT / 'Discord-GameBot' / 'bot.py'), run_name='__main__')
