"""Utilidades de procesos: bombear salida, PIDs, FIFO de consola y puertos."""

CODIGO_PUENTE_CONSOLA = """
import os, sys
fd = int(sys.argv[1])
fifo = sys.argv[2]
while True:
    try:
        f = open(fifo, "r")
    except Exception:
        continue
    try:
        for linea in f:
            linea = linea.strip()
            if linea:
                os.write(fd, (linea + "\\n").encode())
    except Exception:
        pass
    try:
        f.close()
    except Exception:
        pass
"""

import os, queue, signal, socket, stat, subprocess, sys, time
from pathlib import Path
from .config import FIFO_CONSOLA, LOGS_DIR
from .ui import _sin_ansi, aviso, limpiar_linea

# ─── 4. Primera ejecución para generar carpeta plugins ────────────────────
def _bombeo_salida(proc, cola: queue.Queue):
    """Lee la salida del proceso línea a línea sin bloquear el hilo principal."""
    try:
        if proc.stdout:
            for linea in iter(proc.stdout.readline, ""):
                cola.put(linea)
    except (ValueError, OSError):
        pass
    finally:
        cola.put(None)

def _drenar(cola: queue.Queue, destino=None) -> list:
    """Vacía la cola imprimiendo cada línea. Devuelve las líneas leídas."""
    leidas = []
    while True:
        try:
            linea = cola.get_nowait()
        except queue.Empty:
            return leidas
        if linea is None:
            continue
        texto = _sin_ansi(linea).rstrip()
        if not texto:
            continue
        leidas.append(texto)
        if destino is not None:
            destino.write(texto + "\n")
        else:
            limpiar_linea()
            print(texto)

def args_sin_backup_auto() -> bool:
    return "--sin-backup-auto" in sys.argv

def _detener_proceso(proc, escribir_stop: bool = True, timeout: int = 30):
    """Envía 'stop' (servidor) o SIGTERM (túnel) y espera; si no responde, mata."""
    if proc.poll() is not None:
        return
    if escribir_stop and proc.stdin:
        try:
            proc.stdin.write("stop\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
    else:
        _senal_grupo(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        aviso("El proceso no se detuvo a tiempo; forzando cierre...")
        _senal_grupo(proc.pid, signal.SIGKILL)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

def puerto_abierto(puerto: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, puerto)) == 0

# ─── 12. Modo "--solo-notificar": procesos en segundo plano ───────────────
def _leer_pid(ruta: Path):
    try:
        return int(ruta.read_text().strip())
    except (OSError, ValueError):
        return None

def _vivo(pid) -> bool:
    """True si el proceso existe y no es un zombie ya terminado.

    os.kill(pid, 0) sigue respondiendo OK para un proceso defunct, así que
    sin esta comprobación un proceso muerto parecería vivo y el script
    esperaría a un cierre que ya ocurrió.
    """
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError, PermissionError):
        return False
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            estado = f.read().rsplit(b")", 1)[-1].split()[0]
        return estado != b"Z"
    except (OSError, IndexError, ValueError):
        return True

def _senal_grupo(pid, sig) -> bool:
    """Envía una señal al proceso y, si es líder de su grupo, al grupo entero.

    El túnel se lanza con start_new_session=True, así que npm -> sh -> node
    quedan en su propio grupo. Matar solo al PID madre dejaba vivo el proceso
    node y el túnel seguía sirviendo (túneles huérfanos). El servidor no es
    líder de grupo, así que ahí solo se señaliza su PID: hacerlo al grupo
    mataría también al script.
    """
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
        return True
    except OSError:
        try:
            os.kill(pid, sig)
            return True
        except OSError:
            return False

def _descendientes(pid: int) -> list:
    """PIDs de todos los descendientes de pid, hojas primero.

    El cliente de Asyx se encadena como npm -> sh -> node. Si solo se mata al
    líder, el node sobrevive (además de quedar re-parentado a init y fuera del
    grupo), y el túnel sigue sirviendo con la IP antigua.
    """
    hijos: dict = {}
    try:
        entradas = os.listdir("/proc")
    except OSError:
        return []
    for entrada in entradas:
        if not entrada.isdigit():
            continue
        try:
            # El campo comm puede traer espacios y paréntesis, así que se
            # parte por el último ")": después van state y ppid.
            with open(f"/proc/{entrada}/stat", "rb") as f:
                campos = f.read().rsplit(b")", 1)[-1].split()
            ppid = int(campos[1])
        except (OSError, IndexError, ValueError):
            continue
        hijos.setdefault(ppid, []).append(int(entrada))
    salida: list = []
    pila = [pid]
    while pila:
        for hijo in hijos.get(pila.pop(), []):
            pila.append(hijo)
            salida.append(hijo)
    return salida

def _matar_pid(pid, espera: float = 5.0) -> bool:
    """Mata un proceso y toda su descendencia (grupo y árbol)."""
    if not _vivo(pid):
        return False
    arbol = _descendientes(pid)
    for hijo in arbol:
        _senal_grupo(hijo, signal.SIGTERM)
    if not _senal_grupo(pid, signal.SIGTERM):
        return False
    fin = time.time() + espera
    while time.time() < fin:
        if not _vivo(pid):
            break
        time.sleep(0.2)
    for hijo in arbol:
        if _vivo(hijo):
            _senal_grupo(hijo, signal.SIGKILL)
    if _vivo(pid):
        _senal_grupo(pid, signal.SIGKILL)
    return True

def _limpiar_pid(ruta: Path):
    try:
        ruta.unlink()
    except OSError:
        pass

def _abrir_log(ruta: Path, truncar: bool = False):
    """Abre un log para escribir. Con truncar=True se borra lo anterior.

    Imprescindible para el log del túnel: si se acumulara, el parser leería la
    dirección del túnel de una ejecución previa y anunciaría una IP muerta.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    f = open(ruta, "w" if truncar else "a", buffering=1)
    f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    return f

def asegurar_fifo_consola() -> bool:
    """Crea el FIFO 'consola' para mandar comandos al servidor desacoplado."""
    try:
        if FIFO_CONSOLA.exists():
            if stat.S_ISFIFO(os.stat(FIFO_CONSOLA).st_mode):
                return True
            FIFO_CONSOLA.unlink()
        os.mkfifo(FIFO_CONSOLA, 0o600)
        return True
    except OSError as e:
        aviso(f"No se pudo crear el FIFO de consola: {e}")
        return False

def _lanzar_puente_consola(proc) -> bool:
    """Proceso ayudante que lee el FIFO y lo vuelca en el stdin del servidor.

    pass_fds es imprescindible: con close_fds=False el ayudante NO hereda el
    extremo de escritura del pipe (se queda solo con 0,1,2) y las escrituras
    fallan en silencio. Así, aunque este script termine, el servidor conserva
    su stdin y el canal de comandos sigue vivo.
    """
    if proc.stdin is None:
        return False
    fd = proc.stdin.fileno()
    if not asegurar_fifo_consola():
        return False
    try:
        subprocess.Popen(
            [sys.executable, "-c", CODIGO_PUENTE_CONSOLA, str(fd), str(FIFO_CONSOLA)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            pass_fds=(fd,), start_new_session=True,
        )
        return True
    except OSError as e:
        aviso(f"No se pudo abrir el canal de comandos: {e}")
        return False
