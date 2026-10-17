/**
 * Panel + API del servidor de Minecraft (Cloudflare Workers).
 *
 * Rutas públicas (GET):
 *   /                 → dashboard HTML
 *   /api/manifest     → última versión del script (versión, sha256, changelog, url)
 *   /api/plugins      → registro de plugins que el script instala
 *   /api/instrucciones→ cómo conectar el script con este panel
 *   /dist/<archivo>   → paquete tar.gz del script
 *
 * Rutas con clave (Authorization: Bearer <PANEL_API_KEY> o ?clave=):
 *   GET  /api/status    → estado del servidor (heartbeat)
 *   POST /api/heartbeat → el script reporta que está vivo (o que se apagó)
 *   POST /api/publicar  → publica una versión nueva del script
 *   POST /api/plugins   → actualiza el registro de plugins
 *
 * Almacenamiento: un namespace KV (binding ESTADO) con las claves
 * manifest, plugins, estado y paquete (base64 del tar.gz).
 */

import { HTML_PANEL } from "./dashboard";

/** Si no hay heartbeat en este tiempo, el servidor se muestra como caído. */
const VENTANA_ONLINE_SEG = 15 * 60;
/** Tope de seguridad del paquete del script (KV aguanta 25 MB por valor). */
const MAX_PAQUETE_BYTES = 20 * 1024 * 1024;

interface Env {
  ESTADO: KVNamespace;
  PANEL_API_KEY?: string;
}

/** Plugins que el script instala si la API no dice otra cosa. */
const PLUGINS_DEFECTO = [
  {
    id: "viaversion",
    nombre: "ViaVersion.jar",
    fuente: "github",
    repo: "ViaVersion/ViaVersion",
    en_cada_ejecucion: false,
  },
  {
    id: "viabackwards",
    nombre: "ViaBackwards.jar",
    fuente: "github",
    repo: "ViaVersion/ViaBackwards",
    en_cada_ejecucion: false,
  },
  {
    id: "authme",
    nombre: "AuthMe.jar",
    fuente: "github",
    repo: "AuthMe/AuthMeReloaded",
    preferir: "-Paper.jar|AuthMe.jar",
    en_cada_ejecucion: false,
  },
  {
    id: "skinsrestorer",
    nombre: "SkinsRestorer.jar",
    fuente: "modrinth",
    proyecto: "skinsrestorer",
    carga: ["paper", "purpur", "spigot", "bukkit"],
    en_cada_ejecucion: true,
  },
];

