"""Servidor y túnel desacoplados; modo --solo-notificar."""

import subprocess
from pathlib import Path
from . import panel
from . import tuneles
from .config import LOG_SERVIDOR, LOG_TUNEL, PID_SERVIDOR, PID_TUNEL, SERVER_DIR
from .ui import _sin_ansi, aviso, error, log, ok, titulo
from .procesos import _abrir_log, _lanzar_puente_consola, _leer_pid, _limpiar_pid, _matar_pid, _vivo, args_sin_backup_auto, puerto_abierto
from .versiones import rango_versiones
from .mcserver import cmd_servidor, esperar_puerto
from .jars import configurar_authme, version_skinsrestorer
from .avisos import notificar_servidor, obtener_webhook
from .tuneles import NotificadorTunel, PROVEEDORES, _listo_para_verificar, abrir_tunel_detached
from .respaldo import BACKUP_AUTO_MANTENER, BACKUP_AUTO_MINUTOS, arrancar_backups_auto, subir_cambios

def direccion_en_log(ruta: Path) -> str:
    """Última dirección pública de túnel que aparece en su log.

    Se prueban los parsers de todos los proveedores: así sirve para recuperar
    un túnel anterior sin saber cuál se usó.
    """
    try:
        texto = _sin_ansi(ruta.read_text(errors="replace"))
    except OSError:
        return ""
    for prov in PROVEEDORES.values():
        direccion = prov["parsear"](texto)
        if direccion:
            return direccion
    return ""

def arrancar_servidor_detached(puerto: int):
    """Arranca el servidor desacoplado: sobrevive a la salida del script."""
    log(f"Arrancando servidor en segundo plano (puerto {puerto})...")
    mango = _abrir_log(LOG_SERVIDOR)
    try:
        proc = subprocess.Popen(
            cmd_servidor(puerto), cwd=str(SERVER_DIR), stdin=subprocess.PIPE,
            stdout=mango, stderr=subprocess.STDOUT, start_new_session=True,
        )
    except FileNotFoundError:
        raise RuntimeError("No se encontró 'java' en el PATH.")
    finally:
        mango.close()
    PID_SERVIDOR.write_text(f"{proc.pid}\n")
    _lanzar_puente_consola(proc)
    if not args_sin_backup_auto():
        arrancar_backups_auto(puerto=puerto)
    return proc

def _parar_anterior(ruta_pid: Path, etiqueta: str) -> bool:
    """Cierra el proceso de una ejecución anterior para no acumular túneles."""
    pid = _leer_pid(ruta_pid)
    if not _vivo(pid):
        _limpiar_pid(ruta_pid)
        return False
    log(f"Cerrando el {etiqueta} de la ejecución anterior (PID {pid})...")
    _matar_pid(pid)
    _limpiar_pid(ruta_pid)
    return True

