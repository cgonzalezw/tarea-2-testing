"""Verificación de una reparación (pieza 6): ¿pasan los tests del bug? ¿se rompió algo más?

Orden, de lo más barato a lo más caro:
  1. Tests objetivo (comandos_test). Si fallan, no se gasta tiempo en los módulos.
  2. Módulos de test (modulos_test). Si pasan todos, no hay regresiones y se terminó.
  3. Solo si algún test no pasa: línea base. Se mide una vez, con el código original, qué tests
     pasaban. Regresión = pasaba con el original y no pasa con el cambio. Así un test que ya
     fallaba antes del cambio no se confunde con una regresión.

La línea base es perezosa: medirla al inicio costaría tiempo también en los bugs donde el primer
cambio deja todos los tests en verde y nunca hace falta.
"""
from dataclasses import dataclass, field
from pathlib import Path

import edicion
import ejecutor
import tiempo

TIMEOUT_OBJETIVO = 40.0    # un cambio puede dejar un bucle infinito; estos tests tardan segundos
TIMEOUT_MODULOS = 120.0
MARGEN_FINAL = 8.0         # tiempo que se reserva para dejar el repo en orden y escribir resultados
ESTIMACION_MODULOS = 10.0  # duración supuesta de los módulos mientras no se haya medido


@dataclass
class Veredicto:
    objetivo_pasa: bool
    regresion_verificada: bool = False           # False si no se alcanzó a comprobar
    regresiones: list = field(default_factory=list)
    salida: str = ""                             # salida de los tests objetivo que fallan
    mensajes: dict = field(default_factory=dict) # test con regresión -> mensaje de error

    @property
    def aprobado(self) -> bool:
        return self.objetivo_pasa and self.regresion_verificada and not self.regresiones

    def detalle(self) -> str:
        """Descripción de la falla, escrita para dársela al modelo en el reintento."""
        if not self.objetivo_pasa:
            return self.salida
        if self.regresiones:
            filas = [f"- {t}\n{self.mensajes.get(t, '')}" for t in self.regresiones]
            return "Estos tests pasaban antes del cambio y ahora fallan:\n" + "\n".join(filas)
        return "" if self.regresion_verificada else "No se alcanzó a comprobar si hay regresiones."


class Verificador:
    """Verifica cambios de un bug. Guarda la línea base para medirla una sola vez."""

    def __init__(self, repo: Path, info: dict):
        self.repo = repo
        self.comandos = info.get("comandos_test") or []
        self.modulos = info.get("modulos_test") or []
        self.linea_base = None       # tests que pasan con el código original
        self.duracion_modulos = ESTIMACION_MODULOS

    def _correr_modulos(self):
        """Corre los módulos si alcanza el tiempo y mide cuánto tardan. None si no se pudo."""
        if not tiempo.alcanza(self.duracion_modulos * 1.2 + MARGEN_FINAL):
            print("[verificacion] sin tiempo para los módulos de test", flush=True)
            return None
        inicio = tiempo.transcurrido()
        resultado = ejecutor.ejecutar_modulos(self.repo, self.modulos,
                                              min(TIMEOUT_MODULOS, tiempo.restante() - MARGEN_FINAL))
        self.duracion_modulos = tiempo.transcurrido() - inicio
        print(f"[verificacion] módulos: {len(resultado.pasan)}/{len(resultado.estados)} pasan "
              f"({self.duracion_modulos:.1f}s)", flush=True)
        return resultado if resultado.completo and resultado.estados else None

    def _medir_linea_base(self, contenidos: dict) -> None:
        """Mide la línea base quitando un momento el cambio; al terminar lo vuelve a aplicar."""
        edicion.revertir(self.repo, contenidos)
        try:
            base = self._correr_modulos()
        finally:
            edicion.escribir(self.repo, contenidos)
        if base is not None:
            self.linea_base = base.pasan

    def verificar(self, contenidos: dict) -> Veredicto:
        """Verifica el cambio `contenidos`, que ya debe estar escrito en el repo."""
        objetivo = ejecutor.ejecutar_tests_bug(self.repo, self.comandos,
                                               timeout=min(TIMEOUT_OBJETIVO, tiempo.restante()))
        if not objetivo.pasan:
            return Veredicto(False, salida=objetivo.salida_fallos)
        if not self.modulos:
            return Veredicto(True, regresion_verificada=True)

        modulos = self._correr_modulos()
        if modulos is None:
            return Veredicto(True)
        if len(modulos.pasan) == len(modulos.estados):
            return Veredicto(True, regresion_verificada=True)

        if self.linea_base is None:
            self._medir_linea_base(contenidos)
        if self.linea_base is None:
            return Veredicto(True)
        regresiones = sorted(self.linea_base - modulos.pasan)
        return Veredicto(True, regresion_verificada=True, regresiones=regresiones,
                         mensajes={t: modulos.mensajes.get(t, "") for t in regresiones})
