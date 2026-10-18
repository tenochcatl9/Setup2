#!/usr/bin/env python3
"""
Script de despliegue para servidor de Minecraft Paper en Codespaces.

- Verifica que .env esté oculto en .gitignore. Si no lo está, CANCELA el script.
- El servidor vive en la raíz del proyecto (local), no dentro de una carpeta.
- Detecta si ya existe el servidor (server.jar).
- Si no existe, descarga la última versión estable de Paper desde la API v3
  (con barra de progreso).
- Ejecuta el servidor una vez para generar la carpeta plugins y lo detiene.
- Descarga e instala los plugins ViaVersion, ViaBackwards, AuthMe (login) y
  SkinsRestorer, que se actualiza en cada ejecución contra Modrinth.
  El servidor admite de la 1.10 hasta la última versión: ViaVersion y
  ViaBackwards mantienen el rango actualizado solos.
- Levanta el servidor y espera a que escuche en el puerto 25565.
- Expone el puerto con un túnel público mostrando una barra de carga:
  Asyx (principal, con registro SRV y subdominio permanente) y, si no está
  disponible, Pinggy como respaldo.
- Envía la dirección pública a un webhook de Discord.
- Al terminar, sube los cambios a la rama principal del repositorio.

Modos:
  python3 setup_mc.py                  flujo completo (servidor en primer plano)
  python3 setup_mc.py --solo-notificar deja servidor y túnel en segundo plano,
                                       avisa a Discord con la IP y sube a Git
  python3 setup_mc.py --probar-webhook comprueba el webhook de Discord y sale
  python3 setup_mc.py --importar owner/repo
                                       restaura el mundo desde un .tar.zst
  python3 setup_mc.py --publicar "msg" publica esta versión (panel y/o página)
  python3 setup_mc.py --actualizar     aplica la última versión publicada y sale
  python3 setup_mc.py --ui             solo la página local de control (localhost)

UI local (localhost): al ejecutar el script se levanta una página de control en
http://127.0.0.1:8080 (--puerto-ui, --sin-ui para desactivarla) con el estado
del servidor, la consola (colores incluidos, comandos por el FIFO) y acciones
(parar, arrancar, respaldar, probar webhook). El HTML/JS vive en webui/ y los
temas (CSS) en templates/ (p. ej. templates/template_default.css).

Panel (opcional, ver panel/README.md): con PANEL_URL y PANEL_API_KEY en .env
el script se auto-actualiza al arrancar (--sin-actualizar lo desactiva),
consulta de la API la lista de plugins y reporta estado en vivo cada 5 min.
--publicar sube el script y también la interfaz (webui/ + templates/): la
raíz del panel muestra esa misma interfaz en modo público y el script la
baja al arrancar (sha256 por archivo) para su versión de localhost.

Página pública (Cloudflare Pages, https://tenohost.pages.dev por defecto):
guía de uso y descarga del script (pagina/ + script/ + ui/). De aquí baja el
script la interfaz (html/js/css) al arrancar, y también las versiones nuevas
de sí mismo; setup_mc.py se ofrece suelto y, si el paquete setup_mc/ no está
a su lado, lo descarga solo antes de nada. Se controla con PAGINA_URL (o
PAGINA_URL=no) y PAGES_PROYECTO en .env; --publicar la construye y despliega.

Antes de subir cambios a la rama principal se comprime el servidor en
respaldo/servidor-mc.tar.zst (mundo + plugins + server.jar + manifiesto con
sha256), que es lo que permite importar el mundo en otra máquina. La
instalación (plugins y configuración) va aparte, en
respaldo/instalacion.tar.zst, con este mismo setup_mc.py dentro: se regenera
solo cuando el script cambia.

Túnel: Asyx en modo TCP, que además publica un registro SRV para que el
cliente de Minecraft entre solo con el hostname. Si no está dado de alta o no
responde, el script cae automáticamente a Pinggy (solo ssh, sin cuenta).
"""

import argparse, signal, sys


# ─── Autoinstalación (un solo archivo) ───────────────────────────────────
# setup_mc.py funciona también suelto: si el paquete setup_mc/ no está al
# lado (p. ej. se bajó solo este archivo de la página), aquí se baja el
# script completo antes de importarlo. En el repo no hace nada.
_PAGINA_POR_DEFECTO = "https://tenohost.pages.dev"   # = pagina.URL_POR_DEFECTO


