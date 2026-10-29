"""Protección de secretos, .gitignore y Git de bajo nivel."""

import subprocess, sys
from pathlib import Path
from .config import BASE_DIR, ENV_EJEMPLO, FIN_GITIGNORE, GITIGNORE_FILE, INICIO_GITIGNORE, WEBHOOK_ENV_FILE
from .ui import aviso, error, log, ok, ruta_sensible

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
        # Los automáticos (cada 20 min) son solo locales: ~130 MB cada vez y
        # no deben entrar al historial. El tarball del mundo se versiona
        # mientras quepa en git; si no cabe, se publica como release y esta
        # misma línea se añade aquí sola hasta que vuelva a caber.
        "respaldo/mundo-*.tar.zst",
        # Respaldos previos a una actualización de Paper y el jar anterior:
        # son locales (cientos de MB) y no deben entrar al historial.
        "respaldo/antes-*.tar.zst",
        "respaldo/server-*.jar",
        "respaldo/authme-*.db",
        # Carpeta de la versión anterior del script; se ignora por si queda
        # alguna que no se haya reiniciado con el código nuevo.
        "respaldo/auto/",
        "",
        "# Panel del Worker (Cloudflare): se versiona el código, no los secretos",
        "panel/node_modules/",
        "panel/.wrangler/",
        "panel/dist/",
        "panel/.dev.vars",
        "panel/.dev.vars.*",
        "!panel/.dev.vars.example",
        ".actualizacion-*/",
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
            "# Copia este archivo a .env y rellena lo que uses.\n"
            "# .env está en .gitignore: nunca se sube.\n"
            "DISCORD_WEBHOOK=\n"
            "\n"
            "# Panel (Cloudflare Workers): auto-actualización, plugins y estado en vivo.\n"
            "# Despliega el Worker de panel/ (ver panel/README.md) y pega aquí su URL\n"
            "# y la misma clave que pusiste con: npx wrangler secret put PANEL_API_KEY\n"
            "PANEL_URL=\n"
            "PANEL_API_KEY=\n"
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
