"""UI local: página de control en localhost (estado, consola, acciones, temas).

La levanta el propio script al arrancar (o `python3 setup_mc.py --ui`) y escucha
solo en 127.0.0.1: no se expone fuera de la máquina. En modo desacoplado la UI
corre como proceso aparte (`python3 -m setup_mc.webui`), así sigue viva cuando
el script termina y sirve para controlar el servidor desde el navegador.

Endpoints:
  GET  /               → webui/index.html
  GET  /app.js         → webui/app.js
  GET  /tema/<x>.css   → templates/<x>.css  (temas: un .css por tema)
  GET  /api/salud      → marca de la UI (para no levantar duplicados)
  GET  /api/temas      → lista de temas disponibles
  GET  /api/estado     → estado del servidor, del túnel y de las versiones
  GET  /api/consola    → líneas nuevas de logs/servidor.log (?desde=N)
  POST /api/consola    → manda un comando al servidor por el FIFO
  POST /api/accion     → parar | arrancar | respaldo | webhook | apagar_ui
"""

import json, os, re, socket, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import config
from .avisos import enviar_a_discord, obtener_webhook
from .config import (BASE_DIR, FIFO_CONSOLA, LOG_SERVIDOR, LOG_TUNEL, LOGS_DIR,
                     PID_SERVIDOR, PID_TUNEL, PUERTO_MC)
from .jars import version_skinsrestorer
from .panel import config_panel
from .procesos import _leer_pid, _vivo, parar_todo, puerto_abierto
from .respaldo import respaldo_automatico
from .tuneles import PROVEEDORES
from .ui import _sin_ansi, error, log
from .versiones import rango_versiones

WEBUI_DIR = BASE_DIR / "webui"
TEMPLATES_DIR = BASE_DIR / "templates"
PUERTO_UI_POR_DEFECTO = 8080
TEMA_POR_DEFECTO = "template_default"

_lanzado = {"ts": 0.0}          # cuándo se pidió arrancar el servidor (anti doble clic)
_candado_respaldo = threading.Lock()

# ─── Datos para la página ─────────────────────────────────────────────────

def listar_temas() -> list:
    """Nombres de tema (sin .css) de templates/; template_default primero."""
    try:
        nombres = sorted(p.stem for p in TEMPLATES_DIR.glob("*.css")
                         if re.fullmatch(r"[A-Za-z0-9_-]+", p.stem))
    except OSError:
        nombres = []
    if TEMA_POR_DEFECTO in nombres:
        nombres.remove(TEMA_POR_DEFECTO)
        nombres.insert(0, TEMA_POR_DEFECTO)
    return nombres

def _puerto_servidor() -> int:
    """Puerto real del servidor: el de server.properties si existe."""
    try:
        for linea in (BASE_DIR / "server.properties").read_text(errors="replace").splitlines():
            if linea.strip().startswith("server-port="):
                return int(linea.split("=", 1)[1].strip())
    except (OSError, ValueError):
        pass
    return PUERTO_MC

