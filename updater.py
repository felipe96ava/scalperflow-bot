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
    # Apos o bot fechar (PID some), o Windows ainda pode segurar o file
    # handle do .exe por alguns ms (especialmente PyInstaller --onefile).
    # Por isso o move tem retry com delay crescente — sem isso o move
    # falha com "Acesso negado" em uma fracao das tentativas.
    script = f"""@echo off
echo Aguardando bot fechar (PID {pid})...
:waitloop
tasklist /FI "PID eq {pid}" 2>NUL | find /I "{pid}" >NUL
if not errorlevel 1 (
    timeout /t 1 /nobreak >NUL
    goto waitloop
)
echo Aguardando liberacao do executavel...
timeout /t 2 /nobreak >NUL

set RETRY=0
:tentar_move
echo Substituindo executavel (tentativa %RETRY%)...
move /Y "{new_exe}" "{current}" >NUL 2>&1
if not errorlevel 1 goto move_ok
set /a RETRY+=1
if %RETRY% GEQ 10 (
    echo ERRO: nao foi possivel substituir o executavel apos 10 tentativas.
    echo O arquivo novo esta em "{new_exe}".
    echo Substitua manualmente e reabra o bot.
    pause
    exit /b 1
)
timeout /t 2 /nobreak >NUL
goto tentar_move

:move_ok
echo.
echo ========================================
echo   Atualizacao concluida com sucesso!
echo ========================================
echo.
echo Aguardando 8 segundos para o sistema estabilizar
echo (PyInstaller precisa desse tempo para extrair os arquivos
echo  internos sem conflitar com o cleanup da versao anterior)...
timeout /t 8 /nobreak >NUL

echo.
echo Iniciando bot atualizado...
rem explorer.exe usa o ShellExecute do Windows, mais robusto que
rem 'start' do cmd para .exes do PyInstaller --onefile.
explorer "{current}"

echo.
echo Se o bot nao abrir automaticamente, abra manualmente:
echo   {current}
echo.
echo Esta janela fechara em 10 segundos.
timeout /t 10 /nobreak >NUL

(goto) 2>nul & del "%~f0"
"""
    # cp1252 (default do cmd.exe pt-BR) — evita mojibake nas mensagens do .bat
    bat.write_text(script, encoding="cp1252", errors="replace")
    # CREATE_NEW_CONSOLE sozinho: DETACHED_PROCESS conflita e faz o spawn falhar.
    subprocess.Popen(
        ["cmd.exe", "/c", str(bat)],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        close_fds=True,
    )


def _build_dialog_widgets(root, local: str, remote: str, changelog: str, on_pick):
    """Constroi os widgets do popup. on_pick(choice) eh chamado quando user clica."""
    import tkinter as tk
    from tkinter import scrolledtext

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

    def click(choice):
        try:
            root.destroy()
        finally:
            on_pick(choice)

    tk.Button(btns, text="Atualizar agora", width=16, bg="#0a7", fg="white",
              command=lambda: click("update")).pack(side="left", padx=5)
    tk.Button(btns, text="Lembrar depois", width=16,
              command=lambda: click("later")).pack(side="left", padx=5)
    tk.Button(btns, text="Pular esta versao", width=16,
              command=lambda: click("skip")).pack(side="left", padx=5)

    # Centralizar na tela
    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 2
    root.geometry(f"+{x}+{y}")
    root.attributes("-topmost", True)

    # Botao X tambem aciona "later"
    root.protocol("WM_DELETE_WINDOW", lambda: click("later"))


def _show_dialog_modal_standalone(local: str, remote: str, changelog: str) -> str:
    """
    Popup Tk standalone com mainloop proprio.
    Usado em dev (.py mode sem GUI ja rodando).
    """
    import tkinter as tk

    result = {"choice": "later"}
    root = tk.Tk()
    _build_dialog_widgets(root, local, remote, changelog,
                          lambda choice: result.__setitem__("choice", choice))
    root.mainloop()
    return result["choice"]


