"""Webhook de Discord y notificaciones del estado."""

import json, re, time
import requests
from .config import WEBHOOK_ENV_FILE
from .ui import aviso, error, log, ok
from .versiones import rango_versiones

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
