"""Salida de terminal: logs, errores y barra de progreso."""

import itertools, sys, time
from .config import EXENTAS_SENSIBLES, PATRON_SENSIBLE, RE_ANSI

# ─── Utilidades de salida ─────────────────────────────────────────────────
_barra_activa = None

def _sin_ansi(texto: str) -> str:
    return RE_ANSI.sub("", texto)

def limpiar_linea():
    if _barra_activa is not None and _barra_activa.tty:
        sys.stdout.write("\r\x1b[K")
        sys.stdout.flush()

def log(msg: str):
    limpiar_linea()
    print(f"[*] {msg}")

def aviso(msg: str):
    limpiar_linea()
    print(f"[~] {msg}")

def ok(msg: str):
    limpiar_linea()
    print(f"[+] {msg}")

def error(msg: str):
    limpiar_linea()
    print(f"[!] ERROR: {msg}", file=sys.stderr)
    sys.stdout.flush()

def titulo(texto: str):
    limpiar_linea()
    print("=" * 62)
    print(f"  {texto}")
    print("=" * 62)

def ruta_sensible(ruta: str) -> bool:
    """True si la ruta parece contener un secreto o un archivo generado del server."""
    base = ruta.replace("\\", "/").rsplit("/", 1)[-1]
    if base in EXENTAS_SENSIBLES:
        return False
    return bool(PATRON_SENSIBLE.search(ruta.replace("\\", "/")))

# ─── Barra de carga ───────────────────────────────────────────────────────
class Barra:
    """Barra de progreso con porcentaje, tiempo y spinner.

    - Si conoce `total` (bytes, pasos) muestra el avance real.
    - Si solo conoce `limite` (segundos) muestra el avance por tiempo.
    - En terminal TTY se redibuja; sin TTY imprime líneas cada 10%.
    """

    ANCHO = 24

    def __init__(self, etiqueta: str, total: int | None = None, limite: float | None = None,
                 unidad: str = "bytes"):
        self.etiqueta = etiqueta
        self.total = total
        self.limite = limite
        self.unidad = unidad
        self.valor = 0
        self.inicio = time.time()
        self.tty = sys.stdout.isatty()
        self._ultimo_pct = -10.0
        self._ultimo_pintado = 0.0
        self._spin = itertools.cycle("|/-\\")
        self._pintar()

    # -- internos --
    def _avance(self) -> tuple:
        if self.total:
            frac = min(1.0, self.valor / self.total)
        elif self.limite:
            frac = min(1.0, (time.time() - self.inicio) / self.limite)
        else:
            frac = 0.0
        return frac * 100, frac

    def _cabecera(self, elapsed: float) -> str:
        if self.limite and not self.total:
            return f"{elapsed:5.1f}s/{self.limite:.0f}s"
        if self.total and self.unidad == "bytes":
            return f"{self.valor / 1048576:,.1f}/{self.total / 1048576:,.1f} MiB"
        return f"{elapsed:5.1f}s"

    @property
    def activa(self) -> bool:
        return _barra_activa is self

    def _pintar(self):
        global _barra_activa
        _barra_activa = self
        pct, frac = self._avance()
        elapsed = time.time() - self.inicio
        if elapsed - self._ultimo_pintado < 0.1:
            return
        self._ultimo_pintado = elapsed
        if not self.tty:
            if pct - self._ultimo_pct >= 10:
                self._ultimo_pct = pct
                print(f"    {self.etiqueta}: {pct:5.1f}% ({self._cabecera(elapsed)})")
            return
        llenos = int(self.ANCHO * frac)
        barra = "#" * llenos + "-" * (self.ANCHO - llenos)
        spin = next(self._spin)
        sys.stdout.write(f"\r\x1b[K[*] {self.etiqueta} [{barra}] {pct:5.1f}% "
                         f"{self._cabecera(elapsed)} {spin}")
        sys.stdout.flush()

    # -- público --
    def avanzar(self, cantidad: int = 1):
        self.valor += cantidad
        self._pintar()

    def refrescar(self):
        self._pintar()

    def finalizar(self, mensaje: str | None = None):
        global _barra_activa
        if not self.activa:
            return
        elapsed = time.time() - self.inicio
        if self.tty:
            sys.stdout.write("\r\x1b[K")
            sys.stdout.flush()
        _barra_activa = None
        if mensaje:
            ok(f"{mensaje} ({elapsed:.1f}s)")
