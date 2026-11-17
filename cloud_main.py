"""Cloud entry point: Discord only; no Minecraft listener or Bun required."""
import os
import runpy
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
logging.basicConfig(level=logging.INFO)
from player_backup import bootstrap
asyncio.run(bootstrap())
runpy.run_path(str(ROOT / 'Discord-GameBot' / 'bot.py'), run_name='__main__')
