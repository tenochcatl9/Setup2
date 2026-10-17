"""Cliente del panel (Cloudflare Worker): auto-actualización, latidos y publicación.

Todo es opcional: sin PANEL_URL en el .env, todas las funciones son no-ops y
el script funciona exactamente igual que antes.
"""

import base64
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
import requests
from . import config
from .config import BASE_DIR, LOGS_DIR, WEBHOOK_ENV_FILE
from .ui import Barra, aviso, error, log, ok
from .versiones import _version_mc_del_jar

_VAR_GUARD = "SETUP_MC_ACTUALIZADO"
_ESTADO_LOCAL = Path.home() / ".local" / "state" / "setup_mc"
RE_VERSION_CONFIG = re.compile(r'^VERSION_SCRIPT = "([^"]*)"', re.MULTILINE)


# ─── Configuración (.env) ────────────────────────────────────────────────
def _valor_env(nombre: str) -> str:
    """Lee NOMBRE=valor del .env de la raíz (sin dependencias extra)."""
    try:
        for linea in WEBHOOK_ENV_FILE.read_text().splitlines():
            linea = linea.strip()
            if linea.startswith(nombre + "="):
                return linea.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return os.environ.get(nombre, "")


def config_panel() -> tuple:
    """(url, clave) del panel, tal como están en .env ('' si no hay)."""
    return _valor_env("PANEL_URL").rstrip("/"), _valor_env("PANEL_API_KEY")


def hay_panel() -> bool:
    return bool(config_panel()[0])


def _cabeceras() -> dict:
    _url, clave = config_panel()
    cab = {"User-Agent": "setup-mc-panel-cliente"}
    if clave:
        cab["Authorization"] = f"Bearer {clave}"
    return cab


# ─── Consultas ───────────────────────────────────────────────────────────
def obtener_manifest(timeout: int = 8):
    """Manifest publicado en el panel, o None si no hay panel o no responde."""
    url, _ = config_panel()
    if not url:
        return None
    try:
        r = requests.get(f"{url}/api/manifest", headers=_cabeceras(), timeout=timeout)
        r.raise_for_status()
        datos = r.json()
    except (requests.RequestException, ValueError):
        return None
    return datos if datos and datos.get("version") else None


def lista_plugins(timeout: int = 8):
    """Lista de plugins publicada en el panel, o None si no está disponible."""
    url, _ = config_panel()
    if not url:
        return None
    try:
        r = requests.get(f"{url}/api/plugins", headers=_cabeceras(), timeout=timeout)
        r.raise_for_status()
        datos = r.json()
    except (requests.RequestException, ValueError):
        return None
    lista = datos.get("plugins") if isinstance(datos, dict) else None
    validos = [p for p in (lista or []) if isinstance(p, dict) and p.get("nombre")]
    return validos or None


def latido(online: bool, direccion: str = "", puerto: int = 25565, proveedor: str = "",
           motivo: str = "", timeout: int = 8) -> bool:
    """Manda un único heartbeat al panel (best-effort)."""
    url, _ = config_panel()
    if not url:
        return False
    cuerpo = {
        "online": online,
        "direccion": direccion,
        "puerto": puerto,
        "proveedor": proveedor,
        "motivo": motivo,
        "version_script": config.VERSION_SCRIPT,
        "version_mc": _version_mc_del_jar() or "?",
    }
    try:
        r = requests.post(f"{url}/api/heartbeat", json=cuerpo,
                          headers=_cabeceras(), timeout=timeout)
        return r.ok
    except requests.RequestException:
        return False


