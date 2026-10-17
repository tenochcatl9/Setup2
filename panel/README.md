# Panel del servidor de Minecraft (Cloudflare Worker)

Sirve cuatro cosas:

1. **Auto-actualización del script**: publica versiones de `setup_mc.py` +
   `setup_mc/` y el script las descarga al arrancar (con verificación sha256).
2. **Registro de plugins**: la lista de jars que instala el script vive aquí,
   así se puede cambiar sin tocar el código.
3. **Estado en vivo**: el script manda un *heartbeat* cada 5 minutos y el
   dashboard muestra ONLINE/CAÍDO con la dirección del túnel.
4. **Página pública**: la misma interfaz de la página local (`webui/` +
   `templates/`, subida con `--publicar`) se muestra en `/` en *modo público*
   (estado, IP y versiones, sin consola ni acciones) y el script la vuelve a
   bajar al arrancar si cambió.

## Pasos de despliegue (una vez)

```bash
cd panel
npm install
npx wrangler login                # abre el navegador de Cloudflare

# 1. Crea el espacio de datos (KV) y copia el id que imprime
npx wrangler kv namespace create ESTADO
# → pega el id en wrangler.jsonc (campo "id")

# 2. Define la clave del panel (la misma irá en el .env del script)
openssl rand -hex 24              # genera una clave
npx wrangler secret put PANEL_API_KEY

# 3. Despliega
npx wrangler deploy
# → imprime la URL pública: https://setup-mc-panel.TU-SUBDOMINIO.workers.dev
```

Para desarrollo local: copia `.dev.vars.example` a `.dev.vars` y usa
`npm run dev`; el KV local y la clave son independientes de producción.

## Conectar el script (en la máquina del servidor)

Añade al `.env` de la raíz del proyecto:

```
PANEL_URL=https://setup-mc-panel.TU-SUBDOMINIO.workers.dev
PANEL_API_KEY=la-misma-clave-del-paso-2
```

Con eso:

- Al arrancar, el script consulta `/api/manifest`; si hay una versión más
  nueva se actualiza solo (guarda la anterior en `respaldo/script-antiguo-*/`).
  `--sin-actualizar` lo desactiva.
- `python3 setup_mc.py --publicar "mensaje del cambio"` empaqueta la versión
  local, la sube al panel y actualiza el manifest; además publica la interfaz
  (`webui/` + `templates/`) como página pública. El resto de máquinas se
  actualizarán (script e interfaz) al arrancar.
- Mientras corre el servidor, el script reporta su estado al panel.

## Rutas de la API

| Método | Ruta              | Clave | Qué hace                            |
|--------|-------------------|-------|-------------------------------------|
| GET    | `/`               | no    | Página pública (interfaz subida) o dashboard |
| GET    | `/panel`          | no    | Dashboard de administración         |
| GET    | `/app.js`         | no    | JS de la interfaz publicada         |
| GET    | `/tema/<x>.css`   | no    | Tema (CSS) de la interfaz publicada |
| GET    | `/api/manifest`   | no    | Última versión publicada del script |
| GET    | `/api/plugins`    | no    | Registro de plugins                 |
| GET    | `/api/estado`     | no    | Estado público (formato de la UI)   |
| GET    | `/api/temas`      | no    | Temas de la interfaz publicada      |
| GET    | `/api/ui`         | no    | Índice de la interfaz (rutas + sha256) |
| GET    | `/api/ui/<ruta>`  | no    | Un archivo de la interfaz publicada |
| GET    | `/api/instrucciones` | no | Cómo conectar el script             |
| GET    | `/dist/<archivo>` | no    | Descarga del paquete (`tar.gz`)     |
| GET    | `/api/status`     | sí    | Estado del servidor (heartbeat)     |
| POST   | `/api/heartbeat`  | sí    | El script reporta que está vivo     |
| POST   | `/api/publicar`   | sí    | Publica una versión nueva           |
| POST   | `/api/ui`         | sí    | Publica la interfaz (reemplazo completo) |
| POST   | `/api/plugins`    | sí    | Cambia el registro de plugins       |

La clave se manda como `Authorization: Bearer <clave>` (o `?clave=`).
Los GET de manifest/plugins/dist son públicos a propósito: cualquier máquina
puede actualizar el script sin conocer la clave; publicar y ver el estado sí
la exigen.

## Formato de cada cosa (para depurar)

- `manifest`: `{ "version", "sha256", "changelog", "url", "cuando" }`
- `plugins`: `{ "plugins": [ { "id", "nombre", "fuente": "github"|"modrinth",
  "repo"|"proyecto", "preferir"?, "carga"?, "en_cada_ejecucion" } ] }`
- `estado`: `{ "online", "direccion", "puerto", "version_script",
  "version_mc", "proveedor", "motivo", "cuando" }`
- `ui_indice`: `{ "version", "cuando", "archivos": [ { "ruta", "sha256",
  "tamano", "tipo" } ] }`; cada archivo vive en `ui:<ruta>`. Rutas válidas:
  `index.html`, `app.js` y `templates/<tema>.css` (máx. 512 KB por archivo y
  4 MB en total).
- `paquete`: el tar.gz en base64 (KV admite hasta 25 MB por valor).
