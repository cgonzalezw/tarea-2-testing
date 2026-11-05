"""Construcción del contexto del reparador (pieza 4): qué información recibe el LLM y cuánta.

Cada sección del prompt tiene su propio límite de caracteres, así que el tamaño total está acotado
aunque el bug esté en una función de 2000 líneas o el traceback sea enorme:

  falla (error + traceback)   MAX_CHARS_FALLA
  tests que fallan            MAX_CHARS_TESTS
  datos de test               MAX_CHARS_DATOS
  código sospechoso           MAX_CHARS_CODIGO   (primero las regiones más sospechosas)
  intentos anteriores         MAX_CHARS_HISTORIAL

Solo se muestran regiones del código con números de línea, nunca archivos completos.
"""
from dataclasses import dataclass
from pathlib import Path

import codigo
import ejecutor
from localizador import a_region

MAX_CHARS_FALLA = 6000
MAX_CHARS_TESTS = 5000
MAX_CHARS_DATOS = 3000
MAX_CHARS_CODIGO = 24000
MAX_CHARS_HISTORIAL = 7000
MAX_CHARS_DIFF = 1500        # diff de cada intento anterior
MAX_LINEAS_REGION = 200      # una región más larga se corta (el localizador ya centra las gigantes)
MAX_LINEAS_IMPORTS = 30
MIN_CHARS_REGION = 400       # si queda menos presupuesto que esto, no se agrega otra región

PROMPT_REPARAR = """Eres un experto en reparar bugs en proyectos Python. Unos tests fallan por un bug \
en el código fuente del proyecto. El proyecto corre en Python {python}.

## Falla observada con el código original
{falla}

## Tests que fallan
{tests}
{datos}
## Código sospechoso (ordenado de más a menos sospechoso; cada línea lleva su número)
{regiones}
{historial}
## Tu tarea
Encuentra la causa raíz y corrígela con el cambio mínimo. Los tests son correctos: no se pueden \
modificar. La corrección debe ser general (no un caso especial para el valor del test) y no debe \
romper el comportamiento que hoy funciona para otros usos del código.

Primero explica la causa en un máximo de 5 líneas. Después escribe uno o más bloques con EXACTAMENTE \
este formato:

ARCHIVO: ruta/relativa/al/archivo.py
<<<<<<< BUSCAR
líneas actuales del archivo
=======
líneas que las reemplazan
>>>>>>> REEMPLAZAR

Reglas:
- BUSCAR debe ser una copia exacta de líneas consecutivas del archivo, con su sangría y SIN los \
números de línea ni el separador " | ".
- Incluye en BUSCAR las líneas justas para que el texto aparezca una sola vez en el archivo.
- Usa un bloque por cada lugar que cambias: no juntes en un mismo BUSCAR líneas de partes distintas \
del archivo (por ejemplo, un import y una función).
- REEMPLAZAR debe quedar completo y con la sangría correcta; no abrevies con "...".
- Si necesitas un import nuevo, agrégalo con otro bloque."""


@dataclass
class Intento:
    """Un intento fallido, para mostrárselo al modelo en el siguiente."""
    motivo: str        # p. ej. "los tests del bug siguen fallando"
    detalle: str       # error de aplicación, salida de los tests o regresiones
    diff: str = ""     # cambio que se probó (vacío si no se pudo aplicar)


# ---------------------------------------------------------------- selección de regiones

def regiones_desde_salida(repo: Path, salida: str) -> list:
    """Regiones por las que pasó un traceback nuevo, de la más cercana al error a la más lejana."""
    regiones = []
    for f in reversed(ejecutor.parsear_traceback(salida, repo)):
        if f.es_test or not codigo.es_fuente(f.archivo):
            continue
        b = codigo.bloque_que_contiene(repo, f.archivo, f.linea) \
            or codigo.Bloque(f.archivo, f"<módulo:{f.linea}>", max(1, f.linea - 10), f.linea + 10)
        regiones.append(a_region(b, 0.0, ["traceback del último intento"], f.linea))
    return regiones


def sin_solapes(regiones: list) -> list:
    """Une las regiones que se solapan en un mismo archivo; conserva el orden de sospecha."""
    resultado = []
    for r in regiones:
        for u in resultado:
            if u["archivo"] == r["archivo"] and r["inicio"] <= u["fin"] and u["inicio"] <= r["fin"]:
                u["inicio"], u["fin"] = min(u["inicio"], r["inicio"]), max(u["fin"], r["fin"])
                break
        else:
            resultado.append(dict(r))
    return resultado


# ---------------------------------------------------------------- secciones del prompt

def _imports(repo: Path, archivo: str) -> str:
    """Imports del módulo: el modelo los necesita para saber qué nombres ya están disponibles."""
    raiz = codigo.arbol(repo, archivo)
    if raiz is None:
        return ""
    numeros = []
    for nodo in raiz.body:
        if type(nodo).__name__ in ("Import", "ImportFrom"):
            numeros.extend(range(nodo.lineno, nodo.end_lineno + 1))
    lineas = codigo.leer_lineas(repo, archivo)
    return "\n".join(f"{n} | {lineas[n - 1]}" for n in numeros[:MAX_LINEAS_IMPORTS])


def _recortar_lineas(texto: str, maximo: int) -> str:
    """Corta en un fin de línea, para no mostrarle al modelo una línea de código a medias."""
    return texto[:texto.rfind("\n", 0, maximo) + 1].rstrip("\n") if len(texto) > maximo else texto