# ─── Latidor desacoplado (proceso aparte que vigila al servidor) ─────────
CODIGO_LATIDOR = r'''
import json, os, sys, time, urllib.request
direccion, puerto, proveedor, version_script, version_mc, pid, log_ruta, motivo_base = sys.argv[1:9]
url = os.environ.get("PANEL_LATIDOR_URL", "").rstrip("/")
clave = os.environ.get("PANEL_LATIDOR_CLAVE", "")

def anotar(texto):
    try:
        with open(log_ruta, "a") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + texto + "\n")
    except OSError:
        pass

def enviar(online, motivo=""):
    if not url:
        return
    cuerpo = json.dumps({
        "online": online, "direccion": direccion, "puerto": int(puerto),
        "proveedor": proveedor, "motivo": motivo or motivo_base,
        "version_script": version_script, "version_mc": version_mc,
    }).encode()
    cabeceras = {"Content-Type": "application/json"}
    if clave:
        cabeceras["Authorization"] = "Bearer " + clave
    peticion = urllib.request.Request(url + "/api/heartbeat", data=cuerpo,
                                      method="POST", headers=cabeceras)
    try:
        urllib.request.urlopen(peticion, timeout=8).read()
    except Exception:
        pass

def vivo():
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False

anotar("latidor iniciado (servidor PID %s)" % pid)
while True:
    if not vivo():
        enviar(False, "el servidor se detuvo")
        anotar("el servidor se detuvo: último latido enviado")
        break
    enviar(True)
    anotar("latido online enviado")
    for _ in range(60):  # 60 x 5 s = 5 minutos, vigilando que siga vivo
        time.sleep(5)
        if not vivo():
            break
'''


def lanzar_latidor_desacoplado(pid, direccion: str, puerto: int, proveedor: str,
                               motivo: str = "") -> bool:
    """Arranca un proceso ligero que reporta el estado cada 5 min mientras el
    servidor (PID) siga vivo; al morir manda el aviso de apagado y termina."""
    url, clave = config_panel()
    if not url or not pid:
        return False
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_ruta = LOGS_DIR / "panel.log"
    entorno = dict(os.environ)
    entorno["PANEL_LATIDOR_URL"] = url
    entorno["PANEL_LATIDOR_CLAVE"] = clave
    try:
        subprocess.Popen(
            [sys.executable, "-c", CODIGO_LATIDOR, direccion, str(puerto), proveedor,
             config.VERSION_SCRIPT, _version_mc_del_jar() or "?", str(int(pid)),
             str(log_ruta), motivo],
            cwd=str(BASE_DIR), env=entorno, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError:
        return False


# ─── Actualización del script ────────────────────────────────────────────
def _tupla_version(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)) or (0,)


def _sha256(ruta: Path) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def _miembro_valido(nombre: str) -> bool:
    """Solo setup_mc.py y setup_mc/<algo>.py: nada de rutas raras ni binarios."""
    if ".." in nombre or nombre.startswith(("/", "\\")):
        return False
    partes = Path(nombre).parts
    if partes == ("setup_mc.py",):
        return True
    return len(partes) == 2 and partes[0] == "setup_mc" and partes[1].endswith(".py")


def _descargar_paquete(url: str, destino: Path) -> None:
    with requests.get(url, headers=_cabeceras(), stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0) or None
        barra = Barra("Descargando versión nueva del script", total=total)
        with open(destino, "wb") as f:
            for bloque in r.iter_content(chunk_size=65536):
                if bloque:
                    f.write(bloque)
                    if total:
                        barra.avanzar(len(bloque))
        barra.finalizar("Paquete descargado")


def _archivos_script() -> list:
    """Archivos que forman el script (para empaquetar o respaldar)."""
    archivos = [BASE_DIR / "setup_mc.py"]
    archivos += sorted(p for p in (BASE_DIR / "setup_mc").glob("*.py")
                       if p.name != "__pycache__")
    return [p for p in archivos if p.is_file()]


def _resguardar_version_actual(version: str) -> Path:
    """Copia la versión local a ~/.local/state/setup_mc/script-antiguo-<version>/."""
    destino = _ESTADO_LOCAL / f"script-antiguo-{version or 'desconocida'}"
    if destino.exists():
        shutil.rmtree(destino, ignore_errors=True)
    (destino / "setup_mc").mkdir(parents=True, exist_ok=True)
    for origen in _archivos_script():
        rel = origen.relative_to(BASE_DIR)
        copia = destino / rel
        copia.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origen, copia)
    return destino