def _show_progress_and_install(url: str, remote: str, parent=None) -> None:
    """
    Janela com barra de progresso baixando o .exe; ao terminar, substitui.
    Se parent for fornecido, usa Toplevel (nao bloqueia mainloop principal).
    """
    import tkinter as tk
    from tkinter import ttk, messagebox

    if parent is None:
        root = tk.Tk()
        is_toplevel = False
    else:
        root = tk.Toplevel(parent)
        is_toplevel = True

    root.title("Baixando atualizacao...")
    root.geometry("400x130")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    tk.Label(root, text=f"Baixando ScalperFlow {remote}...", font=("Segoe UI", 10)).pack(pady=(15, 5))
    bar = ttk.Progressbar(root, length=360, mode="determinate")
    bar.pack(pady=5)
    status = tk.Label(root, text="0%", font=("Segoe UI", 9))
    status.pack()

    new_exe = Path(tempfile.gettempdir()) / f"{ASSET_NAME}.new"

    def update_progress_ui(pct, done_mb, total_mb):
        bar["value"] = pct
        status.config(text=f"{pct:.1f}%  ({done_mb:.1f} / {total_mb:.1f} MB)")

    def on_finish(ok):
        try:
            root.destroy()
        except Exception:
            pass
        if not ok:
            print("[updater] falha no download")
            try:
                if parent:
                    messagebox.showerror("Erro", "Falha ao baixar a atualizacao. Tente novamente mais tarde.", parent=parent)
            except Exception:
                pass
            return
        _spawn_replacer(new_exe)
        print("[updater] atualizacao baixada. Encerrando para aplicar...")
        os._exit(0)

    def worker():
        def progress(done, total):
            pct = done * 100 / total
            done_mb = done / 1024 / 1024
            total_mb = total / 1024 / 1024
            # Schedule UI update on main thread (Tkinter nao eh thread-safe)
            root.after(0, lambda: update_progress_ui(pct, done_mb, total_mb))
        ok = _download(url, new_exe, on_progress=progress)
        # Schedule completion handler on main thread
        root.after(100, lambda: on_finish(ok))

    threading.Thread(target=worker, daemon=True).start()

    if not is_toplevel:
        # Standalone mode: precisa rodar mainloop proprio
        root.mainloop()
    # Em modo Toplevel, retornamos imediatamente — mainloop principal
    # cuida do rendering, e os callbacks via root.after() executam neles.


# Estado compartilhado entre thread daemon e main thread.
# Tkinter NAO eh thread-safe: dialogos precisam ser mostrados pela
# main thread. Daemon thread apenas faz API call e enfileira info.
_pending_lock = threading.Lock()
_pending_update: dict | None = None
_local_version_cache = ""


def _check_worker(local: str, interval_seconds: int) -> None:
    """
    Loop infinito em thread daemon: checa GitHub, enfileira info se houver
    nova versao. NAO mostra dialogo (isso eh feito pela main thread).
    """
    global _pending_update, _local_version_cache
    _local_version_cache = local
    while True:
        try:
            release = _fetch_latest_release()
            if release is not None:
                remote = release.get("tag_name", "")
                if remote and _is_newer(remote, local) and _load_skip() != remote:
                    asset_url = _find_asset(release)
                    if asset_url:
                        with _pending_lock:
                            # so atualiza se nao tem nada pendente OU a versao mudou
                            if _pending_update is None or _pending_update.get("remote") != remote:
                                _pending_update = {
                                    "local": local,
                                    "remote": remote,
                                    "asset_url": asset_url,
                                    "changelog": release.get("body", "").strip(),
                                }
                                print(f"[updater] nova versao detectada: {remote}")
        except Exception as e:
            print(f"[updater] erro na checagem: {e}")
        time.sleep(interval_seconds)


def check_for_update_async(local_version: str, interval_seconds: int = 1800) -> None:
    """
    Inicia thread daemon que checa por updates periodicamente.
    A main thread deve chamar `consume_pending_update()` regularmente
    (ex: dentro de Tkinter `after()`) para ver se ha update e mostrar dialogo.
    """
    threading.Thread(
        target=_check_worker,
        args=(local_version, interval_seconds),
        daemon=True,
    ).start()


def consume_pending_update() -> dict | None:
    """
    Chamado pela MAIN THREAD periodicamente. Retorna info da update pendente
    (e a remove do estado), ou None se nao ha nada.
    """
    global _pending_update
    with _pending_lock:
        info = _pending_update
        _pending_update = None
        return info


def handle_update_choice(info: dict, parent=None) -> None:
    """
    Chamado pela MAIN THREAD apos consume_pending_update retornar info.
    Mostra o dialogo, processa a escolha.

    Modo assincrono (com parent): cria Toplevel nao-modal, retorna
    imediatamente. Os botoes do popup invocam o callback diretamente
    quando clicados — sem wait_window/mainloop aninhado, evita bloquear
    o mainloop da GUI principal.

    Modo standalone (parent=None): mainloop proprio. Usado em dev .py.
    """
    import tkinter as tk

    if not _running_as_exe():
        print(f"[updater] nova versao: {info['remote']} (.py mode, sem auto-update)")
        return

    def on_pick(choice):
        if choice == "skip":
            _save_skip(info["remote"])
        elif choice == "update":
            _show_progress_and_install(info["asset_url"], info["remote"], parent=parent)
        # 'later' nao faz nada: na proxima checagem o flag sera setado de novo

    if parent is None:
        # Standalone (dev mode)
        choice = _show_dialog_modal_standalone(info["local"], info["remote"], info["changelog"])
        on_pick(choice)
    else:
        # Async — Toplevel nao-modal, callbacks dos botoes
        top = tk.Toplevel(parent)
        _build_dialog_widgets(top, info["local"], info["remote"], info["changelog"], on_pick)
        # Sem grab_set/wait_window: deixa o mainloop principal renderizar.
        # A janela aparece, _poll retorna imediatamente, callbacks executam
        # quando o usuario clicar.
