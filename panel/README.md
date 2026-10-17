# Panel del servidor de Minecraft (Cloudflare Worker)

Sirve tres cosas:

1. **Auto-actualización del script**: publica versiones de `setup_mc.py` +
   `setup_mc/` y el script las descarga al arrancar (con verificación sha256).
2. **Registro de plugins**: la lista de jars que instala el script vive aquí,
   así se puede cambiar sin tocar el código.
3. **Estado en vivo**: el script manda un *heartbeat* cada 5 minutos y el
   dashboard muestra ONLINE/CAÍDO con la dirección del túnel.

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
  local, la sube al panel y actualiza el manifest. El resto de máquinas se
  actualizarán al arrancar.
- Mientras corre el servidor, el script reporta su estado al panel.

## Rutas de la API

| Método | Ruta              | Clave | Qué hace                            |
|--------|-------------------|-------|-------------------------------------|
| GET    | `/`               | no    | Dashboard                           |
| GET    | `/api/manifest`   | no    | Última versión publicada del script |
| GET    | `/api/plugins`    | no    | Registro de plugins                 |
| GET    | `/api/instrucciones` | no | Cómo conectar el script             |
| GET    | `/dist/<archivo>` | no    | Descarga del paquete (`tar.gz`)     |
| GET    | `/api/status`     | sí    | Estado del servidor (heartbeat)     |
| POST   | `/api/heartbeat`  | sí    | El script reporta que está vivo     |
| POST   | `/api/publicar`   | sí    | Publica una versión nueva           |
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
- `paquete`: el tar.gz en base64 (KV admite hasta 25 MB por valor).