def actualizar(forzar: bool = False, reiniciar: bool = True) -> bool:
    """Aplica la versión del panel si es más nueva que la local.

    - Verifica sha256 del paquete y que compile antes de tocar nada.
    - Respalda la versión actual fuera del repo antes de reemplazarla.
    - Con `reiniciar` relanza el script con el código nuevo (os.execv).
    """
    manifest = obtener_manifest()
    if not manifest:
        log("Panel: no hay ninguna versión publicada (o no responde).")
        return False
    remota = str(manifest.get("version") or "")
    sha = str(manifest.get("sha256") or "")
    url = str(manifest.get("url") or "")
    if not (remota and sha and url):
        aviso("Panel: manifest incompleto (faltan version/sha256/url).")
        return False
    local = config.VERSION_SCRIPT
    if not forzar and _tupla_version(remota) <= _tupla_version(local):
        log(f"Panel: el script está al día (local v{local}, panel v{remota}).")
        return False
    changelog = str(manifest.get("changelog") or "").strip()
    log(f"Panel: hay versión nueva v{remota} (local v{local}).")
    if changelog:
        print("    " + changelog.replace("\n", "\n    ")[:600])

    temporal_dir = Path(tempfile.mkdtemp(prefix=".actualizacion-", dir=str(BASE_DIR)))
    try:
        paquete = temporal_dir / "paquete.tar.gz"
        _descargar_paquete(url, paquete)
        real = _sha256(paquete)
        if real != sha:
            error(f"El paquete no coincide con su sha256 (esperado {sha[:16]}…, "
                  f"recibido {real[:16]}…). No se actualiza nada.")
            return False
        extraido = temporal_dir / "contenido"
        extraido.mkdir()
        with tarfile.open(paquete, "r:gz") as tar:
            for miembro in tar.getmembers():
                if miembro.isdir():
                    continue
                if not miembro.isfile() or not _miembro_valido(miembro.name):
                    error(f"El paquete trae un miembro inesperado: {miembro.name}. "
                          "No se actualiza nada.")
                    return False
            tar.extractall(extraido, filter="data")
        archivos_nuevos = [p for p in extraido.rglob("*.py") if p.is_file()]
        if not archivos_nuevos:
            error("El paquete no traía archivos .py. No se actualiza nada.")
            return False
        prueba = subprocess.run(
            [sys.executable, "-m", "py_compile", *map(str, archivos_nuevos)],
            cwd=str(extraido), capture_output=True, text=True)
        if prueba.returncode != 0:
            error("La versión nueva no compila; se conserva la actual:\n"
                  + (prueba.stderr or prueba.stdout).strip()[:500])
            return False
        resguardo = _resguardar_version_actual(local)
        log(f"Versión actual resguardada en {resguardo}")
        for archivo in archivos_nuevos:
            rel = archivo.relative_to(extraido)
            destino = BASE_DIR / rel
            destino.parent.mkdir(parents=True, exist_ok=True)
            os.replace(archivo, destino)
        config.VERSION_SCRIPT = remota
        ok(f"Script actualizado a v{remota} ({len(archivos_nuevos)} archivo/s).")
        if reiniciar:
            os.environ[_VAR_GUARD] = remota
            log("Reiniciando con el código nuevo...")
            os.execv(sys.executable, [sys.executable, *sys.argv])
        return True
    finally:
        shutil.rmtree(temporal_dir, ignore_errors=True)


def actualizar_al_arrancar(manual: bool = False) -> bool:
    """Comprobación de arranque; nunca aborta el script por un fallo del panel."""
    url, _ = config_panel()
    if not url:
        if manual:
            error("El panel no está configurado: añade PANEL_URL al .env.")
        return False
    # La interfaz (html/js/css) va aparte del script: se comprueba siempre, y
    # también cuando el script ya se actualizó en este mismo arranque.
    try:
        actualizar_ui()
    except (requests.RequestException, OSError, ValueError):
        pass
    if not manual and os.environ.get(_VAR_GUARD):
        aviso(f"Panel: ya se actualizó a v{os.environ[_VAR_GUARD]} en este arranque.")
        return False
    try:
        hecho = actualizar()
    except (requests.RequestException, OSError, ValueError, tarfile.TarError) as exc:
        aviso(f"Panel: no se pudo comprobar la actualización ({exc}). "
              "Se continúa con la versión local.")
        return False
    if manual and not hecho:
        ok(f"Panel: no hay nada que actualizar (v{config.VERSION_SCRIPT} al día).")
    return hecho


