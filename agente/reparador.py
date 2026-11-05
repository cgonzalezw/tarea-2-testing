"""REPARADOR: modifica el código fuente de repo/ para que los tests del bug pasen.

Uso por separado (con una localización ya guardada):
    python reparador.py <carpeta_del_bug> <localizacion.json>

Recibe:
  carpeta       Path de la carpeta del bug (contiene repo/, bug.json, run_test.sh, ...)
  info          contenido de bug.json como dict
  localizacion  lo que devolvió localizar() (ver localizador.py)

Hace: deja la reparación aplicada en carpeta/repo/. Solo puede cambiar código fuente, no tests.

Devuelve un dict con:
  {"resuelto": true, "aprobado": true, "intentos": 2}
  resuelto: los tests del bug pasan con lo que quedó aplicado.
  aprobado: además se comprobó que no hay regresiones.

Cómo repara (pieza 7): mientras quede tiempo e intentos,
  1. contexto.py arma el prompt con las regiones, la falla y los intentos anteriores.
  2. edicion.py convierte la respuesta en un cambio y lo valida; si no sirve, es un intento fallido.
  3. verificacion.py comprueba el cambio ya aplicado (tests del bug y regresiones).
  4. Si está aprobado termina. Si no, revierte y reintenta contándole al modelo qué pasó.
Cada intento parte del código original y muestra más regiones que el anterior. Si se acaba el tiempo
queda aplicado el mejor candidato: el que pasa los tests del bug con menos regresiones.
"""
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import contexto
import edicion
import ejecutor
import llm
import localizador
import tiempo
from contexto import Intento
from edicion import ErrorEdicion
from verificacion import Verificador

MAX_INTENTOS = 6
REGIONES_POR_INTENTO = (3, 4, 6)   # regiones del localizador que ve cada intento (la última se repite)
TIEMPO_MIN_INTENTO = 15.0          # no empezar un intento si queda menos que esto
TEMPERATURA_BASE = 0.2
AUMENTO_TEMPERATURA = 0.15         # en cada reintento, para no recibir la misma respuesta
TEMPERATURA_MAX = 0.8

# Lo que está en el repo y lo mejor encontrado: main.py lo necesita si corta por tiempo.
_estado = {"repo": None, "aplicado": None, "mejor": None}


@dataclass
class Candidato:
    contenidos: dict      # archivo -> contenido nuevo
    regresiones: int


def dejar_mejor_candidato() -> None:
    """Deja en el repo el mejor candidato, o el código original si no hay ninguno."""
    repo, aplicado, mejor = _estado["repo"], _estado["aplicado"], _estado["mejor"]
    if repo is None:
        return
    contenidos = mejor.contenidos if mejor else None
    if aplicado is not contenidos:
        if aplicado:
            edicion.revertir(repo, aplicado)
        if contenidos:
            edicion.escribir(repo, contenidos)
        _estado["aplicado"] = contenidos
    ejecutor.restaurar_tests(repo)


def _falla_inicial(repo: Path, info: dict, localizacion: dict) -> dict:
    """La falla que midió el localizador; si no la entregó (usó su respaldo), se mide aquí."""
    if localizacion.get("falla"):
        return localizacion["falla"]
    resultado = ejecutor.ejecutar_tests_bug(repo, info.get("comandos_test", []))
    salida = resultado.salida_fallos
    tests = localizador.codigo_de_tests(repo, localizador.tests_objetivo(repo, info.get("comandos_test", [])))
    return {"tests_pasan_originalmente": resultado.pasan,
            "salida": ejecutor.recortar(salida, 8000),
            "errores": ejecutor.lineas_de_error(salida),
            "frames": [{"archivo": f.archivo, "linea": f.linea} for f in ejecutor.parsear_traceback(salida, repo)],
            "tests": [{k: t[k] for k in ("archivo", "nombre", "inicio", "fin", "codigo")} for t in tests]}


def _regiones_del_intento(n: int, regiones: list, extras: list) -> list:
    """Las más sospechosas según el localizador, más las que señaló el último traceback."""
    cantidad = REGIONES_POR_INTENTO[min(n, len(REGIONES_POR_INTENTO) - 1)]
    return regiones[:cantidad] + extras


