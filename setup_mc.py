#!/usr/bin/env python3
"""
Script de despliegue para servidor de Minecraft Paper en Codespaces.

- Verifica que .env esté oculto en .gitignore. Si no lo está, CANCELA el script.
- El servidor vive en la raíz del proyecto (local), no dentro de una carpeta.
- Detecta si ya existe el servidor (server.jar).
- Si no existe, descarga la última versión estable de Paper desde la API v3
  (con barra de progreso).
- Ejecuta el servidor una vez para generar la carpeta plugins y lo detiene.
- Descarga e instala los plugins ViaVersion y ViaBackwards.
- Levanta el servidor y espera a que escuche en el puerto 25565.
- Expone el puerto con QuickTunnel mostrando una barra de carga.
- Envía la dirección pública a un webhook de Discord.
- Al terminar, sube los cambios a la rama principal del repositorio.

Modos:
  python3 setup_mc.py                  flujo completo (servidor en primer plano)
  python3 setup_mc.py --solo-notificar deja servidor y túnel en segundo plano,
                                       avisa a Discord con la IP y sube a Git
"""

import argparse
import itertools
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
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

PAPER_API = "https://fill.papermc.io/v3/projects/paper"
USER_AGENT = "CodeSpace-MC-Setup/1.0 (contact: tu@email.com)"

QUICKTUNNEL_HOST = "t.tn3w.dev"
PUERTO_MC = 25565
PUERTO_TUNEL = 80
MEMORIA_INICIAL = "1G"
MEMORIA_MAXIMA = "2G"
TIMEOUT_TUNEL = 60
TIMEOUT_ARRANQUE = 240

INICIO_GITIGNORE = "# >>> INICIO bloque gestionado por setup_mc.py >>>"
FIN_GITIGNORE = "# <<< FIN bloque gestionado por setup_mc.py <<<"

RE_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
RE_VERSION_ESTABLE = re.compile(r"^\d+\.\d+(\.\d+)?$")
SUFIJOS_TUNEL = ("lhr.life", "localhost.run", "sslip.io", "tn3w.dev", "lhr.me")
RE_URL_TUNEL = re.compile(
    r"https?://([A-Za-z0-9][A-Za-z0-9.-]*\.(?:" +
    "|".join(re.escape(s) for s in SUFIJOS_TUNEL) + r"))", re.IGNORECASE)
RE_URL_CUALQUIERA = re.compile(r"https?://([A-Za-z0-9][A-Za-z0-9-]*\.[A-Za-z]{2,}(?:\.[A-Za-z]{2,})?)")

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


def comprobar_ssh() -> bool:
    return shutil.which("ssh") is not None


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
    finally:
        _detener_proceso(proc, escribir_stop=True)
        log("Servidor detenido.")


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
        try:
            proc.terminate()
        except (OSError, AttributeError, ValueError):
            pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        aviso("El proceso no se detuvo a tiempo; forzando cierre...")
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


# ─── 5. Descargar plugins (ViaVersion + ViaBackwards) ────────────────────
def descargar_plugin_github(repo: str, nombre_archivo: str, forzar: bool = False):
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
    jar_asset = None
    for a in release.get("assets", []):
        nombre = a.get("name", "")
        if nombre.endswith(".jar") and "sources" not in nombre and "javadoc" not in nombre:
            jar_asset = a
            break
    if not jar_asset:
        raise RuntimeError(f"No se encontró un .jar en el último release de {repo}.")
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


def instalar_plugins(forzar: bool = False):
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    descargar_plugin_github("ViaVersion/ViaVersion", "ViaVersion.jar", forzar)
    descargar_plugin_github("ViaVersion/ViaBackwards", "ViaBackwards.jar", forzar)


