"""Pruebas del bucle de reparación con un LLM simulado (respuestas programadas), sin gastar API.

Corren tests de verdad en un proyecto de juguete, con el entorno de un bug ya instalado.
Uso: python prueba_reparador.py
"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import codigo
import llm
import reparador
import tiempo
from verificacion import Veredicto

DATASET = Path(codigo.__file__).resolve().parent.parent / "dataset"
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
"""
INFO = {"comandos_test": ["python -m pytest -q -p no:cacheprovider tests/test_mod.py::test_objetivo"],
        "modulos_test": ["tests/test_mod.py"], "archivos_test": ["tests/test_mod.py"], "python": "3.8.3"}
LOCALIZACION = {
    "regiones": [{"archivo": "pkg/mod.py", "inicio": 1, "fin": 2, "funcion": "doble"},
                 {"archivo": "pkg/mod.py", "inicio": 5, "fin": 6, "funcion": "triple"}],
    "falla": {"tests_pasan_originalmente": False, "salida": "assert 6 == 10", "errores": ["E   assert 6 == 10"],
              "tests": [{"archivo": "tests/test_mod.py", "nombre": "test_objetivo",
                         "codigo": "def test_objetivo():\n    assert doble(5) == 10"}]}}


def cambio(buscar, reemplazar, archivo="pkg/mod.py"):
    return f"Causa: x.\nARCHIVO: {archivo}\n<<<<<<< BUSCAR\n{buscar}\n=======\n{reemplazar}\n>>>>>>> REEMPLAZAR\n"


ARREGLO = cambio("    return x + 1", "    return x * 2")
INUTIL = cambio("    return x + 1", "    return x + 2")
ROTO = cambio("    return x + 1", "    return (x * 2")
NO_COINCIDE = cambio("    return x + 99", "    return x * 2")
# Hace pasar el objetivo, pero rompe test_triple.
REGRESION = ARREGLO.replace("Causa: x.\n", "") + cambio("    return x * 3", "    return x * 4")
EN_TEST = cambio("    assert doble(5) == 10", "    assert True", "tests/test_mod.py")


