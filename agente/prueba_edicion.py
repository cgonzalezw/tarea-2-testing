"""Pruebas de edicion.py sobre un repo git temporal (no necesitan API key ni entornos de bugs).

Uso: python prueba_edicion.py
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

import codigo
import edicion
from edicion import ErrorEdicion

FUENTE = """import os


def suma(a, b):
    return a - b


class Caja:
    def valor(self):
        x = 1
        return x

    def otro(self):
        x = 1
        return x + 1
"""


def bloque(buscar, reemplazar, archivo="pkg/mod.py"):
    cabecera = f"ARCHIVO: {archivo}\n" if archivo else ""
    return f"{cabecera}<<<<<<< BUSCAR\n{buscar}\n=======\n{reemplazar}\n>>>>>>> REEMPLAZAR\n"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        (self.repo / "pkg").mkdir()
        (self.repo / "tests").mkdir()
        (self.repo / "pkg/mod.py").write_text(FUENTE)
        (self.repo / "pkg/otro.py").write_text("y = 1\n")
        (self.repo / "tests/test_mod.py").write_text("def test_x():\n    assert True\n")
        for cmd in (["init", "-q"], ["add", "."],
                    ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"]):
            subprocess.run(["git", *cmd], cwd=self.repo, check=True)
        codigo.invalidar_cache()

    def preparar(self, texto, regiones=()):
        return edicion.preparar(self.repo, edicion.parsear(texto), regiones)


class Parseo(Base):
    def test_con_texto_alrededor_y_cercas(self):
        texto = "Análisis: resta en vez de suma.\n```\n" + bloque("    return a - b", "    return a + b") + "```\n"
        e = edicion.parsear(texto)
        self.assertEqual(len(e), 1)
        self.assertEqual(e[0].archivo, "pkg/mod.py")
        self.assertEqual(e[0].buscar, ["    return a - b"])

    def test_archivo_con_formato_markdown(self):
        e = edicion.parsear("**ARCHIVO: `pkg/mod.py`**\n" + bloque("a", "b", archivo=None))
        self.assertEqual(e[0].archivo, "pkg/mod.py")

    def test_hereda_archivo_del_bloque_anterior(self):
        e = edicion.parsear(bloque("a", "b") + bloque("c", "d", archivo=None))
        self.assertEqual([x.archivo for x in e], ["pkg/mod.py", "pkg/mod.py"])

    def test_quita_numeros_de_linea(self):
        e = edicion.parsear(bloque(" 5 |     return a - b", " 5 |     return a + b"))
        self.assertEqual(e[0].buscar, ["    return a - b"])
        self.assertEqual(e[0].reemplazar, ["    return a + b"])

    def test_reemplazo_vacio_borra_lineas(self):
        e = edicion.parsear("ARCHIVO: pkg/mod.py\n<<<<<<< BUSCAR\nimport os\n=======\n>>>>>>> REEMPLAZAR\n")
        self.assertEqual(e[0].reemplazar, [])

    def test_basura(self):
        for basura in (None, "", "no sé cómo arreglarlo", "<<<<<<< BUSCAR\nsin cierre"):
            self.assertEqual(edicion.parsear(basura), [])


class Aplicacion(Base):
    def test_exacta(self):
        nuevo = self.preparar(bloque("    return a - b", "    return a + b"))["pkg/mod.py"]
        self.assertIn("return a + b", nuevo)
        self.assertTrue(nuevo.endswith("return x + 1\n"))

    def test_no_toca_el_disco(self):
        self.preparar(bloque("    return a - b", "    return a + b"))
        self.assertEqual((self.repo / "pkg/mod.py").read_text(), FUENTE)

    def test_espacios_al_final(self):
        self.assertIn("a + b", self.preparar(bloque("    return a - b   ", "    return a + b"))["pkg/mod.py"])

    def test_sangria_distinta_se_corrige(self):
        nuevo = self.preparar(bloque("return a - b", "if a:\n    return a + b"))["pkg/mod.py"]
        self.assertIn("    if a:\n        return a + b\n", nuevo)

    def test_espacios_internos(self):
        self.assertIn("return a + b", self.preparar(bloque("    return  a  -  b", "    return a + b"))["pkg/mod.py"])

    def test_ambigua_sin_region_falla(self):
        with self.assertRaisesRegex(ErrorEdicion, "varias líneas"):
            self.preparar(bloque("        x = 1", "        x = 2"))

    def test_ambigua_se_resuelve_con_la_region(self):
        region = [{"archivo": "pkg/mod.py", "inicio": 13, "fin": 15}]
        nuevo = self.preparar(bloque("        x = 1", "        x = 2"), region)["pkg/mod.py"]
        self.assertIn("def valor(self):\n        x = 1", nuevo)
        self.assertIn("def otro(self):\n        x = 2", nuevo)

    def test_varias_ediciones_y_archivos(self):
        r = self.preparar(bloque("    return a - b", "    return a + b")
                          + bloque("import os", "import sys") + bloque("y = 1", "y = 2", "pkg/otro.py"))
        self.assertEqual(set(r), {"pkg/mod.py", "pkg/otro.py"})
        self.assertIn("import sys", r["pkg/mod.py"])

    def test_atomica_si_una_falla_no_aplica_ninguna(self):
        with self.assertRaises(ErrorEdicion):
            self.preparar(bloque("y = 1", "y = 2", "pkg/otro.py") + bloque("no existe", "z"))
        self.assertEqual((self.repo / "pkg/otro.py").read_text(), "y = 1\n")

    def test_no_coincide_muestra_parecidas(self):
        with self.assertRaisesRegex(ErrorEdicion, r"(?s)no coincide.*5 \|     return a - b"):
            self.preparar(bloque("    return a - c", "    return a + c"))

    def test_no_coincide_dice_que_linea_no_existe(self):
        with self.assertRaisesRegex(ErrorEdicion, r"(?s)no existe en el archivo:\n  return a - c.*Texto real"):
            self.preparar(bloque("def suma(a, b):\n    return a - c", "def suma(a, b):\n    return a + c"))

    def test_lineas_que_existen_pero_no_son_consecutivas(self):
        with self.assertRaisesRegex(ErrorEdicion, "no son consecutivas"):
            self.preparar(bloque("import os\ndef suma(a, b):", "import sys\ndef suma(a, b):"))

    def test_sin_cambios(self):
        with self.assertRaisesRegex(ErrorEdicion, "no cambian nada"):
            self.preparar(bloque("import os", "import os"))

    def test_sin_bloques(self):
        with self.assertRaisesRegex(ErrorEdicion, "ningún bloque"):
            self.preparar("no sé")

    def test_crlf_se_conserva(self):
        (self.repo / "pkg/otro.py").write_bytes(b"y = 1\r\nz = 2\r\n")
        nuevo = self.preparar(bloque("y = 1", "y = 3", "pkg/otro.py"))["pkg/otro.py"]
        self.assertEqual(nuevo, "y = 3\r\nz = 2\r\n")


class Seguridad(Base):
    def test_no_edita_tests(self):
        for ruta in ("tests/test_mod.py", "./tests/test_mod.py", "TESTS/test_mod.py"):
            with self.assertRaises(ErrorEdicion, msg=ruta):
                self.preparar(bloque("    assert True", "    assert False", ruta))

    def test_no_sale_del_repo(self):
        for ruta in ("../fuera.py", "/etc/passwd", "pkg/../../fuera.py", "pkg/noexiste.py"):
            with self.assertRaises(ErrorEdicion, msg=ruta):
                self.preparar(bloque("a", "b", ruta))

    def test_ruta_con_prefijo_repo(self):
        self.assertIn("pkg/mod.py", self.preparar(bloque("import os", "import sys", "repo/pkg/mod.py")))

    def test_sin_archivo_con_un_solo_sospechoso(self):
        region = [{"archivo": "pkg/mod.py", "inicio": 1, "fin": 5}]
        self.assertIn("pkg/mod.py", self.preparar(bloque("import os", "import sys", archivo=None), region))

    def test_sin_archivo_con_varios_sospechosos_falla(self):
        regiones = [{"archivo": "pkg/mod.py", "inicio": 1, "fin": 5}, {"archivo": "pkg/otro.py", "inicio": 1, "fin": 1}]
        with self.assertRaisesRegex(ErrorEdicion, "ARCHIVO"):
            self.preparar(bloque("import os", "import sys", archivo=None), regiones)


class Sintaxis(Base):
    def test_rota_se_rechaza(self):
        with self.assertRaisesRegex(ErrorEdicion, "rompe la sintaxis"):
            self.preparar(bloque("    return a - b", "    return (a + b"))

    def test_original_invalido_no_bloquea(self):
        (self.repo / "pkg/otro.py").write_text("y = (\n")
        self.assertIn("pkg/otro.py", self.preparar(bloque("y = (", "y = ( # c", "pkg/otro.py")))


class Disco(Base):
    def test_escribir_y_revertir(self):
        cambio = self.preparar(bloque("    return a - b", "    return a + b"))
        diff = edicion.diferencia(self.repo, cambio)
        self.assertIn("-    return a - b", diff)
        self.assertIn("+    return a + b", diff)
        edicion.escribir(self.repo, cambio)
        self.assertIn("a + b", (self.repo / "pkg/mod.py").read_text())
        self.assertIn("a + b", "\n".join(codigo.leer_lineas(self.repo, "pkg/mod.py")))   # sin caché vieja
        edicion.revertir(self.repo, cambio)
        self.assertEqual((self.repo / "pkg/mod.py").read_text(), FUENTE)
        self.assertEqual(subprocess.run(["git", "status", "--short"], cwd=self.repo,
                                        capture_output=True, text=True).stdout, "")

    def test_bytes_no_utf8_se_conservan(self):
        (self.repo / "pkg/otro.py").write_bytes(b"# -*- coding: latin-1 -*-\n# ma\xf1ana\ny = 1\n")
        cambio = self.preparar(bloque("y = 1", "y = 2", "pkg/otro.py"))
        edicion.escribir(self.repo, cambio)
        self.assertEqual((self.repo / "pkg/otro.py").read_bytes(), b"# -*- coding: latin-1 -*-\n# ma\xf1ana\ny = 2\n")


if __name__ == "__main__":
    unittest.main()
