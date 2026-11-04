"""Explicit stable-release checks; never git-pull into a working checkout."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import threading

import requests

REPOSITORY = 'ShradAp/kid'
APP_ROOT = Path(__file__).resolve().parents[1]
TAG = re.compile(r'v(\d+)\.(\d+)\.(\d+)\Z')
_lock = threading.Lock()
_installer = None


def loader_root():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'kid-launcher'


def installed_tag():
    try:
        value = json.loads((APP_ROOT / '.release.json').read_text(encoding='utf-8-sig'))
        if value['schema'] == 1 and value['repository'] == REPOSITORY and TAG.fullmatch(value['tag']):
            return value['tag']
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def managed_install():
    root = loader_root()
    try:
        relative = APP_ROOT.resolve().relative_to((root / 'versions').resolve())
        return len(relative.parts) == 1 and bool(re.fullmatch(r'[a-f0-9]{32}', relative.parts[0])) and (root / 'loader.ps1').is_file()
    except ValueError:
        return False


def check():
    # This checks GitHub only, independently of the protected Codex transport.
    response = requests.get(f'https://api.github.com/repos/{REPOSITORY}/releases/latest',
                            headers={'User-Agent': 'kid-updater', 'Accept': 'application/vnd.github+json'}, timeout=20)
    if response.status_code == 404:
        return {'available': False, 'current': installed_tag(), 'message': 'Готовых релизов пока нет.'}
    response.raise_for_status()
    release = response.json()
    latest = release.get('tag_name', '')
    if not TAG.fullmatch(latest) or release.get('draft') or release.get('prerelease'):
        raise ValueError('GitHub не вернул поддерживаемый стабильный релиз.')
    assets = {asset['name'] for asset in release.get('assets', [])}
    if not {'kid-source.zip', 'SHA256SUMS'} <= assets:
        return {'available': False, 'current': installed_tag(), 'message': 'Релиз ещё собирается. Повторите проверку позже.'}
    current = installed_tag()
    available = current is None or tuple(map(int, TAG.fullmatch(latest).groups())) > tuple(map(int, TAG.fullmatch(current).groups()))
    managed = managed_install()
    return {'available': available, 'current': current, 'latest': latest, 'can_install': managed,
            'message': ('Доступна новая версия.' if available else 'Установлена актуальная версия.') if managed
            else 'Запущена копия исходников. Для автоматических обновлений установите kid через kid-setup.exe.'}


def install(tag):
    global _installer
    if not isinstance(tag, str) or not TAG.fullmatch(tag):
        raise ValueError('Недопустимый номер версии.')
    if not managed_install():
        raise ValueError('Автоматическое обновление доступно только установке через загрузчик.')
    with _lock:
        if _installer is not None and _installer.poll() is None:
            raise ValueError('Установщик уже открыт. Проверьте его окно.')
        shell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        _installer = subprocess.Popen([str(shell), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                                      str(loader_root() / 'loader.ps1'), '-ReleaseTag', tag, '-NoLaunch'],
                                     creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0))
    return {'started': True, 'message': 'Открыт установщик. Дождитесь успешного завершения, затем закройте kid через трей и запустите ярлык на рабочем столе.'}


def handle(handler, method):
    try:
        if method == 'GET':
            result = check()
        else:
            body = json.loads(handler.rfile.read(int(handler.headers.get('Content-Length', '0'))))
            if body.get('confirm') is not True:
                raise ValueError('Подтвердите установку обновления.')
            result = install(body.get('tag'))
        status = 200
    except (requests.RequestException, OSError):
        result, status = {'error': 'Не удалось связаться с GitHub или запустить установщик. Проверьте сеть и повторите попытку.'}, 502
    except ValueError as error:
        result, status = {'error': str(error)}, 400
    except (TypeError, AttributeError):
        result, status = {'error': 'Обновление недоступно. Проверьте версию, подтверждение и наличие установки через загрузчик.'}, 400
    handler._set_cors(status)
    handler.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
