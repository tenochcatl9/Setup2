"use strict";

(() => {
  const urlInput = document.getElementById("url-servidor");
  const status = document.getElementById("estado-conexion");
  const buttons = document.querySelectorAll(".instalar-plugin");
  const serverHint = new URLSearchParams(location.search).get("server");
  if (serverHint) urlInput.value = serverHint;

  function uiUrl() {
    const url = new URL(urlInput.value.trim());
    const hostname = url.hostname.toLowerCase();
    const local = ["127.0.0.1", "localhost", "::1"].includes(hostname);
    const codespace = hostname.endsWith(".app.github.dev");
    if (!["http:", "https:"].includes(url.protocol) || (!local && !codespace)
        || url.username || url.password) {
      throw new Error("Usa localhost, 127.0.0.1 o la URL reenviada de Codespaces.");
    }
    return `${url.origin}/?plugins=1`;
  }

  function openUi() {
    let url;
    try {
      url = uiUrl();
    } catch (error) {
      status.textContent = error.message;
      return;
    }
    const tab = window.open(url, "_blank");
    if (tab) tab.opener = null;
    status.textContent = tab
      ? "UI abierta. En la sección «plugins», instala Permadeath o sube un .jar."
      : "El navegador bloqueó la pestaña; permite ventanas emergentes y vuelve a intentarlo.";
  }

  document.getElementById("btn-conectar").addEventListener("click", openUi);
  buttons.forEach((button) => button.addEventListener("click", openUi));
})();