def _miembro_del_script(nombre: str) -> bool:
    partes = nombre.split("/")
    if partes == ["setup_mc.py"]:
        return True
    return (len(partes) == 2 and partes[0] == "setup_mc"
            and partes[1].endswith(".py"))


def _descargar(url: str, timeout: int) -> bytes:
    """Descarga con urllib y User-Agent propio (Cloudflare bloquea el de urllib)."""
    import urllib.request
    peticion = urllib.request.Request(url, headers={"User-Agent": "setup-mc-instalador"})
    with urllib.request.urlopen(peticion, timeout=timeout) as r:
        return r.read()


def _cargar_paquete_si_falta() -> None:
    """Completa la instalación bajando de la página el resto del script."""
    import hashlib, json, os, tarfile, tempfile
    from pathlib import Path

    aqui = Path(__file__).resolve().parent
    if (aqui / "setup_mc" / "config.py").is_file():
        return
    base = ""
    try:
        for linea in (aqui / ".env").read_text().splitlines():
            linea = linea.strip()
            if linea.startswith("PAGINA_URL="):
                base = linea.split("=", 1)[1].strip().strip('"').strip("'")
                break
    except OSError:
        pass
    if base.lower() in ("", "no", "off", "ninguna"):
        base = _PAGINA_POR_DEFECTO
    base = base.rstrip("/")
    tar_url = f"{base}/script/setup_mc.tar.gz"
    print(f"[*] Falta el paquete setup_mc/: bajando el script completo de {base} ...")
    try:
        datos = _descargar(tar_url, 60)
    except OSError as exc:
        raise SystemExit(
            f"    No se pudo bajar {tar_url} ({exc}).\n"
            "    Revisa la conexión o descarga setup_mc.tar.gz y descomprímelo aquí.")
    try:
        esperado = str(json.loads(_descargar(f"{base}/script/manifest.json", 15)).get("sha256") or "")
    except (OSError, ValueError):
        esperado = ""
    real = hashlib.sha256(datos).hexdigest()
    if esperado and esperado != real:
        raise SystemExit("    El paquete no coincide con su sha256; no se instala nada.")
    if not esperado:
        print("[~] No se pudo verificar el sha256 (manifest no disponible).")
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as f:
        f.write(datos)
        temporal = f.name
    try:
        with tarfile.open(temporal, "r:gz") as tar:
            for miembro in tar.getmembers():
                if miembro.isfile() and _miembro_del_script(miembro.name):
                    try:
                        tar.extract(miembro, aqui, filter="data")
                    except TypeError:          # Python < 3.12
                        tar.extract(miembro, aqui)
    finally:
        os.unlink(temporal)
    print("[+] Script completo instalado; sigue la ejecución normal.")


_cargar_paquete_si_falta()


from setup_mc import config
from setup_mc import mcserver
from setup_mc import pagina
from setup_mc import panel
from setup_mc import tuneles
from setup_mc import webui
from setup_mc.avisos import enviar_a_discord, notificar_apagado, notificar_servidor, obtener_webhook
from setup_mc.config import PUERTO_MC, SERVER_DIR, SERVER_JAR
from setup_mc.estado import modo_notificacion
from setup_mc.gitops import configurar_gitignore, exigir_env_protegido
from setup_mc.jars import configurar_authme, instalar_plugins, version_skinsrestorer
from setup_mc.mcserver import arrancar_servidor, esperar_puerto, seguir_servidor
from setup_mc.paper import aceptar_eula, comprobar_java, descargar_paper, generar_plugins_dir, servidor_existe
from setup_mc.procesos import _detener_proceso, parar_todo
from setup_mc.respaldo import importar_respaldo, preguntar_crear_o_importar, preguntar_url_respaldo, subir_cambios
from setup_mc.tuneles import NotificadorTunel, PROVEEDORES, abrir_tunel, asyx_esta_alto, asyx_registrar, detener_tunel, diagnostico_tunel
from setup_mc.ui import aviso, error, log, ok, titulo
from setup_mc.versiones import rango_versiones


try:
    sys.stdout.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass

try:
    import requests
except ImportError:  # pragma: no cover
    print("[!] ERROR: falta la librería 'requests'. Instálala con: pip install requests",
          file=sys.stderr)
    sys.exit(1)
else:
    _ = requests.__version__


