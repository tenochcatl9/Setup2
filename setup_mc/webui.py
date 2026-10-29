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
  GET  /api/plugins    → lista de plugins locales instalados
  GET  /api/plugins/catalog → plugins propios disponibles
  POST /api/plugins/instalar → descarga y verifica un plugin de la tienda
  GET  /api/estado     → estado del servidor, del túnel y de las versiones
  GET  /api/consola    → líneas nuevas de logs/servidor.log (?desde=N)
  POST /api/consola    → manda un comando al servidor por el FIFO
  POST /api/accion     → parar | arrancar | respaldo | webhook | apagar_ui
"""

import hashlib, io, json, os, re, socket, struct, subprocess, sys, tempfile, threading, time, urllib.error, urllib.request, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import config
from . import mcserver
from . import pagina
from .avisos import enviar_a_discord, obtener_webhook
from .config import (BASE_DIR, FIFO_CONSOLA, LOG_SERVIDOR, LOG_TUNEL, LOGS_DIR,
                     PID_SERVIDOR, PID_TUNEL, PUERTO_MC)
from .jars import version_skinsrestorer
from .panel import actualizar_ui, config_panel
from .paper import estado_paper
from .procesos import _leer_pid, _vivo, parar_todo, puerto_abierto
from .respaldo import respaldo_automatico
from .tuneles import PROVEEDORES
from .ui import _sin_ansi, error, log
from .versiones import rango_versiones

WEBUI_DIR = BASE_DIR / "webui"
TEMPLATES_DIR = BASE_DIR / "templates"
PUERTO_UI_POR_DEFECTO = 8080
TEMA_POR_DEFECTO = "template_default"
MAX_PLUGIN_BYTES = 100 * 1024 * 1024
RE_PLUGIN_NOMBRE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,123}\.jar$")
CATALOGO_PLUGIN_IDS = {"permadeath", "combat-basics"}

_lanzado = {"ts": 0.0}          # cuándo se pidió arrancar el servidor (anti doble clic)
_candado_respaldo = threading.Lock()
_candado_paper = threading.Lock()
_ultima_actualizacion = {"ts": 0.0}   # anti doble clic del botón de Paper

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

# ─── Jugadores en vivo (Server List Ping) ─────────────────────────────────
# Se pregunta al propio servidor por el protocolo de estado de Minecraft. Así
# el contador es exacto y no hace falta RCON, consultar la consola ni escribir
# nada en el log. enable-status=true es el valor por defecto de Paper.
_CACHE_JUGADORES = {"ts": 0.0, "puerto": 0, "datos": None}
_CANDADO_JUGADORES = threading.Lock()

def _max_jugadores() -> int:
    try:
        for linea in (BASE_DIR / "server.properties").read_text(errors="replace").splitlines():
            if linea.strip().startswith("max-players="):
                return int(linea.split("=", 1)[1].strip())
    except (OSError, ValueError):
        pass
    return 20

def memoria_sistema() -> dict:
    """Memoria del sistema disponible, limitada por cgroups cuando existan."""
    try:
        valores = {}
        for linea in Path("/proc/meminfo").read_text().splitlines():
            clave, bruto = linea.split(":", 1)
            if clave in ("MemTotal", "MemAvailable"):
                valores[clave] = int(bruto.strip().split()[0]) * 1024
        total = valores["MemTotal"]
        disponible = valores["MemAvailable"]
    except (OSError, ValueError, KeyError, IndexError):
        return {"total_mb": 0, "disponible_mb": 0}

    for limite_ruta, uso_ruta in (
        (Path("/sys/fs/cgroup/memory.max"), Path("/sys/fs/cgroup/memory.current")),
        (Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
         Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")),
    ):
        try:
            limite_texto = limite_ruta.read_text().strip()
            limite = int(limite_texto) if limite_texto != "max" else 0
            uso = int(uso_ruta.read_text().strip())
            if 0 < limite < (1 << 60):
                total = min(total, limite)
                disponible = min(disponible, max(0, limite - uso))
                break
        except (OSError, ValueError):
            continue
    return {"total_mb": total // (1024 * 1024),
            "disponible_mb": disponible // (1024 * 1024)}

def listar_plugins_instalados() -> list:
    """JARs instalados directamente en la carpeta plugins/."""
    if not config.PLUGINS_DIR.exists():
        return []
    return [
        {"nombre": archivo.name, "tamano": archivo.stat().st_size}
        for archivo in sorted(config.PLUGINS_DIR.glob("*.jar"), key=lambda p: p.name.lower())
        if archivo.is_file() and not archivo.is_symlink()
    ]

def guardar_plugin(nombre: str, datos: bytes, reemplazar: bool = False) -> str:
    """Valida e instala atómicamente un plugin JAR sin reiniciar el servidor."""
    if not RE_PLUGIN_NOMBRE.fullmatch(nombre):
        raise ValueError("nombre inválido: usa un archivo .jar sin rutas.")
    if not datos or len(datos) > MAX_PLUGIN_BYTES:
        raise ValueError("el plugin debe ocupar entre 1 byte y 100 MB.")
    try:
        with zipfile.ZipFile(io.BytesIO(datos)) as archivo:
            nombres = set(archivo.namelist())
    except (OSError, zipfile.BadZipFile):
        raise ValueError("el archivo no es un JAR/ZIP válido.")
    if not ({"plugin.yml", "paper-plugin.yml"} & nombres):
        raise ValueError("el JAR no contiene plugin.yml ni paper-plugin.yml.")

    destino = config.PLUGINS_DIR / nombre
    if destino.exists() and not reemplazar:
        raise FileExistsError(f"{nombre} ya existe; confirma el reemplazo para actualizarlo.")
    config.PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    temporal = None
    try:
        with tempfile.NamedTemporaryFile(dir=config.PLUGINS_DIR, prefix=".plugin-",
                                         suffix=".part", delete=False) as archivo:
            temporal = Path(archivo.name)
            archivo.write(datos)
        os.replace(temporal, destino)
    finally:
        if temporal is not None:
            temporal.unlink(missing_ok=True)
    return nombre

def _obtener_manifest_plugins() -> tuple:
    """Carga el catálogo de la página pública configurada."""
    url, _ = pagina.pagina_config()
    if not url or urlsplit(url).scheme != "https":
        raise RuntimeError("la página de plugins no está configurada con HTTPS.")
    solicitud = urllib.request.Request(
        f"{url}/plugins/manifest.json",
        headers={"User-Agent": "setup-mc-plugin-installer"},
    )
    try:
        with urllib.request.urlopen(solicitud, timeout=15) as respuesta:
            if respuesta.status != 200:
                raise RuntimeError(f"el catálogo respondió HTTP {respuesta.status}.")
            contenido = respuesta.read(256 * 1024 + 1)
            if len(contenido) > 256 * 1024:
                raise RuntimeError("el catálogo de plugins es demasiado grande.")
            datos = json.loads(contenido)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise RuntimeError(f"no se pudo leer el catálogo de plugins: {exc}") from exc
    if not isinstance(datos, dict) or not isinstance(datos.get("plugins"), list):
        raise RuntimeError("el catálogo de plugins no tiene un formato válido.")
    return url, datos

def plugins_catalogo() -> list:
    """Plugins propios disponibles para instalar desde Pages."""
    _url, manifest = _obtener_manifest_plugins()
    disponibles = []
    for plugin in manifest["plugins"]:
        if not isinstance(plugin, dict) or plugin.get("id") not in CATALOGO_PLUGIN_IDS:
            continue
        disponibles.append({
            "id": plugin["id"],
            "nombre": str(plugin.get("nombre") or ""),
            "version": str(plugin.get("version") or ""),
            "descripcion": str(plugin.get("descripcion") or ""),
        })
    return disponibles

def instalar_plugin_catalogo(plugin_id: str) -> str:
    """Descarga un JAR permitido del catálogo, valida su hash y lo guarda."""
    if plugin_id not in CATALOGO_PLUGIN_IDS:
        raise ValueError(f"plugin desconocido: {plugin_id}")
    url, manifest = _obtener_manifest_plugins()
    plugin = next((p for p in manifest["plugins"]
                   if isinstance(p, dict) and p.get("id") == plugin_id), None)
    if plugin is None:
        raise RuntimeError(f"{plugin_id} no está publicado en el catálogo.")
    nombre = str(plugin.get("archivo") or "")
    esperado = str(plugin.get("sha256") or "")
    if (not RE_PLUGIN_NOMBRE.fullmatch(nombre)
            or not re.fullmatch(r"[a-fA-F0-9]{64}", esperado)):
        raise RuntimeError("el catálogo no publica un nombre o SHA-256 válido.")
    solicitud = urllib.request.Request(
        f"{url}/plugins/{quote(nombre)}",
        headers={"User-Agent": "setup-mc-plugin-installer"},
    )
    try:
        with urllib.request.urlopen(solicitud, timeout=60) as respuesta:
            if respuesta.status != 200:
                raise RuntimeError(f"la descarga respondió HTTP {respuesta.status}.")
            datos = respuesta.read(MAX_PLUGIN_BYTES + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimeError(f"no se pudo descargar {nombre}: {exc}") from exc
    if len(datos) > MAX_PLUGIN_BYTES:
        raise RuntimeError("el JAR supera el máximo permitido de 100 MB.")
    if hashlib.sha256(datos).hexdigest().lower() != esperado.lower():
        raise RuntimeError("el SHA-256 del JAR no coincide con el catálogo.")
    return guardar_plugin(nombre, datos, reemplazar=True)

def _origen_paginas() -> str:
    url, _ = pagina.pagina_config()
    partes = urlsplit(url)
    return f"{partes.scheme}://{partes.netloc}" if partes.scheme and partes.netloc else ""

def _origen_permitido(request: BaseHTTPRequestHandler) -> bool:
    origen = request.headers.get("Origin", "")
    return bool(origen and origen == _origen_paginas())

def validar_memoria(memoria: str) -> str:
    """Acepta un máximo entero en GB antes de pasarlo a la línea de comandos."""
    if not re.fullmatch(r"[1-9][0-9]{0,2}G", memoria):
        raise ValueError("la RAM debe ser un número entero de 1 a 128 GB.")
    gigas = int(memoria[:-1])
    if gigas > 128:
        raise ValueError("la RAM máxima permitida desde la UI es 128 GB.")
    return f"{gigas}G"

def _varint(numero: int) -> bytes:
    """Varint sin signo del protocolo de Minecraft."""
    numero &= 0xFFFFFFFF
    salida = bytearray()
    while True:
        byte = numero & 0x7F
        numero >>= 7
        if numero:
            salida.append(byte | 0x80)
        else:
            salida.append(byte)
            return bytes(salida)

def _varint_de_bytes(datos: bytes, inicio: int = 0) -> tuple:
    numero = 0
    for i in range(5):
        if inicio + i >= len(datos):
            raise ValueError("varint incompleto")
        byte = datos[inicio + i]
        numero |= (byte & 0x7F) << (7 * i)
        if not byte & 0x80:
            return numero, inicio + i + 1
    raise ValueError("varint demasiado largo")

def _leer_varint(sock) -> int:
    numero = 0
    for i in range(5):
        byte = sock.recv(1)
        if not byte:
            raise OSError("conexión cerrada")
        valor = byte[0]
        numero |= (valor & 0x7F) << (7 * i)
        if not valor & 0x80:
            return numero
    raise OSError("varint demasiado largo")

def _leer_paquete(sock) -> bytes:
    largo = _leer_varint(sock)
    if largo < 1 or largo > 2 * 1024 * 1024:
        raise OSError("paquete de estado inválido")
    datos = bytearray()
    while len(datos) < largo:
        trozo = sock.recv(largo - len(datos))
        if not trozo:
            raise OSError("conexión cerrada")
        datos.extend(trozo)
    return bytes(datos)

def _ping_jugadores(puerto: int, timeout: float = 1.2) -> dict:
    with socket.create_connection(("127.0.0.1", puerto), timeout=timeout) as sock:
        sock.settimeout(timeout)
        host = "127.0.0.1"
        handshake = (b"\x00" + _varint(-1) + _varint(len(host))
                     + host.encode("utf-8") + struct.pack(">H", puerto) + _varint(1))
        sock.sendall(_varint(len(handshake)) + handshake)
        sock.sendall(b"\x01\x00")           # status request
        datos = _leer_paquete(sock)
    if not datos or datos[0] != 0:
        raise OSError("respuesta de estado inesperada")
    largo, inicio = _varint_de_bytes(datos, 1)
    texto = datos[inicio:inicio + largo].decode("utf-8", errors="replace")
    status = json.loads(texto)
    jugadores = status.get("players") or {}
    muestra = jugadores.get("sample") or []
    nombres = [str(p.get("name")) for p in muestra
               if isinstance(p, dict) and p.get("name")]
    return {
        "en_linea": int(jugadores.get("online") or 0),
        "max": int(jugadores.get("max") or _max_jugadores()),
        "nombres": nombres,
        "disponible": True,
    }

def estado_jugadores(puerto: int | None = None) -> dict:
    """Jugadores conectados ahora, con caché corta para no repetir el ping."""
    puerto = puerto or _puerto_servidor()
    ahora = time.time()
    with _CANDADO_JUGADORES:
        datos = _CACHE_JUGADORES["datos"]
        if (datos is not None and _CACHE_JUGADORES["puerto"] == puerto
                and ahora - _CACHE_JUGADORES["ts"] < 3.0):
            return datos
    try:
        datos = _ping_jugadores(puerto)
    except (OSError, ValueError, json.JSONDecodeError):
        datos = {"en_linea": 0, "max": _max_jugadores(), "nombres": [],
                 "disponible": False}
    with _CANDADO_JUGADORES:
        _CACHE_JUGADORES.update(ts=ahora, puerto=puerto, datos=datos)
    return datos

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
        "modo": "local",
        "servidor": {
            "vivo": propio or abierto,
            "pid": pid_srv if propio else None,
            "externo": bool(abierto and not propio),
            "puerto": puerto,
            "puerto_abierto": abierto,
            "uptime_seg": _uptime(pid_srv) if propio else None,
            "xmx": _xmx(pid_srv) if propio else "",
        },
        "memoria_sistema": memoria_sistema(),
        "memoria_configurada": mcserver.MEMORIA_MAXIMA,
        "tunel": _tunel_info(),
        "jugadores": estado_jugadores(puerto),
        "paper": estado_paper(),
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

def _accion_arrancar(memoria: str = mcserver.MEMORIA_MAXIMA) -> tuple:
    if _vivo(_leer_pid(PID_SERVIDOR)) or puerto_abierto(_puerto_servidor()):
        return False, "ya hay un servidor en marcha"
    try:
        memoria = validar_memoria(memoria)
    except ValueError as exc:
        return False, str(exc)
    ahora = time.time()
    if ahora - _lanzado["ts"] < 90:
        return False, "ya se está arrancando (tarda un poco; mira la consola)"
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        mango = open(LOGS_DIR / "arranque-ui.log", "a", buffering=1)
        subprocess.Popen(
            [sys.executable, str(BASE_DIR / "setup_mc.py"), "--solo-notificar",
             "--sin-ui", "--sin-abrir-ui", "--memoria", memoria],
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

def _accion_actualizar_paper() -> tuple:
    """Lanza la actualización de Paper en segundo plano (para el botón de la UI)."""
    info = estado_paper()
    if not info.get("hay_nueva"):
        return False, "Paper ya está al día (o aún se está consultando la última versión)"
    with _candado_paper:
        ahora = time.time()
        if ahora - _ultima_actualizacion["ts"] < 15 * 60:
            return False, "ya hay una actualización en curso; sigue la consola"
        _ultima_actualizacion["ts"] = ahora
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        # Se escribe en el mismo log que lee la consola: el usuario ve el
        # respaldo, la descarga y el arranque desde la propia página.
        mango = open(LOG_SERVIDOR, "a", buffering=1)
        subprocess.Popen(
            [sys.executable, str(BASE_DIR / "setup_mc.py"),
             "--actualizar-paper", "--solo-notificar", "--sin-push",
             "--sin-ui", "--sin-abrir-ui"],
            cwd=str(BASE_DIR), stdin=subprocess.DEVNULL, stdout=mango,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
    except OSError as e:
        return False, f"no se pudo lanzar la actualización: {e}"
    return True, (f"actualizando Paper {info.get('actual')} -> {info.get('ultima')}: "
                  "se para el servidor, se respalda y vuelve a arrancar; sigue la consola")

def _accion_webhook() -> tuple:
    webhook = obtener_webhook()
    if not webhook:
        return False, "no hay webhook de Discord configurado en .env"
    if enviar_a_discord(webhook, "✅ **Prueba desde la UI local** — el script y el webhook se hablan bien."):
        return True, "mensaje de prueba enviado a Discord"
    return False, "Discord rechazó el mensaje (revisa el webhook)"

def ejecutar_accion(nombre: str, servidor=None, memoria: str = mcserver.MEMORIA_MAXIMA) -> tuple:
    """Devuelve (código HTTP, cuerpo JSON) para POST /api/accion."""
    if nombre == "parar":
        ok_, detalle = _accion_parar()
        return 200, {"ok": ok_, "detalle": detalle}
    if nombre == "arrancar":
        ok_, detalle = _accion_arrancar(memoria)
        return (200 if ok_ else 409), {"ok": ok_, "detalle": detalle}
    if nombre == "respaldo":
        ok_, detalle = _accion_respaldo()
        return (200 if ok_ else 409), {"ok": ok_, "detalle": detalle}
    if nombre == "webhook":
        ok_, detalle = _accion_webhook()
        return (200 if ok_ else 409), {"ok": ok_, "detalle": detalle}
    if nombre == "actualizar_paper":
        ok_, detalle = _accion_actualizar_paper()
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
        origen = self.headers.get("Origin", "")
        if origen == _origen_paginas() and _origen_permitido(self):
            self.send_header("Access-Control-Allow-Origin", origen)
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Vary", "Origin")
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

    def _descargar_log(self):
        """Entrega el log completo como descarga, sin el límite de /api/consola."""
        fuente = _fuente_consola()
        try:
            cuerpo = fuente.read_bytes()
        except OSError:
            return self._json({"error": "todavía no hay log de consola"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Content-Disposition", f'attachment; filename="{fuente.name}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(cuerpo)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _cuerpo_json(self) -> dict:
        try:
            largo = int(self.headers.get("Content-Length") or 0)
            if largo <= 0 or largo > 65536:
                return {}
            datos = json.loads(self.rfile.read(largo) or b"{}")
            return datos if isinstance(datos, dict) else {}
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
        if ruta == "/api/plugins":
            try:
                return self._json({"plugins": listar_plugins_instalados()})
            except OSError as exc:
                return self._json({"error": f"no se pudo leer plugins/: {exc}"}, 500)
        if ruta == "/api/plugins/catalog":
            try:
                return self._json({"plugins": plugins_catalogo()})
            except (OSError, RuntimeError) as exc:
                return self._json({"error": str(exc)}, 502)
        if ruta == "/api/estado":
            return self._json(estado())
        if ruta == "/api/consola/descargar":
            return self._descargar_log()
        if ruta == "/api/consola":
            try:
                desde = int((consulta.get("desde") or ["0"])[0])
            except ValueError:
                desde = 0
            return self._json(leer_consola(max(0, desde)))
        return self._json({"error": "ruta no encontrada"}, 404)

    def do_OPTIONS(self):
        if (not _origen_permitido(self)
                or self.headers.get("Origin") != _origen_paginas()):
            return self._json({"error": "origen no permitido"}, 403)
        metodo = self.headers.get("Access-Control-Request-Method", "")
        cabeceras = {
            valor.strip().lower()
            for valor in self.headers.get("Access-Control-Request-Headers", "").split(",")
            if valor.strip()
        }
        if metodo not in ("GET", "POST") or not cabeceras.issubset(
                {"cache-control", "content-type"}):
            return self._json({"error": "solicitud CORS no permitida"}, 403)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", _origen_paginas())
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Cache-Control, Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "300")
        self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        partes = urlsplit(self.path)
        ruta = partes.path.rstrip("/") or "/"
        if ruta == "/api/plugins/instalar":
            if not _origen_permitido(self):
                return self._json({"error": "origen no permitido; conecta desde la página oficial"}, 403)
            cuerpo = self._cuerpo_json()
            plugin_id = str(cuerpo.get("plugin") or "")
            try:
                nombre = instalar_plugin_catalogo(plugin_id)
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            except (OSError, RuntimeError) as exc:
                return self._json({"error": str(exc)}, 502)
            return self._json({"ok": True, "nombre": nombre,
                               "detalle": "instalado; se cargará al próximo arranque"})
        if ruta.startswith("/api/plugins/"):
            if self.headers.get("X-Setup2-Plugin-Upload") != "1":
                return self._json({"error": "falta la cabecera de carga local"}, 403)
            nombre = unquote(ruta.removeprefix("/api/plugins/"))
            try:
                largo = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._json({"error": "tamaño de archivo inválido"}, 400)
            if largo <= 0 or largo > MAX_PLUGIN_BYTES:
                return self._json({"error": "el plugin debe ocupar entre 1 byte y 100 MB"}, 413)
            datos = self.rfile.read(largo)
            if len(datos) != largo:
                return self._json({"error": "la carga del archivo quedó incompleta"}, 400)
            reemplazar = "1" in parse_qs(partes.query).get("reemplazar", [])
            try:
                nombre = guardar_plugin(nombre, datos, reemplazar)
            except FileExistsError as exc:
                return self._json({"error": str(exc)}, 409)
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            except OSError as exc:
                return self._json({"error": f"no se pudo guardar el plugin: {exc}"}, 500)
            return self._json({"ok": True, "nombre": nombre})
        cuerpo = self._cuerpo_json()
        if ruta == "/api/consola":
            comando = str(cuerpo.get("comando") or "").strip()
            if not comando or len(comando) > 300 or "\n" in comando:
                return self._json({"error": "comando vacío o inválido"}, 400)
            if enviar_comando(comando):
                return self._json({"ok": True})
            return self._json({"error": "la consola no está disponible (¿servidor parado?)"}, 409)
        if ruta == "/api/accion":
            codigo, respuesta = ejecutar_accion(
                str(cuerpo.get("accion") or ""), self.server,
                str(cuerpo.get("memoria") or mcserver.MEMORIA_MAXIMA))
            return self._json(respuesta, codigo)
        return self._json({"error": "ruta no encontrada"}, 404)

def _crear_servidor(puerto: int) -> ThreadingHTTPServer:
    host = "0.0.0.0" if os.environ.get("CODESPACE_NAME") else "127.0.0.1"
    servidor = ThreadingHTTPServer((host, puerto), _Manejador)
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
            # Igual que el servidor (allow_reuse_address): un TIME_WAIT
            # reciente no debería mandarnos a un puerto más alto sin motivo.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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

def abrir_navegador(puerto: int) -> bool:
    """Abre la UI en el navegador del usuario, sin romper nada si no hay uno.

    En Codespaces se usa la URL reenviada (el helper $BROWSER ejecuta
    `code --openExternal`); en local, 127.0.0.1. webbrowser.open puede devolver
    False aunque el navegador se abra en segundo plano, así que el fallback es
    solo informativo.
    """
    url = url_codespaces(puerto)
    if url:
        class NoSeguirRedirecciones(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(NoSeguirRedirecciones)
        limite = time.monotonic() + 20
        while time.monotonic() < limite:
            try:
                with opener.open(url + "/api/salud", timeout=2) as response:
                    if 200 <= response.status < 400:
                        break
            except urllib.error.HTTPError as exc:
                if 300 <= exc.code < 400:
                    break
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(0.5)
        else:
            log(f"UI local activa en {url_local(puerto)}, pero Codespaces aún no "
                f"reenvía el puerto {puerto}; revisa la pestaña PORTS.")
            return False
    else:
        url = url_local(puerto)
    try:
        import webbrowser
        if webbrowser.open(url, new=2):
            log(f"UI abierta en el navegador: {url}")
            return True
    except Exception:  # navegador ausente, sin permisos, entorno headless...
        pass
    log(f"Abre la UI en el navegador: {url}")
    return False

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
        proceso = subprocess.Popen(
            [sys.executable, "-m", "setup_mc.webui", "--puerto", str(puerto)],
            cwd=str(BASE_DIR), stdin=subprocess.DEVNULL, stdout=mango,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
    except OSError:
        return None
    for _ in range(100):                    # hasta ~15 s a que conteste
        if _salud(puerto):
            return puerto
        if proceso.poll() is not None:
            error(f"La UI terminó al arrancar (código {proceso.returncode}); "
                  f"revisa {LOGS_DIR / 'webui.log'}.")
            return None
        time.sleep(0.15)
    proceso.terminate()
    error(f"La UI no respondió en el puerto {puerto}; revisa {LOGS_DIR / 'webui.log'}.")
    return None

def _refresco_silencioso() -> None:
    """Baja interfaz nueva del panel o de la página, si la hay (sin ruido)."""
    try:
        if config_panel()[0]:
            actualizar_ui()
    except Exception:  # nunca puede tumbar la UI por el panel
        pass
    try:
        pagina.actualizar_ui_pagina()
    except Exception:  # ni por la página
        pass

def servir_foreground(puerto_preferido: int = PUERTO_UI_POR_DEFECTO,
                      abrir: bool = False) -> int:
    """Modo `--ui`: sirve la página en primer plano hasta Ctrl+C."""
    puerto = _puerto_libre(puerto_preferido)
    if puerto is None:
        error(f"No hay ningún puerto libre cerca de {puerto_preferido} para la UI.")
        return 1
    servidor = _crear_servidor(puerto)
    # Si aún no hay interfaz local (primera vez: solo se bajó el script), se
    # trae de la página antes de servir; si no, se refresca en segundo plano.
    if not (BASE_DIR / "webui" / "index.html").is_file():
        log("Interfaz no encontrada: bajándola de la página...")
        _refresco_silencioso()
    else:
        threading.Thread(target=_refresco_silencioso, daemon=True).start()
    print()
    print("=" * 62)
    print("  UI LOCAL (página de control del servidor)")
    print("=" * 62)
    imprimir_urls(puerto)
    print("  Ctrl+C aquí apaga solo la UI; el servidor no se toca.")
    print("=" * 62)
    print()
    if abrir:
        abrir_navegador(puerto)
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