@unittest.skipUnless(ENTORNO, "hace falta al menos un bug instalado con setup.sh")
class Reparador(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.carpeta = Path(self._tmp.name)
        self.repo = self.carpeta / "repo"
        (self.repo / "pkg").mkdir(parents=True)
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

    def reparar(self, respuestas, localizacion=LOCALIZACION):
        """Corre el reparador con respuestas programadas. Si se acaban, el modelo no responde."""
        self.prompts = []

        def generar(prompt, **_):
            self.prompts.append(prompt)
            return respuestas[len(self.prompts) - 1] if len(self.prompts) <= len(respuestas) else None

        with mock.patch.object(llm, "generar", generar):
            return reparador.reparar(self.carpeta, INFO, localizacion)

    def fuente(self):
        return (self.repo / "pkg/mod.py").read_text()

    def repo_limpio(self):
        return subprocess.run(["git", "status", "--short"], cwd=self.repo,
                              capture_output=True, text=True).stdout == ""

    def test_arregla_a_la_primera(self):
        r = self.reparar([ARREGLO])
        self.assertEqual((r["resuelto"], r["aprobado"], r["intentos"]), (True, True, 1))
        self.assertIn("x * 2", self.fuente())
        self.assertEqual(len(self.prompts), 1)
        self.assertNotIn("Intentos anteriores", self.prompts[0])

    def test_respuesta_basura_y_luego_arregla(self):
        r = self.reparar(["no sé cómo arreglarlo", ARREGLO])
        self.assertTrue(r["aprobado"])
        self.assertIn("ningún bloque", self.prompts[1])

    def test_buscar_no_coincide_y_luego_arregla(self):
        r = self.reparar([NO_COINCIDE, ARREGLO])
        self.assertTrue(r["aprobado"])
        self.assertIn("no coincide", self.prompts[1])

    def test_sintaxis_rota_y_luego_arregla(self):
        r = self.reparar([ROTO, ARREGLO])
        self.assertTrue(r["aprobado"])
        self.assertIn("rompe la sintaxis", self.prompts[1])

    def test_no_arregla_y_el_siguiente_recibe_diff_y_salida(self):
        r = self.reparar([INUTIL, ARREGLO])
        self.assertTrue(r["aprobado"])
        self.assertIn("los tests del bug siguen fallando", self.prompts[1])
        self.assertIn("inclúyelo de nuevo", self.prompts[1])
        self.assertIn("+    return x + 2", self.prompts[1])
        self.assertIn("assert 7 == 10", self.prompts[1])

    def test_motivo_distingue_si_el_error_cambio(self):
        original = ["E   assert 6 == 10"]
        igual = Veredicto(False, salida="E   assert 6 == 10\n")
        distinto = Veredicto(False, salida="E   assert 7 == 10\n")
        self.assertEqual(reparador._motivo(igual, original), "los tests del bug siguen fallando")
        self.assertIn("error distinto", reparador._motivo(distinto, original))

    def test_cada_intento_parte_del_codigo_original(self):
        self.reparar([INUTIL, ARREGLO])
        self.assertIn("return x + 1", self.prompts[1])
        self.assertIn("    return x * 2\n", self.fuente())

    def test_regresion_y_luego_arregla(self):
        r = self.reparar([REGRESION, ARREGLO])
        self.assertTrue(r["aprobado"])
        self.assertIn("el cambio rompió otros tests", self.prompts[1])
        self.assertIn("test_triple", self.prompts[1])
        self.assertIn("x * 3", self.fuente())

    def test_solo_regresiones_deja_el_mejor_candidato(self):
        r = self.reparar([REGRESION])
        self.assertEqual((r["resuelto"], r["aprobado"], r["regresiones"]), (True, False, 1))
        self.assertIn("x * 2", self.fuente())

    def test_prefiere_el_candidato_con_menos_regresiones(self):
        peor = cambio("    return x * 3", "    return x * 4") + ARREGLO.replace("Causa: x.\n", "")
        (self.repo / "tests/test_mod.py").write_text(TESTS + "\n\ndef test_triple_2():\n    assert triple(1) == 3\n")
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "m"],
                       cwd=self.repo, check=True)
        # Primer candidato: 2 regresiones. Segundo: la misma corrección con otra forma, 1 sola.
        mejor_con_una = ARREGLO.replace("Causa: x.\n", "") + cambio(
            "    return x * 3", "    return x * 3 if x == 1 else x * 4")
        r = self.reparar([peor, mejor_con_una, INUTIL])
        self.assertEqual(r["regresiones"], 1)
        self.assertIn("x == 1", self.fuente())

    def test_nada_funciona_deja_el_repo_original(self):
        r = self.reparar([INUTIL, ROTO, "basura"])
        self.assertEqual((r["resuelto"], r["aprobado"]), (False, False))
        self.assertTrue(self.repo_limpio())

    def test_api_sin_respuesta_no_se_cae(self):
        r = self.reparar([])
        self.assertFalse(r["resuelto"])
        self.assertEqual(len(self.prompts), reparador.MAX_INTENTOS)
        self.assertTrue(self.repo_limpio())

    def test_cambio_repetido_no_vuelve_a_correr_tests(self):
        with mock.patch.object(reparador, "_probar", wraps=reparador._probar) as probar:
            r = self.reparar([INUTIL, INUTIL, ARREGLO])
        self.assertTrue(r["aprobado"])
        self.assertEqual(probar.call_count, 2)
        self.assertIn("repetiste un cambio", self.prompts[2])

    def test_nunca_edita_tests(self):
        r = self.reparar([EN_TEST, ARREGLO])
        self.assertTrue(r["aprobado"])
        self.assertIn("archivo de test", self.prompts[1])
        self.assertEqual((self.repo / "tests/test_mod.py").read_text(), TESTS)

    def test_tests_que_ya_pasan_no_llaman_al_modelo(self):
        loc = {"regiones": LOCALIZACION["regiones"], "falla": {"tests_pasan_originalmente": True}}
        r = self.reparar([ARREGLO], loc)
        self.assertEqual((r["resuelto"], r["intentos"]), (True, 0))
        self.assertEqual(self.prompts, [])

    def test_sin_tiempo_no_llama_al_modelo(self):
        with mock.patch.object(tiempo, "alcanza", return_value=False):
            r = self.reparar([ARREGLO])
        self.assertFalse(r["resuelto"])
        self.assertEqual(self.prompts, [])

    def test_localizador_de_respaldo_mide_la_falla_y_usa_su_traceback(self):
        r = self.reparar([ARREGLO], {"regiones": [{"archivo": "pkg/mod.py", "inicio": 5, "fin": 6}]})
        self.assertTrue(r["aprobado"])
        self.assertIn("assert doble(5) == 10", self.prompts[0])

    def test_mas_regiones_en_cada_intento(self):
        regiones = [{"archivo": "pkg/mod.py", "inicio": i, "fin": i} for i in range(1, 7)]
        self.assertEqual(len(reparador._regiones_del_intento(0, regiones, [])), 3)
        self.assertEqual(len(reparador._regiones_del_intento(1, regiones, [])), 4)
        self.assertEqual(len(reparador._regiones_del_intento(5, regiones, [])), 6)

    def test_corte_de_main_deja_el_mejor_candidato(self):
        # main.py llama a esto al cortar por tiempo, con otro cambio aplicado a medias.
        self.reparar([REGRESION])
        mejor = self.fuente()
        malo = {"pkg/mod.py": FUENTE.replace("x + 1", "x + 5")}
        (self.repo / "pkg/mod.py").write_text(malo["pkg/mod.py"])
        reparador._estado["aplicado"] = malo
        reparador.dejar_mejor_candidato()
        self.assertEqual(self.fuente(), mejor)

    def test_corte_de_main_sin_candidato_deja_el_original(self):
        self.reparar([])
        malo = {"pkg/mod.py": FUENTE.replace("x + 1", "x + 5")}
        (self.repo / "pkg/mod.py").write_text(malo["pkg/mod.py"])
        reparador._estado["aplicado"] = malo
        reparador.dejar_mejor_candidato()
        self.assertTrue(self.repo_limpio())


if __name__ == "__main__":
    unittest.main()
