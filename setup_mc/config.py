"""Constantes, rutas y patrones compartidos."""

import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent   # Raíz local del proyecto

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

# Versión del script. --publicar la actualiza sola; el panel la compara al
# arrancar para decidir si hay que auto-actualizarse (formato AAAA.MM.DD.HHMM).
VERSION_SCRIPT = "2026.10.29.2330"

PUERTO_MC = 25565

PUERTO_TUNEL = 80

TIMEOUT_TUNEL = 60

TIMEOUT_ARRANQUE = 240

INTERVALO_AVISO_TUNEL = 10

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
