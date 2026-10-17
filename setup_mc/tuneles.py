"""Notificador de túnel y proveedores Asyx / Pinggy."""

import json, os, queue, re, shutil, socket, subprocess, sys, threading, time
from pathlib import Path
import requests
from . import ui
from .config import INTERVALO_AVISO_TUNEL, LOG_TUNEL, PID_TUNEL, PUERTO_TUNEL, RE_ANSI, TIMEOUT_TUNEL
from .ui import Barra, _sin_ansi, aviso, error, limpiar_linea, log, ok, titulo
from .procesos import _abrir_log, _bombeo_salida, _detener_proceso, _drenar, _limpiar_pid, _matar_pid
from .avisos import bloque_ip

MOTIVO_FALLO_TUNEL = ""

BARRA_DISCORD = "▓▓▓▓▓▓▓░░░"

def _barra_texto(frac: float) -> str:
    llenos = int(len(BARRA_DISCORD) * frac)
    return BARRA_DISCORD[:llenos] + BARRA_DISCORD[llenos:]

class NotificadorTunel:
    """Un único mensaje en Discord: 'Iniciando Túnel' + barra, editado cada 10s.

    Se edita el mismo mensaje (PATCH) en vez de enviar uno nuevo cada vez, así el
    canal no se llena y solo hay una petición cada 10 segundos.
    """

    def __init__(self, webhook: str, intervalo: int = INTERVALO_AVISO_TUNEL):
        self.webhook = webhook
        self.intervalo = intervalo
        self.id_mensaje = None
        self.ultimo_envio = 0.0

    def _url_mensaje(self) -> str:
        return f"{self.webhook}/messages/{self.id_mensaje}"

    def _enviar(self, contenido: str) -> bool:
        if not self.webhook:
            return False
        try:
            if self.id_mensaje:
                r = requests.patch(self._url_mensaje(), json={"content": contenido},
                                   timeout=20)
                if r.status_code == 404:
                    # El mensaje ya no existe (cancho borrado): se crea otro.
                    self.id_mensaje = None
                    r = requests.post(self.webhook, json={"content": contenido}, timeout=20)
            else:
                r = requests.post(self.webhook, json={"content": contenido}, timeout=20)
        except Exception as e:  # noqa: BLE001
            error(f"No se pudo avisar a Discord del túnel: {e}")
            return False
        if r.status_code in (200, 201, 204):
            try:
                datos = r.json()
                if datos.get("id"):
                    self.id_mensaje = datos["id"]
            except ValueError:
                pass
            return True
        if r.status_code == 429:
            aviso("Discord limita el aviso del túnel (429); se reintentará en la "
                  "próxima actualización.")
            return False
        error(f"Discord rechazó el aviso del túnel: HTTP {r.status_code}")
        return False

    def iniciar(self, barra: Barra) -> None:
        if not self.webhook:
            return
        self.ultimo_envio = time.time()
        self._enviar("⏳ **Iniciando Túnel**\nEspere un momento mientras se abre "
                     "el acceso público al servidor.")

    def pulso(self, barra: Barra) -> None:
        """Actualiza la barra como mucho cada `intervalo` segundos."""
        if not self.webhook or self.id_mensaje is None:
            return
        ahora = time.time()
        if ahora - self.ultimo_envio < self.intervalo:
            return
        self.ultimo_envio = ahora
        pct, frac = barra._avance()
        transcurrido = ahora - barra.inicio
        restante = max(0, (barra.limite or 0) - transcurrido)
        self._enviar(f"⏳ **Iniciando Túnel**\n`{_barra_texto(frac)}` {pct:.0f}% · "
                     f"{transcurrido:.0f}s · ~{restante:.0f}s restantes")

    def finalizar(self, ip: str = "", motivo: str = "") -> None:
        if not self.webhook or self.id_mensaje is None:
            return
        if ip:
            self._enviar("✅ **Túnel listo**\n\n" + bloque_ip(ip, "IP pública"))
        else:
            self._enviar(f"❌ **No se pudo abrir el túnel**\n{motivo}")

# ─── 9. Túneles: Asyx (principal) y Pinggy (respaldo) ─────────────────────
# Asyx: nativo de Minecraft, subdominio permanente y registro SRV (el jugador
# escribe solo el hostname). Necesita alta con email, una vez.
# Pinggy: solo ssh, sin cuenta, pero con tope de 60 min y URL cambiante.
ASYX_REGISTRO = "https://www.asyx.ai/register"