def seccion_codigo(repo: Path, regiones: list, maximo: int = MAX_CHARS_CODIGO) -> str:
    partes, usado, con_imports, omitidas = [], 0, set(), 0
    for r in regiones:
        restante = maximo - usado
        if restante < MIN_CHARS_REGION:
            omitidas += 1
            continue
        archivo, inicio, fin = r["archivo"], r["inicio"], r["fin"]
        corte_lineas = min(fin, inicio + MAX_LINEAS_REGION - 1)
        completo = codigo.fragmento(repo, archivo, inicio, corte_lineas)
        texto = _recortar_lineas(completo, restante)
        cortada = corte_lineas < fin or texto != completo
        titulo = f"### {archivo}"
        if archivo not in con_imports:
            imports = _imports(repo, archivo)
            if imports and len(imports) + len(texto) <= restante:
                titulo += f"\nImports del archivo (puede haber otras líneas entre ellos):\n{imports}\n"
                con_imports.add(archivo)
        nombre = f" :: {r['funcion']}" if r.get("funcion") else ""
        cola = f"\n... (región recortada; continúa hasta la línea {fin})" if cortada else ""
        bloque = f"{titulo}\nLíneas {inicio}-{fin}{nombre}\n{texto}{cola}"
        partes.append(bloque)
        usado += len(bloque)
    if omitidas:
        partes.append(f"({omitidas} regiones menos sospechosas omitidas por tamaño)")
    return "\n\n".join(partes) or "(no disponible)"


def seccion_falla(falla: dict) -> str:
    errores = "\n".join(falla.get("errores") or [])
    salida = ejecutor.recortar(falla.get("salida") or "", MAX_CHARS_FALLA - len(errores) - 50)
    return f"Errores:\n{errores}\n\nSalida de los tests:\n{salida}" if errores else salida or "(no disponible)"


def _marcar_linea_que_falla(test: dict, frames: list) -> str:
    """Señala la línea del test donde ocurrió la falla: un test puede tener varias aserciones
    parecidas y el modelo necesita saber cuál es la que no se cumple."""
    lineas = test["codigo"].split("\n")
    if test.get("inicio") and test.get("fin"):
        fallan = {f["linea"] for f in frames if f["archivo"] == test["archivo"]}
        for numero in fallan & set(range(test["inicio"], test["fin"] + 1)):
            lineas[numero - test["inicio"]] += "   # <-- ESTA LÍNEA FALLA"
    return "\n".join(lineas)


def seccion_tests(falla: dict) -> str:
    frames = falla.get("frames") or []
    texto = "\n\n".join(f"# {t['archivo']} :: {t['nombre']}\n{_marcar_linea_que_falla(t, frames)}"
                        for t in falla.get("tests") or [])
    return ejecutor.recortar(texto, MAX_CHARS_TESTS) or "(no disponible)"


def seccion_datos(repo: Path, info: dict, falla: dict) -> str:
    """Archivos auxiliares de los tests (entradas y salidas esperadas), si son pequeños."""
    ya_mostrados = {t["archivo"] for t in falla.get("tests") or []} | set(info.get("modulos_test") or [])
    partes, usado = [], 0
    for archivo in info.get("archivos_test") or []:
        if archivo in ya_mostrados:
            continue
        try:
            texto = (repo / archivo).read_text(errors="replace")
        except OSError:
            continue
        if usado + len(texto) <= MAX_CHARS_DATOS:
            partes.append(f"# {archivo}\n{texto}")
            usado += len(texto)
    return "\n## Datos de los tests\n" + "\n\n".join(partes) + "\n" if partes else ""


def seccion_historial(intentos: list) -> str:
    """Intentos fallidos: del último se muestra todo el detalle; de los anteriores, lo esencial,
    para que el modelo no repita lo mismo sin gastar el presupuesto en salidas viejas."""
    if not intentos:
        return ""
    partes, restante = [], MAX_CHARS_HISTORIAL
    for n, intento in reversed(list(enumerate(intentos, 1))):
        maximo_detalle = restante // 2 if n == len(intentos) else 300
        detalle = ejecutor.recortar(intento.detalle, maximo_detalle)
        diff = ejecutor.recortar(intento.diff, MAX_CHARS_DIFF)
        texto = f"### Intento {n}: {intento.motivo}\n"
        texto += f"Cambio probado:\n{diff}\n" if diff else ""
        texto += f"Resultado:\n{detalle}"
        if len(texto) > restante:
            break
        partes.append(texto)
        restante -= len(texto)
    partes.reverse()
    return ("\n## Intentos anteriores que NO funcionaron (el código volvió a su estado original)\n"
            + "\n\n".join(partes)
            + "\n\nTu respuesta se aplica sobre el código original, no sobre un intento anterior. Si un cambio "
              "anterior corrigió parte del problema (el error cambió), inclúyelo de nuevo y agrega lo que falta. "
              "No repitas cambios que no tuvieron efecto: la causa puede estar en otro lugar.\n")


def construir_prompt(repo: Path, info: dict, falla: dict, regiones: list, intentos: list = ()) -> str:
    secciones = {
        "falla": seccion_falla(falla),
        "tests": seccion_tests(falla),
        "datos": seccion_datos(repo, info, falla),
        "regiones": seccion_codigo(repo, sin_solapes(regiones)),
        "historial": seccion_historial(list(intentos)),
    }
    prompt = PROMPT_REPARAR.format(python=info.get("python", "3"), **secciones)
    print(f"[reparador] prompt de {len(prompt)} caracteres: "
          + ", ".join(f"{k}={len(v)}" for k, v in secciones.items()), flush=True)
    return prompt