# ─── MAIN ─────────────────────────────────────────────────────────────────
def _al_interrumpir(signum, frame):
    """SIGTERM (Codespaces parando la celda) se trata como Ctrl+C."""
    raise KeyboardInterrupt


def parse_args():
    p = argparse.ArgumentParser(description="Servidor de Minecraft Paper + túnel público")
    p.add_argument("--puerto", type=int, default=PUERTO_MC, help="Puerto del servidor (25565)")
    p.add_argument("--memoria", default=mcserver.MEMORIA_MAXIMA, help="Memoria máxima, p. ej. 3G")
    p.add_argument("--sin-tunnel", action="store_true", help="No levantar ningún túnel")
    p.add_argument("--tunel", choices=("auto", *PROVEEDORES), default="auto",
                   help="Servicio de túnel: asyx (por defecto), pinggy o auto "
                        "(Asyx con respaldo a Pinggy)")
    p.add_argument("--tunel-diag", action="store_true",
                   help="Comprobar en segundos si el túnel puede abrirse y salir")
    p.add_argument("--registrar-asyx", action="store_true",
                   help="Registrar el dispositivo en Asyx (imprime el enlace) y salir")
    p.add_argument("--sin-push", action="store_true", help="No hacer commit/push al final")
    p.add_argument("--sin-respaldo", action="store_true",
                   help="No comprimir el servidor antes de subirlo")
    p.add_argument("--respaldo-sin-jar", action="store_true",
                   help="Respaldo sin server.jar (~7 MB en vez de ~70 MB)")
    p.add_argument("--sin-backup-auto", action="store_true",
                   help="No hacer respaldos automáticos mientras el servidor corre")
    p.add_argument("--parar", action="store_true",
                   help="Cerrar el túnel y parar el servidor, y salir")
    p.add_argument("--forzar-respaldo", action="store_true",
                   help="Reempaquetar aunque el mundo no haya cambiado")
    p.add_argument("--solo-notificar", action="store_true",
                   help="Deja servidor y túnel en segundo plano, avisa a Discord y sube a Git")
    p.add_argument("--sin-notificar-tunel", action="store_true",
                   help="No mandar a Discord la barra de progreso del túnel")
    p.add_argument("--probar-webhook", action="store_true",
                   help="Manda un mensaje de prueba a Discord y sale")
    p.add_argument("--crear-nuevo", action="store_true",
                   help="Si no hay servidor, crearlo sin preguntar")
    p.add_argument("--importar", metavar="URL", default="",
                   help="Importa un respaldo (owner/repo, URL de GitHub o URL directa)")
    p.add_argument("--reinstalar-plugins", action="store_true", help="Volver a descargar los plugins")
    p.add_argument("--sin-actualizar", action="store_true",
                   help="No buscar versiones nuevas (panel o página) al arrancar")
    p.add_argument("--actualizar", action="store_true",
                   help="Busca y aplica la última versión publicada (panel o página), y sale")
    p.add_argument("--publicar", metavar="MENSAJE", default=None,
                   help="Publica esta versión: la sube al panel y/o despliega la página, y sale")
    p.add_argument("--ui", action="store_true",
                   help="Solo la página local de control (localhost), sin tocar el servidor")
    p.add_argument("--puerto-ui", type=int, default=8080,
                   help="Puerto de la UI local (8080)")
    p.add_argument("--sin-ui", action="store_true",
                   help="No arrancar la UI local")
    p.add_argument("--rama", default="main", help="Rama a la que se empuja (main)")
    return p.parse_args()


def preparar_servidor(args) -> bool:
    """Deja el server.jar, la EULA y los plugins listos. False si se canceló."""
    nuevo = not servidor_existe()
    if nuevo:
        if args.importar:
            log(f"Importando respaldo desde: {args.importar}")
        elif args.crear_nuevo:
            aviso("No se encontró server.jar: se descargará Paper (--crear-nuevo).")
        else:
            opcion = preguntar_crear_o_importar()
            if opcion == "cancelar":
                return False
            if opcion == "importar":
                args.importar = preguntar_url_respaldo()
                if not args.importar:
                    error("No se indicó el repositorio del respaldo.")
                    return False
            else:
                aviso("No se encontró server.jar: se descargará Paper.")
        if args.importar:
            try:
                importar_respaldo(args.importar)
            except Exception as e:  # noqa: BLE001
                error(f"No se pudo importar: {e}")
                return False
            nuevo = False
            # El mundo grande se publica sin server.jar (pesaba demasiado), así
            # que tras importar puede no haber servidor. Sin esto, una cuenta
            # nueva se queda con el mundo importado pero sin nada que arranque.
            if not SERVER_JAR.is_file():
                aviso("El respaldo no traía server.jar; se descarga Paper.")
                descargar_paper()
        else:
            descargar_paper()
    else:
        log(f"Ya existe un servidor ({SERVER_JAR}).")
    aceptar_eula()
    if nuevo:
        generar_plugins_dir()
    instalar_plugins(forzar=args.reinstalar_plugins)
    return True


