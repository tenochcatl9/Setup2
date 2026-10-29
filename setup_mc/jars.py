"""Descarga y configuración de los plugins del servidor."""

import hashlib, io, json, os, re, tempfile, time, zipfile
from pathlib import Path
import requests
from . import panel
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

PLUGINS_PROPIOS = {
    "permadeath": "Permadeath.jar",
    "combat-basics": "CombatBasics.jar",
}

# SkinsRestorer se publica en Modrinth (no en el release de GitHub del proyecto).
PROYECTO_SKINSRESTORER = "skinsrestorer"

# Se pide en este orden: la primera coincidencia gana.
CARGA_SKINSRESTORER = ["paper", "purpur", "spigot", "bukkit"]

# Última versión instalada de cada plugin que se reza actualiza; comparte
# carpeta con ESTADO_RESPALDO (~/.local/state/setup_mc/).
ESTADO_PLUGINS = Path.home() / ".local" / "state" / "setup_mc" / "plugins.json"

# Lista de plugins por defecto. El panel puede sobrescribirla sin tocar el
# código (POST /api/plugins): aquí solo queda lo que se instala si no hay
# panel o la API no responde.
PLUGINS_DEFECTO = [
    {"id": "viaversion", "nombre": PLUGIN_VIAVERSION, "fuente": "github",
     "repo": "ViaVersion/ViaVersion", "en_cada_ejecucion": False},
    {"id": "viabackwards", "nombre": PLUGIN_VIABACKWARDS, "fuente": "github",
     "repo": "ViaVersion/ViaBackwards", "en_cada_ejecucion": False},
    {"id": "authme", "nombre": PLUGIN_AUTHME, "fuente": "github",
     "repo": "AuthMe/AuthMeReloaded", "preferir": "-Paper.jar|AuthMe.jar",
     "en_cada_ejecucion": False},
    {"id": "skinsrestorer", "nombre": PLUGIN_SKINSRESTORER, "fuente": "modrinth",
     "proyecto": PROYECTO_SKINSRESTORER, "carga": CARGA_SKINSRESTORER,
     "en_cada_ejecucion": True},
]

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