def _probar(repo: Path, verificador: Verificador, contenidos: dict, diff: str):
    """Aplica el cambio, lo verifica y registra si es el mejor. Devuelve (veredicto, extras)."""
    edicion.escribir(repo, contenidos)
    _estado["aplicado"] = contenidos
    veredicto = verificador.verificar(contenidos)
    print(f"[reparador] objetivo={'pasa' if veredicto.objetivo_pasa else 'falla'} "
          f"regresiones={len(veredicto.regresiones)} verificado={veredicto.regresion_verificada}\n{diff}",
          flush=True)
    if veredicto.objetivo_pasa:
        mejor = _estado["mejor"]
        if mejor is None or len(veredicto.regresiones) < mejor.regresiones:
            _estado["mejor"] = Candidato(contenidos, len(veredicto.regresiones))
    # Se calcula antes de revertir: los números de línea del traceback son del código con el cambio.
    extras = contexto.regiones_desde_salida(repo, veredicto.detalle())
    return veredicto, extras
    
def _motivo(veredicto, errores_originales: list) -> str:
    """Resume el resultado para el modelo. Un error nuevo puede significar que el cambio avanzó."""
    if veredicto.regresiones:
        return "el cambio rompió otros tests"
    if veredicto.objetivo_pasa:
        return "no se alcanzó a comprobar si hay regresiones"
    errores = ejecutor.lineas_de_error(veredicto.salida)
    if errores and errores != errores_originales:
        return "los tests del bug siguen fallando, pero con un error distinto al original"
    return "los tests del bug siguen fallando"

def _quitar_cambio(repo: Path) -> None:
    edicion.revertir(repo, _estado["aplicado"])
    _estado["aplicado"] = None


def reparar(carpeta: Path, info: dict, localizacion: dict) -> dict:
    repo = carpeta / "repo"
    _estado.update(repo=repo, aplicado=None, mejor=None)
    regiones = localizacion.get("regiones") or []
    falla = _falla_inicial(repo, info, localizacion)
    if falla.get("tests_pasan_originalmente"):
        print("[reparador] los tests del bug ya pasan: no hay nada que reparar")
        return {"resuelto": True, "aprobado": True, "intentos": 0}

    verificador = Verificador(repo, info)
    intentos, probados = [], set()
    extras = contexto.regiones_desde_salida(repo, falla.get("salida", ""))
    aprobado, usados = False, 0
    try:
        for n in range(MAX_INTENTOS):
            if not tiempo.alcanza(TIEMPO_MIN_INTENTO):
                print(f"[reparador] sin tiempo para otro intento ({tiempo.transcurrido():.1f}s)", flush=True)
                break
            usados = n + 1
            visibles = _regiones_del_intento(n, regiones, extras)
            prompt = contexto.construir_prompt(repo, info, falla, visibles, intentos)
            temperatura = min(TEMPERATURA_BASE + n * AUMENTO_TEMPERATURA, TEMPERATURA_MAX)
            respuesta = llm.generar(prompt, temperatura=temperatura, etiqueta=f"reparador {n + 1}")
            if respuesta is None:   # llm.py ya reintentó los errores temporales
                continue

            try:
                contenidos = edicion.preparar(repo, edicion.parsear(respuesta), visibles)
            except ErrorEdicion as e:
                print(f"[reparador] intento {n + 1}: no se pudo aplicar: {e}", flush=True)
                intentos.append(Intento("el cambio no se pudo aplicar", str(e)))
                continue

            diff = edicion.diferencia(repo, contenidos)
            if diff in probados:
                print(f"[reparador] intento {n + 1}: cambio repetido", flush=True)
                intentos.append(Intento("repetiste un cambio que ya había fallado",
                                        "Propón una solución distinta.", diff))
                continue
            probados.add(diff)

            veredicto, extras = _probar(repo, verificador, contenidos, diff)
            if veredicto.aprobado:
                aprobado = True
                break
            _quitar_cambio(repo)
            intentos.append(Intento(_motivo(veredicto, falla.get("errores") or []), veredicto.detalle(), diff))            
    finally:
        dejar_mejor_candidato()

    mejor = _estado["mejor"]
    return {"resuelto": mejor is not None, "aprobado": aprobado, "intentos": usados,
            "regresiones": mejor.regresiones if mejor else None}


if __name__ == "__main__":
    carpeta = Path(sys.argv[1]).resolve()
    info = json.loads((carpeta / "bug.json").read_text())
    localizacion = json.loads(Path(sys.argv[2]).read_text())
    tiempo.iniciar()
    print(json.dumps(reparar(carpeta, info, localizacion), indent=2, ensure_ascii=False))
