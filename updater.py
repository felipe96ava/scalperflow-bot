"""
Auto-update via GitHub Releases.

Checa a release mais recente no GitHub, compara com __version__ local,
mostra um popup Tkinter e, se aprovado, baixa o novo .exe e relanca o bot
atraves de um .bat helper (necessario porque o Windows trava o .exe em uso).
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = "felipe96ava/scalperflow-bot"
ASSET_NAME = "ScalperFlowBot.exe"   # nome do .exe no Release (bate com ScalperFlowBot.spec)
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
SKIP_FILE = Path(os.getenv("APPDATA", tempfile.gettempdir())) / "scalperflow" / "skip.json"


def _parse_version(tag: str) -> tuple:
    """'v1.2.3' ou '1.2.3' -> (1, 2, 3). Tag invalida -> (0, 0, 0)."""
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", tag.strip())
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def _is_newer(remote: str, local: str) -> bool:
    return _parse_version(remote) > _parse_version(local)


def _load_skip() -> str:
    try:
        return json.loads(SKIP_FILE.read_text()).get("skip", "")
    except Exception:
        return ""


def _save_skip(version: str) -> None:
    try:
        SKIP_FILE.parent.mkdir(parents=True, exist_ok=True)
        SKIP_FILE.write_text(json.dumps({"skip": version}))
    except Exception:
        pass


def _fetch_latest_release() -> dict | None:
    try:
        req = urllib.request.Request(API_URL, headers={"User-Agent": "scalperflow-updater"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _find_asset(release: dict) -> str | None:
    for asset in release.get("assets", []):
        if asset.get("name") == ASSET_NAME:
            return asset.get("browser_download_url")
    return None


def _download(url: str, dest: Path, on_progress=None) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "scalperflow-updater"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            done = 0
            with open(dest, "wb") as f:
                while chunk := resp.read(64 * 1024):
                    f.write(chunk)
                    done += len(chunk)
                    if on_progress and total:
                        on_progress(done, total)
        return True
    except Exception:
        if dest.exists():
            try:
                dest.unlink()
            except Exception:
                pass
        return False


def _running_as_exe() -> bool:
    """True quando rodando do .exe gerado pelo PyInstaller."""
    return getattr(sys, "frozen", False)


def _current_exe_path() -> Path:
    return Path(sys.executable).resolve()


def _spawn_replacer(new_exe: Path) -> None:
    """
    Cria um .bat que espera o processo atual fechar, substitui o .exe
    e relanca o bot. Necessario porque o Windows trava arquivos em uso.
    """
    current = _current_exe_path()
    pid = os.getpid()
    bat = Path(tempfile.gettempdir()) / "scalperflow_update.bat"
    script = f"""@echo off
