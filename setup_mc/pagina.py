"""Página pública (Cloudflare Pages): guía, descarga del script y de la interfaz.

La página vive en la carpeta `pagina/` (html/css/js reales) y se publica junto
con dos bloques de archivos:

  script/  → el script en tar.gz y manifest.json (versión, sha256, changelog)
  ui/      → la interfaz local (index.html, app.js, templates/*.css) y un
             indice.json con rutas y sha256

Al arrancar, el script (sin panel) baja de aquí la interfaz si cambió y
también las versiones nuevas del propio script. Sin PAGINA_URL utilizable,
todo esto es no-op y el script se comporta como siempre.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

import requests

from . import config
from . import panel
from .config import BASE_DIR
from .ui import aviso, error, log, ok

# La página por defecto: quien descarga solo el script no necesita configurar
# nada para recibir la interfaz y las actualizaciones. PAGINA_URL=no la apaga.
URL_POR_DEFECTO = "https://tenohost.pages.dev"
APAGADA = ("", "no", "off", "ninguna")

TIMEOUT_INDICE = 6
TIMEOUT_ARCHIVO = 30


# ─── Configuración (.env) ────────────────────────────────────────────────
def pagina_config() -> tuple:
    """(url pública, proyecto Pages) del .env; '' donde no proceda."""
    url = panel._valor_env("PAGINA_URL").strip() or URL_POR_DEFECTO
    if url.lower() in APAGADA:
        url = ""
    proyecto = panel._valor_env("PAGES_PROYECTO").strip()
    if proyecto.lower() in APAGADA:
        proyecto = ""
    return url.rstrip("/"), proyecto


# ─── Bajadas desde la página (lo que consume el script) ──────────────────
def _bajar_ui(url: str, logica: str):
    """Bytes de un archivo de la interfaz publicado en la página, o None."""
    try:
        r = requests.get(f"{url}/ui/{logica}", timeout=TIMEOUT_ARCHIVO)
        r.raise_for_status()
        return r.content
    except requests.RequestException:
        return None


def actualizar_ui_pagina(base: Path | None = None) -> int:
    """Baja de la página la interfaz si cambió (sha256). Nº de archivos.

    Nunca aborta nada: sin página, sin red o con respuestas raras se sigue
    con los archivos locales.
    """
    url, _ = pagina_config()
    if not url:
        return 0
    base = base or BASE_DIR
    try:
        r = requests.get(f"{url}/ui/indice.json", timeout=TIMEOUT_INDICE)
        r.raise_for_status()
        indice = r.json()
    except (requests.RequestException, ValueError):
        return 0
    actualizados = panel._aplicar_ui(indice, lambda logica: _bajar_ui(url, logica), base)
    if actualizados:
        ok(f"Página: {actualizados} archivo/s de la interfaz al día.")
    return actualizados


def _manifest_pagina(url: str):
    """manifest.json del script en la página, o None si no responde."""
    try:
        r = requests.get(f"{url}/script/manifest.json", timeout=TIMEOUT_INDICE)
        r.raise_for_status()
        datos = r.json()
    except (requests.RequestException, ValueError):
        return None
    return datos if isinstance(datos, dict) and datos.get("version") else None


def actualizar_script_pagina() -> bool:
    """Aplica la versión publicada en la página si es más nueva que la local."""
    url, _ = pagina_config()
    if not url:
        return False
    manifest = _manifest_pagina(url)
    if not manifest:
        log("Página: no hay ninguna versión publicada (o no responde).")
        return False
    # Nunca se fuerza la reaplicación: con `--actualizar` eso sería un bucle
    # de execv tras execv; la comparación de versiones ya lo evita.
    return panel.actualizar(manifest=manifest, origen=url, fuente="Página")


def actualizar_al_arrancar(manual: bool = False) -> bool:
    """Comprobación de arranque; nunca aborta el script por un fallo de la página."""
    url, _ = pagina_config()
    if not url:
        if manual:
            error("La página no está configurada: define PAGINA_URL en el .env.")
        return False
    # La interfaz (html/js/css) va aparte del script: se comprueba siempre, y
    # también cuando el script ya se actualizó en este mismo arranque.
    try:
        actualizar_ui_pagina()
    except (requests.RequestException, OSError, ValueError):
        pass
    if not manual and os.environ.get(panel._VAR_GUARD):
        aviso(f"Página: ya se actualizó a v{os.environ[panel._VAR_GUARD]} en este arranque.")
        return False
    try:
        hecho = actualizar_script_pagina()
    except (requests.RequestException, OSError, ValueError, tarfile.TarError) as exc:
        aviso(f"Página: no se pudo comprobar la actualización ({exc}). "
              "Se continúa con la versión local.")
        return False
    if manual and not hecho:
        ok(f"Página: no hay nada que actualizar (v{config.VERSION_SCRIPT} al día).")
    return hecho


# ─── Construcción y despliegue (--publicar) ──────────────────────────────
def _tipo_ui(logica: str) -> str:
    if logica.endswith(".html"):
        return "text/html; charset=utf-8"
    if logica.endswith(".js"):
        return "text/javascript; charset=utf-8"
    return "text/css; charset=utf-8"


def construir_sitio(destino: Path, version: str, changelog: str = "") -> dict:
    """Prepara en `destino` lo que se sube a Cloudflare Pages.

    Copia la landing de `pagina/`, la interfaz publicable en `ui/` (con su
    índice sha256) y el script en `script/`: `setup_mc.py` suelto (se completa
    solo al ejecutarse), `setup_mc.tar.gz` y `manifest.json`. Devuelve el
    manifest del script.
    """
    destino.mkdir(parents=True, exist_ok=True)

    landing = BASE_DIR / "pagina"
    if not landing.is_dir():
        raise FileNotFoundError(f"falta la carpeta de la página: {landing}")
    for ruta in sorted(landing.iterdir()):
        if ruta.is_file():
            shutil.copy2(ruta, destino / ruta.name)

    archivos_ui = []
    for logica, origen in panel.rutas_ui().items():
        datos = origen.read_bytes()
        copia = destino / "ui" / logica
        copia.parent.mkdir(parents=True, exist_ok=True)
        copia.write_bytes(datos)
        archivos_ui.append({
            "ruta": logica,
            "sha256": hashlib.sha256(datos).hexdigest(),
            "tamano": len(datos),
            "tipo": _tipo_ui(logica),
        })
    indice = {"version": version, "cuando": time.time(), "archivos": archivos_ui}
    (destino / "ui" / "indice.json").write_text(
        json.dumps(indice, ensure_ascii=False, indent=2) + "\n")

    paquete = panel._empaquetar_script()
    (destino / "script").mkdir(parents=True, exist_ok=True)
    (destino / "script" / "setup_mc.tar.gz").write_bytes(paquete)
    shutil.copy2(BASE_DIR / "setup_mc.py", destino / "script" / "setup_mc.py")
    manifest = {
        "version": version,
        "sha256": hashlib.sha256(paquete).hexdigest(),
        "tamano": len(paquete),
        "changelog": changelog,
        "url": "script/setup_mc.tar.gz",
        "cuando": time.time(),
    }
    (destino / "script" / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def _wrangler(args: list, timeout: int = 600, cwd: Path | None = None):
    """Ejecuta `npx -y wrangler@4 <args>`; None si no hay npx.

    Los comandos de Pages corren desde el directorio del sitio (no desde el
    repo: un wrangler.jsonc en la raíz haría que wrangler se crea que es un
    Worker).
    """
    if not shutil.which("npx"):
        error("Página: hace falta Node.js (npx) para desplegar en Cloudflare Pages.")
        return None
    try:
        return subprocess.run(["npx", "-y", "wrangler@4", *args],
                              cwd=str(cwd or BASE_DIR), capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        error(f"Página: no se pudo ejecutar wrangler: {exc}")
        return None


def publicar_pagina(version: str, mensaje: str = "") -> bool:
    """Construye la página y la despliega en Cloudflare Pages (--publicar)."""
    url, proyecto = pagina_config()
    if not proyecto:
        aviso("Página: falta PAGES_PROYECTO en el .env; no se publica la página.")
        return False
    with tempfile.TemporaryDirectory(prefix="pagina-setup2-") as tmp:
        destino = Path(tmp)
        try:
            manifest = construir_sitio(destino, version, mensaje)
        except OSError as exc:
            error(f"Página: no se pudo preparar el contenido: {exc}")
            return False
        log(f"Página: contenido listo (v{version}, paquete de {manifest['tamano']:,} bytes).")

        # Al crear hace falta `--force`: sin él, wrangler 4.x delega la orden
        # en la integración nueva de Pages-en-Workers y falla. Solo es
        # necesario la primera vez: si el proyecto ya existe, el deploy va
        # directo contra Cloudflare Pages.
        creado = _wrangler(["pages", "project", "create", proyecto,
                            "--production-branch", "main", "--force"],
                           timeout=120, cwd=destino)
        if creado is None:
            return False
        if creado.returncode != 0 and "already exist" not in (creado.stdout + creado.stderr).lower():
            error("Página: no se pudo crear el proyecto en Pages:\n"
                  + (creado.stderr or creado.stdout).strip()[-600:])
            return False

        log(f"Página: desplegando en Cloudflare Pages ({proyecto})...")
        despliegue = _wrangler(["pages", "deploy", str(destino),
                                "--project-name", proyecto, "--branch", "main",
                                "--commit-dirty=true"], cwd=destino)
        if despliegue is None:
            return False
        if despliegue.returncode != 0:
            error("Página: el despliegue en Pages falló:\n"
                  + (despliegue.stderr or despliegue.stdout).strip()[-800:])
            return False
        ok(f"Página publicada (v{version}): {url or 'https://' + proyecto + '.pages.dev'}")
        return True