def _uptime(pid) -> int | None:
    """Segundos que lleva vivo el proceso (leyendo /proc; none si no se puede)."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            campos = f.read().rsplit(b")", 1)[-1].split()
        inicio = int(campos[19])            # campo 22 de /proc/pid/stat
        with open("/proc/uptime") as f:
            en_pie = float(f.read().split()[0])
        segundos = en_pie - inicio / os.sysconf("SC_CLK_TCK")
        return int(segundos) if segundos >= 0 else None
    except (OSError, ValueError, IndexError):
        return None

def _xmx(pid) -> str:
    """Memoria máxima real del java en marcha (-Xmx), leída de su línea de órdenes."""
    try:
        for trozo in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0"):
            if trozo.startswith(b"-Xmx"):
                return trozo[4:].decode(errors="replace")
    except OSError:
        pass
    return ""

def _tunel_info() -> dict:
    pid = _leer_pid(PID_TUNEL)
    if not _vivo(pid):
        return {"vivo": False, "pid": None, "direccion": "", "proveedor": ""}
    direccion, proveedor = "", ""
    try:
        texto = _sin_ansi(LOG_TUNEL.read_text(errors="replace"))
    except OSError:
        texto = ""
    for clave, prov in PROVEEDORES.items():
        encontrada = prov["parsear"](texto)
        if encontrada:
            direccion, proveedor = encontrada, str(prov.get("nombre") or clave)
            break
    return {"vivo": True, "pid": pid, "direccion": direccion, "proveedor": proveedor}

def estado() -> dict:
    puerto = _puerto_servidor()
    pid_srv = _leer_pid(PID_SERVIDOR)
    propio = _vivo(pid_srv)
    abierto = puerto_abierto(puerto)
    try:
        conf = config_panel()
        panel_url = conf[0] if conf else ""
    except Exception:
        panel_url = ""
    return {
        "servidor": {
            "vivo": propio or abierto,
            "pid": pid_srv if propio else None,
            "externo": bool(abierto and not propio),
            "puerto": puerto,
            "puerto_abierto": abierto,
            "uptime_seg": _uptime(pid_srv) if propio else None,
            "xmx": _xmx(pid_srv) if propio else "",
        },
        "tunel": _tunel_info(),
        "script": {
            "version": config.VERSION_SCRIPT,
            "mc": rango_versiones(),
            "skins": version_skinsrestorer(),
            "panel": panel_url,
        },
        "consola_disponible": FIFO_CONSOLA.exists(),
    }

def leer_consola(desde: int, maximo: int = 800) -> dict:
    """Líneas nuevas de la consola a partir de `desde` (inclusive).

    La fuente es el log más reciente de los dos que puede haber: servidor.log
    (lo escribe el script con la salida del java) o logs/latest.log (lo escribe
    Paper siempre; cubre servidores arrancados por versiones anteriores).
    """
    fuente = _fuente_consola()
    try:
        lineas = fuente.read_text(errors="replace").splitlines()
    except OSError:
        lineas = []
    total = len(lineas)
    if desde > total:               # el log se truncó: hubo arranque nuevo
        desde = 0
    if total - desde > maximo:
        desde = total - maximo
    return {"desde": desde, "total": total, "lineas": lineas[desde:],
            "fuente": fuente.name}

def _fuente_consola() -> Path:
    """Log que mejor representa la consola ahora mismo (el más reciente)."""
    candidatos = [LOG_SERVIDOR, BASE_DIR / "logs" / "latest.log"]
    mejores = []
    for ruta in candidatos:
        try:
            mejores.append((ruta.stat().st_mtime, ruta))
        except OSError:
            continue
    if not mejores:
        return LOG_SERVIDOR
    return max(mejores, key=lambda par: par[0])[1]

def enviar_comando(comando: str) -> bool:
    """Manda una línea al servidor por el FIFO; False si la consola no está."""
    try:
        # O_NONBLOCK: si nadie lee el FIFO (servidor parado o puente muerto)
        # falla al momento con ENXIO en vez de quedarse colgado para siempre.
        fd = os.open(FIFO_CONSOLA, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        os.write(fd, (comando + "\n").encode())
        return True
    except OSError:
        return False
    finally:
        os.close(fd)

# ─── Acciones (botones de la página) ──────────────────────────────────────

def _accion_parar() -> tuple:
    def _tarea():
        parar_todo()
        log("UI: servidor y túnel parados.")
    threading.Thread(target=_tarea, daemon=True).start()
    return True, "parando servidor y túnel (el mundo se guarda; sigue la consola)"

def _accion_arrancar() -> tuple:
    if _vivo(_leer_pid(PID_SERVIDOR)) or puerto_abierto(_puerto_servidor()):
        return False, "ya hay un servidor en marcha"
    ahora = time.time()
    if ahora - _lanzado["ts"] < 90:
        return False, "ya se está arrancando (tarda un poco; mira la consola)"
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        mango = open(LOGS_DIR / "arranque-ui.log", "a", buffering=1)
        subprocess.Popen(
            [sys.executable, str(BASE_DIR / "setup_mc.py"), "--solo-notificar"],
            cwd=str(BASE_DIR), stdin=subprocess.DEVNULL, stdout=mango,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        _lanzado["ts"] = ahora
        return True, "arrancando servidor + túnel (modo segundo plano); sigue la consola"
    except OSError as e:
        return False, f"no se pudo arrancar: {e}"

def _accion_respaldo() -> tuple:
    if _candado_respaldo.locked():
        return False, "ya hay un respaldo en curso"
    def _tarea():
        with _candado_respaldo:
            respaldo_automatico(_puerto_servidor())
    threading.Thread(target=_tarea, daemon=True).start()
    return True, "respaldo lanzado (sigue el progreso en la consola)"

def _accion_webhook() -> tuple:
    webhook = obtener_webhook()
    if not webhook:
        return False, "no hay webhook de Discord configurado en .env"
    if enviar_a_discord(webhook, "✅ **Prueba desde la UI local** — el script y el webhook se hablan bien."):
        return True, "mensaje de prueba enviado a Discord"
    return False, "Discord rechazó el mensaje (revisa el webhook)"

def ejecutar_accion(nombre: str, servidor=None) -> tuple:
    """Devuelve (código HTTP, cuerpo JSON) para POST /api/accion."""
    if nombre == "parar":
        ok_, detalle = _accion_parar()
        return 200, {"ok": ok_, "detalle": detalle}
    if nombre == "arrancar":
        ok_, detalle = _accion_arrancar()
        return (200 if ok_ else 409), {"ok": ok_, "detalle": detalle}
    if nombre == "respaldo":
        ok_, detalle = _accion_respaldo()
        return (200 if ok_ else 409), {"ok": ok_, "detalle": detalle}
    if nombre == "webhook":
        ok_, detalle = _accion_webhook()
        return (200 if ok_ else 409), {"ok": ok_, "detalle": detalle}
    if nombre == "apagar_ui":
        if servidor is not None:
            threading.Thread(target=servidor.shutdown, daemon=True).start()
        return 200, {"ok": True, "detalle": "ui apagada (el servidor no se toca)"}
    return 400, {"error": f"acción desconocida: {nombre}"}

# ─── Servidor HTTP ────────────────────────────────────────────────────────

class _Manejador(BaseHTTPRequestHandler):
    server_version = "setup-mc-webui"
    protocol_version = "HTTP/1.1"

    def log_message(self, formato, *args):      # silencio: el log útil es el del servidor
        pass

    # -- respuestas --
    def _enviar(self, codigo: int, cuerpo: bytes, tipo: str):
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(cuerpo)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, datos, codigo: int = 200):
        self._enviar(codigo, json.dumps(datos, ensure_ascii=False).encode(),
                     "application/json; charset=utf-8")

    def _archivo(self, ruta: Path, tipo: str):
        try:
            self._enviar(200, ruta.read_bytes(), tipo)
        except OSError:
            self._json({"error": f"falta {ruta.name} en {ruta.parent.name}/"}, 404)

    def _cuerpo_json(self) -> dict:
        try:
            largo = int(self.headers.get("Content-Length") or 0)
            if largo <= 0 or largo > 65536:
                return {}
            return json.loads(self.rfile.read(largo) or b"{}")
        except (ValueError, OSError):
            return {}

    # -- rutas --
    def do_GET(self):
        partes = urlsplit(self.path)
        ruta = partes.path.rstrip("/") or "/"
        consulta = parse_qs(partes.query)
        if ruta == "/":
            return self._archivo(WEBUI_DIR / "index.html", "text/html; charset=utf-8")
        if ruta == "/app.js":
            return self._archivo(WEBUI_DIR / "app.js", "text/javascript; charset=utf-8")
        m = re.fullmatch(r"/tema/([A-Za-z0-9_-]+)\.css", ruta)
        if m:
            return self._archivo(TEMPLATES_DIR / f"{m.group(1)}.css", "text/css; charset=utf-8")
        if ruta == "/api/salud":
            return self._json({"setup_mc_webui": True, "version": config.VERSION_SCRIPT,
                               "puerto": self.server.server_port})
        if ruta == "/api/temas":
            return self._json({"temas": listar_temas(), "defecto": TEMA_POR_DEFECTO})
        if ruta == "/api/estado":
            return self._json(estado())
        if ruta == "/api/consola":
            try:
                desde = int((consulta.get("desde") or ["0"])[0])
            except ValueError:
                desde = 0
            return self._json(leer_consola(max(0, desde)))
        return self._json({"error": "ruta no encontrada"}, 404)

    def do_POST(self):
        ruta = urlsplit(self.path).path.rstrip("/") or "/"
        cuerpo = self._cuerpo_json()
        if ruta == "/api/consola":
            comando = str(cuerpo.get("comando") or "").strip()
            if not comando or len(comando) > 300 or "\n" in comando:
                return self._json({"error": "comando vacío o inválido"}, 400)
            if enviar_comando(comando):
                return self._json({"ok": True})
            return self._json({"error": "la consola no está disponible (¿servidor parado?)"}, 409)
        if ruta == "/api/accion":
            codigo, respuesta = ejecutar_accion(str(cuerpo.get("accion") or ""), self.server)
            return self._json(respuesta, codigo)
        return self._json({"error": "ruta no encontrada"}, 404)

def _crear_servidor(puerto: int) -> ThreadingHTTPServer:
    servidor = ThreadingHTTPServer(("127.0.0.1", puerto), _Manejador)
    servidor.daemon_threads = True
    return servidor

# ─── Arranque y direcciones ───────────────────────────────────────────────

def url_local(puerto: int) -> str:
    return f"http://127.0.0.1:{puerto}"

def url_codespaces(puerto: int) -> str:
    """URL reenviada del puerto en Codespaces (si estamos en uno)."""
    nombre = os.environ.get("CODESPACE_NAME")
    if not nombre:
        return ""
    dominio = os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")
    return f"https://{nombre}-{puerto}.{dominio}"

def _salud(puerto: int, timeout: float = 0.6) -> bool:
    """¿Hay ya una UI nuestra escuchando en ese puerto?"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{puerto}/api/salud", timeout=timeout) as r:
            return bool(json.loads(r.read()).get("setup_mc_webui"))
    except Exception:
        return False

