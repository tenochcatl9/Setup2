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

Antes de subir cambios a la rama principal se comprime el servidor en
respaldo/servidor-mc.tar.zst (mundo + plugins + server.jar + manifiesto con
sha256). Ese archivo SÍ se versiona a propósito: es lo que permite importar
el mundo en otra máquina.

Túnel: Asyx en modo TCP, que además publica un registro SRV para que el
cliente de Minecraft entre solo con el hostname. Si no está dado de alta o no
responde, el script cae automáticamente a Pinggy (solo ssh, sin cuenta).
"""

import argparse
import fnmatch
import hashlib
import itertools
import json
import os
import queue
import re
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

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

# ─── Configuración general ───────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent   # Raíz local del proyecto
SERVER_DIR = BASE_DIR                        # El servidor vive aquí, en la raíz
SERVER_JAR = SERVER_DIR / "server.jar"
PLUGINS_DIR = SERVER_DIR / "plugins"
EULA_FILE = SERVER_DIR / "eula.txt"
WEBHOOK_ENV_FILE = SERVER_DIR / ".env"
GITIGNORE_FILE = SERVER_DIR / ".gitignore"
ENV_EJEMPLO = SERVER_DIR / ".env.example"
LOGS_DIR = SERVER_DIR / "logs"
LOG_SERVIDOR = LOGS_DIR / "servidor.log"
LOG_TUNEL = LOGS_DIR / "tunel.log"
PID_SERVIDOR = SERVER_DIR / "servidor.pid"
PID_TUNEL = SERVER_DIR / "tunel.pid"
FIFO_CONSOLA = SERVER_DIR / "consola"
RESPALDO_DIR = SERVER_DIR / "respaldo"
MANIFIESTO = "manifest.json"

PAPER_API = "https://fill.papermc.io/v3/projects/paper"
USER_AGENT = "CodeSpace-MC-Setup/1.0 (contact: tu@email.com)"

PUERTO_MC = 25565
PUERTO_TUNEL = 80
MEMORIA_INICIAL = "1G"
MEMORIA_MAXIMA = "2G"
TIMEOUT_TUNEL = 60
TIMEOUT_ARRANQUE = 240
INTERVALO_AVISO_TUNEL = 10
MOTIVO_FALLO_TUNEL = ""

INICIO_GITIGNORE = "# >>> INICIO bloque gestionado por setup_mc.py >>>"
FIN_GITIGNORE = "# <<< FIN bloque gestionado por setup_mc.py <<<"

RE_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
RE_VERSION_ESTABLE = re.compile(r"^\d+\.\d+(\.\d+)?$")
# Hosts que salen en la consola de los túneles pero no sirven para entrar al juego.
HOSTS_NO_TUNEL = {"localhost", "github.com", "www.github.com", "twitter.com",
                  "papermc.io", "www.papermc.io", "asyx.ai", "www.asyx.ai",
                  "pinggy.io", "www.pinggy.io"}

# Rutas que nunca deben entrar en un commit
PATRON_SENSIBLE = re.compile(
    r"(^|/)\.env($|\.)"
    r"|(^|/)(id_rsa|id_dsa|id_ecdsa|id_ed25519)$"
    r"|\.pem$|\.key$|\.p12$|\.pfx$"
    r"|(^|/)(credentials|secrets?)(\.|/|$)"
    r"|(^|/)ops\.json$|(^|/)whitelist\.json$|(^|/)server\.properties$"
    r"|\.jar$|(^|/)eula\.txt$",
    re.IGNORECASE,
)
EXENTAS_SENSIBLES = {".env.example", ".env.sample", ".env.template"}

# Secretos reales: nunca deben ir ni a un commit ni dentro de un respaldo.
RE_SECRETO = re.compile(
    r"(^|/)\.env($|\.)"
    r"|(^|/)(id_rsa|id_dsa|id_ecdsa|id_ed25519)$"
    r"|\.pem$|\.key$|\.p12$|\.pfx$"
    r"|(^|/)(credentials|secrets?)(\.|/|$)",
    re.IGNORECASE,
)

# ─── Utilidades de salida ─────────────────────────────────────────────────
_barra_activa = None


def _sin_ansi(texto: str) -> str:
    return RE_ANSI.sub("", texto)


def limpiar_linea():
    if _barra_activa is not None and _barra_activa.tty:
        sys.stdout.write("\r\x1b[K")
        sys.stdout.flush()


def log(msg: str):
    limpiar_linea()
    print(f"[*] {msg}")


def aviso(msg: str):
    limpiar_linea()
    print(f"[~] {msg}")


def ok(msg: str):
    limpiar_linea()
    print(f"[+] {msg}")


def error(msg: str):
    limpiar_linea()
    print(f"[!] ERROR: {msg}", file=sys.stderr)
    sys.stdout.flush()


def titulo(texto: str):
    limpiar_linea()
    print("=" * 62)
    print(f"  {texto}")
    print("=" * 62)


def ruta_sensible(ruta: str) -> bool:
    """True si la ruta parece contener un secreto o un archivo generado del server."""
    base = ruta.replace("\\", "/").rsplit("/", 1)[-1]
    if base in EXENTAS_SENSIBLES:
        return False
    return bool(PATRON_SENSIBLE.search(ruta.replace("\\", "/")))


# ─── Barra de carga ───────────────────────────────────────────────────────
class Barra:
    """Barra de progreso con porcentaje, tiempo y spinner.

    - Si conoce `total` (bytes, pasos) muestra el avance real.
    - Si solo conoce `limite` (segundos) muestra el avance por tiempo.
    - En terminal TTY se redibuja; sin TTY imprime líneas cada 10%.
    """

    ANCHO = 24

    def __init__(self, etiqueta: str, total: int | None = None, limite: float | None = None,
                 unidad: str = "bytes"):
        self.etiqueta = etiqueta
        self.total = total
        self.limite = limite
        self.unidad = unidad
        self.valor = 0
        self.inicio = time.time()
        self.tty = sys.stdout.isatty()
        self._ultimo_pct = -10.0
        self._ultimo_pintado = 0.0
        self._spin = itertools.cycle("|/-\\")
        self._pintar()

    # -- internos --
    def _avance(self) -> tuple:
        if self.total:
            frac = min(1.0, self.valor / self.total)
        elif self.limite:
            frac = min(1.0, (time.time() - self.inicio) / self.limite)
        else:
            frac = 0.0
        return frac * 100, frac

    def _cabecera(self, elapsed: float) -> str:
        if self.limite and not self.total:
            return f"{elapsed:5.1f}s/{self.limite:.0f}s"
        if self.total and self.unidad == "bytes":
            return f"{self.valor / 1048576:,.1f}/{self.total / 1048576:,.1f} MiB"
        return f"{elapsed:5.1f}s"

    @property
    def activa(self) -> bool:
        return _barra_activa is self

    def _pintar(self):
        global _barra_activa
        _barra_activa = self
        pct, frac = self._avance()
        elapsed = time.time() - self.inicio
        if elapsed - self._ultimo_pintado < 0.1:
            return
        self._ultimo_pintado = elapsed
        if not self.tty:
            if pct - self._ultimo_pct >= 10:
                self._ultimo_pct = pct
                print(f"    {self.etiqueta}: {pct:5.1f}% ({self._cabecera(elapsed)})")
            return
        llenos = int(self.ANCHO * frac)
        barra = "#" * llenos + "-" * (self.ANCHO - llenos)
        spin = next(self._spin)
        sys.stdout.write(f"\r\x1b[K[*] {self.etiqueta} [{barra}] {pct:5.1f}% "
                         f"{self._cabecera(elapsed)} {spin}")
        sys.stdout.flush()

    # -- público --
    def avanzar(self, cantidad: int = 1):
        self.valor += cantidad
        self._pintar()

    def refrescar(self):
        self._pintar()

    def finalizar(self, mensaje: str | None = None):
        global _barra_activa
        if not self.activa:
            return
        elapsed = time.time() - self.inicio
        if self.tty:
            sys.stdout.write("\r\x1b[K")
            sys.stdout.flush()
        _barra_activa = None
        if mensaje:
            ok(f"{mensaje} ({elapsed:.1f}s)")


# ─── Requisitos ───────────────────────────────────────────────────────────
def comprobar_java() -> str:
    """Verifica que Java esté disponible y devuelve la versión."""
    java = shutil.which("java")
    if not java:
        raise RuntimeError("No se encontró 'java' en el PATH. Instala Java 21+ y reintenta.")
    try:
        r = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=30)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"No se pudo ejecutar java: {e}")
    salida = (r.stderr or "") + (r.stdout or "")
    m = re.search(r'version "(\d+)', salida)
    if not m:
        raise RuntimeError("No se pudo determinar la versión de Java.")
    version = int(m.group(1))
    if version < 21:
        aviso(f"Java {version} es antiguo; Paper 1.21+ pide Java 21 o superior.")
    return str(version)


# ─── 1. Detectar si ya existe el servidor ─────────────────────────────────
def servidor_existe() -> bool:
    return SERVER_JAR.is_file()


# ─── 2. Descargar Paper (última versión estable) ───────────────────────────
def obtener_versiones_candidatas() -> list:
    """Devuelve las versiones estables (p. ej. 1.21.11) de más nueva a más vieja.

    La API v3 devuelve {"versions": {familia: [versiones...]}} donde la
    primera familia puede ser una generación de Paper (26.x) sin builds
    STABLE. Por eso se aplanan los valores y se filtran los sufijos
    -rc/-pre/-snapshot.
    """
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(PAPER_API, headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()
    familias = data.get("versions")
    if not familias:
        raise RuntimeError("No se encontraron versiones en la API de PaperMC.")
    candidatas = []
    for familia in familias.values():
        for v in familia:
            if isinstance(v, str) and RE_VERSION_ESTABLE.match(v) and v not in candidatas:
                candidatas.append(v)
    if not candidatas:
        raise RuntimeError("No se encontraron versiones estables en la API de PaperMC.")
    return candidatas


def obtener_build_estable(version: str, mostrar: bool = True) -> dict:
    """Devuelve el build estable más reciente para una versión concreta."""
    if mostrar:
        log(f"Buscando build estable para {version}...")
    headers = {"User-Agent": USER_AGENT}
    url = f"{PAPER_API}/versions/{version}/builds"
    r = requests.get(url, headers=headers, timeout=20)
    if r.status_code == 404:
        raise RuntimeError(f"La versión {version} no existe en la API.")
    r.raise_for_status()
    payload = r.json()
    builds = payload.get("builds") if isinstance(payload, dict) else payload
    if not isinstance(builds, list):
        raise RuntimeError(f"Respuesta inesperada de la API para la versión {version}.")
    estables = sorted(
        [b for b in builds if b.get("channel") == "STABLE"],
        key=lambda b: b.get("id", 0),
        reverse=True,
    )
    if not estables:
        raise RuntimeError(f"No hay build estable para la versión {version}.")
    build = estables[0]
    if mostrar:
        log(f"Build estable encontrado: #{build['id']}")
    return build


def descargar_paper():
    """Descarga el JAR de Paper y lo guarda como server.jar en la raíz."""
    candidatas = obtener_versiones_candidatas()
    build = None
    version_elegida = None
    ultimo_error = None
    for version in candidatas:
        try:
            build = obtener_build_estable(version, mostrar=False)
            version_elegida = version
            break
        except RuntimeError as e:
            ultimo_error = e
            log(f"Versión {version} descartada: {e}")
    if build is None:
        raise RuntimeError(
            f"No se encontró ninguna versión con build estable. Último error: {ultimo_error}")
    try:
        descarga = build["downloads"]["server:default"]
        url_descarga = descarga["url"]
    except (KeyError, TypeError):
        raise RuntimeError(f"El build #{build.get('id')} no trae URL de descarga.")
    tamano = int(descarga.get("size") or 0)
    log(f"Descargando Paper {version_elegida} build #{build['id']}")
    log(f"  {url_descarga}")
    headers = {"User-Agent": USER_AGENT}
    with requests.get(url_descarga, headers=headers, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = tamano or int(r.headers.get("Content-Length") or 0)
        barra = Barra("Descargando server.jar", total=total or None)
        temporal = SERVER_JAR.with_suffix(".jar.part")
        with open(temporal, "wb") as f:
            for chunk in r.iter_content(chunk_size=262144):
                if chunk:
                    f.write(chunk)
                    if total:
                        barra.avanzar(len(chunk))
        barra.finalizar("Descarga completada")
    temporal.replace(SERVER_JAR)
    log(f"Paper {version_elegida} build #{build['id']} guardado en {SERVER_JAR}")


# ─── 3. Aceptar EULA ──────────────────────────────────────────────────────
def aceptar_eula():
    if EULA_FILE.is_file() and "eula=true" in EULA_FILE.read_text().lower():
        log("EULA ya aceptada.")
        return
    EULA_FILE.write_text("eula=true\n")
    log(f"EULA aceptada ({EULA_FILE} creado).")


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


def generar_plugins_dir(timeout_arranque: int = TIMEOUT_ARRANQUE):
    """Ejecuta el servidor brevemente para que cree la carpeta plugins y el mundo."""
    log("Arrancando el servidor por primera vez para generar mundo y plugins...")
    cola: queue.Queue = queue.Queue()
    try:
        proc = subprocess.Popen(
            ["java", f"-Xms{MEMORIA_INICIAL}", f"-Xmx{MEMORIA_MAXIMA}",
             "-jar", str(SERVER_JAR), "nogui"],
            cwd=str(SERVER_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        raise RuntimeError("No se encontró 'java' en el PATH. Instala Java 21+ antes de continuar.")
    threading.Thread(target=_bombeo_salida, args=(proc, cola), daemon=True).start()
    barra = Barra("Generando mundo y carpeta plugins", limite=timeout_arranque)
    try:
        listo = False
        while time.time() - barra.inicio < timeout_arranque:
            if proc.poll() is not None:
                raise RuntimeError(
                    "El servidor terminó prematuramente. Ejecuta "
                    f"'java -jar {SERVER_JAR.name} nogui' a mano para ver el error.")
            for texto in _drenar(cola):
                if 'Done (' in texto or "For help, type" in texto:
                    listo = True
            if listo and PLUGINS_DIR.is_dir():
                break
            barra.refrescar()
            time.sleep(0.2)
        if listo:
            barra.finalizar("Mundo y plugins generados")
        else:
            barra.finalizar("Aviso: no se confirmó el arranque, se continúa igualmente")
        # El puente del FIFO hace que 'echo "save-all flush" > consola' y el
        # respaldo automático hablen con el servidor también en primer plano.
        _lanzar_puente_consola(proc)
        if not args_sin_backup_auto():
            if arrancar_backups_auto():
                log(f"Respaldo automático cada {BACKUP_AUTO_MINUTOS} min en "
                    f"respaldo/auto/ (se guardan los {BACKUP_AUTO_MANTENER} últimos).")
    finally:
        _detener_proceso(proc, escribir_stop=True)
        log("Servidor detenido.")


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


# ─── 5. Descargar plugins (ViaVersion + ViaBackwards + AuthMe + SkinsRestorer) ──
# repo de GitHub, nombre local, y qué texto del asset identify (evita coger
# la build de Bungee/Velocity/Folia en repos que publican varias).
PLUGIN_AUTHME = "AuthMe.jar"
PLUGIN_VIAVERSION = "ViaVersion.jar"
PLUGIN_VIABACKWARDS = "ViaBackwards.jar"
PLUGIN_SKINSRESTORER = "SkinsRestorer.jar"
# SkinsRestorer se publica en Modrinth (no en el release de GitHub del proyecto).
PROYECTO_SKINSRESTORER = "skinsrestorer"
# Se pide en este orden: la primera coincidencia gana.
CARGA_SKINSRESTORER = ["paper", "purpur", "spigot", "bukkit"]
# Última versión instalada de cada plugin que se reza actualiza; comparte
# carpeta con ESTADO_RESPALDO (~/.local/state/setup_mc/).
ESTADO_PLUGINS = Path.home() / ".local" / "state" / "setup_mc" / "plugins.json"
# El minimo depende del AuthMe: las builds modernas de AuthMe son 1.10+.
VER_MINIMA_MC = "1.10"


def _elegir_asset(release: dict, repo: str, preferido: str = "") -> dict:
    """Escoge el .jar correcto: por sufijo preferido o el primero válido."""
    assets = release.get("assets", [])
    candidatos = []
    for a in assets:
        nombre = a.get("name", "")
        if not nombre.endswith(".jar"):
            continue
        if "sources" in nombre or "javadoc" in nombre:
            continue
        candidatos.append(a)
    if not candidatos:
        raise RuntimeError(f"No se encontró un .jar en el último release de {repo}.")
    for sufijo in [s for s in preferido.split("|") if s]:
        for a in candidatos:
            if a["name"].lower().endswith(sufijo.lower()):
                return a
    return candidatos[0]


def descargar_plugin_github(repo: str, nombre_archivo: str, forzar: bool = False,
                            preferir: str = "") -> None:
    """Descarga el último release de un repositorio de GitHub."""
    destino = PLUGINS_DIR / nombre_archivo
    if destino.is_file() and not forzar:
        log(f"{nombre_archivo} ya instalado ({destino.stat().st_size:,} bytes).")
        return
    log(f"Buscando última versión de {repo}...")
    api_url = f"https://api.github.com/repos/{repo}/releases/latest"
    r = requests.get(api_url, headers={"Accept": "application/vnd.github+json"}, timeout=20)
    if r.status_code == 404:
        raise RuntimeError(f"No se encontró ningún release en {repo}.")
    r.raise_for_status()
    release = r.json()
    jar_asset = _elegir_asset(release, repo, preferir)
    log(f"  release {release.get('tag_name')}: {jar_asset['name']}")
    barra = Barra(f"Descargando {nombre_archivo}", total=int(jar_asset.get("size") or 0) or None)
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    temporal = destino.with_suffix(".jar.part")
    with requests.get(jar_asset["browser_download_url"], stream=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(temporal, "wb") as f:
            for chunk in resp.iter_content(chunk_size=262144):
                if chunk:
                    f.write(chunk)
                    if barra.total:
                        barra.avanzar(len(chunk))
    temporal.replace(destino)
    barra.finalizar(f"{nombre_archivo} instalado en {destino}")


def _sha1_archivo(ruta: Path) -> str:
    h = hashlib.sha1()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def _leer_estado_plugins() -> dict:
    try:
        return json.loads(ESTADO_PLUGINS.read_text())
    except (OSError, ValueError):
        return {}


def _guardar_estado_plugins(nombre: str, datos: dict) -> None:
    estado = _leer_estado_plugins()
    estado[nombre] = datos
    try:
        ESTADO_PLUGINS.parent.mkdir(parents=True, exist_ok=True)
        ESTADO_PLUGINS.write_text(json.dumps(estado, indent=2))
    except OSError:
        pass


def version_skinsrestorer() -> str:
    return _leer_estado_plugins().get(PROYECTO_SKINSRESTORER, {}).get("version", "?")


def actualizar_skinsrestorer() -> str:
    """Deja siempre la última versión de SkinsRestorer (se comprueba en cada
    ejecución, a diferencia de los plugins de GitHub que solo se instalan una vez).

    Se compara el sha1 del jar local con el que publica Modrinth: así se
    detectan versiones nuevas sin volver a bajar 8 MB cuando nada ha cambiado.
    """
    destino = PLUGINS_DIR / PLUGIN_SKINSRESTORER
    cabeceras = {"User-Agent": "setup-mc (respaldo servidor minecraft)"}
    url = f"https://api.modrinth.com/v2/project/{PROYECTO_SKINSRESTORER}/version"
    r = requests.get(url, params={"loaders": json.dumps(CARGA_SKINSRESTORER)},
                     headers=cabeceras, timeout=25)
    r.raise_for_status()
    versiones = [v for v in r.json() if v.get("files")]
    if not versiones:
        raise RuntimeError("Modrinth no devolvió ninguna versión de SkinsRestorer.")
    version = versiones[0]
    archivo = version["files"][0]
    sha1 = archivo["hashes"]["sha1"]
    if destino.is_file() and _sha1_archivo(destino) == sha1:
        log(f"SkinsRestorer ya está al día: v{version['version_number']}.")
        return version["version_number"]
    barra = Barra(f"Descargando SkinsRestorer {version['version_number']}",
                  total=int(archivo.get("size") or 0) or None)
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    temporal = destino.with_suffix(".jar.part")
    with requests.get(archivo["url"], stream=True, headers=cabeceras, timeout=180) as resp:
        resp.raise_for_status()
        with open(temporal, "wb") as f:
            for bloque in resp.iter_content(chunk_size=262144):
                if bloque:
                    f.write(bloque)
                    if barra.total:
                        barra.avanzar(len(bloque))
    if _sha1_archivo(temporal) != sha1:
        temporal.unlink(missing_ok=True)
        raise RuntimeError("El jar de SkinsRestorer no coincide con su sha1 en Modrinth.")
    temporal.replace(destino)
    barra.finalizar(f"SkinsRestorer v{version['version_number']} en {destino}")
    _guardar_estado_plugins(PROYECTO_SKINSRESTORER, {
        "version": version["version_number"],
        "sha1": sha1,
        "actualizado": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    return version["version_number"]


CONFIG_AUTHME = PLUGINS_DIR / "AuthMe" / "config.yml"


# Ajustes de AuthMe que el script impone. Con túnel, el servidor ve una única
# IP pública para todos los jugadores, así que cualquier límite por IP los
# confunde entre sí: se rechazan registros o se les acusa de multicuenta.
AJUSTES_AUTHME = {
    # Sin esto solo se puede registrar UNA cuenta desde la IP del túnel: el
    # segundo amigo que entra es rechazado directamente.
    "maxRegPerIp": "0",
    # Sin esto, al entrar se les muestra "Eres propietario de N cuentas",
    # juntando las cuentas de todos los que entran por el túnel.
    "displayOtherAccounts": "false",
    # Se dejan explícitos aunque ya vengan a 0, por si el plugin los cambia.
    "maxJoinPerIp": "0",
    "maxLoginPerIp": "0",
}


def _ajustar_yaml(clave: str, valor: str) -> bool:
    """Pone una clave YAML al valor pedido (edición por líneas, sin perder
    los comentarios). Devuelve True si cambió algo."""
    lineas = CONFIG_AUTHME.read_text().splitlines(keepends=True)
    patron = re.compile(rf"^(\s*){re.escape(clave)}:\s*(\S.*)$")
    for i, linea in enumerate(lineas):
        m = patron.match(linea.rstrip("\n"))
        if m and m.group(2).strip() != valor:
            lineas[i] = f"{m.group(1)}{clave}: {valor}\n"
            CONFIG_AUTHME.write_text("".join(lineas))
            return True
    return False


def configurar_authme() -> bool:
    """Desactiva la base GeoIP de AuthMe (hace falta clave MaxMind de pago).

    Sin esto, AuthMe intenta descargarla en cada arranque y llena el log de
    avisos. Además relaja las restricciones por IP, que con un túnel de
    acceso son falsas: todos los jugadores llegan desde la misma IP.
    Se edita por líneas para no perder los comentarios del YAML.
    """
    if not CONFIG_AUTHME.is_file():
        return False
    lineas = CONFIG_AUTHME.read_text().splitlines(keepends=True)
    dentro = False
    cambiado = False
    for i, linea in enumerate(lineas):
        if re.match(r"^\s*geoIpDatabase:\s*$", linea):
            dentro = True
            continue
        if dentro:
            m = re.match(r"^(\s*)enabled:\s*(true|false)\s*$", linea, re.IGNORECASE)
            if m:
                if m.group(2).lower() == "true":
                    lineas[i] = f"{m.group(1)}enabled: false\n"
                    cambiado = True
                dentro = False
            elif linea.strip() and not linea.startswith((" ", "\t")):
                dentro = False
    if cambiado:
        CONFIG_AUTHME.write_text("".join(lineas))
        ok("AuthMe: base GeoIP desactivada (hacía falta una clave MaxMind de pago).")
    for clave, valor in AJUSTES_AUTHME.items():
        if _ajustar_yaml(clave, valor):
            log(f"AuthMe: {clave} = {valor} (con túnel todos parecen la misma IP).")
            cambiado = True
    return cambiado


def instalar_plugins(forzar: bool = False) -> None:
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    descargar_plugin_github("ViaVersion/ViaVersion", PLUGIN_VIAVERSION, forzar)
    descargar_plugin_github("ViaVersion/ViaBackwards", PLUGIN_VIABACKWARDS, forzar)
    # AuthMe: login/registro. Paper build (no Bungee/Velocity/Folia/Spigot).
    descargar_plugin_github("AuthMe/AuthMeReloaded", PLUGIN_AUTHME, forzar,
                            preferir="-Paper.jar|AuthMe.jar")
    configurar_authme()
    # SkinsRestorer sí se revisa en cada ejecución (cambia a menudo y sin
    # avisos de versión nueva). Si Modrinth falla, el servidor arranca igual.
    try:
        version_skin = actualizar_skinsrestorer()
    except (requests.RequestException, RuntimeError, KeyError) as exc:
        version_skin = "?"
        aviso(f"No se pudo comprobar SkinsRestorer en Modrinth: {exc}")
    ok(f"Plugins listos: {rango_versiones()} vía ViaVersion/ViaBackwards, "
       f"login con AuthMe y SkinsRestorer {version_skin}.")


# ─── 6. .gitignore y protección de secretos ───────────────────────────────
def _contenido_gitignore() -> str:
    return "\n".join([
        INICIO_GITIGNORE,
        "# Secretos: nunca deben llegar a un commit",
        ".env",
        ".env.*",
        "!.env.example",
        "id_rsa*",
        "id_dsa*",
        "id_ecdsa*",
        "id_ed25519*",
        "*.pem",
        "*.key",
        "*.p12",
        "*.pfx",
        "",
        "# Servidor de Minecraft (vive en la raíz del proyecto)",
        "*.jar",
        "server.jar",
        "eula.txt",
        "server.properties",
        "bukkit.yml",
        "spigot.yml",
        "commands.yml",
        "paper-*.yml",
        "config/",
        ".paper/",
        "ops.json",
        "whitelist.json",
        "banned-players.json",
        "banned-ips.json",
        "banned-cache.json",
        "usercache.json",
        "world/",
        "world_nether/",
        "world_the_end/",
        "logs/",
        "plugins/",
        "libraries/",
        "cache/",
        "versions/",
        "crash-reports/",
        "generated/",
        "*.log",
        "*.pid",
        "*.lock",
        "*.part",
        "consola",
        "",
        "# El respaldo comprimido del servidor SÍ se versiona a propósito",
        "# (es la forma de importar el mundo en otro equipo/máquina)",
        "!respaldo/",
        "!respaldo/**",
        # Los respaldos automáticos (cada 20 min) son solo locales: unos 45 MB
        # cada 20 min meterían ~3 GB al día en el historial. El que se
        # versiona es respaldo/servidor-mc.tar.zst, que se regenera al parar.
        "respaldo/auto/",
        "",
        "# Python",
        "__pycache__/",
        "*.py[cod]",
        ".venv/",
        "venv/",
        "env/",
        "",
        "# Editores y sistema operativo",
        ".vscode/",
        ".idea/",
        ".DS_Store",
        FIN_GITIGNORE,
    ])


def configurar_gitignore():
    """Escribe (o repara) el bloque gestionado de .gitignore sin duplicarlo."""
    bloque = _contenido_gitignore() + "\n"
    if GITIGNORE_FILE.exists():
        texto = GITIGNORE_FILE.read_text()
        if INICIO_GITIGNORE in texto and FIN_GITIGNORE in texto:
            ini = texto.index(INICIO_GITIGNORE)
            fin = texto.index(FIN_GITIGNORE) + len(FIN_GITIGNORE)
            nuevo = texto[:ini] + bloque.rstrip("\n") + texto[fin:]
            if nuevo != texto:
                GITIGNORE_FILE.write_text(nuevo)
                log(".gitignore actualizado (bloque gestionado).")
            else:
                log(".gitignore ya estaba actualizado.")
        else:
            separador = "" if texto.endswith("\n") or not texto else "\n"
            GITIGNORE_FILE.write_text(texto + separador + "\n" + bloque)
            log(".gitignore ampliado con el bloque gestionado.")
    else:
        GITIGNORE_FILE.write_text(bloque)
        log(f".gitignore creado en {GITIGNORE_FILE}")
    if not ENV_EJEMPLO.exists():
        ENV_EJEMPLO.write_text(
            "# Copia este archivo a .env y rellena el webhook.\n"
            "# .env está en .gitignore: nunca se sube.\n"
            "DISCORD_WEBHOOK=\n"
        )
        log(f"{ENV_EJEMPLO.name} creado como plantilla.")


def _git(args: list, capture: bool = True):
    return subprocess.run(
        ["git", *args], cwd=str(BASE_DIR), capture_output=capture,
        text=True, check=False,
    )


def es_repo_git() -> bool:
    return _git(["rev-parse", "--is-inside-work-tree"]).stdout.strip() == "true"


def verificar_env_protegido() -> tuple:
    """Comprueba que .env esté ignorado y que ningún secreto esté versionado.

    Devuelve (ok, motivo). Si algo falla, el script debe cancelarse.
    """
    if not es_repo_git():
        return True, ""  # Sin repo no hay commits que arruinar
    raiz = _git(["rev-parse", "--show-toplevel"]).stdout.strip()
    if not raiz or Path(raiz).resolve() != BASE_DIR:
        return True, ""  # El .gitignore está fuera del repo: no aplica
    if _git(["check-ignore", "-q", WEBHOOK_ENV_FILE.name]).returncode != 0:
        return False, (
            f"{WEBHOOK_ENV_FILE.name} NO está oculto en .gitignore. Un commit podría publicar "
            f"el webhook de Discord y eso no se puede deshacer.\n"
            f"    Solución: revisa el bloque gestionado por setup_mc.py en {GITIGNORE_FILE} "
            f"y añade una línea '.env'."
        )
    r = _git(["ls-files", "--error-unmatch", WEBHOOK_ENV_FILE.name])
    if r.returncode == 0:
        return False, (
            f"{WEBHOOK_ENV_FILE.name} está versionado en Git. SCRIPT CANCELADO para no "
            f"filtrar el webhook.\n"
            f"    Solución: ejecuta  git rm --cached {WEBHOOK_ENV_FILE.name}  y vuelve a lanzar."
        )
    r = _git(["ls-files"])
    trackeados = [p for p in r.stdout.splitlines() if p.strip()]
    sospechosos = [p for p in trackeados if ruta_sensible(p)]
    if sospechosos:
        return False, (
            "Hay archivos sensibles ya versionados. SCRIPT CANCELADO:\n    - " +
            "\n    - ".join(sospechosos) +
            "\n    Solución: bórralos del índice con  git rm --cached <archivo>  y añade "
            "sus reglas a .gitignore."
        )
    return True, ""


def exigir_env_protegido():
    """Verifica la protección y cancela el script (SystemExit) si falla."""
    if not es_repo_git():
        aviso("No es un repositorio Git: se omite la verificación de .env y el push final.")
        return
    bien, motivo = verificar_env_protegido()
    if not bien:
        error(motivo)
        print(file=sys.stderr)
        print("[!] SCRIPT CANCELADO por seguridad. No se ha subido nada.", file=sys.stderr)
        sys.exit(1)
    ok(f".env está oculto y no hay secretos versionados ({WEBHOOK_ENV_FILE.name} a salvo).")


# ─── 7. Webhook de Discord ────────────────────────────────────────────────
def _webhook_valido(url: str) -> bool:
    return re.match(r"^https://(discord(?:app)?\.com)/api/webhooks/\d+/[\w-]+$", url) is not None


def leer_webhook() -> str:
    if not WEBHOOK_ENV_FILE.exists():
        return ""
    for line in WEBHOOK_ENV_FILE.read_text().splitlines():
        if line.strip().startswith("DISCORD_WEBHOOK="):
            return line.split("=", 1)[1].strip()
    return ""


def guardar_webhook(url: str):
    lineas = []
    if WEBHOOK_ENV_FILE.exists():
        lineas = [l for l in WEBHOOK_ENV_FILE.read_text().splitlines()
                  if not l.strip().startswith("DISCORD_WEBHOOK=")]
    lineas.append(f"DISCORD_WEBHOOK={url}")
    WEBHOOK_ENV_FILE.write_text("\n".join(lineas) + "\n")
    try:
        WEBHOOK_ENV_FILE.chmod(0o600)
    except OSError:
        pass
    log(f"Webhook guardado en {WEBHOOK_ENV_FILE} (permisos 600, fuera de Git).")


def verificar_webhook(url: str) -> bool:
    """Comprueba que el webhook existe (GET: no envía ningún mensaje)."""
    if not url:
        return False
    if not _webhook_valido(url):
        error("La URL del webhook no tiene el formato de Discord "
              "(https://discord.com/api/webhooks/ID/TOKEN).")
        return False
    try:
        r = requests.get(url, timeout=15)
    except Exception as e:  # noqa: BLE001
        error(f"No se pudo contactar con Discord: {e}")
        return False
    if r.status_code == 200:
        try:
            ok(f"Webhook verificado ({r.json().get('name', 'sin nombre')}).")
        except ValueError:
            ok("Webhook verificado.")
        return True
    if r.status_code in (401, 403):
        error("El webhook fue borrado o su token ya no es válido "
              f"(HTTP {r.status_code}). Crea uno nuevo en el canal de Discord.")
    elif r.status_code == 404:
        error("Ese webhook no existe (HTTP 404). Revisa la URL en .env.")
    elif r.status_code == 429:
        error("Discord está limitando las peticiones (HTTP 429). Espera unos segundos.")
    else:
        error(f"Discord respondió HTTP {r.status_code}: {r.text[:200]}")
    return False


def obtener_webhook() -> str:
    url = leer_webhook()
    if url:
        log(f"Webhook de Discord cargado desde {WEBHOOK_ENV_FILE.name}")
        verificar_webhook(url)
        return url
    print()
    print("=" * 62)
    print("No hay webhook de Discord configurado.")
    print("Créalo en: Configuración del canal → Integraciones → Webhooks → Nueva webhook")
    print("=" * 62)
    try:
        url = input("Pega aquí la URL del webhook (Enter para omitir): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        aviso("Sin webhook: se continuará sin notificar a Discord.")
        return ""
    if not url:
        aviso("Sin webhook: se continuará sin notificar a Discord.")
        return ""
    if not verificar_webhook(url):
        aviso("El webhook no responde; se guarda igualmente y se intentará avisar al final.")
    guardar_webhook(url)
    return url


def enviar_a_discord(webhook_url: str, mensaje: str) -> bool:
    """POST con un reintento en 429/5xx. Devuelve True si se envió."""
    if not webhook_url:
        return False
    for intento in (1, 2):
        try:
            r = requests.post(webhook_url, json={"content": mensaje}, timeout=20)
        except Exception as e:  # noqa: BLE001
            error(f"No se pudo enviar el mensaje a Discord: {e}")
            return False
        if r.status_code in (200, 204):
            ok("Mensaje enviado a Discord.")
            return True
        if r.status_code == 429 and intento == 1:
            espera = 2
            try:
                espera = float(json.loads(r.text or "{}").get("retry_after", 2))
            except (ValueError, TypeError):
                pass
            aviso(f"Discord pide esperar {espera:.0f}s (límite de peticiones); reintento...")
            time.sleep(min(espera, 10))
            continue
        if r.status_code >= 500 and intento == 1:
            aviso(f"Discord devolvió HTTP {r.status_code}; reintento...")
            time.sleep(2)
            continue
        error(f"Discord rechazó el mensaje: HTTP {r.status_code} — {r.text[:300]}")
        if r.status_code in (401, 403, 404):
            aviso("El webhook está muerto o revocado: crea uno nuevo y ponlo en .env.")
        return False
    error("No se pudo enviar el mensaje a Discord tras varios intentos.")
    return False


def _version_max_operativa() -> str:
    """Versión de Minecraft que da el server.jar, leída sin arrancarlo."""
    v = _version_mc_del_jar()
    return v if v and v != "desconocida" else "la más reciente"


def rango_versiones() -> str:
    """Rango admitida: la mínima la impone AuthMe, la máxima la del server.jar."""
    return f"{VER_MINIMA_MC} a {_version_max_operativa()}"


def bloque_ip(ip: str, titulo: str = "IP para Minecraft") -> str:
    """IP dentro de un bloque de código de Discord: sale con recuadro y botón de copiar."""
    return f"**{titulo}** *(copia y pega)*\n```\n{ip}\n```\n"


def _mensaje_activado(direccion: str, puerto: int, motivo_fallo: str = "") -> str:
    if direccion:
        return ("🎮 **Servidor de Minecraft activo**\n\n"
                + bloque_ip(direccion)
                + "\n**Versiones:** " + rango_versiones() + " *(ViaVersion y "
                  "ViaBackwards mantienen el rango al día solos)*\n"
                  "**Login:** AuthMe — en la primera entrada registras contraseña; "
                  "después, con `/login`\n"
                  "**Conexión:** Multijugador → Directo → pega la IP de arriba")
    return ("⚠️ **Servidor arrancado, pero SIN IP pública**\n\n"
            f"El túnel no se pudo abrir: {motivo_fallo or 'motivo desconocido'}\n"
            f"El servidor sí escucha en el puerto local `{puerto}`, pero hace falta "
            "salida a internet por el puerto 22 para darle IP pública.")


def notificar_servidor(webhook: str, direccion: str, puerto: int, motivo_fallo: str = "") -> bool:
    """Avisa a Discord siempre: con la IP si la hay, o con el fallo si no."""
    if not webhook:
        return False
    return enviar_a_discord(webhook, _mensaje_activado(direccion, puerto, motivo_fallo))


def notificar_apagado(webhook: str, direccion: str = "", motivo: str = "") -> bool:
    """Avisa de que el servidor (y el túnel) se han apagado."""
    if not webhook:
        return False
    lineas = ["🔴 **Servidor de Minecraft apagado**"]
    if direccion:
        lineas.append("Esta IP ya no da servicio:")
        lineas.append("```\n" + direccion + "\n```")
    if motivo:
        lineas.append(f"**Motivo:** {motivo}")
    return enviar_a_discord(webhook, "\n".join(lineas))


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
    threading.Thread(target=_bombeo_salida, args=(proc, cola), daemon=True).start()
    PID_SERVIDOR.write_text(f"{proc.pid}\n")
    # El puente del FIFO y el hilo de respaldos automáticos hacen falta aquí
    # también: este es el camino normal (servidor ya existente), no solo el de
    # la instalación inicial. Sin el puente, el respaldo automático no puede
    # congelar el mundo con save-off.
    _lanzar_puente_consola(proc)
    if not args_sin_backup_auto():
        if arrancar_backups_auto(puerto=puerto):
            log(f"Respaldo automático cada {BACKUP_AUTO_MINUTOS} min en "
                f"respaldo/auto/ (se guardan los {BACKUP_AUTO_MANTENER} últimos).")
    return proc, cola


def puerto_abierto(puerto: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, puerto)) == 0


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


BARRA_DISCORD = "▓▓▓▓▓▓▓░░░"


def _barra_texto(frac: float) -> str:
    llenos = int(len(BARRA_DISCORD) * frac)
    return BARRA_DISCORD[:llenos] + BARRA_DISCORD[llenos:]

class NotificadorTunel:
    """Un único mensaje en Discord: 'Iniciando Túnel' + barra, editado cada 10s.

    Se edita el mismo mensaje (PATCH) en vez de enviar uno nuevo cada vez, así el
    canal no se llena y solo hay una petición cada 10 segundos.
    """

    def __init__(self, webhook: str, intervalo: int = INTERVALO_AVISO_TUNEL):
        self.webhook = webhook
        self.intervalo = intervalo
        self.id_mensaje = None
        self.ultimo_envio = 0.0

    def _url_mensaje(self) -> str:
        return f"{self.webhook}/messages/{self.id_mensaje}"

    def _enviar(self, contenido: str) -> bool:
        if not self.webhook:
            return False
        try:
            if self.id_mensaje:
                r = requests.patch(self._url_mensaje(), json={"content": contenido},
                                   timeout=20)
                if r.status_code == 404:
                    # El mensaje ya no existe (cancho borrado): se crea otro.
                    self.id_mensaje = None
                    r = requests.post(self.webhook, json={"content": contenido}, timeout=20)
            else:
                r = requests.post(self.webhook, json={"content": contenido}, timeout=20)
        except Exception as e:  # noqa: BLE001
            error(f"No se pudo avisar a Discord del túnel: {e}")
            return False
        if r.status_code in (200, 201, 204):
            try:
                datos = r.json()
                if datos.get("id"):
                    self.id_mensaje = datos["id"]
            except ValueError:
                pass
            return True
        if r.status_code == 429:
            aviso("Discord limita el aviso del túnel (429); se reintentará en la "
                  "próxima actualización.")
            return False
        error(f"Discord rechazó el aviso del túnel: HTTP {r.status_code}")
        return False

    def iniciar(self, barra: Barra) -> None:
        if not self.webhook:
            return
        self.ultimo_envio = time.time()
        self._enviar("⏳ **Iniciando Túnel**\nEspere un momento mientras se abre "
                     "el acceso público al servidor.")

    def pulso(self, barra: Barra) -> None:
        """Actualiza la barra como mucho cada `intervalo` segundos."""
        if not self.webhook or self.id_mensaje is None:
            return
        ahora = time.time()
        if ahora - self.ultimo_envio < self.intervalo:
            return
        self.ultimo_envio = ahora
        pct, frac = barra._avance()
        transcurrido = ahora - barra.inicio
        restante = max(0, (barra.limite or 0) - transcurrido)
        self._enviar(f"⏳ **Iniciando Túnel**\n`{_barra_texto(frac)}` {pct:.0f}% · "
                     f"{transcurrido:.0f}s · ~{restante:.0f}s restantes")

    def finalizar(self, ip: str = "", motivo: str = "") -> None:
        if not self.webhook or self.id_mensaje is None:
            return
        if ip:
            self._enviar("✅ **Túnel listo**\n\n" + bloque_ip(ip, "IP pública"))
        else:
            self._enviar(f"❌ **No se pudo abrir el túnel**\n{motivo}")

# ─── 9. Túneles: Asyx (principal) y Pinggy (respaldo) ─────────────────────
# Asyx: nativo de Minecraft, subdominio permanente y registro SRV (el jugador
# escribe solo el hostname). Necesita alta con email, una vez.
# Pinggy: solo ssh, sin cuenta, pero con tope de 60 min y URL cambiante.
ASYX_REGISTRO = "https://www.asyx.ai/register"
ASYX_CONSOLE = "https://www.asyx.ai/console/tunnel"
ASYX_STS = "https://www.asyx.ai/www/sts"
ASYNX_DOMINIO = ".tunnel.asyx.ai"
PINGGY_SERVIDOR = "free.pinggy.io"
PINGGY_PUERTO = 443
PINGGY_AVISO = ("Pinggy gratis corta el túnel a los 60 minutos y cambia la "
                "dirección al reconectar: la IP de Discord quedará obsoleta.")

RE_PINGGY_TCP = re.compile(r"tcp://([A-Za-z0-9][A-Za-z0-9.-]*):(\d{2,5})", re.IGNORECASE)
RE_ASYX_HOST = re.compile(r"\b([A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*"
                          r"\.tunnel\.asyx\.ai)\b", re.IGNORECASE)
RE_HOST_PUERTO = re.compile(r"\b([A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}):(\d{2,5})\b")


# ── Utilidades comunes ─────────────────────────────────────────────────────
def _dividir_host_puerto(valor: str, puerto_defecto=None) -> str:
    """Normaliza 'host', 'host:puerto' o 'tcp://host:puerto' a 'host:puerto'."""
    valor = RE_ANSI.sub("", valor or "").strip()
    valor = re.sub(r"^[a-z]+://", "", valor, flags=re.IGNORECASE)
    m = RE_HOST_PUERTO.search(valor)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    if valor and puerto_defecto:
        return f"{valor}:{puerto_defecto}"
    return valor


def _servidor_alcanzable(host: str, puerto: int, timeout: int = 6) -> bool:
    try:
        with socket.create_connection((host, puerto), timeout=timeout):
            return True
    except OSError:
        return False


def _comando_existe(nombre: str) -> bool:
    return shutil.which(nombre) is not None


def _preguntar(texto: str) -> str:
    """Pregunta al usuario. Devuelve la opción en minúscula ('' si no hay TTY)."""
    if not sys.stdin.isatty():
        return ""
    try:
        return input(texto).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


# ── Proveedor: Asyx ────────────────────────────────────────────────────────
def asyx_estado_alta() -> str:
    """Estado del alta de Asyx: 'completa', 'incompleta' o 'sin_alta'.

    No basta con que ~/.asyx exista: una alta a medias deja device.json y el
    CSR, pero Asyx exige el manifest.json que entrega al completar el registro.
    """
    base = Path.home() / ".asyx"
    if not base.is_dir():
        return "sin_alta"
    manifest = list((base / "certs").glob("*/manifest.json")) if (base / "certs").is_dir() else []
    if manifest:
        return "completa"
    if any(base.iterdir()):
        return "incompleta"
    return "sin_alta"


def asyx_esta_alto() -> bool:
    """True solo si el alta está realmente completa."""
    return asyx_estado_alta() == "completa"


def asyx_registrar(minutos: int = 15) -> bool:
    """Da de alta el dispositivo en Asyx mostrando la URL de registro.

    El CLI oficial (npx asyx setup) abre el navegador con la URL del registro
    pero no la imprime, así que en un Codespace headless no hay forma de
    completarlo. Aquí hacemos lo mismo a mano, enseñando la URL y esperando
    bastante más tiempo: el registro se hace en el navegador del usuario, no
    en esta máquina.
    """
    if not _comando_existe("openssl"):
        error("Asyx necesita 'openssl' para generar la clave del dispositivo.")
        return False
    base = Path.home() / ".asyx"
    device_json = base / "device.json"
    client_id = ""
    if device_json.is_file():
        try:
            client_id = json.loads(device_json.read_text()).get("clientId", "")
        except (ValueError, OSError):
            client_id = ""
    if not re.fullmatch(r"cli_[A-F0-9]{12}", client_id or ""):
        client_id = "cli_" + os.urandom(6).hex().upper()
    base.mkdir(parents=True, exist_ok=True)
    device_json.write_text(json.dumps({"clientId": client_id}, indent=2))
    certs = base / "certs" / client_id
    certs.mkdir(parents=True, exist_ok=True)
    try:
        certs.chmod(0o700)
    except OSError:
        pass
    clave = certs / "private.pem.key"
    csr = certs / "device.csr.pem"

    if not clave.is_file():
        r = subprocess.run(["openssl", "genrsa", "-traditional", "-out", str(clave), "2048"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not clave.is_file():
            error(f"No se pudo generar la clave: {(r.stderr or '').strip()[:200]}")
            return False
    clave.chmod(0o600)
    r = subprocess.run(["openssl", "req", "-new", "-key", str(clave),
                        "-subj", f"/CN={client_id}/O=Asyx", "-sha256", "-out", str(csr)],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not csr.is_file():
        error(f"No se pudo generar el CSR: {(r.stderr or '').strip()[:200]}")
        return False

    ticket = os.urandom(16).hex()
    url = f"{ASYX_CONSOLE}?clientId={client_id}&ticket={ticket}"
    titulo("ALTA DE ASYX: REGÍSTRATE EN TU NAVEGADOR")
    print("  1) Abre este enlace en el navegador de tu ORDENADOR")
    print("     (no en el Codespace: aquí no hay navegador):")
    print()
    print(f"       {url}")
    print()
    print("  2) Inicia sesión o crea la cuenta (es gratis, sin tarjeta) y")
    print("     completa el registro.")
    print(f"  3) Vuelve aquí: esperaré hasta {minutos} minutos a que Asyx")
    print("     emita el certificado.")
    print()
    try:
        abrir_navegador(url)
    except Exception:  # noqa: BLE001
        pass

    limite = time.time() + minutos * 60
    barra = Barra("Esperando el certificado de Asyx", limite=minutos * 60)
    intentos = 0
    try:
        while time.time() < limite:
            intentos += 1
            barra.refrescar()
            try:
                r = requests.post(ASYX_STS, json={"ticket": ticket, "clientId": client_id,
                                                 "csrPem": csr.read_text()},
                                  headers={"content-type": "application/json",
                                           "accept": "application/json"},
                                  timeout=25)
            except requests.RequestException:
                time.sleep(2)
                continue
            if r.status_code in (200, 201):
                if "json" not in (r.headers.get("content-type") or ""):
                    # Sin sesión iniciada Asyx devuelve su web, no el certificado.
                    if intentos < 3:
                        aviso("Aún no veo tu sesión: completa el registro en el enlace.")
                    time.sleep(3)
                    continue
                try:
                    emitido = r.json()
                except ValueError:
                    time.sleep(3)
                    continue
                (certs / "AmazonRootCA1.pem").write_text(emitido["caPem"])
                (certs / "device.pem.crt").write_text(emitido["certPem"])
                manifest = {"clientId": client_id, "endpoint": emitido.get("endpoint", ""),
                            "region": emitido.get("region", ""),
                            "issuedAt": int(time.time() * 1000)}
                manifest_path = certs / "manifest.json"
                manifest_path.write_text(json.dumps(manifest, indent=2))
                for ruta, modo in ((certs / "AmazonRootCA1.pem", 0o644),
                                   (certs / "device.pem.crt", 0o600),
                                   (manifest_path, 0o600)):
                    try:
                        ruta.chmod(modo)
                    except OSError:
                        pass
                barra.finalizar("Certificado recibido")
                ok(f"Asyx dado de alta. Tu subdominio se reservó con {client_id}.")
                return True
            if r.status_code in (401, 403) and intentos < 3:
                aviso("Aún no veo tu sesión: completa el registro en el enlace.")
            if r.status_code in (301, 302, 307, 308):
                time.sleep(3)
                continue
            if r.status_code == 409:
                barra.finalizar()
                error("El registro no corresponde a esta máquina (HTTP 409).")
                log(f"    Vuelve a intentarlo; si persiste, borra {base} y rehaz el alta.")
                return False
            time.sleep(3)
        barra.finalizar()
    finally:
        if _barra_activa is barra:
            barra.finalizar()
    error(f"Asyx no emitió el certificado en {minutos} minutos.")
    log("    Reintenta cuando termines el registro:  npx -y asyx@latest setup")
    return False


def abrir_navegador(url: str) -> bool:
    """Intenta abrir la URL; enCodespace no hay navegador y no es un error."""
    for cmd in (["xdg-open", url], ["gio", "open", url]):
        if _comando_existe(cmd[0]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except OSError:
                continue
    return False


def asyx_preparar() -> bool:
    """Comprueba Node y pide el alta si falta. True si queda listo para usar."""
    if not _comando_existe("node") or not (_comando_existe("npx") or _comando_existe("npm")):
        error("Asyx necesita Node.js 18.17+ (no lo encuentro en el PATH).")
        log("    Instálalo y vuelve a ejecutar el script; Pinggy es el respaldo.")
        return False
    if asyx_esta_alto():
        log("Asyx ya está dado de alta (~/.asyx).")
        return True
    if asyx_estado_alta() == "incompleta":
        titulo("EL ALTA DE ASYX QUEDÓ A MEDIAS")
        error("Falta el manifest.json que Asyx entrega al completar el registro.")
        log("    Se ve así: existe ~/.asyx/device.json y la clave, pero no hay")
        log("    ningún manifest.json en ~/.asyx/certs/*/ . Con el alta a medias el")
        log("    túnel no arranca (error 'missing manifest').")
        print()
        log("    Solución: repite el alta. Elimina lo que quedó a medias y vuelve")
        log("    a empezar el registro:")
        log("        rm -rf ~/.asyx && npx -y asyx@latest setup")
        print()
    else:
        titulo("ASYX NECESITA UNA CUENTA (solo esta vez)")
    print(f"  1) Crea una cuenta gratuita en:  {ASYX_REGISTRO}")
    print("     (solo email, sin tarjeta)")
    print("  2) Vuelve aquí y ejecuta el alta, que abre el navegador para")
    print("     verificar el email y reservar tu subdominio permanente.")
    print()
    if _preguntar("  ¿Empezar el alta ahora? [s/N]: ") == "s":
        if asyx_estado_alta() == "incompleta":
            log("Se reutiliza tu clientId, así que conservas el subdominio.")
        return asyx_registrar()
    log("Hazlo más tarde con:  python3 setup_mc.py --registrar-asyx")
    log("El CLI oficial también vale, pero no imprime el enlace de registro:")
    log("                       npx -y asyx@latest setup")
    if asyx_esta_alto():
        ok("Asyx dado de alta.")
        return True
    aviso("Asyx se queda fuera: el túnel irá por Pinggy (60 min, IP cambiante).")
    log("    Para usar Asyx de verdad:  python3 setup_mc.py --registrar-asyx")
    return False


def asyx_prechequear() -> tuple:
    """(ok, motivo). Sin red que comprobar: lo decisivo es el alta local."""
    if asyx_esta_alto():
        return True, ""
    estado = asyx_estado_alta()
    if estado == "incompleta":
        return False, ("el alta de Asyx está incompleta (falta el manifest.json). "
                       "Arréglalo con:  rm -rf ~/.asyx && npx -y asyx@latest setup")
    return False, ("no tienes cuenta de Asyx. Créala en " + ASYX_REGISTRO +
                   " y ejecuta: npx -y asyx@latest setup")


def asyx_comando(puerto: int) -> list:
    """--minecraft es lo que publica el registro SRV para el cliente Java."""
    return ["npx", "-y", "asyx@latest", "tunnel", "--minecraft", "--port", str(puerto)]


def asyx_parsear(texto: str) -> str:
    """Saca el host:puerto público de la salida de Asyx.

    Acepta las dos formas: 'host:puerto' explícito o solo el hostname (en ese
    caso se consulta el SRV para averiguar el puerto).
    """
    for linea in texto.splitlines():
        limpia = _sin_ansi(linea).strip()
        m = RE_HOST_PUERTO.search(limpia)
        if m and m.group(1).lower().endswith(ASYNX_DOMINIO):
            return f"{m.group(1)}:{m.group(2)}"
        mh = RE_ASYX_HOST.search(limpia)
        if mh:
            host = mh.group(1)
            puerto = _preguntar_puerto_srv(host)
            if puerto:
                return f"{host}:{puerto}"
    return ""


def _preguntar_puerto_srv(host: str):
    """Consulta _minecraft._tcp.<host> para saber el puerto sin escribirlo."""
    if not _comando_existe("dig"):
        return None
    try:
        r = subprocess.run(["dig", "+short", "SRV", f"_minecraft._tcp.{host}"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for linea in r.stdout.splitlines():
        partes = linea.split()
        if len(partes) == 4 and partes[3].rstrip(".").lower() == host.lower():
            return partes[2]
    return None


def asyx_srv_soportado(host: str) -> bool:
    """True si el jugador puede entrar solo con el hostname (SRV publicado)."""
    return _preguntar_puerto_srv(host) is not None


# ── Proveedor: Pinggy ──────────────────────────────────────────────────────
def pinggy_preparar() -> bool:
    if not _comando_existe("ssh"):
        error("Pinggy necesita el cliente ssh, que no está en el PATH.")
        return False
    return True


def pinggy_prechequear() -> tuple:
    if _servidor_alcanzable(PINGGY_SERVIDOR, PINGGY_PUERTO, timeout=8):
        return True, ""
    return False, (f"no hay conexión con {PINGGY_SERVIDOR}:{PINGGY_PUERTO} "
                   f"desde esta máquina (compruébalo con: nc -vz "
                   f"{PINGGY_SERVIDOR} {PINGGY_PUERTO})")


def pinggy_comando(puerto: int) -> list:
    # Sin -N a propósito: la URL se emite en la sesión, con -N nunca llega.
    return [
        "ssh", "-T", "-p", str(PINGGY_PUERTO),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-R", f"0:localhost:{puerto}",
        f"tcp@{PINGGY_SERVIDOR}",
    ]


def pinggy_parsear(texto: str) -> str:
    """Solo la línea TCP: ignora las URL http/https y el debugger local."""
    for linea in texto.splitlines():
        m = RE_PINGGY_TCP.search(_sin_ansi(linea))
        if m:
            return f"{m.group(1)}:{m.group(2)}"
    return ""


# ── Registro de proveedores ────────────────────────────────────────────────
PROVEEDORES = {
    "asyx": {
        "nombre": "Asyx",
        "preparar": asyx_preparar,
        "prechequear": asyx_prechequear,
        "comando": asyx_comando,
        "parsear": asyx_parsear,
        "aviso": None,
    },
    "pinggy": {
        "nombre": "Pinggy",
        "preparar": pinggy_preparar,
        "prechequear": pinggy_prechequear,
        "comando": pinggy_comando,
        "parsear": pinggy_parsear,
        "aviso": PINGGY_AVISO,
    },
}
ORDEN_PROVEEDORES = ("asyx", "pinggy")


def _esperar_direccion(cola: queue.Queue, barra: Barra, timeout: int,
                       parsear, notificador) -> tuple:
    """Lee la salida hasta obtener la dirección pública. Devuelve (dir, terminó)."""
    direccion = ""
    while time.time() - barra.inicio < timeout:
        try:
            linea = cola.get(timeout=0.2)
        except queue.Empty:
            barra.refrescar()
            if notificador:
                notificador.pulso(barra)
            continue
        if linea is None:
            break
        texto = _sin_ansi(linea).rstrip()
        if texto:
            limpiar_linea()
            print(f"    {texto}")
        direccion = parsear(texto) or direccion
        if direccion:
            break
    return direccion


def _intentar_proveedor(clave: str, puerto: int, timeout: int, notificador) -> tuple:
    """Prueba un proveedor. Devuelve (direccion, proceso, ok, motivo)."""
    global MOTIVO_FALLO_TUNEL
    prov = PROVEEDORES[clave]
    nombre = prov["nombre"]
    MOTIVO_FALLO_TUNEL = ""

    # Primero preparar: es donde se pregunta al usuario por el alta de Asyx.
    # Si se comprobara la red antes, el prechequeo fallaría sin alta y nunca
    # se llegaría al prompt, y el script se iría a Pinggy sin preguntar nada.
    if not prov["preparar"]():
        return "", None, False, f"{nombre} no está preparado"
    bien, motivo = prov["prechequear"]()
    if not bien:
        return "", None, False, motivo

    cola: queue.Queue = queue.Queue()
    try:
        proc = subprocess.Popen(prov["comando"](puerto), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
    except FileNotFoundError as e:
        return "", None, False, f"no se pudo ejecutar el cliente ({e})"
    threading.Thread(target=_bombeo_salida, args=(proc, cola), daemon=True).start()
    barra = Barra(f"Levantando túnel con {nombre}", limite=timeout)
    if notificador:
        notificador.iniciar(barra)
    direccion = _esperar_direccion(cola, barra, timeout, prov["parsear"], notificador)
    _drenar(cola)
    barra.finalizar(f"{nombre} activo" if direccion else None)
    if not direccion:
        if proc.poll() is not None:
            MOTIVO_FALLO_TUNEL = f"{nombre} terminó sin dar dirección pública"
        else:
            MOTIVO_FALLO_TUNEL = f"{nombre} no dio dirección pública en {timeout}s"
        _detener_proceso(proc, escribir_stop=False, timeout=5)
        return "", None, False, MOTIVO_FALLO_TUNEL
    return direccion, proc, True, ""


def _intentar_proveedor_detached(clave: str, puerto: int, timeout: int, notificador) -> tuple:
    """Igual que _intentar_proveedor pero dejando el proceso vivo."""
    prov = PROVEEDORES[clave]
    nombre = prov["nombre"]
    MOTIVO_FALLO_TUNEL = ""

    # Primero preparar: es donde se pregunta al usuario por el alta de Asyx.
    # Si se comprobara la red antes, el prechequeo fallaría sin alta y nunca
    # se llegaría al prompt, y el script se iría a Pinggy sin preguntar nada.
    if not prov["preparar"]():
        return "", None, False, f"{nombre} no está preparado"
    bien, motivo = prov["prechequear"]()
    if not bien:
        return "", None, False, motivo

    mango = _abrir_log(LOG_TUNEL, truncar=True)
    try:
        proc = subprocess.Popen(prov["comando"](puerto), stdin=subprocess.DEVNULL,
                                stdout=mango, stderr=subprocess.STDOUT,
                                start_new_session=True)
    except FileNotFoundError as e:
        return "", None, False, f"no se pudo ejecutar el cliente ({e})"
    finally:
        mango.close()
    PID_TUNEL.write_text(f"{proc.pid}\n")
    barra = Barra(f"Levantando túnel con {nombre}", limite=timeout)
    if notificador:
        notificador.iniciar(barra)
    direccion = ""
    try:
        while time.time() - barra.inicio < timeout:
            if proc.poll() is not None:
                barra.finalizar()
                MOTIVO_FALLO_TUNEL = f"{nombre} terminó sin dar dirección pública"
                break
            try:
                direccion = prov["parsear"](_sin_ansi(LOG_TUNEL.read_text(errors="replace")))
            except OSError:
                direccion = ""
            if direccion:
                barra.finalizar(f"{nombre} activo")
                return direccion, proc, True, ""
            barra.refrescar()
            if notificador:
                notificador.pulso(barra)
            time.sleep(0.2)
        barra.finalizar()
        if not direccion and not MOTIVO_FALLO_TUNEL:
            MOTIVO_FALLO_TUNEL = f"{nombre} no dio dirección pública en {timeout}s"
        _matar_pid(proc.pid)
        _limpiar_pid(PID_TUNEL)
        return "", None, False, MOTIVO_FALLO_TUNEL
    finally:
        barra.finalizar()


def _abrir_con_respaldo(modo: str, puerto: int, notificador, detached: bool) -> tuple:
    """Prueba los proveedores en orden hasta obtener un túnel verificado.

    Devuelve (direccion, proceso, proveedor_usado, avisos).
    """
    if modo not in ("auto", *PROVEEDORES):
        raise RuntimeError(f"Túnel desconocido: {modo}. "
                           f"Usa: auto, {', '.join(PROVEEDORES)}")
    orden = ORDEN_PROVEEDORES if modo == "auto" else (modo,)
    problemas = []
    for i, clave in enumerate(orden):
        if i and notificador:
            notificador.finalizar(
                motivo=f"{PROVEEDORES[clave]['nombre']} no disponible, "
                       f"probando {PROVEEDORES[orden[-1]]['nombre']}")
        obtener = _intentar_proveedor_detached if detached else _intentar_proveedor
        direccion, proc, bien, motivo = obtener(clave, puerto, TIMEOUT_TUNEL, notificador)
        if bien and _listo_para_verificar(direccion):
            avisos = [PROVEEDORES[clave]["aviso"]] if PROVEEDORES[clave]["aviso"] else []
            return direccion, proc, clave, avisos
        if proc is not None:
            if detached:
                _matar_pid(proc.pid)
                _limpiar_pid(PID_TUNEL)
            else:
                _detener_proceso(proc, escribir_stop=False, timeout=5)
        if direccion:
            problemas.append(f"{PROVEEDORES[clave]['nombre']}: {direccion} no responde")
        else:
            problemas.append(f"{PROVEEDORES[clave]['nombre']}: {motivo}")
    global MOTIVO_FALLO_TUNEL
    MOTIVO_FALLO_TUNEL = " | ".join(problemas)
    error("Ningún túnel pudo abrirse:\n    - " + "\n    - ".join(problemas))
    return "", None, "", []


def diagnostico_tunel() -> int:
    """Comprueba en segundos qué proveedores pueden funcionar desde aquí."""
    titulo("DIAGNÓSTICO DE TÚNELES")
    for clave in ORDEN_PROVEEDORES:
        prov = PROVEEDORES[clave]
        print()
        log(f"--- {prov['nombre']} ---")
        if clave == "asyx":
            estado = asyx_estado_alta()
            if estado == "completa":
                ok("Cuenta creada y lista para usar (~/.asyx).")
            elif estado == "incompleta":
                error("Alta incompleta: falta ~/.asyx/certs/*/manifest.json.")
                aviso("Se arregla con:  rm -rf ~/.asyx && npx -y asyx@latest setup")
            elif _comando_existe("node") and _comando_existe("npx"):
                aviso("Sin alta todavía: crea la cuenta en " + ASYX_REGISTRO +
                      " y ejecuta  npx -y asyx@latest setup")
            else:
                error("Falta Node.js 18.17+ (no se puede instalar el CLI).")
        else:
            log(f"Probando conexión con {PINGGY_SERVIDOR}:{PINGGY_PUERTO}...")
            inicio = time.time()
            bien, motivo = prov["prechequear"]()
            if bien:
                ok(f"Alcanzable en {time.time() - inicio:.1f}s.")
            else:
                error(motivo)
            aviso(PINGGY_AVISO)
    print()
    aviso("El script usa Asyx y cae a Pinggy automáticamente.")
    return 0


# ─── 10. Utilidades del túnel ─────────────────────────────────────────────


def comprobar_tunel(host: str, puerto: int = PUERTO_TUNEL) -> bool:
    """Comprueba que el puerto público del túnel acepta conexiones."""
    try:
        with socket.create_connection((host, puerto), timeout=8):
            return True
    except OSError:
        return False


def detener_tunel(proc):
    if proc is None or proc.poll() is not None:
        return
    log("Cerrando el túnel...")
    _detener_proceso(proc, escribir_stop=False, timeout=8)


# ─── 11. Apertura y verificación del túnel ────────────────────────────────
def _listo_para_verificar(host_puerto: str, intentos: int = 8, espera: float = 2.0) -> bool:
    """Comprueba que la dirección pública acepta conexiones, con reintentos.

    Importante: al abrirse el túnel, el puerto público tarda unos segundos en
    estar disponible y el primer intento suele recibir un 'connection refused'
    aunque el túnel vaya bien. Con un solo intento se descartaba un túnel
    perfectamente bueno y se pasaba al proveedor de respaldo, así que se reintenta
    durante unos segundos antes de darlo por malo.
    """
    if ":" in host_puerto:
        host, _, puerto = host_puerto.rpartition(":")
    else:
        host, puerto = host_puerto, str(PUERTO_TUNEL)
    puerto = int(puerto)
    for intento in range(1, intentos + 1):
        if comprobar_tunel(host, puerto):
            if intento > 1:
                log(f"El puerto público respondió al reintento {intento} "
                    f"({(intento - 1) * espera:.0f}s de propagación).")
            return True
        if intento < intentos:
            time.sleep(espera)
    return False


def abrir_tunel(puerto: int, notificador=None, modo: str = "auto",
                timeout: int = TIMEOUT_TUNEL) -> tuple:
    """Abre túnel (Asyx, con Pinggy de respaldo) y devuelve (dir, proc, usado, avisos).

    Comprueba siempre que la dirección obtenida responde de verdad: así una IP
    inventada o un puerto equivocado nunca se anuncian como si fueran el
    servidor.
    """
    direccion, proc, usado, avisos = _abrir_con_respaldo(modo, puerto, notificador,
                                                         detached=False)
    if direccion:
        ok(f"Túnel {PROVEEDORES[usado]['nombre']} verificado: {direccion}")
    return direccion, proc, usado, avisos


def abrir_tunel_detached(puerto: int, notificador=None, modo: str = "auto",
                         timeout: int = TIMEOUT_TUNEL) -> tuple:
    """Igual que abrir_tunel() pero dejando el túnel vivo tras salir."""
    direccion, proc, usado, avisos = _abrir_con_respaldo(modo, puerto, notificador,
                                                         detached=True)
    if direccion:
        ok(f"Túnel {PROVEEDORES[usado]['nombre']} verificado: {direccion}")
    return direccion, proc, usado, avisos


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
    notificar_servidor(webhook, direccion, args.puerto, MOTIVO_FALLO_TUNEL)

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
    print(f"  Respaldo auto : cada {BACKUP_AUTO_MINUTOS} min en respaldo/auto/ "
          f"({BACKUP_AUTO_MANTENER} últimos, sin parar el servidor)")
    print("  Login         : AuthMe  (regístrate la primera vez, luego /login)")
    print(f"  Servidor PID  : {_leer_pid(PID_SERVIDOR) or '?'}   (log: {LOG_SERVIDOR})")
    print(f"  Túnel PID     : {_leer_pid(PID_TUNEL) or '?'}   (log: {LOG_TUNEL})")
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
# ─── 13. Respaldo comprimido e importación ───────────────────────────────
# Rutas que se empaquetan (relativas a SERVER_DIR); el resto se regenera al importar.
BACKUP_INCLUYE = ["server.jar", "world", "plugins", "config", ".paper",
                  "server.properties", "eula.txt", "ops.json", "whitelist.json",
                  "banned-players.json", "banned-ips.json", "usercache.json",
                  "bukkit.yml", "spigot.yml", "commands.yml"]
BACKUP_EXCLUYE = [".env", ".env.*", ".git", "*.pid", "*.log", "*.part", "consola",
                  "libraries", "cache", "respaldo", "__pycache__"]
TAMANO_ADVERTENCIA = 50 * 1024 * 1024
TAMANO_MAXIMO = 100 * 1024 * 1024
# Techo del repositorio: cada copia del respaldo queda en el historial para
# siempre, así que sin un límite .git crece ~70 MB en cada ejecución.
GIT_AVISO = 600 * 1024 * 1024
GIT_TECHO = 1024 * 1024 * 1024
ESTADO_RESPALDO = Path.home() / ".local" / "state" / "setup_mc" / "respaldo.json"
RESPALDO_ACTUAL: Path | None = None


def _tamano_git() -> int:
    """Tamaño total de .git en bytes (0 si no es un repo)."""
    raiz = BASE_DIR / ".git"
    if not raiz.is_dir():
        return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(raiz):
        for nombre in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, nombre)).st_size
            except OSError:
                pass
    return total


def _mb(bytes_: int) -> str:
    return f"{bytes_ / 1048576:,.0f} MB"


def decidir_jar_del_respaldo(con_jar: bool) -> tuple:
    """Decide si el respaldo incluye server.jar según el tamaño de .git.

    Devuelve (incluir_jar, motivo). El server.jar son ~62 MB del paquete y es
    lo único que infla el historial, así que en cuanto el repositorio se
    acerca al techo se pasa automáticamente al respaldo pequeño (~7 MB).
    """
    if not con_jar:
        return False, "pedido explícitamente con --respaldo-sin-jar"
    tam = _tamano_git()
    if tam == 0:
        return True, ""
    if tam >= GIT_TECHO:
        aviso(f".git ocupa {_mb(tam)} y el techo es {_mb(GIT_TECHO)}.")
        log("    No se vuelve a subir el respaldo: el repositorio se saturaría.")
        log("    Para desbloquearlo, quita blobs antiguos del historial (git filter-repo")
        log("    / BFG) o vuelve a empezar el repo; o usa --sin-respaldo mientras tanto.")
        return False, f".git ya está en el techo ({_mb(tam)})"
    if tam >= GIT_AVISO:
        aviso(f".git ocupa {_mb(tam)}: se hace el respaldo SIN server.jar "
              f"({_mb(TAMANO_MAXIMO // 10)}) para no engordarlo más.")
        log(f"    Se avisa a partir de {_mb(GIT_AVISO)} y se bloquea a "
            f"{_mb(GIT_TECHO)}. GitHub empieza a frenar pushes sobre 1 GB.")
        return False, f".git grande ({_mb(tam)})"
    return True, ""


def _leer_estado_respaldo() -> dict:
    try:
        return json.loads(ESTADO_RESPALDO.read_text())
    except (OSError, ValueError):
        return {}


def _guardar_estado_respaldo(hash_contenido: str, alias: str = ""):
    """Apunta el contenido del respaldo subido.

    `alias` guarda el hash de la lista de archivos que se probó antes de
    recalcular (por ejemplo cuando el paquete con server.jar resultaba
    demasiado grande): sin él, la comprobación de la siguiente ejecución
    compararía hashes de listas distintas y nunca coincidirían.
    """
    datos = {
        "contenido_sha256": hash_contenido,
        "cuando": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": "servidor-mc.tar.zst",
    }
    if alias and alias != hash_contenido:
        datos["contenido_sha256_previo"] = alias
    try:
        ESTADO_RESPALDO.parent.mkdir(parents=True, exist_ok=True)
        ESTADO_RESPALDO.write_text(json.dumps(datos, indent=2))
    except OSError:
        pass


def _respaldo_sigue_vigente(hash_contenido: str) -> bool:
    """True si el respaldo ya versionado contiene exactamente este contenido.

    Evita volver a subir ~70 MB idénticos cuando el mundo no ha cambiado.
    """
    estado = _leer_estado_respaldo()
    conocidos = {estado.get("contenido_sha256"), estado.get("contenido_sha256_previo")}
    if hash_contenido not in conocidos - {None, ""}:
        return False
    rel = _relativo(_paquete_respaldo())
    return _git(["ls-files", "--error-unmatch", rel]).returncode == 0

RE_RAW = re.compile(r"^https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/(.+)$", re.I)
RE_REPO = re.compile(r"^([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
RE_GITHUB = re.compile(r"^https?://(?:www\.)?github\.com/([^/]+)/([^/]+)", re.I)
NOMBRES_RESPALDO = ("respaldo/servidor-mc.tar.zst", "respaldo/servidor-mc.tar.gz")


def _relativo(ruta: Path) -> str:
    try:
        return ruta.resolve().relative_to(BASE_DIR).as_posix()
    except ValueError:
        return ruta.name


def _sha256_archivo(ruta: Path) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloque)
    return h.hexdigest()


def _compresor() -> tuple:
    """(comando de compresión, extensión) del compresor disponible más compacto."""
    if shutil.which("zstd"):
        return ["zstd", "-19", "-T0", "-q", "-c"], ".tar.zst"
    return ["gzip", "-9", "-c"], ".tar.gz"


def _descompresor(ruta: Path) -> list:
    if ruta.suffix == ".zst" and shutil.which("zstd"):
        return ["zstd", "-dc", str(ruta)]
    return ["gzip", "-dc", str(ruta)]


def _paquete_respaldo() -> Path:
    _, ext = _compresor()
    return RESPALDO_DIR / f"servidor-mc{ext}"


def _miembros_de_respaldo(ruta: Path) -> list:
    """Lista el contenido del paquete sin extraerlo."""
    with tempfile.TemporaryDirectory() as tmp:
        plano = Path(tmp) / "p.tar"
        with open(plano, "wb") as f:
            r = subprocess.run(_descompresor(ruta), stdout=f, stderr=subprocess.PIPE, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"El paquete está corrupto: {(r.stderr or '').strip()[:200]}")
        r = subprocess.run(["tar", "-tf", str(plano)], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("El paquete no se puede leer como tar.")
        return [l for l in r.stdout.splitlines() if l.strip()]


def _version_paper() -> str:
    try:
        texto = (LOGS_DIR / "latest.log").read_text(errors="replace")
    except OSError:
        return ""
    m = (re.search(r"Running Paper ([\w.\-]+)", texto)
         or re.search(r"running Paper version ([\w.\-]+)", texto))
    return m.group(1) if m else ""


def _version_mc_del_jar() -> str:
    """Versión de Minecraft que declara el server.jar, sin arrancarlo."""
    try:
        with zipfile.ZipFile(SERVER_JAR) as z:
            nombres = set(z.namelist())
            for candidato in ("version.json", "META-INF/versions.list"):
                if candidato in nombres:
                    texto = z.read(candidato).decode("utf-8", errors="replace")
                    m = re.search(r'"?id"?\s*[:=]\s*"([\w.\-]+)"', texto)
                    if m and re.match(r"^\d", m.group(1)):
                        return m.group(1)
    except (OSError, zipfile.BadZipFile, KeyError):
        pass
    return ""


def _excluido(rel: str) -> bool:
    base = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(base, pat)
               for pat in BACKUP_EXCLUYE)


def _miembro_peligroso(rel: str) -> bool:
    """Un miembro del paquete es peligroso solo si es un secreto de verdad.

    Los .jar, server.properties o eula.txt son normales dentro de un respaldo
    (a diferencia de un commit, donde no deben subirse sueltos).
    """
    return bool(RE_SECRETO.search(rel))


def _hash_contenido(directorio: Path, incluir: list) -> tuple:
    """(nº de archivos, sha256) del contenido empaquetado, estable entre máquinas."""
    archivos = []
    for patron in incluir:
        objetivo = directorio / patron
        if objetivo.is_dir():
            archivos.extend(sorted(p for p in objetivo.rglob("*") if p.is_file()))
        elif objetivo.is_file():
            archivos.append(objetivo)
    resumen = hashlib.sha256()
    total = 0
    for ruta in archivos:
        rel = ruta.relative_to(directorio).as_posix()
        if _excluido(rel):
            continue
        total += 1
        resumen.update(rel.encode())
        resumen.update(_sha256_archivo(ruta).encode())
    return total, resumen.hexdigest()


# ─── Respaldo automático en caliente (sin parar el servidor) ─────────────
# Cada BACKUP_AUTO_MINUTOS se empaqueta el mundo mientras el servidor sigue
# dando servicio. El peligro es copiar ficheros a medio escribir: por eso antes
# se congela el mundo con save-off/save-all flush y se descongela al terminar.
BACKUP_AUTO_MINUTOS = 20
BACKUP_AUTO_MANTENER = 4          # cuántos respaldos automáticos se conservan
DIR_BACKUP_AUTO = SERVER_DIR / "respaldo" / "auto"
_HILO_BACKUPS = None


def _consola(linea: str) -> bool:
    """Escribe una orden en el servidor por el FIFO de la consola.

    Se usa el FIFO y no el proc del servidor para que funcione igual con el
    servidor en primer plano y en segundo plano.
    """
    if not linea:
        return False
    try:
        # O_NONBLOCK es imprescindible: abrir un FIFO para escribir sin un
        # lector se queda bloqueado para siempre, y el hilo de respaldos
        # (y el propio script) se quedarían colgados.
        fd = os.open(FIFO_CONSOLA, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        os.write(fd, (linea + "\n").encode("utf-8", "replace"))
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _purgar_backups_auto() -> None:
    """Deja solo los BACKUP_AUTO_MANTENER más recientes."""
    try:
        backups = sorted(DIR_BACKUP_AUTO.glob("mundo-*.tar.zst"),
                         key=lambda x: x.name, reverse=True)
    except OSError:
        return
    for viejo in backups[BACKUP_AUTO_MANTENER:]:
        try:
            viejo.unlink()
            log(f"    Borrado el respaldo automático antiguo {viejo.name}.")
        except OSError:
            pass


def respaldo_automatico(puerto: int = 25565) -> bool:
    """Empaqueta el mundo SIN parar el servidor.

    Congela las escrituras (save-off + save-all flush), empaqueta, y siempre
    vuelve a poner save-on: si el servidor se quedara con save-off activated,
    el mundo dejaría de guardarse y se perdería el progreso de los jugadores.
    """
    if not (SERVER_DIR / "world").is_dir():
        return False
    congelado = False
    # Red de seguridad: si este proceso muriese con el mundo congelado, el
    # servidor seguiría sin guardar y los jugadores perderían progreso.
    watchdog = None
    try:
        if puerto_abierto(puerto) and _consola("save-off"):
            _consola("save-all flush")
            congelado = True

            def _descongelar():
                _consola("save-on")

            watchdog = threading.Timer(BACKUP_AUTO_MINUTOS * 30, _descongelar)
            watchdog.daemon = True
            watchdog.start()
            time.sleep(3)      # margen para que el servidor termine de volcar
        incluir = [q for q in BACKUP_INCLUYE if q != "server.jar"]
        archivos, hash_contenido = _hash_contenido(SERVER_DIR, incluir)
        destino = DIR_BACKUP_AUTO / f"mundo-{time.strftime('%Y%m%d-%H%M%S')}.tar.zst"
        # El tamaño comprimido solo se sabe al final; con el del respaldo
        # anterior la barra advances con un porcentaje real en vez de girar sin
        # número. Un margen del 2% evita que se pase del 100 antes de tiempo.
        previos = sorted(DIR_BACKUP_AUTO.glob("mundo-*.tar.zst"),
                         key=lambda x: x.name, reverse=True)
        estimado = int(previos[0].stat().st_size * 1.02) if previos else None
        manifiesto = {
            "creado": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "paper": _version_paper() or "desconocida",
            "minecraft": _version_mc_del_jar() or "desconocida",
            "con_server_jar": False,
            "automatico": True,
            "contenido_sha256": hash_contenido,
            "archivos": archivos,
        }
        _empaquetar(destino, manifiesto, incluir, total_esperado=estimado)
        miembros = _miembros_de_respaldo(destino)
        if MANIFIESTO not in miembros or not any(m.startswith("world/") for m in miembros):
            destino.unlink(missing_ok=True)
            error("El respaldo automático no parecía válido; se borra.")
            return False
        malos = [m for m in miembros if _miembro_peligroso(m)]
        if malos:
            destino.unlink(missing_ok=True)
            error("El respaldo automático traía ficheros sensibles; se borra.")
            return False
        ok(f"Respaldo automático: respaldo/auto/{destino.name} "
           f"({destino.stat().st_size / 1048576:,.0f} MB)")
        _purgar_backups_auto()
        return True
    except (OSError, RuntimeError) as e:
        error(f"No se pudo hacer el respaldo automático: {e}")
        return False
    finally:
        if watchdog is not None:
            watchdog.cancel()
        if congelado:
            _consola("save-on")


def _bucle_backups_auto(intervalo_min: int, puerto: int) -> None:
    while True:
        time.sleep(intervalo_min * 60)
        try:
            # Se pregunta por el puerto y no por el fichero de PID: en el
            # camino interactivo ese fichero no se escribía y el hilo se
            # saltaba todas las copias sin avisar.
            if puerto_abierto(puerto):
                respaldo_automatico(puerto)
            else:
                log("Se pasó el respaldo automático: el servidor no "
                    "escucha en el puerto.")
        except Exception as e:  # noqa: BLE001
            # Ni una excepción debe tumbar el script, pero sí hay que
            # enterarse: antes se silenciaban y no se veía ningún fallo.
            error(f"Fallo inesperado en el respaldo automático: {e}")


def arrancar_backups_auto(intervalo_min: int = BACKUP_AUTO_MINUTOS,
                          puerto: int = 25565) -> bool:
    """Lanza el hilo del respaldo automático (una sola vez)."""
    global _HILO_BACKUPS
    if _HILO_BACKUPS is not None and _HILO_BACKUPS.is_alive():
        return False
    _HILO_BACKUPS = threading.Thread(target=_bucle_backups_auto,
                                     args=(intervalo_min, puerto), daemon=True,
                                     name="respaldo-automatico")
    _HILO_BACKUPS.start()
    return True

# ─── El mundo, cuando ya no cabe en git: release rodante de GitHub ──────
# GitHub no admite blobs de más de 100 MB, y el mundo crece sin parar. En
# cuanto el tarball se pasa, en vez de dejar de respaldar se sube a una
# release con etiqueta fija: la URL no cambia nunca y el import ya sabe
# bajarla. Así el mundo vive fuera del historial y .git deja de crecer.
RELEASE_MUNDO = "world"
ASSET_MUNDO = "servidor-mc.tar.zst"
LIMITE_ASSET_RELEASE = 1900 * 1024 * 1024      # tope por asset de una release


def _slug_repo() -> str:
    """owner/repo del remoto origin, para las llamadas a la API."""
    url = _git(["remote", "get-url", "origin"]).stdout.strip()
    m = re.search(r"github\.com[:/]+([^/]+)/([^/.]+)(?:\.git)?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def _api_github(metodo: str, url: str, **kw):
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    cabeceras = {"Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        cabeceras["Authorization"] = f"Bearer {token}"
    cabeceras.update(kw.pop("headers", {}))
    return requests.request(metodo, url, headers=cabeceras, timeout=kw.pop("timeout", 180),
                            **kw)


def url_mundo() -> str:
    """URL estable y pública del último mundo publicado."""
    slug = _slug_repo()
    return (f"https://github.com/{slug}/releases/download/{RELEASE_MUNDO}/{ASSET_MUNDO}"
            if slug else "")


def publicar_mundo(ruta: Path) -> str:
    """Sube (o reemplaza) el tarball del mundo en la release rodante.

    Devuelve la URL de descarga. Lanza RuntimeError si no puede.
    """
    slug = _slug_repo()
    if not slug:
        raise RuntimeError("No se pudo deducir el repositorio del remoto origin.")
    if not (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")):
        raise RuntimeError("Falta GITHUB_TOKEN en el entorno.")
    tam = ruta.stat().st_size
    if tam > LIMITE_ASSET_RELEASE:
        raise RuntimeError(
            f"El respaldo pesa {tam / 1048576:,.0f} MB y una release de GitHub "
            f"acepta {LIMITE_ASSET_RELEASE // 1048576:,.0f} MB por asset.")
    base = f"https://api.github.com/repos/{slug}"
    datos = {"tag_name": RELEASE_MUNDO,
             "name": "Mundo del servidor (se actualiza solo)",
             "body": "Se sobrescribe en cada respaldo: esta URL siempre da el "
                     "mundo más reciente. Para restaurarlo, `python3 setup_mc.py` "
                     "y luego importar con esta dirección.",
             "draft": False, "prerelease": False}
    r = _api_github("POST", f"{base}/releases", json=datos)
    if r.status_code in (201, 422):        # 422 = ya existe esa etiqueta
        r = _api_github("GET", f"{base}/releases/tags/{RELEASE_MUNDO}")
        r.raise_for_status()
        id_release = r.json()["id"]
        # Se borra el asset anterior: si no, se acumulan uno a uno.
        for asset in _api_github("GET", f"{base}/releases/{id_release}/assets").json():
            if asset.get("name") == ASSET_MUNDO:
                _api_github("DELETE", f"{base}/releases/assets/{asset['id']}")
    else:
        r.raise_for_status()
        id_release = r.json()["id"]
    r = _api_github("POST",
                    f"https://uploads.github.com/repos/{slug}/releases/{id_release}/assets"
                    f"?name={ASSET_MUNDO}",
                    data=ruta.read_bytes(),
                    headers={"Content-Type": "application/octet-stream"})
    r.raise_for_status()
    return url_mundo()


def _despublicar_respaldo() -> None:
    """Deja de versionar el tarball: a partir de aquí vive en la release."""
    rel = _relativo(_paquete_respaldo())
    _git(["rm", "--cached", "-q", "-f", rel])
    if not GITIGNORE_FILE.read_text().count(f"\n{rel}\n"):
        bloque = (f"\n# El mundo ya no cabe en git (>100 MB): vive en la release\n"
                  f"# {RELEASE_MUNDO}, con URL estable. Ver setup_mc.py.\n{rel}\n")
        GITIGNORE_FILE.write_text(GITIGNORE_FILE.read_text() + bloque)

def _empaquetar(destino: Path, manifiesto: dict, incluir: list,
                total_esperado: int | None = None) -> tuple:
    """Escribe el tar comprimido con el manifiesto dentro. Devuelve (bytes, segundos).

    `total_esperado` no es obligatorio, pero sirve para que la barra muestre un
    porcentaje real: el tamaño comprimido solo se conoce al terminar, así que
    se estima a partir del respaldo anterior.
    """
    compresor, _ = _compresor()
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal = destino.with_suffix(destino.suffix + ".part")
    with tempfile.TemporaryDirectory() as tmp:
        man_tmp = Path(tmp) / MANIFIESTO
        man_tmp.write_text(json.dumps(manifiesto, indent=2) + "\n")
        cmd = ["tar", "-I", " ".join(compresor), "-cf", "-", "--ignore-failed-read",
               # El mundo cambia mientras se empaqueta si el servidor está vivo:
               # eso son avisos, no errores.
               "--warning=no-file-changed", "--warning=no-file-removed"]
        for patron in BACKUP_EXCLUYE:
            cmd += ["--exclude", patron]
        cmd += ["-C", tmp, MANIFIESTO, "-C", str(SERVER_DIR), *incluir]
        # stderr a fichero: si lo dejáramos en una tubería y tar escribiese mucho
        # se llenaría y se quedaría bloqueado esperándonos.
        with tempfile.TemporaryFile() as errf, open(temporal, "wb") as f:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf)
            barra = Barra(f"Comprimiendo {destino.name}", total=total_esperado)
            inicio = time.time()
            total = 0
            while True:
                trozo = proc.stdout.read(1024 * 1024)
                if not trozo:
                    break
                f.write(trozo)
                total += len(trozo)
                barra.avanzar(len(trozo))
            proc.stdout.close()
            rc = proc.wait()
            errf.seek(0)
            err = errf.read().decode(errors="replace").strip()
        if rc != 0:
            temporal.unlink(missing_ok=True)
            barra.finalizar()
            if rc < 0:
                raise RuntimeError(
                    f"tar terminó por la señal {-rc} "
                    f"({signal.Signals(-rc).name}). ¿Lo mataste con pkill/ kill?")
            if rc == 1 and err:
                aviso(f"tar avisó (no es fatal): {err.strip()[:200]}")
            else:
                raise RuntimeError(f"tar falló (código {rc}): {err.strip()[:200] or '(sin salida)'}")
    temporal.replace(destino)
    barra.finalizar(f"{total / 1048576:,.1f} MB en {time.time() - inicio:.0f}s")
    return total, time.time() - inicio


def crear_respaldo(con_jar: bool = True, forzar: bool = False) -> bool:
    """Empaqueta el servidor en respaldo/servidor-mc.tar.zst y valida el contenido.

    Se salta si el mundo no ha cambiado desde el último respaldo ya versionado:
    volver a subirlo añadiría ~70 MB idénticos al historial sin aportar nada.
    """
    global RESPALDO_ACTUAL
    if not (SERVER_DIR / "world").is_dir():
        aviso("No hay mundo guardado: se omite el respaldo.")
        return False
    con_jar, motivo_jar = decidir_jar_del_respaldo(con_jar)
    incluir = [p for p in BACKUP_INCLUYE if con_jar or p != "server.jar"]
    archivos, hash_contenido = _hash_contenido(SERVER_DIR, incluir)
    hash_inicial = hash_contenido
    if motivo_jar and not con_jar:
        log(f"    Motivo: {motivo_jar}.")
    RESPALDO_ACTUAL = _paquete_respaldo()
    if not forzar and _respaldo_sigue_vigente(hash_contenido):
        ok("El mundo no ha cambiado: se conserva el respaldo ya subido "
           "(no se suman ~70 MB al repositorio).")
        if not RESPALDO_ACTUAL.is_file():
            aviso("Se regenerará en la próxima ejecución que cambie algo.")
            return False
        return True
    manifiesto = {
        "creado": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "paper": _version_paper() or "desconocida",
        "minecraft": _version_mc_del_jar() or "desconocida",
        "con_server_jar": con_jar,
        "contenido_sha256": hash_contenido,
        "archivos": archivos,
    }
    try:
        _empaquetar(RESPALDO_ACTUAL, manifiesto, incluir)
        # GitHub no acepta blobs de más de 100 MB: si el mundo ha crecido tanto
        # que el paquete con server.jar no cabe, se rehace sin él en vez de
        # dejar un respaldo imposible de subir.
        tam = RESPALDO_ACTUAL.stat().st_size
        if con_jar and tam > TAMANO_MAXIMO:
            aviso(f"Con server.jar el respaldo llega a {tam / 1048576:,.0f} MB "
                  f"y GitHub rechaza archivos de más de {TAMANO_MAXIMO // 1048576} MB: "
                  f"se repite sin server.jar.")
            log("    Se podrá volver a incluir cuando el mundo se pode o se "
                "migre a GitHub Releases.")
            con_jar = False
            incluir = [q for q in incluir if q != "server.jar"]
            archivos, hash_contenido = _hash_contenido(SERVER_DIR, incluir)
            manifiesto["con_server_jar"] = False
            manifiesto["contenido_sha256"] = hash_contenido
            manifiesto["archivos"] = archivos
            _empaquetar(RESPALDO_ACTUAL, manifiesto, incluir)
    except RuntimeError as e:
        error(str(e))
        return False

    miembros = _miembros_de_respaldo(RESPALDO_ACTUAL)
    if MANIFIESTO not in miembros:
        error("El respaldo no lleva manifiesto; se borra.")
        RESPALDO_ACTUAL.unlink(missing_ok=True)
        return False
    malos = [m for m in miembros if _miembro_peligroso(m)]
    if malos:
        RESPALDO_ACTUAL.unlink(missing_ok=True)
        error("El respaldo contenía archivos sensibles; se borra y no se sube:\n    - " +
              "\n    - ".join(malos[:10]))
        return False
    if not any(m.startswith("world/") for m in miembros):
        RESPALDO_ACTUAL.unlink(missing_ok=True)
        error("El respaldo no contiene el mundo; se borra.")
        return False
    if _git(["check-ignore", "-q", _relativo(RESPALDO_ACTUAL)]).returncode == 0:
        aviso(f"Atención: {_relativo(RESPALDO_ACTUAL)} está en .gitignore; "
              f"se forzará su subida.")
    tam = RESPALDO_ACTUAL.stat().st_size
    if tam > TAMANO_ADVERTENCIA:
        aviso(f"El respaldo pesa {tam / 1048576:,.0f} MB. GitHub avisa a partir de "
              f"{TAMANO_ADVERTENCIA // 1048576} MB; por encima de "
              f"{TAMANO_MAXIMO // 1048576} MB ya no se puede subir.")
    if tam > TAMANO_MAXIMO:
        # El mundo ya no cabe en git. Se va a una release rodante con URL fija
        # en vez de dejar de respaldar, y se deja de versionar para que .git
        # no siga engordando con el mundo.
        aviso(f"El respaldo llega a {tam / 1048576:,.0f} MB y GitHub no acepta "
              f"blobs de más de {TAMANO_MAXIMO // 1048576} MB: se publica como "
              "release en lugar de subirse al repositorio.")
        try:
            enlace = publicar_mundo(RESPALDO_ACTUAL)
        except (RuntimeError, requests.RequestException) as e:
            RESPALDO_ACTUAL.unlink(missing_ok=True)
            error(f"No se pudo publicar el mundo en la release: {e}")
            log("    Se deja el respaldo solo en disco, en respaldo/auto/.")
            return False
        _despublicar_respaldo()
        ok(f"Mundo publicado en la release: {enlace}")
        log("    Esa URL no cambia: siempre da el mundo más reciente, y el")
        log("    import la baja automáticamente si el repositorio no lo trae.")
        RESPALDO_ACTUAL = None
        return True
    ok(f"Respaldo listo: {_relativo(RESPALDO_ACTUAL)} "
       f"(MC {manifiesto['minecraft']}, {archivos} archivos, "
       f"{'con' if con_jar else 'sin'} server.jar)")
    # Se apunta el contenido para que la próxima ejecución con el mundo
    # idéntico no vuelva a subir el mismo tarball.
    _guardar_estado_respaldo(hash_contenido, hash_inicial)
    return True


def _descargar_a(origen: str, destino: Path, headers: dict, barra: Barra) -> None:
    """Descarga en streaming a un temporal y lo renombra al terminar."""
    temporal = destino.with_suffix(destino.suffix + ".descarga")
    with requests.get(origen, headers=headers, stream=True, timeout=300) as r:
        if r.status_code == 404:
            raise RuntimeError("No se encuentra el respaldo en esa ruta.")
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        barra.total = total or None
        with open(temporal, "wb") as f:
            for trozo in r.iter_content(1024 * 1024):
                if trozo:
                    f.write(trozo)
                    if total:
                        barra.avanzar(len(trozo))
    temporal.replace(destino)


def _rama_por_defecto(user: str, repo: str, headers: dict) -> str:
    r = requests.get(f"https://api.github.com/repos/{user}/{repo}", headers=headers, timeout=20)
    if r.status_code == 404:
        raise RuntimeError(f"No existe el repositorio {user}/{repo} "
                           f"(o es privado: se necesita un token).")
    r.raise_for_status()
    return r.json().get("default_branch") or "main"


def _descargar_respaldo(origen: str, destino: Path) -> Path:
    """Descarga un paquete desde owner/repo, URL de GitHub o URL directa."""
    headers = {"User-Agent": USER_AGENT}
    gh = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    origen = origen.strip().strip("'\"")
    if not origen:
        raise RuntimeError("No se indicó ningún repositorio.")
    barra = Barra(f"Descargando {destino.name}", total=None)

    m_raw = RE_RAW.match(origen)
    m_repo = RE_REPO.match(origen)
    m_gh = RE_GITHUB.match(origen)
    if m_raw:
        _descargar_a(origen, destino, headers, barra)
    elif m_repo and not origen.startswith("http"):
        user, repo = m_repo.group(1), m_repo.group(2)
        rama = _rama_por_defecto(user, repo, gh)
        log(f"Buscando el respaldo en {user}/{repo} (rama por defecto: {rama})...")
        ultimo = None
        for nombre in NOMBRES_RESPALDO:
            url = f"https://raw.githubusercontent.com/{user}/{repo}/{rama}/{nombre}"
            try:
                _descargar_a(url, destino, headers, barra)
                ultimo = None
                break
            except RuntimeError as e:
                ultimo = e
        if ultimo is not None:
            raise RuntimeError(f"No hay ningún respaldo en {NOMBRES_RESPALDO[0]} "
                               f"de {user}/{repo} (rama {rama}).")
    elif m_gh:
        user, repo = m_gh.group(1), m_gh.group(2).removesuffix(".git")
        rama = _rama_por_defecto(user, repo, gh)
        base = f"https://raw.githubusercontent.com/{user}/{repo}/{rama}"
        log(f"Buscando el respaldo en {user}/{repo} (rama por defecto: {rama})...")
        for nombre in NOMBRES_RESPALDO:
            try:
                _descargar_a(f"{base}/{nombre}", destino, headers, barra)
                break
            except RuntimeError:
                continue
        else:
            raise RuntimeError(f"No hay ningún respaldo en {user}/{repo} (rama {rama}).")
    else:
        _descargar_a(origen, destino, headers, barra)
    barra.finalizar("Descarga completada")
    return destino


def _leer_manifiesto(ruta: Path) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        plano = Path(tmp) / "p.tar"
        with open(plano, "wb") as f:
            r = subprocess.run(_descompresor(ruta), stdout=f, stderr=subprocess.PIPE, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"El paquete está corrupto: {(r.stderr or '').strip()[:200]}")
        r = subprocess.run(["tar", "-xf", str(plano), "-C", tmp, MANIFIESTO],
                           capture_output=True, text=True)
        if r.returncode != 0 or not (Path(tmp) / MANIFIESTO).is_file():
            raise RuntimeError("El paquete no trae manifest.json: no es un respaldo válido.")
        return json.loads((Path(tmp) / MANIFIESTO).read_text())


def _extraer_respaldo(origen: Path, destino: Path) -> None:
    """Descomprime el paquete en `destino` (la raíz local), sin tocar .env ni Git."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        plano = base / "p.tar"
        with open(plano, "wb") as f:
            r = subprocess.run(_descompresor(origen), stdout=f, stderr=subprocess.PIPE, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"El paquete está corrupto: {(r.stderr or '').strip()[:200]}")
        r = subprocess.run(["tar", "-xf", str(plano), "-C", str(base)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"No se pudo descomprimir: {(r.stderr or '').strip()[:200]}")
        plano.unlink(missing_ok=True)
        if (base / ".git").exists():
            raise RuntimeError("El paquete contiene un .git: no se importa por seguridad.")
        if (base / ".env").exists():
            (base / ".env").unlink()
            aviso("El paquete traía un .env (webhook); se descarta por seguridad.")
        destino.mkdir(parents=True, exist_ok=True)
        for hijo in sorted(base.iterdir()):
            if hijo.name == MANIFIESTO:
                continue
            final = destino / hijo.name
            if hijo.is_dir():
                shutil.copytree(hijo, final, dirs_exist_ok=True)
            else:
                shutil.copy2(hijo, final)


def importar_respaldo(origen: str) -> bool:
    """Descarga, verifica y extrae un respaldo en la raíz local.

    Si `origen` está vacío se prueban solas, por este orden, las fuentes del
    proyecto: el tarball versionado en git y la release rodante del mundo.
    """
    if not origen:
        slug = _slug_repo()
        candidatas = []
        if slug:
            candidatas.append(f"https://raw.githubusercontent.com/{slug}/main/"
                              f"respaldo/{ASSET_MUNDO}")
        candidatas.append(url_mundo())
        for intento in [c for c in candidatas if c]:
            log(f"Probando {intento}")
            try:
                return _importar_desde(intento)
            except (RuntimeError, requests.RequestException) as e:
                aviso(f"No se pudo ({type(e).__name__}).")
        error("No se encontró el mundo ni en el repositorio ni en la release.")
        return False
    return _importar_desde(origen)


def _importar_desde(origen: str) -> bool:
    _, ext = _compresor()
    RESPALDO_DIR.mkdir(parents=True, exist_ok=True)
    destino = RESPALDO_DIR / f"descargado{ext}"
    archivo = _descargar_respaldo(origen, destino)
    ok(f"Paquete descargado ({archivo.stat().st_size / 1048576:,.1f} MB).")
    manifiesto = _leer_manifiesto(archivo)
    _extraer_respaldo(archivo, SERVER_DIR)
    archivo.unlink(missing_ok=True)

    esperado = manifiesto.get("contenido_sha256", "")
    if esperado:
        incluir = [p for p in BACKUP_INCLUYE
                   if manifiesto.get("con_server_jar", True) or p != "server.jar"]
        _, hash_real = _hash_contenido(SERVER_DIR, incluir)
        if hash_real != esperado:
            error(f"El contenido extraído no coincide con el manifiesto.\n"
                  f"    esperado: {esperado}\n    obtenido: {hash_real}")
        else:
            ok("Contenido verificado contra el manifiesto (sha256 correcto).")
    log(f"Servidor importado: MC {manifiesto.get('minecraft', '?')}, "
        f"Paper {manifiesto.get('paper', '?')}, {manifiesto.get('archivos', '?')} archivos.")
    return True


def preguntar_crear_o_importar() -> str:
    """Menú cuando no hay servidor. Devuelve 'crear', 'importar' o 'cancelar'."""
    print()
    print("=" * 62)
    print("  NO HAY NINGÚN SERVIDOR EN ESTA CARPETA")
    print("=" * 62)
    print("  1) Crear uno nuevo (descargar Paper y generar mundo)")
    print("  2) Importar un respaldo (.tar.zst) de este repositorio o de la release")
    print("  3) Cancelar")
    print("=" * 62)
    if not sys.stdin.isatty():
        aviso("Sin terminal interactiva: se crea uno nuevo.")
        return "crear"
    try:
        opcion = input("Elige 1, 2 o 3 [1]: ").strip() or "1"
    except (EOFError, KeyboardInterrupt):
        print()
        aviso("Cancelado.")
        return "cancelar"
    if opcion in ("1", "crear", "nuevo", "c"):
        return "crear"
    if opcion in ("2", "importar", "i"):
        return "importar"
    if opcion in ("3", "cancelar", "x", "q"):
        return "cancelar"
    aviso("Opción no válida; se crea uno nuevo.")
    return "crear"


def preguntar_url_respaldo() -> str:
    print()
    print("  Formatos aceptados:")
    print("    - owner/repo                     (p. ej. tenochcatl9/Setup2)")
    print("    - https://github.com/owner/repo")
    print("    - URL directa a un .tar.zst / .tar.gz")
    try:
        return input("  URL o owner/repo del respaldo: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


# ─── 14. Commit y push a la rama principal ───────────────────────────────
def subir_cambios(mensaje: str, rama: str = "main", con_respaldo: bool = True,
                  con_jar: bool = True, forzar_respaldo: bool = False) -> bool:
    """Verifica secretos, crea el respaldo, hace commit y empuja a la rama principal."""
    if not es_repo_git():
        aviso("No es un repositorio Git: no se puede subir nada.")
        return False
    bien, motivo = verificar_env_protegido()
    if not bien:
        error("No se sube nada porque la protección de secretos falló:")
        print(motivo, file=sys.stderr)
        return False

    if con_respaldo:
        if not crear_respaldo(con_jar=con_jar, forzar=forzar_respaldo):
            # Antes esto abortaba todo el commit, y con el mundo grande no se
            # podía ni subir un cambio de código. El código siempre sube.
            aviso("El respaldo del mundo no se pudo hacer, pero los cambios de "
                  "código se suben igualmente.")

    _git(["add", "-A"])
    # El respaldo se versiona a propósito: si el .gitignore lo tapara, se fuerza.
    if con_respaldo and RESPALDO_ACTUAL is not None:
        rel = _relativo(RESPALDO_ACTUAL)
        if rel not in _git(["diff", "--cached", "--name-only"]).stdout.splitlines():
            log(f"Forzando la subida de {rel} (lo ignoraba el .gitignore).")
            _git(["add", "-f", rel])

    staged = [p for p in _git(["diff", "--cached", "--name-only"]).stdout.splitlines() if p.strip()]
    if not staged:
        aviso("No hay cambios nuevos que subir.")
        return False
    sospechosos = [p for p in staged if ruta_sensible(p)]
    if sospechosos:
        _git(["reset", "-q"])
        error("Se aborta el commit: se intentó subir algo sensible.\n    - " +
              "\n    - ".join(sospechosos))
        return False
    _git(["commit", "-q", "-m", mensaje])
    hash_commit = _git(["rev-parse", "--short", "HEAD"]).stdout.strip()
    log(f"Commit creado: {hash_commit} ({len(staged)} archivo/s)")
    for p in staged:
        print(f"      + {p}")
    actual = _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    log(f"Empujando a origin/{rama} (rama actual: {actual})...")
    r = _git(["push", "origin", f"HEAD:{rama}"], capture=True)
    if r.returncode != 0:
        detalle = (r.stderr or r.stdout).strip()
        error(f"No se pudo hacer push a {rama}:\n{detalle}")
        if "100 MB" in detalle or "too large" in detalle.lower():
            aviso("GitHub no acepta archivos de más de 100 MB. Vuelve a generar el "
                  "respaldo con  --respaldo-sin-jar  (queda en ~7 MB).")
        else:
            aviso(f"Resuélvelo manualmente con:  git pull --rebase origin {rama} && "
                  f"git push origin HEAD:{rama}")
        return False
    ok(f"Cambios subidos a origin/{rama} ({hash_commit}).")
    tam = _tamano_git()
    if tam:
        nivel = "AVISO" if tam >= GIT_AVISO else "OK"
        log(f"[{nivel}] Tamaño de .git tras el commit: {_mb(tam)} "
            f"(aviso desde {_mb(GIT_AVISO)}, techo {_mb(GIT_TECHO)}).")
    return True


# ─── MAIN ─────────────────────────────────────────────────────────────────
def _al_interrumpir(signum, frame):
    """SIGTERM (Codespaces parando la celda) se trata como Ctrl+C."""
    raise KeyboardInterrupt


def parse_args():
    p = argparse.ArgumentParser(description="Servidor de Minecraft Paper + túnel público")
    p.add_argument("--puerto", type=int, default=PUERTO_MC, help="Puerto del servidor (25565)")
    p.add_argument("--memoria", default=MEMORIA_MAXIMA, help="Memoria máxima, p. ej. 3G")
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
        else:
            descargar_paper()
    else:
        log(f"Ya existe un servidor ({SERVER_JAR}).")
    aceptar_eula()
    if nuevo:
        generar_plugins_dir()
    instalar_plugins(forzar=args.reinstalar_plugins)
    return True


def parar_todo() -> int:
    """Cierra el túnel y para el servidor usando el grupo de procesos.

    Sin esto no había forma fiable de pararlos: `kill $(cat tunel.pid)` mata
    al envoltorio npm y deja vivo el proceso node de Asyx.
    """
    titulo("PARANDO TÚNEL Y SERVIDOR")
    pid = _leer_pid(PID_TUNEL)
    if pid and _vivo(pid):
        log(f"Cerrando túnel (PID {pid})...")
        _matar_pid(pid, espera=10)
        ok("Túnel cerrado.")
    else:
        log("No hay túnel activo.")
    _limpiar_pid(PID_TUNEL)

    pid = _leer_pid(PID_SERVIDOR)
    if pid and _vivo(pid):
        log(f"Parando servidor (PID {pid}); Minecraft guarda el mundo...")
        _matar_pid(pid, espera=60)
        ok("Servidor parado.")
    else:
        log("No hay servidor activo.")
    _limpiar_pid(PID_SERVIDOR)
    return 0


def main():
    global MEMORIA_INICIAL, MEMORIA_MAXIMA
    args = parse_args()
    if args.parar:
        return parar_todo()
    MEMORIA_MAXIMA = args.memoria
    if MEMORIA_INICIAL.endswith("G") and args.memoria.endswith("G"):
        MEMORIA_INICIAL = f"{max(1, int(args.memoria[:-1]) // 2)}G"
    try:
        signal.signal(signal.SIGTERM, _al_interrumpir)
    except (ValueError, OSError):
        pass

    titulo("SETUP SERVIDOR MINECRAFT PAPER + TÚNEL PÚBLICO")
    log(f"Directorio del servidor (raíz local): {SERVER_DIR}")
    log(f"JAR del servidor: {SERVER_JAR}")

    # 0. Requisitos y secretos: si .env no está oculto, se cancela aquí
    version_java = comprobar_java()
    ok(f"Java {version_java} detectado.")
    configurar_gitignore()
    exigir_env_protegido()

    # 0a. Alta de Asyx (registro en el navegador del usuario)
    if args.registrar_asyx:
        if asyx_esta_alto():
            ok("Asyx ya está dado de alta; no hace falta registrarlo.")
            return 0
        if not sys.stdin.isatty():
            error("El alta de Asyx necesita una terminal: ejecuta el script sin "
                  "redirigir la entrada.")
            return 1
        return 0 if asyx_registrar() else 1

    # 0b. Diagnóstico del túnel
    if args.tunel_diag:
        return diagnostico_tunel()

    # 0bis. Comprobación rápida del webhook (falla en 1s si está muerto)
    if args.probar_webhook:
        webhook = obtener_webhook()
        if not webhook:
            return 1
        return 0 if enviar_a_discord(webhook, (
            "✅ **Prueba de webhook**\n"
            "El script se está comunicando bien con Discord. "
            "Cuando arranque el servidor aquí irá la IP del túnel.")) else 1

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

        # 4. Discord (siempre: con la IP o con el motivo del fallo)
        motivo = MOTIVO_FALLO_TUNEL
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