# ─── 6. .gitignore y protección de secretos ───────────────────────────────
def _contenido_gitignore() -> str:
    return "\n".join([
        INICIO_GITIGNORE,
        "# Secretos: nunca deben llegar a un commit",
        ".env",
        ".env.*",
        "!.env.example",
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


def obtener_webhook() -> str:
    url = leer_webhook()
    if url:
        log("Webhook de Discord cargado desde .env")
        return url
    print()
    print("=" * 62)
    print("No hay webhook de Discord configurado.")
    print("Créalo en: Configuración del canal → Integraciones → Webhooks → Nueva webhook")
    print("=" * 62)
    try:
        url = input("Pega aquí la URL del webhook (Enter para omitir): ").strip()
    except (EOFError, KeyboardInterrupt):
        aviso("Sin webhook: se continuará sin notificar a Discord.")
        return ""
    if not url:
        aviso("Sin webhook: se continuará sin notificar a Discord.")
        return ""
    if not _webhook_valido(url):
        aviso("Esa URL no parece un webhook de Discord; se guardará pero no se enviará nada.")
    guardar_webhook(url)
    return url


def enviar_a_discord(webhook_url: str, mensaje: str):
    if not webhook_url:
        return
    try:
        r = requests.post(webhook_url, json={"content": mensaje}, timeout=15)
        r.raise_for_status()
        ok("Mensaje enviado a Discord.")
    except Exception as e:  # noqa: BLE001
        error(f"No se pudo enviar el mensaje a Discord: {e}")


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


def seguir_servidor(proc, cola: queue.Queue):
    """Muestra la consola del servidor hasta que termine o el usuario interrumpa."""
    print()
    print("--- CONSOLA DEL SERVIDOR (Ctrl+C para detener todo) ---")
    while proc.poll() is None:
        _drenar(cola)
        time.sleep(0.3)
    _drenar(cola)
    aviso("El servidor se ha detenido.")


# ─── 9. QuickTunnel con barra de carga ────────────────────────────────────
def cmd_ssh(puerto: int) -> list:
    """Comando de QuickTunnel: redirige el puerto remoto al local."""
    return [
        "ssh", "-N",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-R", f"{PUERTO_TUNEL}:localhost:{puerto}",
        QUICKTUNNEL_HOST,
    ]


def buscar_url(texto: str) -> str:
    """Extrae el host público del túnel de una línea de salida de ssh."""
    m = RE_URL_TUNEL.search(texto) or RE_URL_CUALQUIERA.search(texto)
    return m.group(1) if m else ""


def iniciar_quicktunnel(puerto: int = PUERTO_MC, timeout: int = TIMEOUT_TUNEL) -> tuple:
    """Lanza QuickTunnel vía SSH y devuelve (direccion_mc, proceso).

    Muestra una barra de carga mientras espera la URL pública.
    """
    if not comprobar_ssh():
        error("No se encontró 'ssh' en el PATH. Instala openssh-client para usar QuickTunnel.")
        return "", None
    cmd = cmd_ssh(puerto)
    cola: queue.Queue = queue.Queue()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
    except FileNotFoundError:
        error("No se pudo ejecutar ssh.")
        return "", None
    threading.Thread(target=_bombeo_salida, args=(proc, cola), daemon=True).start()
    barra = Barra(f"Levantando QuickTunnel ({QUICKTUNNEL_HOST})", limite=timeout)
    host_publico = None
    terminado = False
    try:
        while time.time() - barra.inicio < timeout:
            if proc.poll() is not None:
                terminado = True
                break
            try:
                linea = cola.get(timeout=0.1)
            except queue.Empty:
                barra.refrescar()
                continue
            if linea is None:
                terminado = True
                break
            texto = _sin_ansi(linea).strip()
            if texto:
                limpiar_linea()
                print(f"    {texto}")
            host_publico = buscar_url(texto)
            if host_publico:
                break
        _drenar(cola)
    finally:
        barra.finalizar("QuickTunnel activo" if host_publico else None)
    if not host_publico:
        if terminado:
            error("QuickTunnel terminó antes de dar la URL (¿sin salida a internet o puerto 22 bloqueado?).")
        else:
            error(f"No se obtuvo la URL de QuickTunnel en {timeout} segundos.")
        _detener_proceso(proc, escribir_stop=False, timeout=5)
        return "", None
    direccion_mc = f"{host_publico}:{PUERTO_TUNEL}"
    ok(f"Dirección para Minecraft: {direccion_mc}")
    return direccion_mc, proc


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
    log("Cerrando QuickTunnel...")
    _detener_proceso(proc, escribir_stop=False, timeout=8)


# ─── 10. Modo "--solo-notificar": procesos en segundo plano ───────────────
def _leer_pid(ruta: Path):
    try:
        return int(ruta.read_text().strip())
    except (OSError, ValueError):
        return None


def _vivo(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError, PermissionError):
        return False


def _matar_pid(pid, espera: float = 5.0) -> bool:
    if not _vivo(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    fin = time.time() + espera
    while time.time() < fin:
        if not _vivo(pid):
            return True
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    return True


def _limpiar_pid(ruta: Path):
    try:
        ruta.unlink()
    except OSError:
        pass


def _abrir_log(ruta: Path):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    f = open(ruta, "a", buffering=1)
    f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    return f


def url_en_log(ruta: Path) -> str:
    """Última URL de túnel que aparece en el log de ssh."""
    try:
        texto = ruta.read_text(errors="replace")
    except OSError:
        return ""
    for patron in (RE_URL_TUNEL, RE_URL_CUALQUIERA):
        encontrados = patron.findall(texto)
        if encontrados:
            return encontrados[-1]
    return ""


def arrancar_servidor_detached(puerto: int):
    """Arranca el servidor desacoplado: sobrevive a la salida del script."""
    log(f"Arrancando servidor en segundo plano (puerto {puerto})...")
    mango = _abrir_log(LOG_SERVIDOR)
    try:
        proc = subprocess.Popen(
            cmd_servidor(puerto), cwd=str(SERVER_DIR), stdin=subprocess.DEVNULL,
            stdout=mango, stderr=subprocess.STDOUT, start_new_session=True,
        )
    except FileNotFoundError:
        raise RuntimeError("No se encontró 'java' en el PATH.")
    finally:
        mango.close()
    PID_SERVIDOR.write_text(f"{proc.pid}\n")
    return proc


def iniciar_tunel_detached(puerto: int, timeout: int = TIMEOUT_TUNEL) -> tuple:
    """QuickTunnel desacoplado. La URL se lee del log porque no hay tubería."""
    cmd = cmd_ssh(puerto)
    if shutil.which("stdbuf"):
        cmd = ["stdbuf", "-oL", "-eL", *cmd]
    mango = _abrir_log(LOG_TUNEL)
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=mango, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except FileNotFoundError:
        raise RuntimeError("No se encontró 'ssh' en el PATH.")
    finally:
        mango.close()
    PID_TUNEL.write_text(f"{proc.pid}\n")
    barra = Barra(f"Levantando QuickTunnel ({QUICKTUNNEL_HOST})", limite=timeout)
    try:
        while time.time() - barra.inicio < timeout:
            if proc.poll() is not None:
                barra.finalizar()
                detalle = LOG_TUNEL.read_text(errors="replace").strip() if LOG_TUNEL.is_file() else ""
                error("QuickTunnel terminó antes de dar la URL:\n" +
                      (detalle[-400:] or "(sin salida)"))
                _limpiar_pid(PID_TUNEL)
                return "", None
            host = url_en_log(LOG_TUNEL)
            if host:
                barra.finalizar("QuickTunnel activo")
                return f"{host}:{PUERTO_TUNEL}", proc
            barra.refrescar()
            time.sleep(0.2)
        barra.finalizar()
        error(f"No se obtuvo la URL de QuickTunnel en {timeout} segundos.")
        _matar_pid(proc.pid)
        _limpiar_pid(PID_TUNEL)
        return "", None
    finally:
        barra.finalizar()


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
        ok(f"Servidor en segundo plano (PID {proc_srv.pid}, log: {LOG_SERVIDOR}).")

    # 2. Túnel
    direccion = ""
    pid_tunel = _leer_pid(PID_TUNEL)
    if _vivo(pid_tunel):
        host = url_en_log(LOG_TUNEL)
        if host and comprobar_tunel(host):
            direccion = f"{host}:{PUERTO_TUNEL}"
            ok(f"QuickTunnel anterior sigue vivo (PID {pid_tunel}): {direccion}")
    if not direccion:
        _parar_anterior(PID_TUNEL, "túnel")
        if args.sin_tunnel:
            aviso("QuickTunnel omitido (--sin-tunnel): la IP no será pública.")
        else:
            direccion, _proc = iniciar_tunel_detached(args.puerto)
            if direccion:
                if comprobar_tunel(direccion.rsplit(":", 1)[0]):
                    ok("Túnel verificado: el puerto público responde.")
                else:
                    aviso("El túnel aún no responde; puede tardar unos segundos más.")

    # 3. Discord
    webhook = obtener_webhook()
    if direccion:
        enviar_a_discord(webhook, (
            "🎮 **Servidor de Minecraft activo**\n"
            f"IP: `{direccion}`\n"
            "Entra con cualquier versión de 1.8 a 1.21: ViaVersion y "
            "ViaBackwards hacen de traductor."
        ))
    else:
        aviso("Sin IP pública no hay nada que anunciar en Discord.")

    # 4. Panel con cómo pararlo
    print()
    print("=" * 62)
    print("  SERVIDOR Y TÚNEL EN SEGUNDO PLANO")
    print("=" * 62)
    print(f"  IP pública    : {direccion or '(sin túnel)'}")
    print(f"  Servidor PID  : {_leer_pid(PID_SERVIDOR) or '?'}   (log: {LOG_SERVIDOR})")
    print(f"  Túnel PID     : {_leer_pid(PID_TUNEL) or '?'}   (log: {LOG_TUNEL})")
    print()
    print("  Ver la consola  :  tail -f logs/servidor.log")
    print("  Cerrar túnel    :  kill $(cat tunel.pid)    # la IP deja de servir")
    print("  Parar servidor  :  kill $(cat servidor.pid)")
    print("=" * 62)
    print()

    # 5. Git
    if args.sin_push:
        aviso("Push omitido (--sin-push).")
    else:
        titulo("SUBIENDO CAMBIOS A LA RAMA PRINCIPAL")
        if not subir_cambios(
                "chore: modo --solo-notificar (servidor y túnel en segundo plano)",
                rama=args.rama):
            codigo = 1
    return codigo


# ─── 11. Commit y push a la rama principal ───────────────────────────────
def subir_cambios(mensaje: str, rama: str = "main") -> bool:
    """Verifica secretos, hace commit y empuja a la rama principal."""
    if not es_repo_git():
        aviso("No es un repositorio Git: no se puede subir nada.")
        return False
    bien, motivo = verificar_env_protegido()
    if not bien:
        error("No se sube nada porque la protección de secretos falló:")
        print(motivo, file=sys.stderr)
        return False
    _git(["add", "-A"])
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
        error(f"No se pudo hacer push a {rama}:\n{(r.stderr or r.stdout).strip()}")
        aviso(f"Resuélvelo manualmente con:  git pull --rebase origin {rama} && "
              f"git push origin HEAD:{rama}")
        return False
    ok(f"Cambios subidos a origin/{rama} ({hash_commit}).")
    return True


# ─── MAIN ─────────────────────────────────────────────────────────────────
def _al_interrumpir(signum, frame):
    """SIGTERM (Codespaces parando la celda) se trata como Ctrl+C."""
    raise KeyboardInterrupt


def parse_args():
    p = argparse.ArgumentParser(description="Servidor de Minecraft Paper + QuickTunnel")
    p.add_argument("--puerto", type=int, default=PUERTO_MC, help="Puerto del servidor (25565)")
    p.add_argument("--memoria", default=MEMORIA_MAXIMA, help="Memoria máxima, p. ej. 3G")
    p.add_argument("--sin-tunnel", action="store_true", help="No levantar QuickTunnel")
    p.add_argument("--sin-push", action="store_true", help="No hacer commit/push al final")
    p.add_argument("--solo-notificar", action="store_true",
                   help="Deja servidor y túnel en segundo plano, avisa a Discord y sube a Git")
    p.add_argument("--reinstalar-plugins", action="store_true", help="Volver a descargar los plugins")
    p.add_argument("--rama", default="main", help="Rama a la que se empuja (main)")
    return p.parse_args()


def main():
    global MEMORIA_INICIAL, MEMORIA_MAXIMA
    args = parse_args()
    MEMORIA_MAXIMA = args.memoria
    if MEMORIA_INICIAL.endswith("G") and args.memoria.endswith("G"):
        MEMORIA_INICIAL = f"{max(1, int(args.memoria[:-1]) // 2)}G"
    try:
        signal.signal(signal.SIGTERM, _al_interrumpir)
    except (ValueError, OSError):
        pass

    titulo("SETUP SERVIDOR MINECRAFT PAPER + QUICKTUNNEL")
    log(f"Directorio del servidor (raíz local): {SERVER_DIR}")
    log(f"JAR del servidor: {SERVER_JAR}")

    # 0. Requisitos y secretos: si .env no está oculto, se cancela aquí
    version_java = comprobar_java()
    ok(f"Java {version_java} detectado.")
    configurar_gitignore()
    exigir_env_protegido()

    # 1. Servidor
    nuevo = not servidor_existe()
    if nuevo:
        aviso("No se encontró server.jar: se descargará Paper.")
        descargar_paper()
    else:
        log(f"Ya existe un servidor ({SERVER_JAR}).")
    aceptar_eula()
    if nuevo:
        generar_plugins_dir()
    instalar_plugins(forzar=args.reinstalar_plugins)

    servidor_proc = None
    tunel_proc = None
    cola = None
    direccion = ""
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

    try:
        # 2. Servidor en marcha
        servidor_proc, cola = arrancar_servidor(args.puerto)
        if not esperar_puerto(args.puerto, servidor_proc, cola=cola):
            raise RuntimeError(f"El servidor no abrió el puerto {args.puerto} a tiempo.")

        # 3. Túnel con barra de carga
        if not args.sin_tunnel:
            direccion, tunel_proc = iniciar_quicktunnel(args.puerto)
            if direccion:
                host = direccion.rsplit(":", 1)[0]
                if comprobar_tunel(host):
                    ok("Túnel verificado: el puerto público responde.")
                else:
                    aviso("El túnel no responde todavía; puede tardar unos segundos más.")
        else:
            aviso("QuickTunnel omitido (--sin-tunnel).")

        listo_para_subir = True

        # 4. Discord
        webhook = obtener_webhook()
        if direccion:
            enviar_a_discord(webhook, (
                "🎮 **Servidor de Minecraft activo**\n"
                f"IP: `{direccion}`\n"
                "Entra con cualquier versión de 1.8 a 1.21: ViaVersion y "
                "ViaBackwards hacen de traductor."
            ))
        elif not args.sin_tunnel:
            error("Sin QuickTunnel el servidor no es accesible desde Internet.")

        # 5. Panel final
        print()
        print("=" * 62)
        print("  SERVIDOR LISTO")
        print("=" * 62)
        print(f"  Ruta local   : {SERVER_DIR}")
        print(f"  Puerto local : {args.puerto}")
        print(f"  Dirección MC : {direccion or '(sin túnel)'}")
        print("  Presiona Ctrl+C para detener el servidor y cerrar el túnel.")
        print()

        # 6. Consola en primer plano
        seguir_servidor(servidor_proc, cola)
    except KeyboardInterrupt:
        aviso("Interrumpido por el usuario.")
    except Exception as e:  # noqa: BLE001
        error(str(e))
        codigo = 1
    finally:
        if servidor_proc is not None:
            log("Deteniendo el servidor...")
            _detener_proceso(servidor_proc)
        detener_tunel(tunel_proc)
        if args.sin_push:
            aviso("Push omitido (--sin-push).")
        elif listo_para_subir:
            titulo("SUBIENDO CAMBIOS A LA RAMA PRINCIPAL")
            subir_cambios(
                f"chore: servidor Minecraft en la raíz local y túnel QuickTunnel ({args.rama})",
                rama=args.rama,
            )
        else:
            aviso("El setup no llegó a completarse: no se sube nada a Git para no dejar basura.")
    return codigo


if __name__ == "__main__":
    sys.exit(main())