function json(datos: unknown, estado = 200): Response {
  return new Response(JSON.stringify(datos, null, 2), {
    status: estado,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function texto(cuerpo: string, estado = 200, tipo = "text/plain; charset=utf-8"): Response {
  return new Response(cuerpo, { status: estado, headers: { "content-type": tipo } });
}

async function leerKV<T>(kv: KVNamespace, clave: string, porDefecto: T): Promise<T> {
  try {
    const crudo = await kv.get(clave);
    return crudo ? (JSON.parse(crudo) as T) : porDefecto;
  } catch {
    return porDefecto;
  }
}

function claveDe(req: Request): string | null {
  const cabecera = req.headers.get("Authorization") ?? "";
  if (cabecera.toLowerCase().startsWith("bearer ")) return cabecera.slice(7).trim();
  return new URL(req.url).searchParams.get("clave");
}

function autorizado(req: Request, env: Env): boolean {
  if (!env.PANEL_API_KEY) return false;
  const dada = claveDe(req);
  if (!dada) return false;
  // Comparación en tiempo constante (las claves no son largas, pero es gratis).
  if (dada.length !== env.PANEL_API_KEY.length) return false;
  let dif = 0;
  for (let i = 0; i < dada.length; i++) {
    dif |= dada.charCodeAt(i) ^ env.PANEL_API_KEY.charCodeAt(i);
  }
  return dif === 0;
}

function sinClave(motivo = "falta la clave o no coincide"): Response {
  return json({ error: motivo, pista: "usa Authorization: Bearer <PANEL_API_KEY>" }, 401);
}

function esCadenaCorta(v: unknown, max = 200): v is string {
  return typeof v === "string" && v.length > 0 && v.length <= max;
}

async function manejar(req: Request, env: Env): Promise<Response> {
  const url = new URL(req.url);
  const ruta = url.pathname.replace(/\/+$/, "") || "/";
  const metodo = req.method.toUpperCase();

  // ── Público ──────────────────────────────────────────────────────────
  if (metodo === "GET" && ruta === "/") {
    return texto(HTML_PANEL, 200, "text/html; charset=utf-8");
  }

  if (metodo === "GET" && ruta === "/api/manifest") {
    return json(await leerKV(env.ESTADO, "manifest", {}));
  }

  if (metodo === "GET" && ruta === "/api/plugins") {
    const guardado = await leerKV(env.ESTADO, "plugins", { plugins: PLUGINS_DEFECTO });
    return json(guardado);
  }

  if (metodo === "GET" && ruta === "/api/instrucciones") {
    const origen = url.origin;
    return json({
      panel: origen,
      env: {
        PANEL_URL: origen,
        PANEL_API_KEY: "(la misma que pusiste con: npx wrangler secret put PANEL_API_KEY)",
      },
      publicar: 'python3 setup_mc.py --publicar "mensaje del cambio"',
      actualizar: "python3 setup_mc.py --actualizar  (o simplemente arrancar el script)",
      nota: "Con PANEL_URL y PANEL_API_KEY en .env, el script consulta /api/manifest al arrancar y se actualiza solo si hay versión nueva.",
    });
  }

  if (metodo === "GET" && ruta.startsWith("/dist/")) {
    const paqueteB64 = await env.ESTADO.get("paquete");
    if (!paqueteB64) return json({ error: "todavía no hay ninguna versión publicada" }, 404);
    const bytes = base64ABytes(paqueteB64);
    return new Response(bytes, {
      headers: {
        "content-type": "application/gzip",
        "content-disposition": `attachment; filename="${ruta.slice("/dist/".length)}"`,
      },
    });
  }

  // ── Con clave ────────────────────────────────────────────────────────
  if (metodo === "GET" && ruta === "/api/status") {
    if (!autorizado(req, env)) return sinClave();
    const estado = await leerKV<Record<string, unknown> | null>(env.ESTADO, "estado", null);
    if (!estado) return json({ online: false, detalle: "nunca se recibió un heartbeat" });
    const cuando = Number(estado.cuando ?? 0);
    const online = Boolean(estado.online) && Date.now() / 1000 - cuando < VENTANA_ONLINE_SEG;
    return json({ ...estado, online, edad_seg: Math.round(Date.now() / 1000 - cuando) });
  }

  if (metodo === "POST" && ruta === "/api/heartbeat") {
    if (!autorizado(req, env)) return sinClave();
    let cuerpo: Record<string, unknown>;
    try {
      cuerpo = (await req.json()) as Record<string, unknown>;
    } catch {
      return json({ error: "el cuerpo debe ser JSON" }, 400);
    }
    const previo = await leerKV<Record<string, unknown>>(env.ESTADO, "estado", {});
    const estado = {
      online: Boolean(cuerpo.online),
      direccion: esCadenaCorta(cuerpo.direccion, 300)
        ? cuerpo.direccion
        : String(previo.direccion ?? ""),
      puerto: Number(cuerpo.puerto ?? previo.puerto ?? 25565),
      version_script: esCadenaCorta(cuerpo.version_script, 40)
        ? cuerpo.version_script
        : String(previo.version_script ?? "?"),
      version_mc: esCadenaCorta(cuerpo.version_mc, 40)
        ? cuerpo.version_mc
        : String(previo.version_mc ?? "?"),
      proveedor: esCadenaCorta(cuerpo.proveedor, 40)
        ? cuerpo.proveedor
        : String(previo.proveedor ?? ""),
      motivo: esCadenaCorta(cuerpo.motivo, 300) ? cuerpo.motivo : "",
      cuando: Date.now() / 1000,
    };
    await env.ESTADO.put("estado", JSON.stringify(estado));
    return json({ ok: true, guardado: estado });
  }

  if (metodo === "POST" && ruta === "/api/publicar") {
    if (!autorizado(req, env)) return sinClave();
    let cuerpo: Record<string, unknown>;
    try {
      cuerpo = (await req.json()) as Record<string, unknown>;
    } catch {
      return json({ error: "el cuerpo debe ser JSON" }, 400);
    }
    const { version, sha256, changelog, paquete_b64 } = cuerpo;
    if (!esCadenaCorta(version, 40)) return json({ error: "falta version" }, 400);
    if (!esCadenaCorta(sha256, 64) || !/^[0-9a-f]{64}$/.test(sha256)) {
      return json({ error: "sha256 debe ser hex de 64" }, 400);
    }
    if (typeof paquete_b64 !== "string" || !paquete_b64) return json({ error: "falta paquete_b64" }, 400);
    if (paquete_b64.length > MAX_PAQUETE_BYTES * 1.4) {
      return json({ error: `paquete demasiado grande (>${MAX_PAQUETE_BYTES} bytes)` }, 413);
    }
    const manifest = {
      version,
      sha256,
      changelog: esCadenaCorta(changelog, 4000) ? changelog : "",
      url: `${url.origin}/dist/setup_mc-${version}.tar.gz`,
      cuando: Date.now() / 1000,
    };
    await env.ESTADO.put("paquete", paquete_b64);
    await env.ESTADO.put("manifest", JSON.stringify(manifest));
    return json({ ok: true, manifest });
  }

  if (metodo === "POST" && ruta === "/api/plugins") {
    if (!autorizado(req, env)) return sinClave();
    let cuerpo: { plugins?: unknown };
    try {
      cuerpo = (await req.json()) as { plugins?: unknown };
    } catch {
      return json({ error: "el cuerpo debe ser JSON" }, 400);
    }
    if (!Array.isArray(cuerpo.plugins)) return json({ error: "falta la lista plugins" }, 400);
    const limpios = [];
    for (const p of cuerpo.plugins) {
      if (typeof p !== "object" || p === null) continue;
      const it = p as Record<string, unknown>;
      if (!esCadenaCorta(it.nombre, 120)) continue;
      if (!esCadenaCorta(it.fuente, 12) || !["github", "modrinth"].includes(it.fuente)) continue;
      if (it.fuente === "github" && !esCadenaCorta(it.repo, 120)) continue;
      if (it.fuente === "modrinth" && !esCadenaCorta(it.proyecto, 120)) continue;
      limpios.push({
        id: esCadenaCorta(it.id, 40) ? it.id : it.nombre.toLowerCase().replace(/[^a-z0-9]+/g, ""),
        nombre: it.nombre,
        fuente: it.fuente,
        repo: it.repo,
        proyecto: it.proyecto,
        preferir: esCadenaCorta(it.preferir, 120) ? it.preferir : "",
        carga: Array.isArray(it.carga) ? it.carga.filter((c) => esCadenaCorta(c, 20)) : [],
        en_cada_ejecucion: Boolean(it.en_cada_ejecucion),
      });
    }
    if (!limpios.length) return json({ error: "ningún plugin válido en la lista" }, 400);
    await env.ESTADO.put("plugins", JSON.stringify({ plugins: limpios }));
    return json({ ok: true, plugins: limpios });
  }

  return json({ error: "ruta no encontrada", ruta, metodo }, 404);
}

function base64ABytes(b64: string): Uint8Array {
  const binario = atob(b64);
  const bytes = new Uint8Array(binario.length);
  for (let i = 0; i < binario.length; i++) bytes[i] = binario.charCodeAt(i);
  return bytes;
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    try {
      return await manejar(req, env);
    } catch (e) {
      return json({ error: String(e) }, 500);
    }
  },
};