def main():
    args = parse_args()
    if args.parar:
        return parar_todo()
    if args.ui:
        return webui.servir_foreground(args.puerto_ui)
    mcserver.MEMORIA_MAXIMA = args.memoria
    if mcserver.MEMORIA_INICIAL.endswith("G") and args.memoria.endswith("G"):
        mcserver.MEMORIA_INICIAL = f"{max(1, int(args.memoria[:-1]) // 2)}G"
    try:
        signal.signal(signal.SIGTERM, _al_interrumpir)
    except (ValueError, OSError):
        pass

    # 0. Panel y página: publicar, actualizar o comprobar versión antes de nada
    if args.publicar is not None:
        return 0 if panel.publicar(args.publicar) else 1
    if args.actualizar:
        if panel.hay_panel():
            panel.actualizar_al_arrancar(manual=True)
        if pagina.pagina_config()[0]:
            pagina.actualizar_al_arrancar(manual=True)
        if not panel.hay_panel() and not pagina.pagina_config()[0]:
            error("Ni panel ni página configurados: revisa PANEL_URL y "
                  "PAGINA_URL en el .env.")
        return 0
    if not args.sin_actualizar:
        panel.actualizar_al_arrancar()
        pagina.actualizar_al_arrancar()

    titulo("SETUP SERVIDOR MINECRAFT PAPER + TÚNEL PÚBLICO")
    log(f"Directorio del servidor (raíz local): {SERVER_DIR}")
    log(f"JAR del servidor: {SERVER_JAR}")
    if panel.hay_panel():
        log(f"Panel: {panel.config_panel()[0]}  (script v{config.VERSION_SCRIPT})")
    if pagina.pagina_config()[0]:
        log(f"Página: {pagina.pagina_config()[0]}  (script v{config.VERSION_SCRIPT})")

    # 0a. Requisitos y secretos: si .env no está oculto, se cancela aquí
    version_java = comprobar_java()
    ok(f"Java {version_java} detectado.")
    configurar_gitignore()
    exigir_env_protegido()

    # 0b. Alta de Asyx (registro en el navegador del usuario)
    if args.registrar_asyx:
        if asyx_esta_alto():
            ok("Asyx ya está dado de alta; no hace falta registrarlo.")
            return 0
        if not sys.stdin.isatty():
            error("El alta de Asyx necesita una terminal: ejecuta el script sin "
                  "redirigir la entrada.")
            return 1
        return 0 if asyx_registrar() else 1

    # 0c. Diagnóstico del túnel
    if args.tunel_diag:
        return diagnostico_tunel()

    # 0d. Comprobación rápida del webhook (falla en 1s si está muerto)
    if args.probar_webhook:
        webhook = obtener_webhook()
        if not webhook:
            return 1
        return 0 if enviar_a_discord(webhook, (
            "✅ **Prueba de webhook**\n"
            "El script se está comunicando bien con Discord. "
            "Cuando arranque el servidor aquí irá la IP del túnel.")) else 1

    # 0e. UI local: página de control (estado, consola, acciones y temas)
    puerto_ui = None
    if not args.sin_ui:
        puerto_ui = webui.lanzar_desacoplado(args.puerto_ui)
        if puerto_ui:
            log(f"UI local: {webui.url_local(puerto_ui)}")

    # 1. Servidor
    if not preparar_servidor(args):
        return 1

    servidor_proc = None
    tunel_proc = None
    cola = None
    direccion = ""
    proveedor_usado = ""
    avisos_tunel = []
    listo_para_subir = False
    codigo = 0

    if args.solo_notificar:
        try:
            return modo_notificacion(args)
        except KeyboardInterrupt:
            aviso("Interrumpido por el usuario.")
            return 1
        except Exception as e:  # noqa: BLE001
            error(str(e))
            return 1

    webhook = ""
    notificador = None
    motivo_apagado = "el script terminó"
    if not args.sin_notificar_tunel:
        webhook = obtener_webhook()
        if webhook:
            notificador = NotificadorTunel(webhook)

    try:
        # 2. Servidor en marcha
        servidor_proc, cola = arrancar_servidor(args.puerto)
        if not esperar_puerto(args.puerto, servidor_proc, cola=cola):
            raise RuntimeError(f"El servidor no abrió el puerto {args.puerto} a tiempo.")
        configurar_authme()  # la config de AuthMe existe tras el primer arranque

        # 3. Túnel con barra de carga (Asyx, con Pinggy de respaldo)
        if not args.sin_tunnel:
            direccion, tunel_proc, proveedor_usado, avisos_tunel = abrir_tunel(
                args.puerto, notificador=notificador, modo=args.tunel)
            for extra in avisos_tunel:
                aviso(extra)
        else:
            aviso("Túnel omitido (--sin-tunnel).")

        listo_para_subir = True

        # 3b. Panel: latidos cada 5 min mientras el servidor siga vivo (y
        # aviso de apagado cuando se detenga, aunque el script ya no esté).
        motivo_panel = "" if direccion else tuneles.MOTIVO_FALLO_TUNEL
        if panel.lanzar_latidor_desacoplado(servidor_proc.pid, direccion,
                                            args.puerto, proveedor_usado,
                                            motivo_panel):
            log("Panel: enviando estado en vivo (latido cada 5 min).")

        # 4. Discord (siempre: con la IP o con el motivo del fallo)
        motivo = tuneles.MOTIVO_FALLO_TUNEL
        if args.sin_tunnel:
            motivo = "el túnel se omitió con --sin-tunnel"
        notificar_servidor(webhook, direccion, args.puerto, motivo)

        # 5. Panel final
        print()
        print("=" * 62)
        print("  SERVIDOR LISTO")
        print("=" * 62)
        print(f"  Ruta local   : {SERVER_DIR}")
        print(f"  Puerto local : {args.puerto}")
        print(f"  Dirección MC : {direccion or '(sin túnel)'}")
        if proveedor_usado:
            print(f"  Servicio     : {PROVEEDORES[proveedor_usado]['nombre']}")
        if panel.hay_panel():
            print(f"  Panel        : {panel.config_panel()[0]}  (estado y actualizaciones)")
        if puerto_ui:
            print(f"  UI local     : {webui.url_local(puerto_ui)}")
            reenviada = webui.url_codespaces(puerto_ui)
            if reenviada:
                print(f"  UI reenviada : {reenviada}  (pestaña PORTS)")
        print(f"  Versiones    : {rango_versiones()}  (ViaVersion + ViaBackwards)")
        print(f"  Skins        : SkinsRestorer {version_skinsrestorer()}  (se actualiza al correr)")
        print("  Login        : AuthMe  (regístrate la primera vez, luego /login)")
        print("  Presiona Ctrl+C para detener el servidor y cerrar el túnel.")
        print()

        # 6. Consola en primer plano
        seguir_servidor(servidor_proc, cola)
    except KeyboardInterrupt:
        aviso("Interrumpido por el usuario (Ctrl+C).")
        motivo_apagado = "el usuario lo detuvo con Ctrl+C"
    except Exception as e:  # noqa: BLE001
        error(str(e))
        codigo = 1
        motivo_apagado = str(e)
    finally:
        if servidor_proc is not None:
            log("Deteniendo el servidor...")
            _detener_proceso(servidor_proc)
        detener_tunel(tunel_proc)
        if servidor_proc is not None:
            notificar_apagado(webhook, direccion, motivo_apagado)
        if args.sin_push:
            aviso("Push omitido (--sin-push).")
        elif listo_para_subir:
            titulo("SUBIENDO CAMBIOS A LA RAMA PRINCIPAL")
            subir_cambios(
                f"chore: servidor Minecraft en la raíz local y túnel Asyx ({args.rama})",
                rama=args.rama,
                con_respaldo=not args.sin_respaldo,
                con_jar=not args.respaldo_sin_jar,
                forzar_respaldo=args.forzar_respaldo,
            )
        else:
            aviso("El setup no llegó a completarse: no se sube nada a Git para no dejar basura.")
    return codigo


if __name__ == "__main__":
    sys.exit(main())
