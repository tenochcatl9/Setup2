"""Respaldo comprimido, releases de GitHub, importación y push."""

import fnmatch, hashlib, json, os, re, shutil, signal, subprocess, sys, tempfile, threading, time
from pathlib import Path
import requests
from .config import BASE_DIR, FIFO_CONSOLA, GITIGNORE_FILE, LOGS_DIR, MANIFIESTO, RESPALDO_DIR, RE_SECRETO, SERVER_DIR, USER_AGENT
from .ui import Barra, aviso, error, log, ok, ruta_sensible
from .procesos import puerto_abierto
from .versiones import _version_mc_del_jar
from .gitops import _git, es_repo_git, verificar_env_protegido
from .avisos import enviar_a_discord, obtener_webhook

# ─── 13. Respaldo comprimido e importación ───────────────────────────────
# Rutas que se empaquetan (relativas a SERVER_DIR); el resto se regenera al importar.
# Lo que se versiona en git: plugins, configuración y datos de Paper. Entra en
# ~20 MB, así que el repositorio lo aguanta siempre. El mundo, que ya no cabe,
# va aparte a la release.
BACKUP_INCLUYE_INSTALACION = ["plugins", "config", ".paper", "server.properties",
                              "bukkit.yml", "spigot.yml", "eula.txt"]

BACKUP_INCLUYE = ["server.jar", "world", "plugins", "config", ".paper",
                  "server.properties", "eula.txt", "ops.json", "whitelist.json",
                  "banned-players.json", "banned-ips.json", "usercache.json",
                  "bukkit.yml", "spigot.yml", "commands.yml"]

# El respaldo del mundo va a una release PÚBLICA, así que fuera de aquí no
# entran las cuentas: plugins/AuthMe/authme.db lleva usuario, hash de
# contraseña e IP de cada jugador. Quien lo baje podría atacarlos por fuerza
# bruta. Si prefieres conservarlo, haz el repositorio privado.
BACKUP_EXCLUYE = [".env", ".env.*", ".git", "*.pid", "*.log", "*.part", "consola",
                  "libraries", "cache", "respaldo", "__pycache__",
                  "plugins/AuthMe/*.db", "plugins/AuthMe/*.db-wal",
                  "plugins/AuthMe/*.db-shm"]

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

# Solo "owner/repo" a secas: una URL de release
# (.../releases/download/world/...) tiene más segmentos y debe tratarse como
# descarga directa, no como repositorio (si no, busca el tarball en la rama).
RE_GITHUB = re.compile(r"^https?://(?:www\.)?github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", re.I)

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

DIR_BACKUP_AUTO = RESPALDO_DIR   # van junto al resto, en respaldo/

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

_BACKUP_AVISADO = False          # ya se avisó del primer respaldo de esta corrida

def _avisar_backup(mensaje: str) -> None:
    """Notifica a Discord, pero como mucho una vez por corrida (y al fallar).

    Avisar en cada copia sería un mensaje cada 20 minutos, día y noche; con
    avisar al primer respaldo ya se confirma que funcionan, y si alguno falla
    sí que hay que enterarse (antes los fallos pasaban inadvertidos).
    """
    global _BACKUP_AVISADO
    if _BACKUP_AVISADO and "fall" not in mensaje.lower():
        return
    webhook = obtener_webhook()
    if not webhook:
        return
    if enviar_a_discord(webhook, mensaje):
        _BACKUP_AVISADO = True

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
        ok(f"Respaldo automático: respaldo/{destino.name} "
           f"({destino.stat().st_size / 1048576:,.0f} MB)")
        _purgar_backups_auto()
        _avisar_backup(
            "**Respaldo automático hecho**\n"
            f"`respaldo/{destino.name}` — "
            f"{destino.stat().st_size / 1048576:,.0f} MB\n"
            f"Se sigue haciendo uno cada {BACKUP_AUTO_MINUTOS} min sin parar el "
            f"servidor (se guardan los {BACKUP_AUTO_MANTENER} últimos).")
        return True
    except (OSError, RuntimeError) as e:
        error(f"No se pudo hacer el respaldo automático: {e}")
        _avisar_backup(f"**Falló el respaldo automático**\n{e}")
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

