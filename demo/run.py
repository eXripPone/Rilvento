"""Одна команда: python run.py. Создаёт локальную .venv при первом запуске."""
from pathlib import Path
import os
import subprocess
import sys
import venv

root = Path(__file__).resolve().parent
env = root / '.venv'
python = env / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
if not python.exists():
    print('Создание локальной среды демо...', flush=True)
    venv.create(env, with_pip=True)
if subprocess.run([str(python), '-c', 'import uvicorn, fastapi, sqlalchemy, networkx, httpx'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
    subprocess.check_call([str(python), '-m', 'pip', 'install', '--retries', '1', '-r', str(root / 'requirements-lock.txt')])
os.chdir(root)
try:
    subprocess.check_call([str(python), '-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', '8765'])
except KeyboardInterrupt:
    print('Демонстрационный сервер остановлен.')
