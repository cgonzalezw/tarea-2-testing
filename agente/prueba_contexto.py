"""Pruebas de contexto.py sobre un repo git temporal (no necesitan API key ni entornos de bugs).

Uso: python prueba_contexto.py
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

import codigo
import contexto
from contexto import Intento

INFO = {"python": "3.8.3", "modulos_test": ["tests/test_mod.py"],
        "archivos_test": ["tests/test_mod.py", "tests/data/entrada.txt", "tests/data/enorme.txt"]}
FALLA = {"errores": ["E   AssertionError: 1 != 2"], "salida": "salida de pytest\nE   AssertionError: 1 != 2",
         "tests": [{"archivo": "tests/test_mod.py", "nombre": "test_x", "codigo": "def test_x():\n    assert f() == 2"}]}
LIMITE_TOTAL = (contexto.MAX_CHARS_FALLA + contexto.MAX_CHARS_TESTS + contexto.MAX_CHARS_DATOS
                + contexto.MAX_CHARS_CODIGO + contexto.MAX_CHARS_HISTORIAL + 3000)


def region(archivo, inicio, fin, funcion=None):
    return {"archivo": archivo, "inicio": inicio, "fin": fin, "funcion": funcion}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        (self.repo / "pkg").mkdir()
        (self.repo / "tests/data").mkdir(parents=True)
        cuerpo = "".join(f"    x{i} = {i}\n" for i in range(2000))
        (self.repo / "pkg/mod.py").write_text("import os\nfrom sys import (\n    path,\n)\n\n\ndef f():\n    return 1\n")
        (self.repo / "pkg/gigante.py").write_text("def g():\n" + cuerpo)
        (self.repo / "tests/test_mod.py").write_text("def test_x():\n    assert f() == 2\n")
        (self.repo / "tests/data/entrada.txt").write_text("dato de entrada\n")
        (self.repo / "tests/data/enorme.txt").write_text("z" * 100000)
        for cmd in (["init", "-q"], ["add", "."],
                    ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"]):
            subprocess.run(["git", *cmd], cwd=self.repo, check=True)
        codigo.invalidar_cache()

    def prompt(self, regiones, intentos=(), falla=FALLA):
        return contexto.construir_prompt(self.repo, INFO, falla, regiones, intentos)


class Contenido(Base):
    def test_incluye_falla_tests_y_codigo_con_numeros(self):
        p = self.prompt([region("pkg/mod.py", 7, 8, "f")])
        self.assertIn("AssertionError: 1 != 2", p)
        self.assertIn("assert f() == 2", p)
        self.assertIn("8 |     return 1", p)
        self.assertIn("Python 3.8.3", p)

    def test_incluye_imports_multilinea(self):
        p = self.prompt([region("pkg/mod.py", 7, 8, "f")])
        self.assertIn("1 | import os", p)
        self.assertIn("3 |     path,", p)

    def test_incluye_datos_pequenos_y_omite_los_enormes(self):
        p = self.prompt([region("pkg/mod.py", 7, 8)])
        self.assertIn("dato de entrada", p)
        self.assertNotIn("zzzz", p)

    def test_llaves_en_el_codigo_no_rompen_el_formato(self):
        falla = dict(FALLA, salida="{'a': 1} {x}", errores=["KeyError: {y}"])
        self.assertIn("{'a': 1} {x}", self.prompt([region("pkg/mod.py", 7, 8)], falla=falla))

    def test_marca_la_linea_del_test_que_falla(self):
        falla = {"frames": [{"archivo": "tests/test_mod.py", "linea": 11},
                            {"archivo": "pkg/mod.py", "linea": 12}],
                 "tests": [{"archivo": "tests/test_mod.py", "nombre": "t", "inicio": 10, "fin": 12,
                            "codigo": "def t():\n    assert a\n    assert b\n\ndef ayuda():\n    pass"}]}
        texto = contexto.seccion_tests(falla)
        self.assertIn("    assert a   # <-- ESTA LÍNEA FALLA\n    assert b\n", texto)
        self.assertEqual(texto.count("ESTA LÍNEA FALLA"), 1)

    def test_sin_numeros_de_linea_no_marca_nada(self):
        falla = {"frames": [{"archivo": "a.py", "linea": 1}],
                 "tests": [{"archivo": "a.py", "nombre": "t", "codigo": "def t(): pass"}]}
        self.assertNotIn("ESTA LÍNEA FALLA", contexto.seccion_tests(falla))

    def test_falla_vacia_no_rompe(self):
        self.assertIn("(no disponible)", self.prompt([], falla={}))


class Tamano(Base):
    def test_funcion_de_2000_lineas_queda_acotada(self):
        p = self.prompt([region("pkg/gigante.py", 1, 2001, "g")])
        self.assertLess(len(p), LIMITE_TOTAL)
        self.assertIn("región recortada; continúa hasta la línea 2001", p)
        self.assertNotIn("x1999", p)

    def test_todo_enorme_a_la_vez_queda_acotado(self):
        falla = {"errores": ["E  " + "e" * 200] * 15, "salida": "s" * 500000,
                 "tests": [{"archivo": "tests/test_mod.py", "nombre": "t", "codigo": "c" * 500000}]}
        regiones = [region("pkg/gigante.py", i, i + 300) for i in range(1, 1800, 400)]
        intentos = [Intento("sigue fallando", "d" * 500000, "+" * 500000) for _ in range(6)]
        p = self.prompt(regiones, intentos, falla)
        self.assertLess(len(p), LIMITE_TOTAL)
        self.assertIn("Intento 6", p)

    def test_presupuesto_prioriza_las_mas_sospechosas(self):
        regiones = [region("pkg/gigante.py", 1500, 1699), region("pkg/gigante.py", 1, 200),
                    region("pkg/gigante.py", 300, 499), region("pkg/gigante.py", 600, 799),
                    region("pkg/gigante.py", 900, 1099), region("pkg/mod.py", 7, 8)]
        codigo_txt = contexto.seccion_codigo(self.repo, regiones, maximo=6000)
        self.assertIn("x1500", codigo_txt)
        self.assertNotIn("return 1", codigo_txt)
        self.assertIn("omitidas por tamaño", codigo_txt)


class Regiones(Base):
    def test_une_solapes_y_conserva_el_orden(self):
        r = contexto.sin_solapes([region("a.py", 50, 60), region("b.py", 1, 5), region("a.py", 55, 70)])
        self.assertEqual([(x["archivo"], x["inicio"], x["fin"]) for x in r], [("a.py", 50, 70), ("b.py", 1, 5)])

    def test_regiones_desde_traceback_sin_tests_y_mas_profunda_primero(self):
        salida = (f'File "{self.repo}/tests/test_mod.py", line 2, in test_x\n'
                  f'File "{self.repo}/pkg/gigante.py", line 1000, in g\n'
                  f'File "{self.repo}/pkg/mod.py", line 8, in f\n')
        r = contexto.regiones_desde_salida(self.repo, salida)
        self.assertEqual([x["archivo"] for x in r], ["pkg/mod.py", "pkg/gigante.py"])
        self.assertLessEqual(r[1]["fin"] - r[1]["inicio"], 100)   # función gigante: ventana


class Historial(Base):
    def test_ultimo_con_detalle_completo_y_anteriores_resumidos(self):
        t = contexto.seccion_historial([Intento("sigue fallando", "VIEJO" * 500, "-a\n+b"),
                                        Intento("regresión", "NUEVO" * 200)])
        self.assertIn("Intento 1: sigue fallando", t)
        self.assertIn("Intento 2: regresión", t)
        self.assertIn("-a\n+b", t)
        self.assertIn("NUEVO" * 200, t)
        self.assertNotIn("VIEJO" * 200, t)

    def test_sin_intentos_no_agrega_seccion(self):
        self.assertEqual(contexto.seccion_historial([]), "")


if __name__ == "__main__":
    unittest.main()
