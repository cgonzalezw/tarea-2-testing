"""Pruebas de verificacion.py: corren tests de verdad en un proyecto de juguete.

Reutilizan el entorno de Python 3.8 de un bug ya instalado (solo necesitan su pytest), así que no
gastan API. Uso: python prueba_verificacion.py
"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import codigo
import ejecutor
import tiempo
import verificacion
from verificacion import Verificador

DATASET = Path(__file__).resolve().parent.parent / "dataset"
ENTORNO = next(DATASET.glob("*/repo/env/bin/activate"), None)

FUENTE = """def doble(x):
    return x + 1


def triple(x):
    return x * 3
"""
TESTS = """from pkg.mod import doble, triple


def test_objetivo():
    assert doble(5) == 10


def test_triple():
    assert triple(2) == 6


def test_ya_fallaba():
    assert False
"""
ARREGLO = FUENTE.replace("x + 1", "x * 2")
ROMPE_TRIPLE = ARREGLO.replace("x * 3", "x * 4")
ROMPE_IMPORT = ARREGLO + "\nimport modulo_que_no_existe\n"
BUCLE = FUENTE.replace("return x + 1", "while True:\n        pass")


@unittest.skipUnless(ENTORNO, "hace falta al menos un bug instalado con setup.sh")
class Verificacion(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        (self.repo / "pkg").mkdir()
        (self.repo / "tests").mkdir()
        (self.repo / "pkg/__init__.py").write_text("")
        (self.repo / "pkg/mod.py").write_text(FUENTE)
        (self.repo / "tests/test_mod.py").write_text(TESTS)
        (self.repo / ".gitignore").write_text("env\n")
        (self.repo / "env").symlink_to(ENTORNO.parent.parent)
        for cmd in (["init", "-q"], ["add", "."],
                    ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"]):
            subprocess.run(["git", *cmd], cwd=self.repo, check=True)
        codigo.invalidar_cache()
        tiempo.iniciar()
        self.v = Verificador(self.repo, {
            "comandos_test": ["python -m pytest -q -p no:cacheprovider tests/test_mod.py::test_objetivo"],
            "modulos_test": ["tests/test_mod.py"]})
        # Sin test_ya_fallaba, para los casos en que todos los tests quedan en verde.
        self.limpio = TESTS[:TESTS.index("def test_ya_fallaba")].rstrip() + "\n"

    def probar(self, contenido):
        cambio = {"pkg/mod.py": contenido}
        (self.repo / "pkg/mod.py").write_text(contenido)
        return cambio, self.v.verificar(cambio)

    def usar_tests_limpios(self):
        (self.repo / "tests/test_mod.py").write_text(self.limpio)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "limpio"],
                       cwd=self.repo, check=True)

    def test_si_el_objetivo_falla_no_corre_los_modulos(self):
        with mock.patch.object(ejecutor, "ejecutar_modulos") as modulos:
            _, r = self.probar(FUENTE + "\n# sin efecto\n")
        self.assertFalse(r.objetivo_pasa)
        self.assertFalse(r.aprobado)
        self.assertIn("doble", r.detalle())
        modulos.assert_not_called()

    def test_arreglo_con_todo_en_verde_no_mide_linea_base(self):
        self.usar_tests_limpios()
        with mock.patch.object(ejecutor, "ejecutar_modulos", wraps=ejecutor.ejecutar_modulos) as modulos:
            _, r = self.probar(ARREGLO)
        self.assertTrue(r.aprobado)
        self.assertEqual(modulos.call_count, 1)
        self.assertIsNone(self.v.linea_base)

    def test_un_test_que_ya_fallaba_no_es_regresion(self):
        _, r = self.probar(ARREGLO)
        self.assertTrue(r.aprobado, r.regresiones)
        self.assertIsNotNone(self.v.linea_base)

    def test_detecta_regresion_y_la_explica(self):
        _, r = self.probar(ROMPE_TRIPLE)
        self.assertTrue(r.objetivo_pasa)
        self.assertFalse(r.aprobado)
        self.assertEqual(r.regresiones, ["tests.test_mod::test_triple"])
        self.assertIn("test_triple", r.detalle())
        self.assertIn("assert", r.detalle())

    def test_el_cambio_queda_aplicado_despues_de_medir_la_linea_base(self):
        self.probar(ROMPE_TRIPLE)
        self.assertEqual((self.repo / "pkg/mod.py").read_text(), ROMPE_TRIPLE)

    def test_linea_base_se_mide_una_sola_vez(self):
        self.probar(ROMPE_TRIPLE)
        with mock.patch.object(self.v, "_medir_linea_base") as medir:
            _, r = self.probar(ROMPE_TRIPLE)
        medir.assert_not_called()
        self.assertEqual(r.regresiones, ["tests.test_mod::test_triple"])

    def test_error_de_import_se_detecta_en_el_objetivo(self):
        _, r = self.probar(ROMPE_IMPORT)
        self.assertFalse(r.objetivo_pasa)
        self.assertIn("modulo_que_no_existe", r.detalle())

    def test_bucle_infinito_se_corta(self):
        with mock.patch.object(verificacion, "TIMEOUT_OBJETIVO", 3.0):
            _, r = self.probar(BUCLE)
        self.assertFalse(r.objetivo_pasa)
        self.assertIn("TIMEOUT", r.detalle())

    def test_sin_tiempo_para_modulos_no_se_da_por_aprobado(self):
        with mock.patch.object(tiempo, "alcanza", return_value=False):
            _, r = self.probar(ARREGLO)
        self.assertTrue(r.objetivo_pasa)
        self.assertFalse(r.regresion_verificada)
        self.assertFalse(r.aprobado)

    def test_sin_modulos_basta_con_el_objetivo(self):
        self.v.modulos = []
        _, r = self.probar(ARREGLO)
        self.assertTrue(r.aprobado)


if __name__ == "__main__":
    unittest.main()