# ─── Interfaz publicada (webui/ + templates/) ────────────────────────────
# La misma interfaz que sirve el localhost se sube al panel como página
# pública (/) y el script la baja al arrancar si cambió (sha256 por archivo).
RE_UI_HTML = re.compile(r"^[A-Za-z0-9_-]+\.html$")
RE_UI_JS = re.compile(r"^[A-Za-z0-9_-]+\.js$")
RE_UI_CSS = re.compile(r"^templates/[A-Za-z0-9_-]+\.css$")
MAX_UI_BYTES = 512 * 1024


def rutas_ui(base: Path = BASE_DIR) -> dict:
    """{ruta lógica: ruta local} de la interfaz publicable.

    La ruta lógica es la que usa el panel: index.html, app.js y
    templates/<tema>.css (las que referencia la propia página).
    """
    mapa = {}
    for carpeta, sufijos, prefijo in (
            (base / "webui", (".html", ".js"), ""),
            (base / "templates", (".css",), "templates/")):
        if not carpeta.is_dir():
            continue
        for ruta in sorted(carpeta.iterdir()):
            if ruta.is_file() and ruta.suffix in sufijos:
                mapa[f"{prefijo}{ruta.name}"] = ruta
    return mapa


def _destino_ui(logica: str, base: Path = BASE_DIR):
    """Ruta local donde vive (o debe vivir) un archivo lógico del panel."""
    if RE_UI_HTML.fullmatch(logica) or RE_UI_JS.fullmatch(logica):
        return base / "webui" / logica
    if RE_UI_CSS.fullmatch(logica):
        return base / "templates" / logica.split("/", 1)[1]
    return None


def publicar_ui(version: str = "") -> bool:
    """Sube webui/ + templates/ al panel: la página pública y sus archivos."""
    url, clave = config_panel()
    if not url or not clave:
        aviso("Panel: faltan PANEL_URL y/o PANEL_API_KEY en el .env; "
              "no se publica la interfaz.")
        return False
    archivos = {}
    for logica, ruta in rutas_ui().items():
        try:
            archivos[logica] = ruta.read_text(errors="replace")
        except OSError:
            continue
    if not archivos:
        aviso("Panel: no hay interfaz que publicar (¿faltan webui/ y templates/?).")
        return False
    cuerpo = {"version": version or config.VERSION_SCRIPT, "archivos": archivos}
    log(f"Panel: publicando la interfaz ({len(archivos)} archivos)...")
    try:
        r = requests.post(f"{url}/api/ui", json=cuerpo, headers=_cabeceras(), timeout=60)
    except requests.RequestException as exc:
        error(f"Panel: no se pudo publicar la interfaz: {exc}")
        return False
    if r.status_code == 401:
        error("El panel rechazó la clave (PANEL_API_KEY).")
        return False
    if not r.ok:
        error(f"Panel: la interfaz fue rechazada: {r.status_code} {r.text[:300]}")
        return False
    ok(f"Panel: interfaz publicada ({len(archivos)} archivos); se ve en {url}/")
    return True