ASYX_CONSOLE = "https://www.asyx.ai/console/tunnel"

ASYX_STS = "https://www.asyx.ai/www/sts"

ASYNX_DOMINIO = ".tunnel.asyx.ai"

PINGGY_SERVIDOR = "free.pinggy.io"

PINGGY_PUERTO = 443

PINGGY_AVISO = ("Pinggy gratis corta el túnel a los 60 minutos y cambia la "
                "dirección al reconectar: la IP de Discord quedará obsoleta.")

RE_PINGGY_TCP = re.compile(r"tcp://([A-Za-z0-9][A-Za-z0-9.-]*):(\d{2,5})", re.IGNORECASE)

RE_ASYX_HOST = re.compile(r"\b([A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*"
                          r"\.tunnel\.asyx\.ai)\b", re.IGNORECASE)

RE_HOST_PUERTO = re.compile(r"\b([A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}):(\d{2,5})\b")

# ── Utilidades comunes ─────────────────────────────────────────────────────
def _dividir_host_puerto(valor: str, puerto_defecto=None) -> str:
    """Normaliza 'host', 'host:puerto' o 'tcp://host:puerto' a 'host:puerto'."""
    valor = RE_ANSI.sub("", valor or "").strip()
    valor = re.sub(r"^[a-z]+://", "", valor, flags=re.IGNORECASE)
    m = RE_HOST_PUERTO.search(valor)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    if valor and puerto_defecto:
        return f"{valor}:{puerto_defecto}"
    return valor

def _servidor_alcanzable(host: str, puerto: int, timeout: int = 6) -> bool:
    try:
        with socket.create_connection((host, puerto), timeout=timeout):
            return True
    except OSError:
        return False

def _comando_existe(nombre: str) -> bool:
    return shutil.which(nombre) is not None

def _preguntar(texto: str) -> str:
    """Pregunta al usuario. Devuelve la opción en minúscula ('' si no hay TTY)."""
    if not sys.stdin.isatty():
        return ""
    try:
        return input(texto).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""

# ── Proveedor: Asyx ────────────────────────────────────────────────────────
def asyx_estado_alta() -> str:
    """Estado del alta de Asyx: 'completa', 'incompleta' o 'sin_alta'.

    No basta con que ~/.asyx exista: una alta a medias deja device.json y el
    CSR, pero Asyx exige el manifest.json que entrega al completar el registro.
    """
    base = Path.home() / ".asyx"
    if not base.is_dir():
        return "sin_alta"
    manifest = list((base / "certs").glob("*/manifest.json")) if (base / "certs").is_dir() else []
    if manifest:
        return "completa"
    if any(base.iterdir()):
        return "incompleta"
    return "sin_alta"

def asyx_esta_alto() -> bool:
    """True solo si el alta está realmente completa."""
    return asyx_estado_alta() == "completa"

