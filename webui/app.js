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
let modo = "local";         // "local" (localhost) o "publico" (página del panel)
let direccionActual = "";   // dirección del túnel, para copiar al clic
let jugadoresActuales = []; // nombres de los jugadores conectados (para TAB)

function hace(segundos) {
  if (segundos == null) return "";
  if (segundos < 90) return "hace " + Math.round(segundos) + " s";
  if (segundos < 5400) return "hace " + Math.round(segundos / 60) + " min";
  return "hace " + (segundos / 3600).toFixed(1) + " h";
}

function copiarDireccion() {
  if (direccionActual && navigator.clipboard) navigator.clipboard.writeText(direccionActual);
}

// La misma página sirve en localhost (todo) y en el panel (solo mirar).
function aplicarModo(nuevo) {
  if (nuevo === modo) return;
  modo = nuevo;
  document.body.classList.toggle("modo-publico", modo === "publico");
  $("marca-texto").textContent = modo === "publico" ? "estado del servidor" : "panel local";
  $("pie").innerHTML = modo === "publico"
    ? "estado en vivo del servidor · se actualiza solo"
    : 'consola: <code>logs/servidor.log</code> · comandos: <code>consola</code> (fifo) · ' +
      'html/js: <code>webui/</code> · temas: <code>templates/</code>';
}

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
  aplicarModo(e.modo || "local");
  const s = e.servidor || {}, t = e.tunel || {}, sc = e.script || {};
  const j = e.jugadores || {}, p = e.paper || {};
  const enMarcha = !!(s.vivo && s.puerto_abierto);

  $("indicador").className = "indicador " + (enMarcha ? "vivo" : "muerto");
  $("indicador-texto").textContent = enMarcha
    ? (j.disponible ? `ONLINE · ${j.en_linea}/${j.max}` : "ONLINE")
    : "APAGADO";
  document.title = (enMarcha ? "● " : "○ ") + " setup2 · panel local";

  let html = "";
  html += fila("servidor", enMarcha
    ? ("en marcha" + (s.externo ? " (externo)" : (s.pid ? ` · pid ${s.pid}` : "")))
    : "parado");
  html += fila("puerto", `${s.puerto} · ${s.puerto_abierto ? "abierto" : "cerrado"}`);
  if (enMarcha && s.uptime_seg != null) html += fila("en pie desde hace", escapar(duracion(s.uptime_seg)));
  if (enMarcha && s.xmx) html += fila("memoria máx.", escapar(s.xmx));
  if (t.vivo) {
    direccionActual = t.direccion || "";
    const dir = t.direccion
      ? `<span class="copiable" title="clic para copiar" onclick="copiarDireccion()">${escapar(t.direccion)}</span>`
      : "(abriendo…)";
    html += fila("túnel", dir +
      (t.proveedor ? ` <span class="tenue">· ${escapar(t.proveedor)}</span>` : ""));
  } else {
    html += fila("túnel", "sin túnel");
  }
  html += fila("minecraft", escapar(sc.mc || "?"));
  if (p.actual) {
    let paper = escapar(p.actual);
    if (p.hay_nueva) paper += ` · <span class="alerta">hay ${escapar(p.ultima)}</span>`;
    else if (p.ultima) paper += " · al día";
    html += fila("paper", paper);
  }
  html += fila("script", "v" + escapar(sc.version || "?"));
  if (sc.skins) html += fila("skinsrestorer", escapar(sc.skins));
  if (sc.panel) {
    const limpio = String(sc.panel).replace(/^https?:\/\//, "");
    html += fila("panel remoto",
      `<a href="${escapar(sc.panel)}" target="_blank" rel="noopener">${escapar(limpio)}</a>`);
  }
  if (modo !== "publico") {
    html += fila("consola", e.consola_disponible ? "canal listo" : "no disponible (servidor parado)");
  }
  if (modo === "publico" && e.edad_seg != null) {
    html += fila("última señal", escapar(hace(e.edad_seg)));
  }
  if (modo === "publico" && e.motivo) {
    html += fila("aviso", `<span class="tenue">${escapar(e.motivo)}</span>`);
  }

  $("estado-filas").innerHTML = html;

  // Jugadores: el contador es exacto aunque la lista de nombres venga vacía.
  jugadoresActuales = Array.isArray(j.nombres) ? j.nombres.filter(Boolean) : [];
  const lista = $("jugadores-lista");
  $("jugadores-online").textContent = j.disponible ? j.en_linea : "—";
  $("jugadores-max").textContent = j.max || "—";
  $("jugadores-nota").textContent = j.disponible
    ? "en vivo"
    : (enMarcha ? "conectando…" : "servidor apagado");
  if (!j.disponible) {
    lista.innerHTML = '<li class="tenue">servidor apagado</li>';
  } else if (!jugadoresActuales.length) {
    lista.innerHTML = j.en_linea > 0
      ? `<li class="tenue">${j.en_linea} conectado(s); el servidor no dio los nombres</li>`
      : '<li class="tenue">no hay jugadores conectados</li>';
  } else {
    lista.innerHTML = jugadoresActuales
      .map((nombre) => `<li>${escapar(nombre)}</li>`).join("");
  }

  if (modo !== "publico") {
    $("btn-arrancar").disabled = enMarcha;
    $("btn-parar").disabled = !enMarcha && !t.vivo;
    const btnPaper = $("btn-actualizar-paper");
    btnPaper.disabled = !p.hay_nueva;
    btnPaper.textContent = p.hay_nueva
      ? `actualizar Paper a ${p.ultima}`
      : "Paper al día";
  }
}