def actualizar_ui(base: Path | None = None) -> int:
    """Baja la interfaz del panel si cambió (sha256). Nº de archivos actualizados.

    Nunca aborta nada: sin panel, sin red o con respuestas raras se sigue con
    los archivos locales. Antes de reemplazar algo se resguarda fuera del repo.
    """
    base = base or BASE_DIR
    url, _ = config_panel()
    if not url:
        return 0
    try:
        r = requests.get(f"{url}/api/ui", headers=_cabeceras(), timeout=8)
        r.raise_for_status()
        indice = r.json()
    except (requests.RequestException, ValueError):
        return 0
    if not isinstance(indice, dict):
        return 0
    actualizados = 0
    respaldo = _ESTADO_LOCAL / f"ui-antigua-{time.strftime('%Y%m%d-%H%M%S')}"
    for info in indice.get("archivos", []):
        if not isinstance(info, dict):
            continue
        logica = str(info.get("ruta") or "")
        sha = str(info.get("sha256") or "")
        destino = _destino_ui(logica, base)
        if destino is None or not re.fullmatch(r"[0-9a-f]{64}", sha):
            continue
        try:
            if int(info.get("tamano") or 0) > MAX_UI_BYTES:
                continue
        except (TypeError, ValueError):
            continue
        if destino.is_file() and _sha256(destino) == sha:
            continue
        try:
            descarga = requests.get(f"{url}/api/ui/{logica}", headers=_cabeceras(), timeout=30)
            descarga.raise_for_status()
        except requests.RequestException:
            continue
        contenido = descarga.content
        if hashlib.sha256(contenido).hexdigest() != sha:
            aviso(f"Panel: {logica} no coincide con su sha256; se conserva el local.")
            continue
        if destino.is_file():
            copia = respaldo / logica
            try:
                copia.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destino, copia)
            except OSError:
                pass
        try:
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(contenido)
        except OSError as exc:
            aviso(f"Panel: no se pudo escribir {logica}: {exc}")
            continue
        actualizados += 1
        log(f"Panel: interfaz actualizada ({logica}).")
    if actualizados:
        ok(f"Panel: {actualizados} archivo/s de la interfaz al día.")
    return actualizados


# ─── Publicar una versión nueva ──────────────────────────────────────────
def _empaquetar_script() -> bytes:
    """tar.gz en memoria con setup_mc.py + setup_mc/*.py (sin __pycache__)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for archivo in _archivos_script():
            tar.add(str(archivo), arcname=str(archivo.relative_to(BASE_DIR)))
    return buffer.getvalue()


def _anotar_version_local(version: str) -> None:
    """Deja VERSION_SCRIPT en config.py apuntando a la versión recién publicada."""
    ruta = BASE_DIR / "setup_mc" / "config.py"
    texto = ruta.read_text()
    nuevo, cambios = RE_VERSION_CONFIG.subn(f'VERSION_SCRIPT = "{version}"', texto, count=1)
    if cambios:
        ruta.write_text(nuevo)
        config.VERSION_SCRIPT = version


def publicar(mensaje: str, version: str = "") -> bool:
    """Empaqueta el script local y lo sube al panel (--publicar)."""
    url, clave = config_panel()
    if not url or not clave:
        error("Faltan PANEL_URL y/o PANEL_API_KEY en el .env: no se puede publicar.")
        return False
    version = version or time.strftime("%Y.%m.%d.%H%M", time.gmtime())
    # La versión se escribe ANTES de empaquetar: así el paquete publicado ya
    # trae este número y quien se actualice no vuelve a verlo como "nuevo".
    _anotar_version_local(version)
    paquete = _empaquetar_script()
    sha = hashlib.sha256(paquete).hexdigest()
    cuerpo = {
        "version": version,
        "sha256": sha,
        "changelog": mensaje,
        "paquete_b64": base64.b64encode(paquete).decode(),
    }
    log(f"Publicando v{version} ({len(paquete):,} bytes) en {url}...")
    try:
        r = requests.post(f"{url}/api/publicar", json=cuerpo,
                          headers=_cabeceras(), timeout=120)
    except requests.RequestException as exc:
        error(f"No se pudo contactar con el panel: {exc}")
        return False
    if r.status_code == 401:
        error("El panel rechazó la clave (PANEL_API_KEY).")
        return False
    if not r.ok:
        error(f"El panel devolvió un error: {r.status_code} {r.text[:300]}")
        return False
    ok(f"Versión v{version} publicada. Las demás máquinas se actualizarán al arrancar.")
    publicar_ui(version)   # la interfaz (webui/ + templates/) viaja con el script
    return True