echo Aguardando bot fechar (PID {pid})...
:waitloop
tasklist /FI "PID eq {pid}" 2>NUL | find /I "{pid}" >NUL
if not errorlevel 1 (
    timeout /t 1 /nobreak >NUL
    goto waitloop
)
echo Substituindo executavel...
move /Y "{new_exe}" "{current}" >NUL
if errorlevel 1 (
    echo ERRO ao substituir o executavel.
    pause
    exit /b 1
)
echo Atualizacao concluida. Reiniciando...
start "" "{current}"
del "%~f0"
"""
    # cp1252 (default do cmd.exe pt-BR) — evita mojibake nas mensagens do .bat
    bat.write_text(script, encoding="cp1252", errors="replace")
    # CREATE_NEW_CONSOLE sozinho: DETACHED_PROCESS conflita e faz o spawn falhar.
    subprocess.Popen(
        ["cmd.exe", "/c", str(bat)],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        close_fds=True,
    )


def _show_dialog(local: str, remote: str, changelog: str) -> str:
    """
    Popup Tkinter. Retorna 'update', 'later' ou 'skip'.
    Tkinter eh stdlib, nao precisa de dependencia extra.
    """
    import tkinter as tk
    from tkinter import scrolledtext

    result = {"choice": "later"}
    root = tk.Tk()
    root.title("ScalperFlow - Nova versao disponivel")
    root.geometry("520x420")
    root.resizable(False, False)

    tk.Label(root, text="Nova versao disponivel!", font=("Segoe UI", 14, "bold")).pack(pady=(15, 5))
    tk.Label(root, text=f"Versao atual: {local}    >>    Nova versao: {remote}",
             font=("Segoe UI", 10)).pack(pady=5)

    tk.Label(root, text="Notas da versao:", font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=20, pady=(10, 0))
    notes = scrolledtext.ScrolledText(root, height=10, wrap="word", font=("Consolas", 9))
    notes.insert("1.0", changelog or "(sem notas)")
    notes.config(state="disabled")
    notes.pack(fill="x", padx=20, pady=5)

    btns = tk.Frame(root)
    btns.pack(pady=15)

    def pick(choice):
        result["choice"] = choice
        root.destroy()

    tk.Button(btns, text="Atualizar agora", width=16, bg="#0a7", fg="white",
              command=lambda: pick("update")).pack(side="left", padx=5)
    tk.Button(btns, text="Lembrar depois", width=16,
              command=lambda: pick("later")).pack(side="left", padx=5)
    tk.Button(btns, text="Pular esta versao", width=16,
              command=lambda: pick("skip")).pack(side="left", padx=5)

    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 2
    root.geometry(f"+{x}+{y}")
    root.attributes("-topmost", True)
    root.mainloop()
    return result["choice"]


def _show_progress_and_install(url: str, remote: str) -> None:
    """Janela com barra de progresso baixando o .exe; ao terminar, substitui."""
    import tkinter as tk
    from tkinter import ttk, messagebox

    root = tk.Tk()
    root.title("Baixando atualizacao...")
    root.geometry("400x130")
    root.resizable(False, False)

    tk.Label(root, text=f"Baixando ScalperFlow {remote}...", font=("Segoe UI", 10)).pack(pady=(15, 5))
    bar = ttk.Progressbar(root, length=360, mode="determinate")
    bar.pack(pady=5)
    status = tk.Label(root, text="0%", font=("Segoe UI", 9))
    status.pack()

    new_exe = Path(tempfile.gettempdir()) / f"{ASSET_NAME}.new"
    state = {"ok": False}

    def worker():
        def progress(done, total):
            pct = done * 100 / total
            bar["value"] = pct
            status.config(text=f"{pct:.1f}%  ({done/1024/1024:.1f} / {total/1024/1024:.1f} MB)")
            root.update_idletasks()
        state["ok"] = _download(url, new_exe, on_progress=progress)
        root.after(100, root.destroy)

    threading.Thread(target=worker, daemon=True).start()
    root.mainloop()

    if not state["ok"]:
        try:
            tk.Tk().withdraw()
            messagebox.showerror("Erro", "Falha ao baixar a atualizacao. Tente novamente mais tarde.")
        except Exception:
            print("[updater] falha no download")
        return

    _spawn_replacer(new_exe)
    print("[updater] atualizacao baixada. Encerrando para aplicar...")
    os._exit(0)


def _check_worker(local: str) -> None:
    release = _fetch_latest_release()
    if not release:
        return

    remote = release.get("tag_name", "")
    if not remote or not _is_newer(remote, local):
        return

    if _load_skip() == remote:
        return

    asset_url = _find_asset(release)
    if not asset_url:
        return  # release sem .exe ainda (build talvez em andamento)

    if not _running_as_exe():
        # rodando como .py em dev: avisa no console e nao tenta substituir
        print(f"[updater] nova versao disponivel: {remote} (rodando como .py, sem auto-update)")
        return

    changelog = release.get("body", "").strip()
    choice = _show_dialog(local, remote, changelog)

    if choice == "skip":
        _save_skip(remote)
    elif choice == "update":
        _show_progress_and_install(asset_url, remote)
    # 'later' nao faz nada: pergunta de novo na proxima execucao


def check_for_update_async(local_version: str) -> None:
    """Chama no startup do bot. Nao bloqueia."""
    threading.Thread(target=_check_worker, args=(local_version,), daemon=True).start()
