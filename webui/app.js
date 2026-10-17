"use strict";

/* UI local de setup2: estado, consola con colores, acciones y temas.
   Todo pasa por /api/* (el servidor HTTP lo pone setup_mc/webui.py).
   Los temas son archivos en templates/: aquí solo se cambia el <link>. */

const $ = (id) => document.getElementById(id);

// ── utilidades ────────────────────────────────────────────────────────────
function escapar(texto) {
  return String(texto).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function duracion(segundos) {
  if (segundos == null) return "";
  const s = Math.max(0, Math.floor(segundos));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d} d ${h} h`;
  if (h > 0) return `${h} h ${m} min`;
  if (m > 0) return `${m} min ${s % 60} s`;
  return `${s} s`;
}

// ── temas (templates/*.css) ───────────────────────────────────────────────
function aplicarTema(nombre) {
  $("hoja-tema").href = "/tema/" + encodeURIComponent(nombre) + ".css";
}

async function cargarTemas() {
  let temas = ["template_default"];
  try {
    const datos = await (await fetch("/api/temas")).json();
    if (Array.isArray(datos.temas) && datos.temas.length) temas = datos.temas;
  } catch (e) { /* seguimos con el tema por defecto del <link> */ }
  const sel = $("selector-tema");
  sel.innerHTML = "";
  for (const tema of temas) {
    const op = document.createElement("option");
    op.value = tema;
    op.textContent = tema;
    sel.appendChild(op);
  }
  let elegido = localStorage.getItem("tema") || "template_default";
  if (!temas.includes(elegido)) elegido = temas.includes("template_default") ? "template_default" : temas[0];
  sel.value = elegido;
  aplicarTema(elegido);
  sel.addEventListener("change", () => {
    aplicarTema(sel.value);
    localStorage.setItem("tema", sel.value);
  });
}

// ── estado en vivo ────────────────────────────────────────────────────────
function fila(etiqueta, valor) {
  return `<div class="fila"><span class="etiqueta">${escapar(etiqueta)}</span>` +
         `<span class="valor">${valor}</span></div>`;
}

async function refrescarEstado() {
  let e;
  try {
    e = await (await fetch("/api/estado")).json();
  } catch (err) {
    $("indicador").className = "indicador muerto";
    $("indicador-texto").textContent = "ui sin conexión";
    return;
  }
  const s = e.servidor || {}, t = e.tunel || {}, sc = e.script || {};
  const enMarcha = !!(s.vivo && s.puerto_abierto);

  $("indicador").className = "indicador " + (enMarcha ? "vivo" : "muerto");
  $("indicador-texto").textContent = enMarcha ? "ONLINE" : "APAGADO";
  document.title = (enMarcha ? "● " : "○ ") + " setup2 · panel local";

  let html = "";
  html += fila("servidor", enMarcha
    ? ("en marcha" + (s.externo ? " (externo)" : (s.pid ? ` · pid ${s.pid}` : "")))
    : "parado");
  html += fila("puerto", `${s.puerto} · ${s.puerto_abierto ? "abierto" : "cerrado"}`);
  if (enMarcha && s.uptime_seg != null) html += fila("en pie desde hace", escapar(duracion(s.uptime_seg)));
  if (enMarcha && s.xmx) html += fila("memoria máx.", escapar(s.xmx));
  html += fila("túnel", t.vivo
    ? escapar(t.direccion || "(abriendo…)") + (t.proveedor ? ` <span class="tenue">· ${escapar(t.proveedor)}</span>` : "")
    : "sin túnel");
  html += fila("minecraft", escapar(sc.mc || "?"));
  html += fila("script", "v" + escapar(sc.version || "?"));
  html += fila("skinsrestorer", escapar(sc.skins || "?"));
  if (sc.panel) {
    const limpio = String(sc.panel).replace(/^https?:\/\//, "");
    html += fila("panel remoto",
      `<a href="${escapar(sc.panel)}" target="_blank" rel="noopener">${escapar(limpio)}</a>`);
  }
  html += fila("consola", e.consola_disponible ? "canal listo" : "no disponible (servidor parado)");

  $("estado-filas").innerHTML = html;

  $("btn-arrancar").disabled = enMarcha;
  $("btn-parar").disabled = !enMarcha && !t.vivo;
}

// ── consola ───────────────────────────────────────────────────────────────
const COLA_MAX = 4000;      // líneas visibles como máximo
let desde = 0;              // líneas ya pintadas de la consola
let alFinal = true;         // ¿el usuario está pegado al final?
let fuenteConsola = "";     // servidor.log o latest.log, según diga la API

// Convierte códigos ANSI del servidor (colores de Minecraft) a <span class="a3x">.
function ansiAHtml(linea) {
  const codigos = [];
  let html = "";
  let i = 0;
  const re = /\x1b\[([0-9;]*)m/g;
  const escribir = (texto) => {
    if (!texto) return;
    const esc = escapar(texto);
    html += codigos.length ? `<span class="${codigos.join(" ")}">${esc}</span>` : esc;
  };
  let m;
  while ((m = re.exec(linea)) !== null) {
    escribir(linea.slice(i, m.index));
    i = re.lastIndex;
    for (const bruto of (m[1] || "0").split(";")) {
      const c = Number(bruto);
      if (c === 0) codigos.length = 0;
      else if (c === 1) { if (!codigos.includes("br")) codigos.push("br"); }
      else if ((c >= 30 && c <= 37) || (c >= 90 && c <= 97)) {
        for (let k = codigos.length - 1; k >= 0; k--) if (/^a\d+$/.test(codigos[k])) codigos.splice(k, 1);
        codigos.push("a" + c);
      }
      else if (c === 39 || c === 49) {
        for (let k = codigos.length - 1; k >= 0; k--) if (/^a\d+$/.test(codigos[k])) codigos.splice(k, 1);
      }
      else if (c === 22) {
        const k = codigos.indexOf("br");
        if (k >= 0) codigos.splice(k, 1);
      }
    }
  }
  escribir(linea.slice(i));
  return html;
}

function agregarLinea(texto, clase = "") {
  const caja = $("consola");
  const div = document.createElement("div");
  div.className = "linea" + (clase ? " " + clase : "");
  div.innerHTML = ansiAHtml(texto) || "&nbsp;";
  caja.appendChild(div);
  while (caja.children.length > COLA_MAX) caja.removeChild(caja.firstChild);
}

function irAlFinal() {
  const caja = $("consola");
  caja.scrollTop = caja.scrollHeight;
  alFinal = true;
}

async function refrescarConsola() {
  let r;
  try {
    r = await (await fetch("/api/consola?desde=" + desde)).json();
  } catch (err) {
    return;
  }
  // la fuente puede cambiar (servidor.log ↔ latest.log): se limpia y se empieza
  if (r.fuente && r.fuente !== fuenteConsola) {
    if (fuenteConsola) {
      $("consola").innerHTML = "";
      agregarLinea(`───── fuente de consola: ${r.fuente} ─────`, "sistema");
      fuenteConsola = r.fuente;
      desde = 0;
      return;                     // el próximo sondeo ya trae desde 0
    }
    fuenteConsola = r.fuente;     // primera vez: seguimos (desde ya vale 0)
  }
  if (typeof r.desde === "number" && r.desde < desde) {
    agregarLinea("───── la consola se reinició (arranque nuevo del servidor) ─────", "sistema");
  }
  desde = (r.desde || 0) + (r.lineas ? r.lineas.length : 0);
  for (const linea of (r.lineas || [])) agregarLinea(linea);
  $("consola-nota").textContent = r.total ? `(${r.total} líneas)` : "(vacía)";
  if ($("auto-scroll").checked && alFinal) irAlFinal();
}

// eco local de los comandos enviados (el servidor no repite lo tecleado)
function agregarEco(texto, error = false) {
  agregarLinea(`\x1b[91m${error ? "[!]" : "[>]"}\x1b[0m ${texto}`, "sistema");
}

async function enviarComando() {
  const entrada = $("comando");
  const cmd = entrada.value.trim();
  if (!cmd) return;
  entrada.value = "";
  guardarHistorial(cmd);
  agregarEco(cmd);
  try {
    const r = await fetch("/api/consola", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ comando: cmd }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      agregarEco(d.error || `error ${r.status}`, true);
    }
  } catch (err) {
    agregarEco("sin conexión con la ui", true);
  }
  if ($("auto-scroll").checked) irAlFinal();
}

// ── historial de comandos (↑ / ↓) ─────────────────────────────────────────
function leerHistorial() {
  try { return JSON.parse(localStorage.getItem("historial") || "[]"); }
  catch (e) { return []; }
}
function guardarHistorial(cmd) {
  const h = leerHistorial().filter((x) => x !== cmd);
  h.unshift(cmd);
  localStorage.setItem("historial", JSON.stringify(h.slice(0, 100)));
  indiceHistorial = -1;
}
let indiceHistorial = -1;

function navegarHistorial(sentido) {
  const h = leerHistorial();
  if (!h.length) return;
  if (sentido < 0) indiceHistorial = Math.min(indiceHistorial + 1, h.length - 1);
  else indiceHistorial = Math.max(indiceHistorial - 1, -1);
  $("comando").value = indiceHistorial >= 0 ? h[indiceHistorial] : "";
}

// ── acciones ──────────────────────────────────────────────────────────────
async function accion(nombre, confirmar) {
  if (confirmar && !window.confirm(confirmar)) return;
  const aviso = $("aviso-accion");
  aviso.textContent = `${nombre}: en curso…`;
  try {
    const r = await fetch("/api/accion", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accion: nombre }),
    });
    const d = await r.json().catch(() => ({}));
    aviso.textContent = (r.ok ? "✓ " : "✗ ") + (d.detalle || d.error || nombre);
  } catch (err) {
    aviso.textContent = "✗ sin conexión con la ui";
  }
  setTimeout(refrescarEstado, 900);
  setTimeout(refrescarEstado, 3000);
}

// ── arranque ──────────────────────────────────────────────────────────────
$("btn-enviar").addEventListener("click", enviarComando);
$("comando").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") { ev.preventDefault(); enviarComando(); }
  else if (ev.key === "ArrowUp") { ev.preventDefault(); navegarHistorial(-1); }
  else if (ev.key === "ArrowDown") { ev.preventDefault(); navegarHistorial(1); }
});

$("auto-scroll").addEventListener("change", () => { if ($("auto-scroll").checked) irAlFinal(); });
$("consola").addEventListener("scroll", () => {
  const caja = $("consola");
  alFinal = caja.scrollTop + caja.clientHeight >= caja.scrollHeight - 40;
});
$("btn-limpiar").addEventListener("click", () => { $("consola").innerHTML = ""; });
$("btn-final").addEventListener("click", irAlFinal);

for (const b of document.querySelectorAll("[data-cmd]")) {
  b.addEventListener("click", () => {
    const cmd = b.dataset.cmd;
    if (b.dataset.confirmar && !window.confirm(b.dataset.confirmar)) return;
    $("comando").value = cmd;
    enviarComando();
  });
}

$("btn-arrancar").addEventListener("click", () => accion("arrancar"));
$("btn-parar").addEventListener("click", () =>
  accion("parar", "¿Parar el servidor y cerrar el túnel? Minecraft guardará el mundo primero."));
$("btn-respaldo").addEventListener("click", () => accion("respaldo"));
$("btn-webhook").addEventListener("click", () => accion("webhook"));
$("btn-apagar").addEventListener("click", () => {
  if (!window.confirm("¿Apagar la UI local? El servidor y el túnel no se tocan.")) return;
  accion("apagar_ui").then(() => {
    document.body.innerHTML =
      '<main style="padding:40px;font-size:20px">ui apagada. el servidor sigue igual; ' +
      "para volver, ejecuta <code>python3 setup_mc.py --ui</code> (" +
      "el script también la levanta solo al arrancar).</main>";
  });
});

cargarTemas();
refrescarEstado();
refrescarConsola();
setInterval(refrescarEstado, 3000);
setInterval(refrescarConsola, 1500);
