"""Convierte la respuesta del LLM en un cambio real sobre el código (pieza 5), sin usar el LLM.

El modelo responde con bloques BUSCAR/REEMPLAZAR:

    ARCHIVO: ruta/relativa.py
    <<<<<<< BUSCAR
    líneas actuales
    =======
    líneas nuevas
    >>>>>>> REEMPLAZAR

- parsear:   extrae los bloques del texto, tolerando lo que rodea a los bloques.
- preparar:  calcula el contenido nuevo de cada archivo EN MEMORIA y valida ruta, coincidencia y
             sintaxis. Si algo falla lanza ErrorEdicion y el repo queda intacto.
- escribir / revertir: llevan el cambio al disco y lo deshacen.

El mensaje de ErrorEdicion está escrito para el modelo: el reparador se lo devuelve como feedback.
"""
import ast
import difflib
import posixpath
import re
import subprocess
import warnings
from dataclasses import dataclass
from pathlib import Path

import codigo

_BLOQUE = re.compile(r"^<{5,}[^\n]*\n(.*?)^={7}[ \t]*\n(.*?)^>{5,}[^\n]*$", re.M | re.S)
_ARCHIVO = re.compile(r"ARCHIVO\s*:\s*[`*\"']*([^\s`*\"']+)")
_NUMERO_DE_LINEA = re.compile(r"^\s*\d+ \|(?: |$)")   # el formato de codigo.fragmento


class ErrorEdicion(Exception):
    """La edición no se puede aplicar; el mensaje explica al modelo por qué."""


@dataclass(frozen=True)
class Edicion:
    archivo: str      # None si el modelo no lo indicó
    buscar: list      # líneas
    reemplazar: list


# ---------------------------------------------------------------- parseo

def _sin_numeros_de_linea(lineas: list) -> list:
    """Si el modelo copió también los números del prompt ('12 | codigo'), los quita."""
    con_texto = [l for l in lineas if l.strip()]
    if con_texto and all(_NUMERO_DE_LINEA.match(l) for l in con_texto):
        return [_NUMERO_DE_LINEA.sub("", l) for l in lineas]
    return lineas


def _a_lineas(texto: str) -> list:
    return _sin_numeros_de_linea(texto.split("\n")[:-1] if texto else [])


def parsear(texto: str) -> list:
    """Bloques de la respuesta, en orden. Un bloque sin ARCHIVO hereda el del bloque anterior."""
    ediciones, archivo, fin_anterior = [], None, 0
    for m in _BLOQUE.finditer(texto or ""):
        nombres = _ARCHIVO.findall(texto[fin_anterior:m.start()])
        archivo = nombres[-1] if nombres else archivo
        ediciones.append(Edicion(archivo, _a_lineas(m.group(1)), _a_lineas(m.group(2))))
        fin_anterior = m.end()
    return ediciones


# ---------------------------------------------------------------- búsqueda en el archivo

# Niveles de coincidencia, del más estricto al más flexible: (normalización, reindentar)
_NIVELES = (
    (lambda l: l, False),                    # exacta
    (str.rstrip, False),                     # ignora espacios al final
    (str.strip, True),                       # ignora además la sangría
    (lambda l: " ".join(l.split()), True),   # ignora además espacios internos
)


def _sangria(linea: str) -> str:
    return linea[:len(linea) - len(linea.lstrip())]


def _reindentar(reemplazo: list, buscadas: list, halladas: list) -> list:
    """Traslada al reemplazo la diferencia de sangría entre lo que pidió el modelo y lo real."""
    primera = next((i for i, l in enumerate(buscadas) if l.strip()), 0)
    modelo, real = _sangria(buscadas[primera]), _sangria(halladas[primera])
    resultado = []
    for l in reemplazo:
        if l.strip() and l.startswith(modelo):
            l = real + l[len(modelo):]
        resultado.append(l)
    return resultado


def _recortar_vacias(buscar: list, reemplazar: list) -> tuple:
    """Quita las líneas en blanco que el bloque tiene igual en ambos lados, al inicio y al final."""
    buscar, reemplazar = list(buscar), list(reemplazar)
    while buscar and reemplazar and not buscar[0].strip() and not reemplazar[0].strip():
        buscar.pop(0), reemplazar.pop(0)
    while buscar and reemplazar and not buscar[-1].strip() and not reemplazar[-1].strip():
        buscar.pop(), reemplazar.pop()
    return buscar, reemplazar


def _coincidencias(lineas: list, buscar: list, normalizar) -> list:
    objetivo = [normalizar(l) for l in buscar]
    normalizadas = [normalizar(l) for l in lineas]
    n = len(objetivo)
    return [i for i in range(len(lineas) - n + 1) if normalizadas[i:i + n] == objetivo]


def _parecidas(lineas: list, buscar: list) -> str:
    """Líneas reales más parecidas a la primera buscada, para orientar al modelo en el reintento."""
    primera = next((l.strip() for l in buscar if l.strip()), "")
    limpias = [l.strip() for l in lineas]
    cercanas = difflib.get_close_matches(primera, limpias, n=3, cutoff=0.6)
    filas = [f"  {limpias.index(c) + 1} | {lineas[limpias.index(c)]}" for c in cercanas]
    return "\nLíneas parecidas en el archivo:\n" + "\n".join(filas) if filas else ""


