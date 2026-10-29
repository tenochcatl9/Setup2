"""Java, descarga de Paper, EULA y primer arranque."""

import json, os, queue, re, shutil, subprocess, tempfile, threading, time, zipfile
from pathlib import Path
import requests
from . import mcserver
from .config import (EULA_FILE, PAPER_API, PID_SERVIDOR, PID_TUNEL, PLUGINS_DIR,
                     PUERTO_MC, RESPALDO_DIR, RE_VERSION_ESTABLE, SERVER_DIR,
                     SERVER_JAR, TIMEOUT_ARRANQUE, USER_AGENT)
from .ui import Barra, aviso, error, log, ok
from .procesos import (_bombeo_salida, _detener_proceso, _drenar, _lanzar_puente_consola,
                       _leer_pid, _vivo, args_sin_backup_auto, parar_todo, puerto_abierto)
from .respaldo import (BACKUP_AUTO_MANTENER, BACKUP_AUTO_MINUTOS,
                       arrancar_backups_auto, crear_respaldo_preactualizacion)
from .versiones import _version_mc_del_jar

# ─── Requisitos ───────────────────────────────────────────────────────────
def comprobar_java() -> str:
    """Verifica que Java esté disponible y devuelve la versión."""
    java = shutil.which("java")
    if not java:
        raise RuntimeError("No se encontró 'java' en el PATH. Instala Java 21+ y reintenta.")
    try:
        r = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=30)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"No se pudo ejecutar java: {e}")
    salida = (r.stderr or "") + (r.stdout or "")
    m = re.search(r'version "(\d+)', salida)
    if not m:
        raise RuntimeError("No se pudo determinar la versión de Java.")
    version = int(m.group(1))
    if version < 21:
        aviso(f"Java {version} es antiguo; Paper 1.21+ pide Java 21 o superior.")
    return str(version)

# ─── 1. Detectar si ya existe el servidor ─────────────────────────────────
def servidor_existe() -> bool:
    return SERVER_JAR.is_file()