def asyx_registrar(minutos: int = 15) -> bool:
    """Da de alta el dispositivo en Asyx mostrando la URL de registro.

    El CLI oficial (npx asyx setup) abre el navegador con la URL del registro
    pero no la imprime, así que en un Codespace headless no hay forma de
    completarlo. Aquí hacemos lo mismo a mano, enseñando la URL y esperando
    bastante más tiempo: el registro se hace en el navegador del usuario, no
    en esta máquina.
    """
    if not _comando_existe("openssl"):
        error("Asyx necesita 'openssl' para generar la clave del dispositivo.")
        return False
    base = Path.home() / ".asyx"
    device_json = base / "device.json"
    client_id = ""
    if device_json.is_file():
        try:
            client_id = json.loads(device_json.read_text()).get("clientId", "")
        except (ValueError, OSError):
            client_id = ""
    if not re.fullmatch(r"cli_[A-F0-9]{12}", client_id or ""):
        client_id = "cli_" + os.urandom(6).hex().upper()
    base.mkdir(parents=True, exist_ok=True)
    device_json.write_text(json.dumps({"clientId": client_id}, indent=2))
    certs = base / "certs" / client_id
    certs.mkdir(parents=True, exist_ok=True)
    try:
        certs.chmod(0o700)
    except OSError:
        pass
    clave = certs / "private.pem.key"
    csr = certs / "device.csr.pem"

    if not clave.is_file():
        r = subprocess.run(["openssl", "genrsa", "-traditional", "-out", str(clave), "2048"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not clave.is_file():
            error(f"No se pudo generar la clave: {(r.stderr or '').strip()[:200]}")
            return False
    clave.chmod(0o600)
    r = subprocess.run(["openssl", "req", "-new", "-key", str(clave),
                        "-subj", f"/CN={client_id}/O=Asyx", "-sha256", "-out", str(csr)],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not csr.is_file():
        error(f"No se pudo generar el CSR: {(r.stderr or '').strip()[:200]}")
        return False

    ticket = os.urandom(16).hex()
    url = f"{ASYX_CONSOLE}?clientId={client_id}&ticket={ticket}"
    titulo("ALTA DE ASYX: REGÍSTRATE EN TU NAVEGADOR")
    print("  1) Abre este enlace en el navegador de tu ORDENADOR")
    print("     (no en el Codespace: aquí no hay navegador):")
    print()
    print(f"       {url}")
    print()
    print("  2) Inicia sesión o crea la cuenta (es gratis, sin tarjeta) y")
    print("     completa el registro.")
    print(f"  3) Vuelve aquí: esperaré hasta {minutos} minutos a que Asyx")
    print("     emita el certificado.")
    print()
    try:
        abrir_navegador(url)
    except Exception:  # noqa: BLE001
        pass

    limite = time.time() + minutos * 60
    barra = Barra("Esperando el certificado de Asyx", limite=minutos * 60)
    intentos = 0
    try:
        while time.time() < limite:
            intentos += 1
            barra.refrescar()
            try:
                r = requests.post(ASYX_STS, json={"ticket": ticket, "clientId": client_id,
                                                 "csrPem": csr.read_text()},
                                  headers={"content-type": "application/json",
                                           "accept": "application/json"},
                                  timeout=25)
            except requests.RequestException:
                time.sleep(2)
                continue
            if r.status_code in (200, 201):
                if "json" not in (r.headers.get("content-type") or ""):
                    # Sin sesión iniciada Asyx devuelve su web, no el certificado.
                    if intentos < 3:
                        aviso("Aún no veo tu sesión: completa el registro en el enlace.")
                    time.sleep(3)
                    continue
                try:
                    emitido = r.json()
                except ValueError:
                    time.sleep(3)
                    continue
                (certs / "AmazonRootCA1.pem").write_text(emitido["caPem"])
                (certs / "device.pem.crt").write_text(emitido["certPem"])
                manifest = {"clientId": client_id, "endpoint": emitido.get("endpoint", ""),
                            "region": emitido.get("region", ""),
                            "issuedAt": int(time.time() * 1000)}
                manifest_path = certs / "manifest.json"
                manifest_path.write_text(json.dumps(manifest, indent=2))
                for ruta, modo in ((certs / "AmazonRootCA1.pem", 0o644),
                                   (certs / "device.pem.crt", 0o600),
                                   (manifest_path, 0o600)):
                    try:
                        ruta.chmod(modo)
                    except OSError:
                        pass
                barra.finalizar("Certificado recibido")
                ok(f"Asyx dado de alta. Tu subdominio se reservó con {client_id}.")
                return True
            if r.status_code in (401, 403) and intentos < 3:
                aviso("Aún no veo tu sesión: completa el registro en el enlace.")
            if r.status_code in (301, 302, 307, 308):
                time.sleep(3)
                continue
            if r.status_code == 409:
                barra.finalizar()
                error("El registro no corresponde a esta máquina (HTTP 409).")
                log(f"    Vuelve a intentarlo; si persiste, borra {base} y rehaz el alta.")
                return False
            time.sleep(3)
        barra.finalizar()
    finally:
        if ui._barra_activa is barra:
            barra.finalizar()
    error(f"Asyx no emitió el certificado en {minutos} minutos.")
    log("    Reintenta cuando termines el registro:  npx -y asyx@latest setup")
    return False

def abrir_navegador(url: str) -> bool:
    """Intenta abrir la URL; enCodespace no hay navegador y no es un error."""
    for cmd in (["xdg-open", url], ["gio", "open", url]):
        if _comando_existe(cmd[0]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except OSError:
                continue
    return False

def asyx_preparar() -> bool:
    """Comprueba Node y pide el alta si falta. True si queda listo para usar."""
    if not _comando_existe("node") or not (_comando_existe("npx") or _comando_existe("npm")):
        error("Asyx necesita Node.js 18.17+ (no lo encuentro en el PATH).")
        log("    Instálalo y vuelve a ejecutar el script; Pinggy es el respaldo.")
        return False
    if asyx_esta_alto():
        log("Asyx ya está dado de alta (~/.asyx).")
        return True
    if asyx_estado_alta() == "incompleta":
        titulo("EL ALTA DE ASYX QUEDÓ A MEDIAS")
        error("Falta el manifest.json que Asyx entrega al completar el registro.")
        log("    Se ve así: existe ~/.asyx/device.json y la clave, pero no hay")
        log("    ningún manifest.json en ~/.asyx/certs/*/ . Con el alta a medias el")
        log("    túnel no arranca (error 'missing manifest').")
        print()
        log("    Solución: repite el alta. Elimina lo que quedó a medias y vuelve")
        log("    a empezar el registro:")
        log("        rm -rf ~/.asyx && npx -y asyx@latest setup")
        print()
    else:
        titulo("ASYX NECESITA UNA CUENTA (solo esta vez)")
    print(f"  1) Crea una cuenta gratuita en:  {ASYX_REGISTRO}")
    print("     (solo email, sin tarjeta)")
    print("  2) Vuelve aquí y ejecuta el alta, que abre el navegador para")
    print("     verificar el email y reservar tu subdominio permanente.")
    print()
    if _preguntar("  ¿Empezar el alta ahora? [s/N]: ") == "s":
        if asyx_estado_alta() == "incompleta":
            log("Se reutiliza tu clientId, así que conservas el subdominio.")
        return asyx_registrar()
    log("Hazlo más tarde con:  python3 setup_mc.py --registrar-asyx")
    log("El CLI oficial también vale, pero no imprime el enlace de registro:")
    log("                       npx -y asyx@latest setup")
    if asyx_esta_alto():
        ok("Asyx dado de alta.")
        return True
    aviso("Asyx se queda fuera: el túnel irá por Pinggy (60 min, IP cambiante).")
    log("    Para usar Asyx de verdad:  python3 setup_mc.py --registrar-asyx")
    return False

def asyx_prechequear() -> tuple:
    """(ok, motivo). Sin red que comprobar: lo decisivo es el alta local."""
    if asyx_esta_alto():
        return True, ""
    estado = asyx_estado_alta()
    if estado == "incompleta":
        return False, ("el alta de Asyx está incompleta (falta el manifest.json). "
                       "Arréglalo con:  rm -rf ~/.asyx && npx -y asyx@latest setup")
    return False, ("no tienes cuenta de Asyx. Créala en " + ASYX_REGISTRO +
                   " y ejecuta: npx -y asyx@latest setup")

def asyx_comando(puerto: int) -> list:
    """--minecraft es lo que publica el registro SRV para el cliente Java."""
    return ["npx", "-y", "asyx@latest", "tunnel", "--minecraft", "--port", str(puerto)]

def asyx_parsear(texto: str) -> str:
    """Saca el host:puerto público de la salida de Asyx.

    Acepta las dos formas: 'host:puerto' explícito o solo el hostname (en ese
    caso se consulta el SRV para averiguar el puerto).
    """
    for linea in texto.splitlines():
        limpia = _sin_ansi(linea).strip()
        m = RE_HOST_PUERTO.search(limpia)
        if m and m.group(1).lower().endswith(ASYNX_DOMINIO):
            return f"{m.group(1)}:{m.group(2)}"
        mh = RE_ASYX_HOST.search(limpia)
        if mh:
            host = mh.group(1)
            puerto = _preguntar_puerto_srv(host)
            if puerto:
                return f"{host}:{puerto}"
    return ""

def _preguntar_puerto_srv(host: str):
    """Consulta _minecraft._tcp.<host> para saber el puerto sin escribirlo."""
    if not _comando_existe("dig"):
        return None
    try:
        r = subprocess.run(["dig", "+short", "SRV", f"_minecraft._tcp.{host}"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for linea in r.stdout.splitlines():
        partes = linea.split()
        if len(partes) == 4 and partes[3].rstrip(".").lower() == host.lower():
            return partes[2]
    return None

def asyx_srv_soportado(host: str) -> bool:
    """True si el jugador puede entrar solo con el hostname (SRV publicado)."""
    return _preguntar_puerto_srv(host) is not None

# ── Proveedor: Pinggy ──────────────────────────────────────────────────────
def pinggy_preparar() -> bool:
    if not _comando_existe("ssh"):
        error("Pinggy necesita el cliente ssh, que no está en el PATH.")
        return False
    return True

def pinggy_prechequear() -> tuple:
    if _servidor_alcanzable(PINGGY_SERVIDOR, PINGGY_PUERTO, timeout=8):
        return True, ""
    return False, (f"no hay conexión con {PINGGY_SERVIDOR}:{PINGGY_PUERTO} "
                   f"desde esta máquina (compruébalo con: nc -vz "
                   f"{PINGGY_SERVIDOR} {PINGGY_PUERTO})")

def pinggy_comando(puerto: int) -> list:
    # Sin -N a propósito: la URL se emite en la sesión, con -N nunca llega.
    return [
        "ssh", "-T", "-p", str(PINGGY_PUERTO),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-R", f"0:localhost:{puerto}",
        f"tcp@{PINGGY_SERVIDOR}",
    ]

def pinggy_parsear(texto: str) -> str:
    """Solo la línea TCP: ignora las URL http/https y el debugger local."""
    for linea in texto.splitlines():
        m = RE_PINGGY_TCP.search(_sin_ansi(linea))
        if m:
            return f"{m.group(1)}:{m.group(2)}"
    return ""

# ── Registro de proveedores ────────────────────────────────────────────────
PROVEEDORES = {
    "asyx": {
        "nombre": "Asyx",
        "preparar": asyx_preparar,
        "prechequear": asyx_prechequear,
        "comando": asyx_comando,
        "parsear": asyx_parsear,
        "aviso": None,
    },
    "pinggy": {
        "nombre": "Pinggy",
        "preparar": pinggy_preparar,
        "prechequear": pinggy_prechequear,
        "comando": pinggy_comando,
        "parsear": pinggy_parsear,
        "aviso": PINGGY_AVISO,
    },
}

ORDEN_PROVEEDORES = ("asyx", "pinggy")

def _esperar_direccion(cola: queue.Queue, barra: Barra, timeout: int,
                       parsear, notificador) -> tuple:
    """Lee la salida hasta obtener la dirección pública. Devuelve (dir, terminó)."""
    direccion = ""
    while time.time() - barra.inicio < timeout:
        try:
            linea = cola.get(timeout=0.2)
        except queue.Empty:
            barra.refrescar()
            if notificador:
                notificador.pulso(barra)
            continue
        if linea is None:
            break
        texto = _sin_ansi(linea).rstrip()
        if texto:
            limpiar_linea()
            print(f"    {texto}")
        direccion = parsear(texto) or direccion
        if direccion:
            break
    return direccion

def _intentar_proveedor(clave: str, puerto: int, timeout: int, notificador) -> tuple:
    """Prueba un proveedor. Devuelve (direccion, proceso, ok, motivo)."""
    global MOTIVO_FALLO_TUNEL
    prov = PROVEEDORES[clave]
    nombre = prov["nombre"]
    MOTIVO_FALLO_TUNEL = ""

    # Primero preparar: es donde se pregunta al usuario por el alta de Asyx.
    # Si se comprobara la red antes, el prechequeo fallaría sin alta y nunca
    # se llegaría al prompt, y el script se iría a Pinggy sin preguntar nada.
    if not prov["preparar"]():
        return "", None, False, f"{nombre} no está preparado"
    bien, motivo = prov["prechequear"]()
    if not bien:
        return "", None, False, motivo

    cola: queue.Queue = queue.Queue()
    try:
        proc = subprocess.Popen(prov["comando"](puerto), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
    except FileNotFoundError as e:
        return "", None, False, f"no se pudo ejecutar el cliente ({e})"
    threading.Thread(target=_bombeo_salida, args=(proc, cola), daemon=True).start()
    barra = Barra(f"Levantando túnel con {nombre}", limite=timeout)
    if notificador:
        notificador.iniciar(barra)
    direccion = _esperar_direccion(cola, barra, timeout, prov["parsear"], notificador)
    _drenar(cola)
    barra.finalizar(f"{nombre} activo" if direccion else None)
    if not direccion:
        if proc.poll() is not None:
            MOTIVO_FALLO_TUNEL = f"{nombre} terminó sin dar dirección pública"
        else:
            MOTIVO_FALLO_TUNEL = f"{nombre} no dio dirección pública en {timeout}s"
        _detener_proceso(proc, escribir_stop=False, timeout=5)
        return "", None, False, MOTIVO_FALLO_TUNEL
    return direccion, proc, True, ""

def _intentar_proveedor_detached(clave: str, puerto: int, timeout: int, notificador) -> tuple:
    """Igual que _intentar_proveedor pero dejando el proceso vivo."""
    prov = PROVEEDORES[clave]
    nombre = prov["nombre"]
    MOTIVO_FALLO_TUNEL = ""

    # Primero preparar: es donde se pregunta al usuario por el alta de Asyx.
    # Si se comprobara la red antes, el prechequeo fallaría sin alta y nunca
    # se llegaría al prompt, y el script se iría a Pinggy sin preguntar nada.
    if not prov["preparar"]():
        return "", None, False, f"{nombre} no está preparado"
    bien, motivo = prov["prechequear"]()
    if not bien:
        return "", None, False, motivo

    mango = _abrir_log(LOG_TUNEL, truncar=True)
    try:
        proc = subprocess.Popen(prov["comando"](puerto), stdin=subprocess.DEVNULL,
                                stdout=mango, stderr=subprocess.STDOUT,
                                start_new_session=True)
    except FileNotFoundError as e:
        return "", None, False, f"no se pudo ejecutar el cliente ({e})"
    finally:
        mango.close()
    PID_TUNEL.write_text(f"{proc.pid}\n")
    barra = Barra(f"Levantando túnel con {nombre}", limite=timeout)
    if notificador:
        notificador.iniciar(barra)
    direccion = ""
    try:
        while time.time() - barra.inicio < timeout:
            if proc.poll() is not None:
                barra.finalizar()
                MOTIVO_FALLO_TUNEL = f"{nombre} terminó sin dar dirección pública"
                break
            try:
                direccion = prov["parsear"](_sin_ansi(LOG_TUNEL.read_text(errors="replace")))
            except OSError:
                direccion = ""
            if direccion:
                barra.finalizar(f"{nombre} activo")
                return direccion, proc, True, ""
            barra.refrescar()
            if notificador:
                notificador.pulso(barra)
            time.sleep(0.2)
        barra.finalizar()
        if not direccion and not MOTIVO_FALLO_TUNEL:
            MOTIVO_FALLO_TUNEL = f"{nombre} no dio dirección pública en {timeout}s"
        _matar_pid(proc.pid)
        _limpiar_pid(PID_TUNEL)
        return "", None, False, MOTIVO_FALLO_TUNEL
    finally:
        barra.finalizar()

def _abrir_con_respaldo(modo: str, puerto: int, notificador, detached: bool) -> tuple:
    """Prueba los proveedores en orden hasta obtener un túnel verificado.

    Devuelve (direccion, proceso, proveedor_usado, avisos).
    """
    if modo not in ("auto", *PROVEEDORES):
        raise RuntimeError(f"Túnel desconocido: {modo}. "
                           f"Usa: auto, {', '.join(PROVEEDORES)}")
    orden = ORDEN_PROVEEDORES if modo == "auto" else (modo,)
    problemas = []
    for i, clave in enumerate(orden):
        if i and notificador:
            notificador.finalizar(
                motivo=f"{PROVEEDORES[clave]['nombre']} no disponible, "
                       f"probando {PROVEEDORES[orden[-1]]['nombre']}")
        obtener = _intentar_proveedor_detached if detached else _intentar_proveedor
        direccion, proc, bien, motivo = obtener(clave, puerto, TIMEOUT_TUNEL, notificador)
        if bien and _listo_para_verificar(direccion):
            avisos = [PROVEEDORES[clave]["aviso"]] if PROVEEDORES[clave]["aviso"] else []
            return direccion, proc, clave, avisos
        if proc is not None:
            if detached:
                _matar_pid(proc.pid)
                _limpiar_pid(PID_TUNEL)
            else:
                _detener_proceso(proc, escribir_stop=False, timeout=5)
        if direccion:
            problemas.append(f"{PROVEEDORES[clave]['nombre']}: {direccion} no responde")
        else:
            problemas.append(f"{PROVEEDORES[clave]['nombre']}: {motivo}")
    global MOTIVO_FALLO_TUNEL
    MOTIVO_FALLO_TUNEL = " | ".join(problemas)
    error("Ningún túnel pudo abrirse:\n    - " + "\n    - ".join(problemas))
    return "", None, "", []

def diagnostico_tunel() -> int:
    """Comprueba en segundos qué proveedores pueden funcionar desde aquí."""
    titulo("DIAGNÓSTICO DE TÚNELES")
    for clave in ORDEN_PROVEEDORES:
        prov = PROVEEDORES[clave]
        print()
        log(f"--- {prov['nombre']} ---")
        if clave == "asyx":
            estado = asyx_estado_alta()
            if estado == "completa":
                ok("Cuenta creada y lista para usar (~/.asyx).")
            elif estado == "incompleta":
                error("Alta incompleta: falta ~/.asyx/certs/*/manifest.json.")
                aviso("Se arregla con:  rm -rf ~/.asyx && npx -y asyx@latest setup")
            elif _comando_existe("node") and _comando_existe("npx"):
                aviso("Sin alta todavía: crea la cuenta en " + ASYX_REGISTRO +
                      " y ejecuta  npx -y asyx@latest setup")
            else:
                error("Falta Node.js 18.17+ (no se puede instalar el CLI).")
        else:
            log(f"Probando conexión con {PINGGY_SERVIDOR}:{PINGGY_PUERTO}...")
            inicio = time.time()
            bien, motivo = prov["prechequear"]()
            if bien:
                ok(f"Alcanzable en {time.time() - inicio:.1f}s.")
            else:
                error(motivo)
            aviso(PINGGY_AVISO)
    print()
    aviso("El script usa Asyx y cae a Pinggy automáticamente.")
    return 0


# ─── 10. Utilidades del túnel ─────────────────────────────────────────────

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
    log("Cerrando el túnel...")
    _detener_proceso(proc, escribir_stop=False, timeout=8)

# ─── 11. Apertura y verificación del túnel ────────────────────────────────
def _listo_para_verificar(host_puerto: str, intentos: int = 8, espera: float = 2.0) -> bool:
    """Comprueba que la dirección pública acepta conexiones, con reintentos.

    Importante: al abrirse el túnel, el puerto público tarda unos segundos en
    estar disponible y el primer intento suele recibir un 'connection refused'
    aunque el túnel vaya bien. Con un solo intento se descartaba un túnel
    perfectamente bueno y se pasaba al proveedor de respaldo, así que se reintenta
    durante unos segundos antes de darlo por malo.
    """
    if ":" in host_puerto:
        host, _, puerto = host_puerto.rpartition(":")
    else:
        host, puerto = host_puerto, str(PUERTO_TUNEL)
    puerto = int(puerto)
    for intento in range(1, intentos + 1):
        if comprobar_tunel(host, puerto):
            if intento > 1:
                log(f"El puerto público respondió al reintento {intento} "
                    f"({(intento - 1) * espera:.0f}s de propagación).")
            return True
        if intento < intentos:
            time.sleep(espera)
    return False

def abrir_tunel(puerto: int, notificador=None, modo: str = "auto",
                timeout: int = TIMEOUT_TUNEL) -> tuple:
    """Abre túnel (Asyx, con Pinggy de respaldo) y devuelve (dir, proc, usado, avisos).

    Comprueba siempre que la dirección obtenida responde de verdad: así una IP
    inventada o un puerto equivocado nunca se anuncian como si fueran el
    servidor.
    """
    direccion, proc, usado, avisos = _abrir_con_respaldo(modo, puerto, notificador,
                                                         detached=False)
    if direccion:
        ok(f"Túnel {PROVEEDORES[usado]['nombre']} verificado: {direccion}")
    return direccion, proc, usado, avisos

def abrir_tunel_detached(puerto: int, notificador=None, modo: str = "auto",
                         timeout: int = TIMEOUT_TUNEL) -> tuple:
    """Igual que abrir_tunel() pero dejando el túnel vivo tras salir."""
    direccion, proc, usado, avisos = _abrir_con_respaldo(modo, puerto, notificador,
                                                         detached=True)
    if direccion:
        ok(f"Túnel {PROVEEDORES[usado]['nombre']} verificado: {direccion}")
    return direccion, proc, usado, avisos