// ── consola ───────────────────────────────────────────────────────────────
const COLA_MAX = 4000;      // líneas visibles como máximo
let desde = 0;              // líneas ya pintadas de la consola
let alFinal = true;         // ¿el usuario está pegado al final?
let fuenteConsola = "";     // servidor.log o latest.log, según diga la API
let filtroConsola = "todos";
let busquedaConsola = "";
let pausaConsola = false;

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

function nivelLinea(texto) {
  const limpio = texto.replace(/\x1b\[[0-9;]*m/g, "");
  if (/\b(ERROR|SEVERE|FATAL|Exception|Caused by)\b/i.test(limpio)) return "error";
  if (/\bWARN(ING)?\b/i.test(limpio)) return "warn";
  return "info";
}

function aplicarFiltroLinea(div) {
  const nivel = div.dataset.nivel || "info";
  const texto = div.dataset.texto || "";
  const porNivel = filtroConsola === "todos" || nivel === filtroConsola || nivel === "sistema";
  const porTexto = !busquedaConsola || texto.includes(busquedaConsola);
  div.hidden = !(porNivel && porTexto);
}

function aplicarFiltros() {
  for (const div of $("consola").querySelectorAll(".linea")) aplicarFiltroLinea(div);
}

function agregarLinea(texto, clase = "") {
  const caja = $("consola");
  const div = document.createElement("div");
  div.className = "linea" + (clase ? " " + clase : "");
  div.dataset.nivel = clase === "sistema" ? "sistema" : nivelLinea(texto);
  div.dataset.texto = texto.replace(/\x1b\[[0-9;]*m/g, "").toLowerCase();
  div.innerHTML = ansiAHtml(texto) || "&nbsp;";
  caja.appendChild(div);
  aplicarFiltroLinea(div);
  while (caja.children.length > COLA_MAX) caja.removeChild(caja.firstChild);
}

function irAlFinal() {
  const caja = $("consola");
  caja.scrollTop = caja.scrollHeight;
  alFinal = true;
}

async function refrescarConsola() {
  if (modo === "publico" || pausaConsola) return;  // en el panel no hay consola
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
  $("consola-fuente").textContent = r.fuente ? `fuente: ${r.fuente}` : "";
  $("consola-nota").textContent = r.total ? `(${r.total} líneas)` : "(vacía)";
  if ($("auto-scroll").checked && alFinal && filtroConsola === "todos" && !busquedaConsola) {
    irAlFinal();
  }
}

// eco local de los comandos enviados (el servidor no repite lo tecleado)
function agregarEco(texto, error = false) {
  agregarLinea(`\x1b[91m${error ? "[!]" : "[>]"}\x1b[0m ${texto}`, "sistema");
}

async function enviarComando() {
  if (modo === "publico") return;     // los comandos solo existen en localhost
  const entrada = $("comando");
  const cmd = entrada.value.trim();
  if (!cmd) return;
  cerrarSugerencias();
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

// ── TAB complete ──────────────────────────────────────────────────────────
// Lista de comandos de consola de Paper + subcomandos frecuentes. Para los
// jugadores se consulta el estado en vivo (/api/estado), así que TAB también
// completa nombres después de op, kick, tp, whitelist add, etc.
const COMANDOS_CONSOLA = [
  "list", "stop", "save-all", "save-off", "save-on", "tps", "reload",
  "say", "op", "deop", "kick", "ban", "ban-ip", "pardon", "pardon-ip",
  "whitelist", "gamemode", "time", "weather", "difficulty", "give", "tp",
  "clear", "effect", "enchant", "xp", "spawnpoint", "setworldspawn",
  "worldborder", "gamerule", "scoreboard", "tag", "team", "title", "tellraw",
  "playsound", "stopsound", "particle", "summon", "execute", "function",
  "datapack", "advancement", "bossbar", "recipe", "attribute", "broadcast",
  "sudo", "paper", "spark", "authme", "login", "register", "skin",
  "skinsrestorer", "help", "plugins", "version", "seed"
];

const SUBCOMANDOS = {
  "save-all": ["flush"],
  "reload": ["confirm"],
  "whitelist": ["on", "off", "list", "add", "remove"],
  "gamemode": ["survival", "creative", "adventure", "spectator"],
  "time": ["set", "add", "query"],
  "time set": ["day", "noon", "night", "midnight"],
  "time add": ["day", "noon", "night", "midnight"],
  "weather": ["clear", "rain", "thunder"],
  "difficulty": ["peaceful", "easy", "normal", "hard"],
  "effect": ["give", "clear"],
  "enchant": ["give", "clear"],
  "xp": ["add", "set", "query"],
  "worldborder": ["set", "add", "center", "damage", "get", "warning", "shrinking"],
  "datapack": ["list", "enable", "disable", "reload"],
  "advancement": ["grant", "revoke"],
  "bossbar": ["add", "remove", "set", "get", "list"],
  "scoreboard": ["objectives", "players"],
  "tag": ["add", "remove", "list"],
  "team": ["add", "remove", "list", "join", "leave", "modify", "empty"],
  "title": ["clear", "reset", "title", "subtitle", "actionbar", "times"],
  "execute": ["as", "at", "run", "if", "unless", "positioned", "facing",
              "align", "anchored", "in", "rotated", "store", "summon"],
  "function": ["run"],
  "gamerule": ["keepInventory", "doDaylightCycle", "doMobSpawning",
                "doWeatherCycle", "mobGriefing", "pvp", "randomTickSpeed"]
};

function aceptaJugador(previos) {
  const cmd = (previos[0] || "").toLowerCase();
  const segundo = (previos[1] || "").toLowerCase();
  if (!cmd) return false;
  if (cmd === "whitelist") return ["add", "remove"].includes(segundo);
  if (cmd === "gamemode") {
    return previos.length === 2
      && ["survival", "creative", "adventure", "spectator"].includes(segundo);
  }
  if (cmd === "tp") return previos.length >= 1;
  if (["op", "deop", "kick", "ban", "pardon", "ban-ip", "pardon-ip",
       "clear", "spawnpoint", "tellraw", "sudo", "skin", "skinsrestorer",
       "give", "effect", "enchant", "xp", "title"].includes(cmd)) {
    return previos.length === 1;
  }
  return false;
}

function candidatosComando(valor, cursor) {
  const antes = valor.slice(0, cursor);
  const inicio = antes.lastIndexOf(" ") + 1;
  const prefijo = antes.slice(inicio);
  const previos = antes.slice(0, inicio).trim().split(/\s+/).filter(Boolean);
  const clave = previos.join(" ").toLowerCase();
  let base;
  if (!previos.length) {
    base = COMANDOS_CONSOLA.slice();
  } else {
    base = (SUBCOMANDOS[clave] || []).slice();
    if (aceptaJugador(previos)) base = base.concat(jugadoresActuales);
  }
  const vistos = new Set();
  const lista = [];
  for (const candidato of base) {
    const limpio = String(candidato);
    if (!limpio || vistos.has(limpio.toLowerCase())) continue;
    if (!limpio.toLowerCase().startsWith(prefijo.toLowerCase())) continue;
    vistos.add(limpio.toLowerCase());
    lista.push(limpio);
  }
  return { lista, inicio, prefijo };
}

let sugerencias = [];
let indiceSugerencia = -1;
let contextoCompletado = "";
let inicioSugerencia = 0;

function cerrarSugerencias() {
  sugerencias = [];
  indiceSugerencia = -1;
  contextoCompletado = "";
  const caja = $("sugerencias");
  caja.hidden = true;
  caja.innerHTML = "";
}

function mostrarSugerencias() {
  const caja = $("sugerencias");
  caja.innerHTML = "";
  if (!sugerencias.length) {
    caja.hidden = true;
    return;
  }
  sugerencias.forEach((candidato, i) => {
    const boton = document.createElement("button");
    boton.type = "button";
    boton.className = "sugerencia" + (i === indiceSugerencia ? " activa" : "");
    boton.textContent = candidato;
    boton.addEventListener("mousedown", (ev) => ev.preventDefault());
    boton.addEventListener("click", () => {
      aplicarSugerencia(candidato, true);
      $("comando").focus();
    });
    caja.appendChild(boton);
  });
  caja.hidden = false;
}

function reemplazarToken(candidato, inicio, espacio = true) {
  const entrada = $("comando");
  const cursor = entrada.selectionStart ?? entrada.value.length;
  const sufijo = entrada.value.slice(cursor);
  const relleno = espacio ? " " : "";
  entrada.value = entrada.value.slice(0, inicio) + candidato + relleno + sufijo;
  const pos = inicio + candidato.length + relleno.length;
  entrada.setSelectionRange(pos, pos);
  return pos;
}

function aplicarSugerencia(candidato, cerrar = false) {
  reemplazarToken(candidato, inicioSugerencia, true);
  if (cerrar) {
    cerrarSugerencias();
    return;
  }
  const entrada = $("comando");
  contextoCompletado = entrada.value + "|" + (entrada.selectionStart ?? entrada.value.length);
  mostrarSugerencias();
}

function completarTab(ev) {
  if (modo === "publico") return;
  ev.preventDefault();
  const entrada = $("comando");
  const valor = entrada.value;
  const cursor = entrada.selectionStart ?? valor.length;
  const contexto = valor + "|" + cursor;
  if (sugerencias.length && contextoCompletado === contexto) {
    const paso = ev.shiftKey ? -1 : 1;
    indiceSugerencia = indiceSugerencia < 0
      ? (paso > 0 ? 0 : sugerencias.length - 1)
      : (indiceSugerencia + paso + sugerencias.length) % sugerencias.length;
    aplicarSugerencia(sugerencias[indiceSugerencia], false);
    return;
  }
  const { lista, inicio, prefijo } = candidatosComando(valor, cursor);
  if (!lista.length) {
    cerrarSugerencias();
    return;
  }
  sugerencias = lista;
  inicioSugerencia = inicio;
  indiceSugerencia = -1;
  if (lista.length === 1) {
    aplicarSugerencia(lista[0], true);
    return;
  }
  // Si todos comparten más prefijo, se completa hasta ahí; el siguiente TAB
  // ya elige una opción concreta y los siguientes van ciclando.
  let comun = lista[0];
  for (const candidato of lista.slice(1)) {
    while (comun && !candidato.toLowerCase().startsWith(comun.toLowerCase())) {
      comun = comun.slice(0, -1);
    }
  }
  if (comun.length > prefijo.length) {
    reemplazarToken(comun, inicioSugerencia, false);
    contextoCompletado = entrada.value + "|" + (entrada.selectionStart ?? entrada.value.length);
    indiceSugerencia = -1;
    mostrarSugerencias();
    return;
  }
  contextoCompletado = contexto;
  mostrarSugerencias();
}

// ── acciones ──────────────────────────────────────────────────────────────
async function accion(nombre, confirmar) {
  if (modo === "publico") return;     // las acciones solo existen en localhost
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
  if (ev.key === "Tab") { completarTab(ev); return; }
  if (ev.key === "Escape") { cerrarSugerencias(); return; }
  if (ev.key === "Enter") { ev.preventDefault(); enviarComando(); }
  else if (ev.key === "ArrowUp") { ev.preventDefault(); cerrarSugerencias(); navegarHistorial(-1); }
  else if (ev.key === "ArrowDown") { ev.preventDefault(); cerrarSugerencias(); navegarHistorial(1); }
});
$("comando").addEventListener("input", cerrarSugerencias);
$("comando").addEventListener("blur", () => setTimeout(cerrarSugerencias, 150));

$("auto-scroll").addEventListener("change", () => { if ($("auto-scroll").checked) irAlFinal(); });
$("consola").addEventListener("scroll", () => {
  const caja = $("consola");
  alFinal = caja.scrollTop + caja.clientHeight >= caja.scrollHeight - 40;
});
$("btn-limpiar").addEventListener("click", () => { $("consola").innerHTML = ""; });
$("btn-final").addEventListener("click", irAlFinal);

for (const boton of document.querySelectorAll(".filtro")) {
  boton.addEventListener("click", () => {
    filtroConsola = boton.dataset.filtro || "todos";
    for (const otro of document.querySelectorAll(".filtro")) {
      otro.classList.toggle("activo", otro === boton);
    }
    aplicarFiltros();
    if ($("auto-scroll").checked && alFinal) irAlFinal();
  });
}
$("buscar-consola").addEventListener("input", () => {
  busquedaConsola = $("buscar-consola").value.trim().toLowerCase();
  aplicarFiltros();
});
$("btn-pausa").addEventListener("click", () => {
  pausaConsola = !pausaConsola;
  $("btn-pausa").textContent = pausaConsola ? "reanudar" : "pausar";
  $("btn-pausa").classList.toggle("activo", pausaConsola);
  if (!pausaConsola) {
    refrescarConsola();
    if ($("auto-scroll").checked) irAlFinal();
  }
});
$("btn-descargar").addEventListener("click", () => {
  window.location.href = "/api/consola/descargar";
});

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
$("btn-actualizar-paper").addEventListener("click", () => {
  const texto = $("btn-actualizar-paper").textContent;
  if (!window.confirm(
    `${texto}: se parará el servidor, se hará un respaldo completo del mundo ` +
    "y se migrará el mundo al arrancar. ¿Continuar?")) return;
  accion("actualizar_paper");
});
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
refrescarEstado().then(() => {
  if (modo !== "publico") {           // la consola solo corre en localhost
    refrescarConsola();
    setInterval(refrescarConsola, 1500);
  }
});
setInterval(refrescarEstado, 3000);
