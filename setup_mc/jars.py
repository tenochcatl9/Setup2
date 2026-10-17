"""Descarga y configuración de los plugins del servidor."""

import hashlib, json, re, time
from pathlib import Path
import requests
from .config import PLUGINS_DIR
from .ui import Barra, aviso, log, ok
from .versiones import rango_versiones

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
