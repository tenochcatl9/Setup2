"use strict";
/* TenoHost: rellena la versión publicada y hace copiables los comandos. */

(async () => {
  const datos = document.getElementById("datos-version");
  const sha = document.getElementById("sha256");

  try {
    const r = await fetch("script/manifest.json", { cache: "no-store" });
    if (!r.ok) throw new Error(String(r.status));
    const m = await r.json();
    const kb = (m.tamano / 1024).toFixed(1).replace(".", ",");
    const fecha = m.cuando
      ? new Date(m.cuando * 1000).toLocaleDateString("es",
          { day: "numeric", month: "long", year: "numeric" })
      : "";
    datos.textContent = `v${m.version} · ${kb} KB · ${fecha}`;
    sha.textContent = m.sha256 || "—";
  } catch (e) {
    datos.textContent = "no se pudo leer la versión (¿sin conexión?)";
    sha.textContent = "—";
  }

  // Clic en un bloque de comandos: se copia al portapapeles.
  for (const bloque of document.querySelectorAll("main pre")) {
    bloque.title = "clic para copiar";
    bloque.addEventListener("click", () => {
      const texto = bloque.innerText.trim();
      if (navigator.clipboard) navigator.clipboard.writeText(texto);
      const antes = bloque.style.borderLeftColor;
      bloque.style.borderLeftColor = "#3adf6a";
      setTimeout(() => { bloque.style.borderLeftColor = antes; }, 300);
    });
  }
})();
