/**
 * Dashboard del panel: página única servida por el Worker, sin dependencias
 * externas. La clave (PANEL_API_KEY) se guarda en localStorage y solo se usa
 * para consultar el estado en vivo y para las acciones que la requieren.
 */

export const HTML_PANEL = `<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Panel del servidor de Minecraft</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px; font-family: system-ui, sans-serif;
    background: #11141a; color: #e6e9ef; line-height: 1.5;
  }
  main { max-width: 860px; margin: 0 auto; }
  h1 { font-size: 1.3rem; margin: 0 0 4px; }
  .sub { color: #8b93a3; font-size: .85rem; margin-bottom: 20px; }
  section {
    background: #181c24; border: 1px solid #242a35; border-radius: 12px;
    padding: 16px 18px; margin-bottom: 16px;
  }
  h2 { font-size: .95rem; margin: 0 0 12px; color: #9db1d6; text-transform: uppercase; letter-spacing: .06em; }
  .fila { display: flex; flex-wrap: wrap; gap: 8px 24px; align-items: baseline; }
  .k { color: #8b93a3; font-size: .82rem; }
  .v { font-weight: 600; }
  code, pre {
    background: #0d1016; border: 1px solid #242a35; border-radius: 8px;
    padding: 2px 7px; font-size: .85rem; color: #a8e6a3;
  }
  pre { padding: 12px; overflow-x: auto; white-space: pre-wrap; }
  button {
    background: #2b3a5c; color: #e6e9ef; border: 0; border-radius: 8px;
    padding: 8px 14px; font-size: .85rem; cursor: pointer;
  }
  button:hover { background: #35476f; }
  input[type=password], input[type=text] {
    background: #0d1016; border: 1px solid #242a35; color: #e6e9ef;
    border-radius: 8px; padding: 8px 10px; font-size: .85rem; width: 100%;
  }
  .punto { display: inline-block; width: 12px; height: 12px; border-radius: 50%; margin-right: 8px; vertical-align: middle; }
  .verde { background: #35c46b; box-shadow: 0 0 10px #35c46b88; }
  .rojo { background: #e05353; box-shadow: 0 0 10px #e0535388; }
  .gris { background: #55607a; }
  table { width: 100%; border-collapse: collapse; font-size: .85rem; }
  th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid #242a35; }
  th { color: #8b93a3; font-weight: 600; }
  .aviso { color: #e0b955; font-size: .85rem; }
  .error { color: #e05353; font-size: .85rem; }
  a { color: #7fb2ff; }
  .copiable { cursor: pointer; }
  .pie { color: #55607a; font-size: .78rem; margin-top: 24px; }
  .oculto { display: none; }
</style>
</head>
<body>
<main>
  <h1>Panel del servidor de Minecraft</h1>
  <div class="sub">Auto-actualización del script y estado del servidor en vivo.</div>

  <section>
    <h2>Clave del panel</h2>
    <div class="fila" style="margin-bottom:10px;">
      <input type="password" id="clave" placeholder="PANEL_API_KEY (solo se guarda en este navegador)">
      <button onclick="guardarClave()">Guardar</button>
    </div>
    <div class="aviso" id="claveAviso">La clave hace falta para ver el estado y publicar. Se guarda en localStorage.</div>
  </section>

  <section>
    <h2>Estado del servidor</h2>
    <div id="estadoLinea"><span class="punto gris"></span>Consultando…</div>
    <div id="estadoDetalle" class="fila" style="margin-top:10px;"></div>
    <div class="aviso oculto" id="estadoAviso"></div>
  </section>

  <section>
    <h2>Script (auto-actualización)</h2>
    <div class="fila" id="scriptLinea"><span class="k">versión publicada:</span> <span class="v" id="version">—</span></div>
    <div id="scriptDetalle" style="margin-top:10px;"></div>
    <details style="margin-top:10px;"><summary class="k">changelog</summary><pre id="changelog">—</pre></details>
    <div style="margin-top:10px;"><a id="descarga" href="#">Descargar última versión (tar.gz)</a></div>
  </section>

  <section>
    <h2>Plugins que instala el script</h2>
    <table>
      <thead><tr><th>Jar</th><th>Fuente</th><th>Política</th></tr></thead>
      <tbody id="plugins"><tr><td colspan="3">cargando…</td></tr></tbody>
    </table>
  </section>

  <section>
    <h2>Cómo conectar tu script</h2>
    <pre id="instrucciones">cargando…</pre>
  </section>

  <div class="pie">Refresco automático cada 30 s · GET públicos: /api/manifest, /api/plugins · Escritura: Authorization Bearer</div>
</main>

<script>
function $(id) { return document.getElementById(id); }
function clave() { return localStorage.getItem("panel_clave") || ""; }

function guardarClave() {
  localStorage.setItem("panel_clave", $("clave").value.trim());
  $("claveAviso").textContent = "Clave guardada.";
  refrescar();
}

function fmtFecha(seg) {
  if (!seg) return "—";
  return new Date(seg * 1000).toLocaleString();
}

function hace(seg) {
  if (seg === null || seg === undefined) return "";
  if (seg < 90) return "hace " + Math.round(seg) + " s";
  if (seg < 5400) return "hace " + Math.round(seg / 60) + " min";
  return "hace " + (seg / 3600).toFixed(1) + " h";
}

async function conClave(ruta) {
  const r = await fetch(ruta, { headers: { Authorization: "Bearer " + clave() } });
  if (r.status === 401) throw new Error("clave incorrecta o ausente");
  if (!r.ok) throw new Error("error " + r.status);
  return r.json();
}

function pintarEstado(e) {
  const linea = $("estadoLinea");
  if (!e || e.detalle) {
    linea.innerHTML = '<span class="punto gris"></span>' + (e ? e.detalle : "sin datos");
    $("estadoDetalle").innerHTML = "";
    return;
  }
  const vivo = Boolean(e.online);
  linea.innerHTML = '<span class="punto ' + (vivo ? "verde" : "rojo") + '"></span>' +
    (vivo ? "ONLINE" : "CAÍDO / apagado") +
    (e.edad_seg !== undefined ? ' <span class="k">(' + hace(e.edad_seg) + ")</span>" : "");
  const dir = e.direccion || "(sin dirección)";
  $("estadoDetalle").innerHTML =
    '<span><span class="k">dirección:</span> <span class="v copiable" title="clic para copiar" onclick="copiar(\\'' + dir + '\\')">' + dir + "</span></span>" +
    '<span><span class="k">version script:</span> <span class="v">' + (e.version_script || "?") + "</span></span>" +
    '<span><span class="k">Minecraft:</span> <span class="v">' + (e.version_mc || "?") + "</span></span>" +
    '<span><span class="k">túnel:</span> <span class="v">' + (e.proveedor || "—") + "</span></span>" +
    '<span><span class="k">última señal:</span> <span class="v">' + fmtFecha(e.cuando) + "</span></span>" +
    (e.motivo ? '<span class="error">' + e.motivo + "</span>" : "");
}

function copiar(texto) {
  if (!texto || texto === "(sin dirección)") return;
  navigator.clipboard.writeText(texto);
}

async function refrescar() {
  // estado (necesita clave)
  try {
    pintarEstado(await conClave("/api/status"));
    $("estadoAviso").classList.add("oculto");
  } catch (err) {
    pintarEstado(null);
    const aviso = $("estadoAviso");
    aviso.textContent = String(err.message || err);
    aviso.classList.remove("oculto");
  }

  // manifest y plugins (públicos)
  try {
    const m = await (await fetch("/api/manifest")).json();
    $("version").textContent = m.version || "(nada publicado aún)";
    $("changelog").textContent = m.changelog || "(sin changelog)";
    if (m.url) { $("descarga").href = m.url; } else { $("descarga").removeAttribute("href"); }
    $("scriptDetalle").innerHTML = m.sha256
      ? '<span class="k">sha256:</span> <code>' + m.sha256.slice(0, 16) + "…</code> <span class='k'>· publicado " + fmtFecha(m.cuando) + "</span>"
      : "";
  } catch (err) { $("version").textContent = "error: " + err.message; }

  try {
    const p = await (await fetch("/api/plugins")).json();
    const filas = (p.plugins || []).map(function (x) {
      const fuente = x.fuente === "github" ? ("github: " + (x.repo || "")) : ("modrinth: " + (x.proyecto || ""));
      const politica = x.en_cada_ejecucion ? "se revisa cada ejecución" : "se instala la primera vez";
      return "<tr><td>" + x.nombre + "</td><td>" + fuente + "</td><td>" + politica + "</td></tr>";
    });
    $("plugins").innerHTML = filas.join("") || "<tr><td colspan='3'>sin plugins</td></tr>";
  } catch (err) { $("plugins").innerHTML = "<tr><td colspan='3'>error: " + err.message + "</td></tr>"; }

  try {
    const i = await (await fetch("/api/instrucciones")).json();
    $("instrucciones").textContent =
      "# en tu .env (una vez):\\n" +
      "PANEL_URL=" + i.env.PANEL_URL + "\\n" +
      "PANEL_API_KEY=" + (clave() ? "(ya guardada aquí)" : "(pon la misma del panel)") + "\\n\\n" +
      "# publicar una versión nueva del script:\\n" + i.publicar;
  } catch (err) { $("instrucciones").textContent = "error: " + err.message; }
}

$("clave").value = clave();
refrescar();
setInterval(refrescar, 30000);
</script>
</body>
</html>`;