def actualizar_plugin_modrinth(proyecto: str, nombre_archivo: str, carga: list) -> str:
    """Deja siempre la última versión de un plugin de Modrinth.

    Se compara el sha1 del jar local con el que publica Modrinth: así se
    detectan versiones nuevas sin volver a bajar el jar cuando nada cambió.
    """
    destino = PLUGINS_DIR / nombre_archivo
    carga = [c for c in carga if c] or CARGA_SKINSRESTORER
    cabeceras = {"User-Agent": "setup-mc (respaldo servidor minecraft)"}
    url = f"https://api.modrinth.com/v2/project/{proyecto}/version"
    r = requests.get(url, params={"loaders": json.dumps(carga)},
                     headers=cabeceras, timeout=25)
    r.raise_for_status()
    versiones = [v for v in r.json() if v.get("files")]
    if not versiones:
        raise RuntimeError(f"Modrinth no devolvió ninguna versión de {proyecto}.")
    version = versiones[0]
    archivo = version["files"][0]
    sha1 = archivo["hashes"]["sha1"]
    if destino.is_file() and _sha1_archivo(destino) == sha1:
        log(f"{nombre_archivo} ya está al día: v{version['version_number']}.")
        return version["version_number"]
    barra = Barra(f"Descargando {nombre_archivo} {version['version_number']}",
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
        raise RuntimeError(f"El jar de {nombre_archivo} no coincide con su sha1 en Modrinth.")
    temporal.replace(destino)
    barra.finalizar(f"{nombre_archivo} v{version['version_number']} en {destino}")
    _guardar_estado_plugins(proyecto, {
        "version": version["version_number"],
        "sha1": sha1,
        "actualizado": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    return version["version_number"]


def actualizar_skinsrestorer() -> str:
    """Compatibilidad: SkinsRestorer es un plugin de Modrinth más."""
    return actualizar_plugin_modrinth(PROYECTO_SKINSRESTORER, PLUGIN_SKINSRESTORER,
                                      CARGA_SKINSRESTORER)

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

def _plugins_del_panel() -> list:
    """Lista de plugins del panel; si no hay API disponible, la de siempre."""
    lista = panel.lista_plugins()
    if lista is None:
        if panel.hay_panel():
            aviso("No se pudo leer la lista de plugins del panel; "
                  "se usan los plugins de siempre.")
        return PLUGINS_DEFECTO
    log(f"Panel: lista de plugins cargada de la API ({len(lista)}).")
    return lista


def instalar_plugins(forzar: bool = False) -> None:
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    versiones = {}
    for p in _plugins_del_panel():
        fuente = str(p.get("fuente") or "github")
        nombre_archivo = str(p["nombre"])
        try:
            if fuente == "modrinth":
                versiones[nombre_archivo] = actualizar_plugin_modrinth(
                    str(p["proyecto"]), nombre_archivo, list(p.get("carga") or []))
            else:
                descargar_plugin_github(str(p["repo"]), nombre_archivo,
                                        forzar or bool(p.get("en_cada_ejecucion")),
                                        str(p.get("preferir") or ""))
        except (requests.RequestException, RuntimeError, KeyError, TypeError) as exc:
            aviso(f"No se pudo instalar {nombre_archivo}: {exc}")
    configurar_authme()
    # SkinsRestorer (si está en la lista) sí se revisa en cada ejecución: cambia
    # a menudo y sin avisos de versión nueva. Si Modrinth falla, el servidor
    # arranca igual.
    version_skin = versiones.get(PLUGIN_SKINSRESTORER) or version_skinsrestorer()
    ok(f"Plugins listos: {rango_versiones()} vía ViaVersion/ViaBackwards, "
       f"login con AuthMe y SkinsRestorer {version_skin}.")


def actualizar_plugins_propios() -> None:
    """Actualiza los plugins propios instalados antes de arrancar Paper."""
    from .pagina import pagina_config

    url, _proyecto = pagina_config()
    if not url.startswith("https://"):
        aviso("No se revisaron las actualizaciones de plugins: la página no usa HTTPS.")
        return
    try:
        respuesta = requests.get(
            f"{url.rstrip('/')}/plugins/manifest.json",
            headers={"User-Agent": "setup-mc-plugin-updater"},
            timeout=15,
        )
        respuesta.raise_for_status()
        manifest = respuesta.json()
    except (requests.RequestException, ValueError) as exc:
        aviso(f"No se pudieron consultar actualizaciones de plugins: {exc}")
        return
    if not isinstance(manifest, dict) or not isinstance(manifest.get("plugins"), list):
        aviso("No se pudieron consultar actualizaciones: el catálogo no es válido.")
        return

    disponibles = {
        plugin.get("id"): plugin for plugin in manifest["plugins"]
        if isinstance(plugin, dict) and plugin.get("id") in PLUGINS_PROPIOS
    }
    for plugin_id, nombre in PLUGINS_PROPIOS.items():
        destino = PLUGINS_DIR / nombre
        plugin = disponibles.get(plugin_id)
        if plugin is None or not destino.is_file():
            continue
        digest = str(plugin.get("sha256") or "")
        archivo = str(plugin.get("archivo") or "")
        if (archivo != nombre or not re.fullmatch(r"[a-fA-F0-9]{64}", digest)):
            aviso(f"Se omitió la actualización de {nombre}: catálogo inválido.")
            continue
        try:
            version_instalada = ""
            try:
                with zipfile.ZipFile(destino) as instalado:
                    manifiestos = {"plugin.yml", "paper-plugin.yml"} & set(instalado.namelist())
                    if manifiestos:
                        texto = instalado.read(sorted(manifiestos)[0]).decode("utf-8")
                        encontrada = re.search(r"(?m)^version:\s*['\"]?([^'\"\s]+)", texto)
                        version_instalada = encontrada.group(1) if encontrada else ""
            except (zipfile.BadZipFile, UnicodeDecodeError):
                pass
            version_publicada = str(plugin.get("version") or "")
            version_pattern = r"\d+(?:\.\d+)*"
            if not re.fullmatch(version_pattern, version_publicada):
                raise RuntimeError("no se pudo leer una versión semántica del plugin.")
            if re.fullmatch(version_pattern, version_instalada):
                actual_parts = tuple(map(int, version_instalada.split(".")))
                nueva_parts = tuple(map(int, version_publicada.split(".")))
                longitud = max(len(actual_parts), len(nueva_parts))
                actual_parts += (0,) * (longitud - len(actual_parts))
                nueva_parts += (0,) * (longitud - len(nueva_parts))
                if nueva_parts < actual_parts:
                    log(f"{nombre} local v{version_instalada} es más reciente que el catálogo "
                        f"v{version_publicada}; no se reemplaza.")
                    continue
            actual = hashlib.sha256(destino.read_bytes()).hexdigest()
            if actual.lower() == digest.lower():
                continue
            chunks = []
            total = 0
            with requests.get(
                    f"{url.rstrip('/')}/plugins/{nombre}",
                    headers={"User-Agent": "setup-mc-plugin-updater"},
                    timeout=60, stream=True) as descarga:
                descarga.raise_for_status()
                for chunk in descarga.iter_content(64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > 100 * 1024 * 1024:
                        raise RuntimeError("el JAR supera los 100 MB.")
                    chunks.append(chunk)
            datos = b"".join(chunks)
            if not datos:
                raise RuntimeError("el JAR está vacío.")
            if hashlib.sha256(datos).hexdigest().lower() != digest.lower():
                raise RuntimeError("el SHA-256 no coincide con el catálogo.")
            with zipfile.ZipFile(io.BytesIO(datos)) as jar:
                if "plugin.yml" not in jar.namelist():
                    raise RuntimeError("el JAR no contiene plugin.yml.")
                descriptor = jar.read("plugin.yml").decode("utf-8")
            if not re.search(
                    rf"(?m)^version:\s*['\"]?{re.escape(version_publicada)}['\"]?\s*$",
                    descriptor):
                raise RuntimeError("la versión declarada no coincide con el catálogo.")
            with tempfile.NamedTemporaryFile(
                    dir=PLUGINS_DIR, prefix=".plugin-update-", suffix=".part",
                    delete=False) as temporal:
                temporal.write(datos)
                temporal_path = Path(temporal.name)
            try:
                os.replace(temporal_path, destino)
            finally:
                temporal_path.unlink(missing_ok=True)
            ok(f"{nombre} actualizado a v{version_publicada}; se cargará al arrancar Paper.")
        except (OSError, requests.RequestException, RuntimeError,
                zipfile.BadZipFile, UnicodeDecodeError) as exc:
            aviso(f"No se pudo actualizar {nombre}; se conserva la versión instalada: {exc}")