def _puerto_libre(preferido: int, intentos: int = 30):
    for puerto in range(preferido, preferido + intentos):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", puerto))
                return puerto
            except OSError:
                continue
    return None

def imprimir_urls(puerto: int) -> None:
    print(f"  UI local      : {url_local(puerto)}")
    reenviada = url_codespaces(puerto)
    if reenviada:
        print(f"  UI reenviada  : {reenviada}  (pestaña PORTS)")

def lanzar_desacoplado(puerto_preferido: int = PUERTO_UI_POR_DEFECTO):
    """Deja la UI escuchando en segundo plano y devuelve su puerto.

    Si ya hay una UI viva cerca del puerto preferido se reutiliza (evita
    duplicados cuando el script se ejecuta varias veces). Si no, se lanza
    `python3 -m setup_mc.webui` con sesión propia: sobrevive a la salida
    del script igual que el latidor del panel.
    """
    for puerto in range(puerto_preferido, puerto_preferido + 21):
        if _salud(puerto):
            return puerto
    puerto = _puerto_libre(puerto_preferido)
    if puerto is None:
        return None
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        mango = open(LOGS_DIR / "webui.log", "a", buffering=1)
        subprocess.Popen(
            [sys.executable, "-m", "setup_mc.webui", "--puerto", str(puerto)],
            cwd=str(BASE_DIR), stdin=subprocess.DEVNULL, stdout=mango,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
    except OSError:
        return None
    for _ in range(40):                     # hasta ~6 s a que conteste
        if _salud(puerto):
            return puerto
        time.sleep(0.15)
    return puerto                           # puede tardar algo más ya arrancando

def servir_foreground(puerto_preferido: int = PUERTO_UI_POR_DEFECTO) -> int:
    """Modo `--ui`: sirve la página en primer plano hasta Ctrl+C."""
    puerto = _puerto_libre(puerto_preferido)
    if puerto is None:
        error(f"No hay ningún puerto libre cerca de {puerto_preferido} para la UI.")
        return 1
    servidor = _crear_servidor(puerto)
    print()
    print("=" * 62)
    print("  UI LOCAL (página de control del servidor)")
    print("=" * 62)
    imprimir_urls(puerto)
    print("  Ctrl+C aquí apaga solo la UI; el servidor no se toca.")
    print("=" * 62)
    print()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print()
        log("UI local apagada.")
    finally:
        servidor.server_close()
    return 0

def _main_webui() -> int:
    import argparse
    p = argparse.ArgumentParser(description="UI local del servidor (solo 127.0.0.1)")
    p.add_argument("--puerto", type=int, default=PUERTO_UI_POR_DEFECTO,
                   help=f"puerto donde servir la página ({PUERTO_UI_POR_DEFECTO})")
    return servir_foreground(p.parse_args().puerto)

if __name__ == "__main__":
    sys.exit(_main_webui())
