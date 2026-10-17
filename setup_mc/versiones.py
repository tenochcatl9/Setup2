"""Detección de versión de Minecraft y rango compatible."""

import re, zipfile
from .config import SERVER_JAR

# El minimo depende del AuthMe: las builds modernas de AuthMe son 1.10+.
VER_MINIMA_MC = "1.10"

def _version_max_operativa() -> str:
    """Versión de Minecraft que da el server.jar, leída sin arrancarlo."""
    v = _version_mc_del_jar()
    return v if v and v != "desconocida" else "la más reciente"

def rango_versiones() -> str:
    """Rango admitida: la mínima la impone AuthMe, la máxima la del server.jar."""
    return f"{VER_MINIMA_MC} a {_version_max_operativa()}"

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