MARCA_MUNDO_EN_RELEASE = ("# mundo en la release (no cabe en git); "
                          "se quita solo en cuanto vuelve a caber")

def _en_release(activo: bool) -> None:
    """Ignora el tarball del mundo solo mientras vive en la release.

    No se deja puesto para siempre: en cuanto el mundo vuelve a caber en git (por
    ejemplo tras vaciar el mundo), se quita la línea y se vuelve a versionar.
    """
    rel = _relativo(_paquete_respaldo())
    lineas = [l for l in GITIGNORE_FILE.read_text().splitlines()
              if l.strip() != rel and l.strip() != MARCA_MUNDO_EN_RELEASE]
    if activo:
        lineas += ["", MARCA_MUNDO_EN_RELEASE, rel]
        # Y sale del índice: si se quedara versionado, el push lo rechazaría
        # GitHub por pasar de 100 MB.
        _git(["rm", "--cached", "-q", "-f", rel])
    GITIGNORE_FILE.write_text("\n".join(lineas).rstrip("\n") + "\n")
    if not activo:
        _git(["add", "-f", rel])

def crear_respaldo_instalacion() -> bool:
    """Empaqueta plugins + configuración (~20 MB) en respaldo/instalacion.tar.zst.

    Este sí se versiona en git: el mundo ya no cabe (160 MB) y se va a una
    release, pero el resto de la instalación del servidor cabe de sobra y así
    queda en el repositorio, que es lo que se puede ver y recuperar.
    """
    destino = RESPALDO_DIR / "instalacion.tar.zst"
    incluir = [q for q in BACKUP_INCLUYE_INSTALACION
               if (SERVER_DIR / q).exists()]
    if not incluir:
        return False
    archivos, hash_contenido = _hash_contenido(SERVER_DIR, incluir)
    manifiesto = {
        "creado": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "paper": _version_paper() or "desconocida",
        "minecraft": _version_mc_del_jar() or "desconocida",
        "solo_instalacion": True,
        "contenido_sha256": hash_contenido,
        "archivos": archivos,
    }
    try:
        _empaquetar(destino, manifiesto, incluir)
    except RuntimeError as e:
        error(str(e))
        return False
    if MANIFIESTO not in _miembros_de_respaldo(destino):
        destino.unlink(missing_ok=True)
        error("El respaldo de instalación no lleva manifiesto; se borra.")
        return False
    ok(f"Respaldo de instalación: {destino.stat().st_size / 1048576:,.0f} MB "
       f"(plugins y configuración, se versiona en git)")
    return True

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
            log("    Se deja el respaldo solo en disco, en respaldo/.")
            return False
        _en_release(True)
        ok(f"Mundo publicado en la release: {enlace}")
        log("    Esa URL no cambia: siempre da el mundo más reciente, y el")
        log("    import la baja automáticamente si el repositorio no lo trae.")
        RESPALDO_ACTUAL = None
        return True
    crear_respaldo_instalacion()
    # Si antes vivía en la release y ya cabe, se vuelve a meter en git.
    _en_release(False)
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
    """Pide de dónde sacar el mundo. Viene con la URL ya puesta."""
    print()
    print("  Si el mundo no cabe en git se publica como release, y esta es la")
    print("  dirección (no cambia nunca, siempre da el mundo más reciente):")
    print("    - owner/repo        p. ej. tenochcatl9/Setup2")
    print("    - URL del repo      https://github.com/owner/repo")
    print("    - URL directa       .../releases/download/world/servidor-mc.tar.zst")
    por_defecto = url_mundo() or NOMBRES_RESPALDO[0]
    try:
        respuesta = input(f"  URL o owner/repo [{por_defecto}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    return respuesta or por_defecto

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
