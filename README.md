# Setup2 — servidor de Minecraft en Codespaces, con túnel, panel y página pública

Un comando despliega un servidor Paper listo para jugar: descarga el jar, instala
los plugins, abre un túnel público, avisa por Discord, respalda el mundo y sube
los cambios a Git. El propio script levanta una **página local de control**
(estado, consola y acciones en el navegador); un panel opcional en Cloudflare
(carpeta `panel/`) permite auto-actualizar el script, cambiar la lista de
plugins, ver el estado en vivo y publicar esa misma interfaz como página
pública del servidor, y una **página de presentación** en Cloudflare Pages
(carpeta `pagina/`) hace de guía, descarga y fuente de actualizaciones.

## Uso rápido

```bash
cp .env.example .env     # rellena DISCORD_WEBHOOK y lo que uses (panel, página)
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
- **Página pública (opcional)**: guía y descarga del script en Cloudflare
  Pages; de ahí baja también la interfaz y las versiones nuevas.

## Flags principales

| Flag | Qué hace |
|------|----------|
| `--solo-notificar` | Deja servidor y túnel en segundo plano, avisa a Discord y sube a Git |
| `--parar` | Cierra el túnel y para el servidor |
| `--ui` | Solo la página local de control (localhost), sin tocar el servidor |
| `--puerto-ui N` | Puerto de la UI local (8080) |
| `--sin-ui` | No arrancar la UI local |
| `--publicar "mensaje"` | Publica esta versión: la sube al panel y/o despliega la página |
| `--actualizar` | Aplica la última versión publicada (panel o página) y sale |
| `--sin-actualizar` | No busca versiones nuevas (panel o página) al arrancar |
| `--reinstalar-plugins` | Vuelve a descargar los plugins |
| `--importar URL` | Restaura un mundo desde un respaldo (`owner/repo`, URL de GitHub o directa) |
| `--probar-webhook` | Manda un mensaje de prueba a Discord y sale |
| `--tunel-diag` | Diagnóstico del túnel |

`python3 setup_mc.py --help` lista todos.

## UI local (localhost)

Al ejecutar el script se levanta una página de control en
`http://127.0.0.1:8080` (en Codespaces, la URL reenviada aparece en la pestaña
**PORTS**; también puede verse con `python3 setup_mc.py --ui`):

- **Estado en vivo**: servidor (PID, puerto, memoria y tiempo en pie), túnel
  (dirección y proveedor), versiones y panel remoto.
- **Consola completa**: la salida del servidor tal cual (con colores ANSI) y
  envío de comandos al servidor (`list`, `save-all`, `op …`) con historial,
  autocompletado y botones rápidos. Funciona por el mismo canal (FIFO) que
  `echo 'op Jugador' > consola`.
- **Acciones**: parar (servidor + túnel), arrancar (modo segundo plano),
  respaldo ahora y prueba del webhook de Discord.
- **Temas**: cada `.css` de `templates/` es un tema seleccionable; el de
  defecto es `templates/template_default.css`.

En modo desacoplado (`--solo-notificar`) la UI corre como proceso aparte y
sobrevive a la salida del script; se apaga con su botón «apagar la ui». Solo
escucha en `127.0.0.1` (nunca se expone sola a internet) y no requiere clave.
El HTML y el JS están en `webui/` (archivos reales, sin nada embebido).

Esos mismos archivos viajan por el panel y por la página pública:
`--publicar` sube `webui/` + `templates/`, y el script los vuelve a bajar al
arrancar si cambiaron (sha256 por archivo, respaldo fuera del repo y
desactivables con `--sin-actualizar`). Así la interfaz evoluciona sin tocar el
script; además, la raíz del panel muestra esa misma página en **modo público**
(estado, IP y versiones, sin consola ni acciones: eso solo existe en localhost).

## Página pública (opcional)

En `pagina/` vive la página de presentación (guía de uso y descarga del
script, nada del panel) que se publica en Cloudflare Pages, por defecto
`https://tenohost.pages.dev`. Junto a ella se publican dos bloques:

- `script/` — `setup_mc.py` suelto (un solo archivo: si el paquete no está a
  su lado, se lo baja solo al ejecutarse), el paquete `setup_mc.tar.gz` y un
  `manifest.json` con versión, sha256 y changelog.
- `ui/` — la interfaz local (`index.html`, `app.js`, `templates/*.css`) y un
  `indice.json` con rutas y sha256.

Con `PAGINA_URL` en `.env` (por defecto la de arriba; `PAGINA_URL=no` la
desactiva) el script baja de ahí la interfaz al arrancar y también las
versiones nuevas de sí mismo, sin necesidad de panel. El paquete de descarga
solo trae los `.py`: la interfaz y los temas llegan solos en el primer
arranque.

Para desplegarla: define `PAGES_PROYECTO` en `.env` (nombre del proyecto en
Pages) y ejecuta `python3 setup_mc.py --publicar "qué cambió"` en una máquina
con `npx wrangler` autenticado (`npx wrangler login`). El primer despliegue
crea el proyecto.

## Panel (opcional)

En `panel/` vive un Worker de Cloudflare que sirve:

- `GET /` — la misma interfaz de la página local en **modo público** (estado
  en vivo, dirección del túnel, versiones y plugins; sin consola ni acciones).
  Si todavía no hay interfaz publicada, muestra el dashboard.
- `GET /panel` — dashboard de administración (estado, changelog y descarga).
- `GET /app.js` y `GET /tema/<x>.css` — archivos de la interfaz publicada.
- `GET /api/estado` — estado en el formato de la UI (público, para la página).
- `GET /api/temas` — temas publicados.
- `GET /api/manifest` — última versión publicada del script (versión, sha256,
  changelog, url del paquete). Es lo que consulta el script al arrancar.
- `GET /api/plugins` — registro de plugins que instala el script.
- `GET /api/ui` y `GET /api/ui/<ruta>` — índice (rutas + sha256) y archivos de
  la interfaz publicada.
- `GET /api/status` — estado del servidor (protegido con clave).
- `POST /api/publicar`, `POST /api/ui`, `POST /api/heartbeat`, `POST /api/plugins`
  — escritura con `Authorization: Bearer <PANEL_API_KEY>`.

Con `PANEL_URL` y `PANEL_API_KEY` en `.env`:

- Al arrancar, si hay versión nueva se descarga y aplica sola (sha256
  verificado, comprobación de que compila y respaldo fuera del repo). Desactívalo
  con `--sin-actualizar`.
- Publica tu versión con `python3 setup_mc.py --publicar "qué cambió"`: sube
  el script y también la interfaz (`webui/` + `templates/`).
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
| `panel` | Cliente del panel: auto-update (script e interfaz), latidos, plugins, `--publicar` |
| `pagina` | Página pública (Pages): construir/desplegar y bajadas estáticas (interfaz y script) |
| `webui` | Página local de control: API, consola por FIFO, acciones, temas |

Fuera del paquete: `webui/` (HTML/JS de la página local), `templates/`
(temas CSS; `template_default` es el de por defecto) y `pagina/` (la landing
que se publica en Pages).

## Seguridad

- `.env` nunca entra en Git (`.gitignore` gestionado) y se fuerza permisos 600.
- Los GET del panel son públicos a propósito (actualizarse no exige clave);
  publicar, ver estado y tocar plugins sí.
- El paquete de actualización se verifica por sha256 y se valida antes de
  aplicarse (solo `setup_mc.py` y `setup_mc/*.py`, compilación de prueba y
  respaldo de la versión anterior).
