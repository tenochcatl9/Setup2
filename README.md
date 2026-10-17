# Setup2 — servidor de Minecraft en Codespaces, con túnel y panel

Un comando despliega un servidor Paper listo para jugar: descarga el jar, instala
los plugins, abre un túnel público, avisa por Discord, respalda el mundo y sube
los cambios a Git. Un panel opcional en Cloudflare (carpeta `panel/`) permite
auto-actualizar el script, cambiar la lista de plugins y ver el estado en vivo.

## Uso rápido

```bash
cp .env.example .env     # rellena DISCORD_WEBHOOK y, si quieres panel, PANEL_URL/PANEL_API_KEY
python3 setup_mc.py
```

## Qué hace el script

- **Java y Paper**: comprueba Java, descarga la última build de Paper para la
  versión de MC que corresponda y configura eula/propiedades/ops.
- **Plugins**: ViaVersion + ViaBackwards (compatibilidad de versiones), AuthMe
  (registro/login adaptado a túnel) y SkinsRestorer. La lista puede venir del
  panel sin tocar el código.
- **Túnel público**: Asyx con Pinggy de respaldo; barra de progreso y avisos a
  Discord, incluida la dirección final.
- **Respaldos**: comprime el mundo y lo sube a Git; respaldos automáticos
  mientras el servidor corre.
- **Panel (opcional)**: auto-actualización del propio script, estado en vivo y
  publicación de versiones.

## Flags principales

| Flag | Qué hace |
|------|----------|
| `--solo-notificar` | Deja servidor y túnel en segundo plano, avisa a Discord y sube a Git |
| `--parar` | Cierra el túnel y para el servidor |
| `--publicar "mensaje"` | Publica esta versión del script en el panel |
| `--actualizar` | Aplica la última versión publicada en el panel y sale |
| `--sin-actualizar` | No busca versiones nuevas al arrancar |
| `--reinstalar-plugins` | Vuelve a descargar los plugins |
| `--importar URL` | Restaura un mundo desde un respaldo (`owner/repo`, URL de GitHub o directa) |
| `--probar-webhook` | Manda un mensaje de prueba a Discord y sale |
| `--tunel-diag` | Diagnóstico del túnel |

`python3 setup_mc.py --help` lista todos.

## Panel (opcional)

En `panel/` vive un Worker de Cloudflare que sirve:

- `GET /api/manifest` — última versión publicada del script (versión, sha256,
  changelog, url del paquete). Es lo que consulta el script al arrancar.
- `GET /api/plugins` — registro de plugins que instala el script.
- `GET /api/status` — estado del servidor (protegido con clave).
- `POST /api/publicar`, `POST /api/heartbeat`, `POST /api/plugins` — escritura
  con `Authorization: Bearer <PANEL_API_KEY>`.
- `GET /` — dashboard con estado en vivo, changelog y descarga.

Con `PANEL_URL` y `PANEL_API_KEY` en `.env`:

- Al arrancar, si hay versión nueva se descarga y aplica sola (sha256
  verificado, comprobación de que compila y respaldo fuera del repo). Desactívalo
  con `--sin-actualizar`.
- Publica tu versión con `python3 setup_mc.py --publicar "qué cambió"`.
- Mientras el servidor vive, un proceso ligero manda un latido cada 5 minutos y
  avisa del apagado (funciona también con `--solo-notificar`).

Sin `PANEL_URL` el script funciona exactamente igual que siempre: cero llamadas
a la API. El despliegue del Worker está paso a paso en `panel/README.md`.

## Estructura del código

`setup_mc.py` es una entrada fina (flags + `main`); el resto vive en `setup_mc/`:

| Módulo | Responsabilidad |
|--------|-----------------|
| `config` | Constantes y rutas (incluida `VERSION_SCRIPT`) |
| `ui` | Colores, logs, barra de progreso |
| `procesos` | Lanzar/detener procesos, consola FIFO, PIDs |
| `versiones` | Versión de Minecraft del `server.jar` |
| `paper` | Descarga de Paper y comprobación de Java |
| `mcserver` | Arranque del servidor, memoria, espera del puerto |
| `jars` | Plugins (GitHub/Modrinth), AuthMe, SkinsRestorer |
| `gitops` | `.gitignore` gestionado, protección de secretos, git |
| `avisos` | Discord: webhook, notificaciones |
| `tuneles` | Asyx y Pinggy, notificador de progreso |
| `estado` | Modo desacoplado (`--solo-notificar`) |
| `respaldo` | Respaldos del mundo, `subir_cambios` |
| `panel` | Cliente del panel: auto-update, latidos, plugins, `--publicar` |

## Seguridad

- `.env` nunca entra en Git (`.gitignore` gestionado) y se fuerza permisos 600.
- Los GET del panel son públicos a propósito (actualizarse no exige clave);
  publicar, ver estado y tocar plugins sí.
- El paquete de actualización se verifica por sha256 y se valida antes de
  aplicarse (solo `setup_mc.py` y `setup_mc/*.py`, compilación de prueba y
  respaldo de la versión anterior).