def modo_notificacion(args) -> int:
    """Deja servidor y túnel vivos, avisa a Discord y sube los cambios."""
    codigo = 0
    # 1. Servidor
    pid_servidor = _leer_pid(PID_SERVIDOR)
    if puerto_abierto(args.puerto):
        ok(f"Ya hay un servidor escuchando en el puerto {args.puerto}"
           + (f" (PID {pid_servidor})" if _vivo(pid_servidor) else " (externo)"))
    else:
        _parar_anterior(PID_SERVIDOR, "servidor")
        proc_srv = arrancar_servidor_detached(args.puerto)
        if not esperar_puerto(args.puerto, proc_srv):
            error("El servidor no abrió el puerto a tiempo.")
            return 1
        configurar_authme()  # la config de AuthMe existe tras el primer arranque
        ok(f"Servidor en segundo plano (PID {proc_srv.pid}, log: {LOG_SERVIDOR}).")

    # 2. Discord (se carga antes del túnel: el progreso se manda mientras abre)
    webhook = obtener_webhook()
    notificador = None
    if webhook and not args.sin_notificar_tunel:
        notificador = NotificadorTunel(webhook)

    # 3. Túnel (Asyx, con Pinggy de respaldo)
    direccion = ""
    avisos_tunel = []
    proveedor_usado = ""
    pid_tunel = _leer_pid(PID_TUNEL)
    if _vivo(pid_tunel):
        previa = direccion_en_log(LOG_TUNEL)
        if previa and _listo_para_verificar(previa):
            direccion = previa
            ok(f"El túnel anterior sigue vivo (PID {pid_tunel}): {direccion}")
    if not direccion:
        _parar_anterior(PID_TUNEL, "túnel")
        if args.sin_tunnel:
            aviso("Túnel omitido (--sin-tunnel): la IP no será pública.")
        else:
            direccion, _proc, proveedor_usado, avisos_tunel = abrir_tunel_detached(
                args.puerto, notificador=notificador, modo=args.tunel)

    # 4. Aviso con la IP (o con el motivo del fallo)
    notificar_servidor(webhook, direccion, args.puerto, tuneles.MOTIVO_FALLO_TUNEL)

    # 4b. Panel: latidos en vivo mientras el servidor siga de fondo
    pid_srv = _leer_pid(PID_SERVIDOR)
    motivo_panel = "" if direccion else tuneles.MOTIVO_FALLO_TUNEL
    if panel.lanzar_latidor_desacoplado(pid_srv, direccion, args.puerto,
                                        proveedor_usado, motivo_panel):
        ok("Panel: enviando estado en vivo (latido cada 5 min).")

    # 4. Panel con cómo pararlo
    print()
    print("=" * 62)
    print("  SERVIDOR Y TÚNEL EN SEGUNDO PLANO")
    print("=" * 62)
    print(f"  IP pública    : {direccion or '(sin túnel)'}")
    if proveedor_usado:
        print(f"  Servicio      : {PROVEEDORES[proveedor_usado]['nombre']}")
    print(f"  Versiones     : {rango_versiones()}  (ViaVersion + ViaBackwards)")
    print(f"  Skins         : SkinsRestorer {version_skinsrestorer()}  (se actualiza al correr)")
    print(f"  Respaldo auto : cada {BACKUP_AUTO_MINUTOS} min en respaldo/ "
          f"({BACKUP_AUTO_MANTENER} últimos, sin parar el servidor)")
    print("  Login         : AuthMe  (regístrate la primera vez, luego /login)")
    print(f"  Servidor PID  : {_leer_pid(PID_SERVIDOR) or '?'}   (log: {LOG_SERVIDOR})")
    print(f"  Túnel PID     : {_leer_pid(PID_TUNEL) or '?'}   (log: {LOG_TUNEL})")
    if panel.hay_panel():
        print(f"  Panel         : {panel.config_panel()[0]}  (estado en vivo y auto-actualización)")
    print()
    for extra in avisos_tunel:
        aviso(extra)
    print("  Ver la consola  :  tail -f logs/servidor.log")
    print("  Mandar comando  :  echo 'op Jugador' > consola   (o 'say hola', 'list'...)")
    print("  Cerrar túnel    :  python3 setup_mc.py --parar   # o: kill -- -$(cat tunel.pid)")
    print("  Parar servidor  :  python3 setup_mc.py --parar   # o: kill $(cat servidor.pid)")
    print("=" * 62)
    print()

    # 5. Git
    if args.sin_push:
        aviso("Push omitido (--sin-push).")
    else:
        titulo("SUBIENDO CAMBIOS A LA RAMA PRINCIPAL")
        if not subir_cambios(
                "chore: modo --solo-notificar (servidor y túnel en segundo plano)",
                rama=args.rama,
                con_respaldo=not args.sin_respaldo,
                con_jar=not args.respaldo_sin_jar,
                forzar_respaldo=args.forzar_respaldo):
            codigo = 1
    return codigo
