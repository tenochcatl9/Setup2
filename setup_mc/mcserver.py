"""Arranque y consola del servidor Minecraft."""

import queue, select, subprocess, sys, threading, time
from .config import LOG_SERVIDOR, PID_SERVIDOR, SERVER_DIR, SERVER_JAR, TIMEOUT_ARRANQUE
from .ui import Barra, aviso, limpiar_linea, log
from .procesos import _abrir_log, _bombeo_salida, _drenar, _lanzar_puente_consola, args_sin_backup_auto, puerto_abierto
from .respaldo import BACKUP_AUTO_MANTENER, BACKUP_AUTO_MINUTOS, arrancar_backups_auto

MEMORIA_INICIAL = "1G"

MEMORIA_MAXIMA = "8G"

# ─── 8. Levantar el servidor ─────────────────────────────────────────────
def cmd_servidor(puerto: int) -> list:
    return ["java", f"-Xms{MEMORIA_INICIAL}", f"-Xmx{MEMORIA_MAXIMA}",
            "-XX:+UseG1GC", "-jar", str(SERVER_JAR), "nogui", "--port", str(puerto)]

def arrancar_servidor(puerto: int):
    """Arranca el servidor en segundo plano y devuelve (proceso, cola de logs)."""
    if puerto_abierto(puerto):
        raise RuntimeError(
            f"El puerto {puerto} ya está ocupado por otro proceso. "
            f"Deténlo, o usa otro con  --puerto XXXX  (si es un servidor de este "
            f"script en modo --solo-notificar, puedes reutilizarlo).")
    log(f"Arrancando servidor en {SERVER_DIR} (puerto {puerto})...")
    try:
        proc = subprocess.Popen(
            cmd_servidor(puerto), cwd=str(SERVER_DIR), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
    except FileNotFoundError:
        raise RuntimeError("No se encontró 'java' en el PATH.")
    cola: queue.Queue = queue.Queue()
    # La salida va a la vez a la terminal (cola) y a logs/servidor.log, que es
    # lo que lee la consola de la UI local. Se trunca en cada arranque para no
    # mezclar la sesión anterior.
    mango = _abrir_log(LOG_SERVIDOR, truncar=True)
    threading.Thread(target=_bombeo_salida, args=(proc, cola, mango), daemon=True).start()
    PID_SERVIDOR.write_text(f"{proc.pid}\n")
    # El puente del FIFO y el hilo de respaldos automáticos hacen falta aquí
    # también: este es el camino normal (servidor ya existente), no solo el de
    # la instalación inicial. Sin el puente, el respaldo automático no puede
    # congelar el mundo con save-off.
    _lanzar_puente_consola(proc)
    if not args_sin_backup_auto():
        if arrancar_backups_auto(puerto=puerto):
            log(f"Respaldo automático cada {BACKUP_AUTO_MINUTOS} min en "
                f"respaldo/ (se guardan los {BACKUP_AUTO_MANTENER} últimos).")
    return proc, cola

def esperar_puerto(puerto: int, proc, timeout: int = TIMEOUT_ARRANQUE, cola=None) -> bool:
    """Espera a que el servidor acepte conexiones, con barra de progreso."""
    barra = Barra(f"Esperando al servidor en el puerto {puerto}", limite=timeout)
    try:
        while time.time() - barra.inicio < timeout:
            if proc.poll() is not None:
                if cola is not None:
                    _drenar(cola)
                raise RuntimeError("El servidor se cerró durante el arranque.")
            if puerto_abierto(puerto):
                barra.finalizar(f"Servidor escuchando en {puerto}")
                return True
            if cola is not None:
                _drenar(cola)
            barra.refrescar()
            time.sleep(0.3)
        return False
    finally:
        barra.finalizar()

def enviar_comando(proc, linea: str) -> bool:
    """Escribe una orden en la consola del servidor. False si el pipe está roto."""
    if not linea:
        return False
    if proc.stdin is None:
        return False
    try:
        proc.stdin.write(linea + "\n")
        proc.stdin.flush()
        return True
    except (BrokenPipeError, OSError, ValueError):
        return False

def _leer_consola(proc) -> None:
    """Pasa al servidor lo que se escriba en esta terminal (/op, say, gamemode...).

    Se apoya en select para no bloquear: si no hay nada tecleado, sigue drainage.
    """
    while proc.poll() is None:
        try:
            # Timeout 0.1: espera activa pequeña en lugar de un bucle que quema CPU.
            listos, _, _ = select.select([sys.stdin], [], [], 0.1)
        except (OSError, ValueError):
            return  # sin terminal (p. ej. entrada desde un fichero): nada que hacer
        if not listos:
            continue
        try:
            linea = sys.stdin.readline()
        except (OSError, ValueError, KeyboardInterrupt):
            return
        if not linea:
            return  # stdin cerrado (fin de fichero): deja de escuchar
        linea = linea.strip()
        if not linea:
            continue
        limpiar_linea()
        print(f"\n[>] {linea}")
        if not enviar_comando(proc, linea):
            aviso("El servidor ya no acepta comandos (proceso terminado).")
            return

def seguir_servidor(proc, cola: queue.Queue):
    """Muestra la consola del servidor y acepta comandos hasta Ctrl+C."""
    print()
    print("--- CONSOLA DEL SERVIDOR ---")
    print("    Escribe un comando y pulsa Enter (p. ej. op Jugador, say hola, list).")
    print("    Ctrl+C detiene el servidor y cierra el túnel.")
    lector = threading.Thread(target=_leer_consola, args=(proc,), daemon=True)
    lector.start()
    while proc.poll() is None:
        _drenar(cola)
        time.sleep(0.2)
    _drenar(cola)
    aviso("El servidor se ha detenido.")
