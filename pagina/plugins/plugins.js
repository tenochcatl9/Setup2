"use strict";

(() => {
  const urlInput = document.getElementById("url-servidor");
  const status = document.getElementById("estado-conexion");
  const buttons = document.querySelectorAll(".instalar-plugin");
  let connection = "";
  const serverHint = new URLSearchParams(location.search).get("server");
  if (serverHint) urlInput.value = serverHint;

  function localUrl(raw) {
    const url = new URL(raw);
    const hostname = url.hostname.toLowerCase();
    const local = ["127.0.0.1", "localhost", "::1"].includes(hostname);
    const codespace = hostname.endsWith(".app.github.dev");
    if (!["http:", "https:"].includes(url.protocol) || (!local && !codespace)
        || url.username || url.password) {
      throw new Error("Usa localhost, 127.0.0.1 o la URL reenviada de Codespaces.");
    }
    return url.origin;
  }

  async function connect() {
    const button = document.getElementById("btn-conectar");
    button.disabled = true;
    status.textContent = "Conectando con la UI local…";
    try {
      connection = localUrl(urlInput.value.trim());
      const response = await fetch(`${connection}/api/salud`, { cache: "no-store" });
      if (!response.ok) throw new Error(`la UI respondió HTTP ${response.status}`);
      const health = await response.json();
      if (!health.setup_mc_webui) throw new Error("la dirección no corresponde a la UI de setup2");
      status.textContent = `Conectado a setup2 v${health.version}. Ya puedes instalar.`;
      buttons.forEach((item) => { item.disabled = false; });
    } catch (error) {
      connection = "";
      buttons.forEach((item) => { item.disabled = true; });
      status.textContent = `No se pudo conectar: ${error.message}. Comprueba que la UI esté abierta en el puerto 8080.`;
    } finally {
      button.disabled = false;
    }
  }

  document.getElementById("btn-conectar").addEventListener("click", connect);
  urlInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") connect();
  });
  buttons.forEach((button) => {
    button.disabled = true;
    button.addEventListener("click", async () => {
      if (!connection) return;
      button.disabled = true;
      status.textContent = "Descargando y verificando Permadeath…";
      try {
        const response = await fetch(`${connection}/api/plugins/instalar`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ plugin: button.closest("[data-plugin]").dataset.plugin }),
          cache: "no-store",
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
        status.textContent = `✓ ${result.nombre} instalado y verificado. Reinicia el servidor cuando estés listo para cargarlo.`;
      } catch (error) {
        status.textContent = `No se pudo instalar: ${error.message}. Si el navegador bloquea la conexión segura, abre /plugins desde la UI local y vuelve a intentarlo.`;
      } finally {
        button.disabled = false;
      }
    });
  });
})();