def _aplicar_una(lineas: list, edicion: Edicion, rangos: list) -> list:
    """Devuelve las líneas con la edición aplicada. `rangos`: regiones sospechosas del archivo."""
    buscar, reemplazar = _recortar_vacias(edicion.buscar, edicion.reemplazar)
    if not any(l.strip() for l in buscar):
        raise ErrorEdicion(f"{edicion.archivo}: BUSCAR está vacío; copia las líneas que quieres cambiar.")
    for normalizar, reindentar in _NIVELES:
        posiciones = _coincidencias(lineas, buscar, normalizar)
        if not posiciones:
            continue
        if len(posiciones) > 1:   # ambiguo: se prefiere la que cae en una región sospechosa
            dentro = [p for p in posiciones
                      if any(ini <= p + len(buscar) - 1 and p + 1 <= fin for ini, fin in rangos)]
            if len(dentro) != 1:
                numeros = ", ".join(str(p + 1) for p in posiciones[:8])
                raise ErrorEdicion(f"{edicion.archivo}: BUSCAR coincide en varias líneas ({numeros}); "
                                   f"incluye más líneas de contexto para que sea único.")
            posiciones = dentro
        p = posiciones[0]
        halladas = lineas[p:p + len(buscar)]
        nuevas = _reindentar(reemplazar, buscar, halladas) if reindentar else reemplazar
        return lineas[:p] + nuevas + lineas[p + len(buscar):]
    raise ErrorEdicion(f"{edicion.archivo}: BUSCAR no coincide con el archivo; copia las líneas "
                       f"tal como están, sin los números de línea.{_parecidas(lineas, buscar)}")


# ---------------------------------------------------------------- preparar y validar

def _resolver_archivo(repo: Path, edicion: Edicion, regiones: list) -> str:
    """Valida la ruta. Solo se aceptan fuentes versionados: excluye tests, env/ y rutas externas
    (se compara con git, no con el disco, que en macOS no distingue mayúsculas)."""
    fuentes = codigo.listar_fuentes(repo)
    if edicion.archivo is None:
        sospechosos = list(dict.fromkeys(r["archivo"] for r in regiones))
        if len(sospechosos) == 1:
            return sospechosos[0]
        raise ErrorEdicion("Falta la línea ARCHIVO: antes del bloque.")
    rel = posixpath.normpath(edicion.archivo.replace("\\", "/"))
    if rel not in fuentes and rel.startswith("repo/"):
        rel = rel[len("repo/"):]
    if rel in fuentes:
        return rel
    if codigo.es_test(rel):
        raise ErrorEdicion(f"{rel} es un archivo de test y no se puede modificar: el bug está en el "
                           f"código fuente.")
    raise ErrorEdicion(f"{edicion.archivo} no es un archivo de código fuente del proyecto.")


def _leer(ruta: Path) -> str:
    # surrogateescape conserva intactos los bytes que no son UTF-8
    return ruta.read_bytes().decode("utf-8", "surrogateescape")


def _error_de_sintaxis(contenido: str):
    """Mensaje del error de sintaxis, o None si el código es válido."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ast.parse(contenido.encode("utf-8", "surrogateescape"))
    except SyntaxError as e:
        texto = (e.text or "").strip()
        return f"línea {e.lineno}: {e.msg}" + (f" -> {texto}" if texto else "")
    except ValueError as e:
        return str(e)
    return None


def preparar(repo: Path, ediciones: list, regiones: list = ()) -> dict:
    """Calcula {archivo: contenido nuevo} sin tocar el disco. Si una sola edición falla, falla
    todo: nunca queda aplicado un cambio a medias."""
    if not ediciones:
        raise ErrorEdicion("La respuesta no tiene ningún bloque BUSCAR/REEMPLAZAR válido.")
    por_archivo = {}
    for edicion in ediciones:
        archivo = _resolver_archivo(repo, edicion, regiones)
        por_archivo.setdefault(archivo, []).append(Edicion(archivo, edicion.buscar, edicion.reemplazar))

    contenidos = {}
    for archivo, lista in por_archivo.items():
        original = _leer(repo / archivo)
        crlf = "\r\n" in original
        lineas = original.replace("\r\n", "\n").split("\n")
        rangos = [(r["inicio"], r["fin"]) for r in regiones if r["archivo"] == archivo]
        for edicion in lista:
            lineas = _aplicar_una(lineas, edicion, rangos)
        nuevo = "\n".join(lineas)
        nuevo = nuevo.replace("\n", "\r\n") if crlf else nuevo
        if nuevo == original:
            continue
        # Solo se rechaza si la sintaxis la rompió el cambio: Python 3.14 podría no aceptar algo
        # válido en el Python del bug, y en ese caso el original tampoco parsearía.
        error = _error_de_sintaxis(nuevo)
        if error and not _error_de_sintaxis(original):
            raise ErrorEdicion(f"{archivo}: el cambio rompe la sintaxis ({error}).")
        contenidos[archivo] = nuevo
    if not contenidos:
        raise ErrorEdicion("Los bloques no cambian nada: REEMPLAZAR es igual a BUSCAR.")
    return contenidos


# ---------------------------------------------------------------- disco

def escribir(repo: Path, contenidos: dict) -> None:
    for archivo, contenido in contenidos.items():
        (repo / archivo).write_bytes(contenido.encode("utf-8", "surrogateescape"))
    codigo.invalidar_cache()


def revertir(repo: Path, archivos) -> None:
    """Deja los archivos como en el commit original."""
    archivos = list(archivos)
    if archivos:
        subprocess.run(["git", "checkout", "-q", "--", *archivos], cwd=repo)
    codigo.invalidar_cache()


def diferencia(repo: Path, contenidos: dict) -> str:
    """Diff entre el disco y `contenidos` (llamar con el repo sin el cambio aplicado)."""
    partes = []
    for archivo, nuevo in contenidos.items():
        partes.extend(difflib.unified_diff(_leer(repo / archivo).splitlines(), nuevo.splitlines(),
                                           f"a/{archivo}", f"b/{archivo}", lineterm="", n=1))
    return "\n".join(partes)
