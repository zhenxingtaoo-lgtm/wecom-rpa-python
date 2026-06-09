from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_ACTIVE_LOCK = threading.Lock()
_ACTIVE_PROCESSES: set[subprocess.Popen[bytes]] = set()


def decode_process_output(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def windows_path(path: Path) -> str:
    wslpath = shutil.which("wslpath")
    if not wslpath:
        return str(path)
    converted = subprocess.run([wslpath, "-w", str(path)], check=False, capture_output=True, text=True)
    return converted.stdout.strip() if converted.returncode == 0 else str(path)


def powershell_exe() -> Path | None:
    native = shutil.which("powershell.exe") or shutil.which("powershell")
    if native:
        return Path(native)
    wsl_path = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    return wsl_path if wsl_path.exists() else None


def hidden_subprocess_kwargs() -> dict[str, object]:
    kwargs: dict[str, object] = {}
    if os.name != "nt":
        return kwargs
    kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    startupinfo = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
    startupinfo.wShowWindow = 0
    kwargs["startupinfo"] = startupinfo
    return kwargs


def run_powershell(script: str, args: list[str] | None = None, *, timeout: int = 20) -> subprocess.CompletedProcess[bytes] | None:
    powershell = powershell_exe()
    if powershell is None:
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", encoding="utf-8-sig", dir=Path(tempfile.gettempdir()), delete=False) as f:
        f.write(script)
        script_path = Path(f.name)
    cmd = [str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", windows_path(script_path), *(args or [])]
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **hidden_subprocess_kwargs())
        with _ACTIVE_LOCK:
            _ACTIVE_PROCESSES.add(proc)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_process(proc)
            stdout, stderr = proc.communicate()
            return subprocess.CompletedProcess(cmd, 124, stdout, stderr)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except Exception as exc:
        log.debug("PowerShell 执行异常：%s", exc)
        return None
    finally:
        if proc is not None:
            with _ACTIVE_LOCK:
                _ACTIVE_PROCESSES.discard(proc)
        try:
            script_path.unlink()
        except OSError:
            pass


def terminate_active_powershell() -> None:
    with _ACTIVE_LOCK:
        processes = list(_ACTIVE_PROCESSES)
    for proc in processes:
        _terminate_process(proc)


def _terminate_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=1)
    except Exception:
        try:
            proc.kill()
        except Exception as exc:
            log.debug("终止 PowerShell 子进程失败：%s", exc)