# ─── 2. Descargar Paper (última versión estable) ───────────────────────────
def obtener_versiones_candidatas() -> list:
    """Devuelve las versiones estables (p. ej. 1.21.11) de más nueva a más vieja.

    La API v3 devuelve {"versions": {familia: [versiones...]}} donde la
    primera familia puede ser una generación de Paper (26.x) sin builds
    STABLE. Por eso se aplanan los valores y se filtran los sufijos
    -rc/-pre/-snapshot.
    """
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(PAPER_API, headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()
    familias = data.get("versions")
    if not familias:
        raise RuntimeError("No se encontraron versiones en la API de PaperMC.")
    candidatas = []
    for familia in familias.values():
        for v in familia:
            if isinstance(v, str) and RE_VERSION_ESTABLE.match(v) and v not in candidatas:
                candidatas.append(v)
    if not candidatas:
        raise RuntimeError("No se encontraron versiones estables en la API de PaperMC.")
    return candidatas

def obtener_build_estable(version: str, mostrar: bool = True) -> dict:
    """Devuelve el build estable más reciente para una versión concreta."""
    if mostrar:
        log(f"Buscando build estable para {version}...")
    headers = {"User-Agent": USER_AGENT}
    url = f"{PAPER_API}/versions/{version}/builds"
    r = requests.get(url, headers=headers, timeout=20)
    if r.status_code == 404:
        raise RuntimeError(f"La versión {version} no existe en la API.")
    r.raise_for_status()
    payload = r.json()
    builds = payload.get("builds") if isinstance(payload, dict) else payload
    if not isinstance(builds, list):
        raise RuntimeError(f"Respuesta inesperada de la API para la versión {version}.")
    estables = sorted(
        [b for b in builds if b.get("channel") == "STABLE"],
        key=lambda b: b.get("id", 0),
        reverse=True,
    )
    if not estables:
        raise RuntimeError(f"No hay build estable para la versión {version}.")
    build = estables[0]
    if mostrar:
        log(f"Build estable encontrado: #{build['id']}")
    return build

def descargar_paper(version: str | None = None,
                    destino: Path | None = None) -> dict:
    """Descarga el JAR de Paper y lo guarda como server.jar (o en `destino`).

    Con `version` se baja esa versión concreta; sin ella, la última estable
    disponible. Devuelve los datos de la versión y el build descargados.
    """
    candidatas = [version] if version else obtener_versiones_candidatas()
    build = None
    version_elegida = None
    ultimo_error = None
    for candidata in candidatas:
        try:
            build = obtener_build_estable(candidata, mostrar=False)
            version_elegida = candidata
            break
        except RuntimeError as e:
            ultimo_error = e
            log(f"Versión {candidata} descartada: {e}")
    if build is None:
        raise RuntimeError(
            f"No se encontró ninguna versión con build estable. Último error: {ultimo_error}")
    try:
        descarga = build["downloads"]["server:default"]
        url_descarga = descarga["url"]
    except (KeyError, TypeError):
        raise RuntimeError(f"El build #{build.get('id')} no trae URL de descarga.")
    tamano = int(descarga.get("size") or 0)
    destino_final = Path(destino) if destino else SERVER_JAR
    destino_final.parent.mkdir(parents=True, exist_ok=True)
    log(f"Descargando Paper {version_elegida} build #{build['id']}")
    log(f"  {url_descarga}")
    headers = {"User-Agent": USER_AGENT}
    temporal = destino_final.with_name(destino_final.name + ".part")
    try:
        with requests.get(url_descarga, headers=headers, stream=True, timeout=120) as r:
            r.raise_for_status()
            total = tamano or int(r.headers.get("Content-Length") or 0)
            barra = Barra(f"Descargando {destino_final.name}", total=total or None)
            with open(temporal, "wb") as f:
                for chunk in r.iter_content(chunk_size=262144):
                    if chunk:
                        f.write(chunk)
                        if total:
                            barra.avanzar(len(chunk))
            barra.finalizar("Descarga completada")
    except Exception:
        temporal.unlink(missing_ok=True)
        raise
    temporal.replace(destino_final)
    log(f"Paper {version_elegida} build #{build['id']} guardado en {destino_final}")
    return {
        "version": version_elegida,
        "build": build["id"],
        "destino": str(destino_final),
    }

# ─── 3. Aceptar EULA ──────────────────────────────────────────────────────
def aceptar_eula():
    if EULA_FILE.is_file() and "eula=true" in EULA_FILE.read_text().lower():
        log("EULA ya aceptada.")
        return
    EULA_FILE.write_text("eula=true\n")
    log(f"EULA aceptada ({EULA_FILE} creado).")

def generar_plugins_dir(timeout_arranque: int = TIMEOUT_ARRANQUE):
    """Ejecuta el servidor brevemente para que cree la carpeta plugins y el mundo."""
    log("Arrancando el servidor por primera vez para generar mundo y plugins...")
    cola: queue.Queue = queue.Queue()
    try:
        proc = subprocess.Popen(
            ["java", f"-Xms{mcserver.MEMORIA_INICIAL}", f"-Xmx{mcserver.MEMORIA_MAXIMA}",
             "-jar", str(SERVER_JAR), "nogui"],
            cwd=str(SERVER_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        raise RuntimeError("No se encontró 'java' en el PATH. Instala Java 21+ antes de continuar.")
    threading.Thread(target=_bombeo_salida, args=(proc, cola), daemon=True).start()
    barra = Barra("Generando mundo y carpeta plugins", limite=timeout_arranque)
    try:
        listo = False
        while time.time() - barra.inicio < timeout_arranque:
            if proc.poll() is not None:
                raise RuntimeError(
                    "El servidor terminó prematuramente. Ejecuta "
                    f"'java -jar {SERVER_JAR.name} nogui' a mano para ver el error.")
            for texto in _drenar(cola):
                if 'Done (' in texto or "For help, type" in texto:
                    listo = True
            if listo and PLUGINS_DIR.is_dir():
                break
            barra.refrescar()
            time.sleep(0.2)
        if listo:
            barra.finalizar("Mundo y plugins generados")
        else:
            barra.finalizar("Aviso: no se confirmó el arranque, se continúa igualmente")
        # El puente del FIFO hace que 'echo "save-all flush" > consola' y el
        # respaldo automático hablen con el servidor también en primer plano.
        _lanzar_puente_consola(proc)
        if not args_sin_backup_auto():
            if arrancar_backups_auto():
                log(f"Respaldo automático cada {BACKUP_AUTO_MINUTOS} min en "
                    f"respaldo/ (se guardan los {BACKUP_AUTO_MANTENER} últimos).")
    finally:
        _detener_proceso(proc, escribir_stop=True)
        log("Servidor detenido.")

# ─── Versión instalada y actualización de Paper ───────────────────────────
# La comprobación de la última estable se hace en segundo plano para que la
# UI no se quede esperando a PaperMC. El CLI puede pedirla en primer plano.
_CACHE_ULTIMA = {"ts": 0.0, "info": None, "error": "", "consultando": False}
_CANDADO_ULTIMA = threading.Lock()

def version_del_jar(ruta: Path | None = None) -> str:
    """Versión de Minecraft que declara un server.jar de Paper, sin arrancarlo."""
    ruta = Path(ruta) if ruta else SERVER_JAR
    try:
        with zipfile.ZipFile(ruta) as z:
            if "version.json" not in z.namelist():
                return ""
            datos = json.loads(z.read("version.json").decode("utf-8", errors="replace"))
            return str(datos.get("id") or "")
    except (OSError, zipfile.BadZipFile, ValueError, KeyError):
        return ""

def version_instalada() -> str:
    """Versión de Minecraft del server.jar actual (o cadena vacía)."""
    return version_del_jar(SERVER_JAR) or _version_mc_del_jar()

def _consultar_ultima() -> dict | None:
    """Busca la última versión estable con builds y la deja en la caché."""
    info = None
    try:
        for version in obtener_versiones_candidatas():
            try:
                build = obtener_build_estable(version, mostrar=False)
            except RuntimeError:
                continue
            descarga = build.get("downloads", {}).get("server:default", {})
            info = {
                "version": version,
                "build": build.get("id"),
                "url": descarga.get("url", ""),
                "size": int(descarga.get("size") or 0),
            }
            break
        if info is None:
            raise RuntimeError("PaperMC no devolvió ninguna build estable.")
        with _CANDADO_ULTIMA:
            _CACHE_ULTIMA.update(ts=time.time(), info=info, error="")
    except Exception as e:  # sin red o API caída: la UI sigue funcionando
        with _CANDADO_ULTIMA:
            _CACHE_ULTIMA["error"] = f"{type(e).__name__}: {e}"
    finally:
        with _CANDADO_ULTIMA:
            _CACHE_ULTIMA["consultando"] = False
    return info

def ultima_estable(esperar: bool = False, ttl: int = 3600) -> dict | None:
    """Última estable de Paper, cacheada una hora.

    Sin `esperar`, la consulta se lanza en un hilo y se devuelve lo que ya se
    sepa (la UI llama a esta función cada pocos segundos, no debe bloquearse).
    Con `esperar`, si ya hay otra consulta en marcha se espera a que termine.
    """
    limite = time.time() + 30
    while True:
        lanzar = False
        with _CANDADO_ULTIMA:
            info = _CACHE_ULTIMA["info"]
            if info and time.time() - _CACHE_ULTIMA["ts"] < ttl:
                return info
            if not _CACHE_ULTIMA["consultando"]:
                _CACHE_ULTIMA["consultando"] = True
                lanzar = True
            elif not esperar:
                return info
        if lanzar:
            # _consultar_ultima toma el candado por su cuenta: se llama SIEMPRE
            # fuera del `with` (si no, el hilo se bloquea a sí mismo).
            if not esperar:
                threading.Thread(target=_consultar_ultima, daemon=True).start()
                return info
            return _consultar_ultima() or info
        if time.time() > limite:
            with _CANDADO_ULTIMA:
                return _CACHE_ULTIMA["info"]
        time.sleep(0.1)

def estado_paper() -> dict:
    """Datos para la UI: versión instalada, última estable y si toca actualizar."""
    actual = version_instalada()
    if os.environ.get("SETUP_MC_SIN_ACTUALIZAR") == "1":
        # --sin-actualizar apaga también esta comprobación: ni red ni aviso.
        return {"actual": actual, "ultima": "", "build": None, "hay_nueva": False,
                "consultando": False, "error": ""}
    info = ultima_estable()
    ultima = str(info.get("version") or "") if info else ""
    return {
        "actual": actual,
        "ultima": ultima,
        "build": info.get("build") if info else None,
        "hay_nueva": bool(actual and ultima and actual != ultima),
        "consultando": bool(_CACHE_ULTIMA["consultando"]),
        "error": _CACHE_ULTIMA["error"][:160],
    }

def avisar_si_hay_nueva() -> None:
    """Aviso informativo al arrancar; nunca actualiza por su cuenta."""
    info = ultima_estable(esperar=True)
    actual = version_instalada()
    if info and actual and actual != info["version"]:
        aviso(f"Paper {actual} instalado; la última estable es {info['version']}. "
              "Actualiza con: python3 setup_mc.py --actualizar-paper")

def _puerto_servidor() -> int:
    try:
        for linea in (SERVER_DIR / "server.properties").read_text(errors="replace").splitlines():
            if linea.strip().startswith("server-port="):
                return int(linea.split("=", 1)[1].strip())
    except (OSError, ValueError):
        pass
    return PUERTO_MC

def actualizar_paper(forzar: bool = False) -> dict | None:
    """Actualiza server.jar a la última estable, con respaldo y rollback del jar.

    El mundo no se toca aquí: Paper lo migra en el primer arranque. Antes se para
    el servidor y se empaqueta el mundo + configuración + jar en un tar local.
    Devuelve un dict con el resultado; None si no se pudo actualizar.
    """
    if not SERVER_JAR.is_file():
        error("No hay server.jar: crea o importa el servidor antes de actualizar Paper.")
        return None
    actual = version_instalada()
    info = ultima_estable(esperar=True)
    if not info:
        error(f"No se pudo consultar la última versión de Paper: {_CACHE_ULTIMA['error']}")
        return None
    ultima = str(info.get("version") or "")
    if actual == ultima and not forzar:
        ok(f"Paper ya está en la última versión estable ({actual}).")
        return {"ok": True, "actualizada": False, "actual": actual, "ultima": ultima}
    if not ultima:
        error("PaperMC no devolvió una versión válida.")
        return None

    log(f"Paper: {actual or 'desconocida'} -> {ultima} (build {info.get('build')})")
    puerto = _puerto_servidor()
    if (_vivo(_leer_pid(PID_SERVIDOR)) or _vivo(_leer_pid(PID_TUNEL))
            or puerto_abierto(puerto)):
        log("Parando servidor y túnel antes de actualizar (se guardará el mundo)...")
        parar_todo()
    if puerto_abierto(puerto):
        error(f"El puerto {puerto} sigue ocupado por otro proceso: no se actualiza.")
        return None

    respaldo = crear_respaldo_preactualizacion(actual)
    if not respaldo:
        error("No se pudo respaldar la instalación; Paper no se ha tocado.")
        return None
    # El jar anterior queda al lado del respaldo para poder volver atrás sin
    # descomprimir cientos de MB. Es local (respaldo/server-*.jar está ignorado).
    copia_anterior = RESPALDO_DIR / f"server-{actual or 'anterior'}.jar"
    try:
        shutil.copy2(SERVER_JAR, copia_anterior)
    except OSError as e:
        error(f"No se pudo conservar el server.jar anterior: {e}")
        return None

    temporal = Path(tempfile.NamedTemporaryFile(
        suffix=".jar", prefix="server-nuevo-", dir=str(SERVER_DIR), delete=False).name)
    try:
        descargar_paper(version=ultima, destino=temporal)
        if version_del_jar(temporal) != ultima:
            raise RuntimeError("el jar descargado no declara la versión esperada")
        temporal.replace(SERVER_JAR)
    except Exception as e:  # red, API, jar inválido...
        temporal.unlink(missing_ok=True)
        error(f"No se pudo descargar o verificar Paper {ultima}: {e}")
        error(f"El server.jar anterior sigue intacto y el respaldo está en {respaldo}.")
        return None

    ok(f"Paper {ultima} instalado. El mundo (formato {actual}) se migrará "
       "al arrancar; el respaldo previo es " f"{respaldo}.")
    return {
        "ok": True,
        "actualizada": True,
        "actual": actual,
        "ultima": ultima,
        "anterior": str(copia_anterior),
        "respaldo": str(respaldo),
    }

def revertir_paper(resultado: dict | None) -> bool:
    """Restaura el server.jar anterior si el nuevo no llegó a arrancar."""
    anterior = (resultado or {}).get("anterior")
    if not anterior or not Path(anterior).is_file():
        return False
    try:
        shutil.copy2(anterior, SERVER_JAR)
    except OSError as e:
        error(f"No se pudo restaurar el Paper anterior: {e}")
        return False
    aviso(f"Se restauró el server.jar anterior ({Path(anterior).name}). "
          f"El respaldo del mundo sigue en {resultado.get('respaldo')}.")
    return True
