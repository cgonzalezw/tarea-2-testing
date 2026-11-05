"""Pruebas de robustez del agente completo: corre main.py en un subproceso, como en la evaluación,
con un cliente de Gemini simulado que falla de distintas formas. No gasta API.

Comprueba en cada caso que el agente no se cae, que los dos JSON quedan válidos y que las métricas
son las reales. Uso: python prueba_robustez.py
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

AGENTE = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENTE))
import codigo  # noqa: E402

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
ARREGLO = ("ARCHIVO: pkg/mod.py\n<<<<<<< BUSCAR\n    return x + 1\n=======\n    return x * 2\n"
           ">>>>>>> REEMPLAZAR\n")
INUTIL = ARREGLO.replace("x * 2", "x + 2")
TOKENS_ENTRADA, TOKENS_SALIDA = 100, 7


class ErrorApi(Exception):
    def __init__(self, code):
        super().__init__(f"error {code}")
        self.code = code


def respuesta(texto, con_uso=True):
    uso = types.SimpleNamespace(prompt_token_count=TOKENS_ENTRADA, candidates_token_count=TOKENS_SALIDA)
    return types.SimpleNamespace(text=texto, usage_metadata=uso if con_uso else None)


class ClienteFalso:
    """Se comporta según el escenario. Lleva la cuenta de las llamadas que recibe."""

    def __init__(self, escenario):
        self.escenario = escenario
        self.recibidas = 0
        self.models = self

    def generate_content(self, model, contents, config):
        self.recibidas += 1
        e = self.escenario
        if config.http_options.timeout < 10000:   # igual que la API real
            raise ErrorApi(400)
        es_localizador = '"ranking"' in contents
        if e == "429_y_luego_ok" and self.recibidas <= 2:
            raise ErrorApi(429)
        if e == "localizador_saturado" and es_localizador:
            raise ErrorApi(503)
        if e == "503_siempre":
            raise ErrorApi(503)
        if e == "key_invalida":
            raise ErrorApi(400)
        if e == "sin_red":
            raise ConnectionError("sin red")
        if e == "se_cuelga" or (e == "cuelga_con_candidato" and self.recibidas > 2):
            time.sleep(600)
        if e == "basura":
            return respuesta("{{{ esto no sirve <<<<<<<")
        if e == "texto_none":
            return respuesta(None)
        if e == "sin_uso":
            return respuesta(ARREGLO, con_uso=False)
        if es_localizador:
            return respuesta('{"ranking": [{"id": 0, "razon": "x"}]}')
        return respuesta(INUTIL if e == "nunca_arregla" else ARREGLO)


def correr_agente(carpeta, escenario):
    """Punto de entrada del subproceso: main.py real con el cliente simulado."""
    import llm
    import localizador
    import main
    import tiempo
    llm._cliente = ClienteFalso(escenario)
    llm.ESPERA_BASE = float(os.environ.get("ESPERA_BASE", 0.01))
    localizador.PLAZO_LLM = float(os.environ.get("PLAZO_LLM", localizador.PLAZO_LLM))
    tiempo.LIMITE_DURO = float(os.environ.get("LIMITE_DURO", tiempo.LIMITE_DURO))
    sys.argv = ["main.py", carpeta]
    main.main()


@unittest.skipUnless(ENTORNO, "hace falta al menos un bug instalado con setup.sh")
class Robustez(unittest.TestCase):
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
        (self.carpeta / "bug.json").write_text(json.dumps({
            "id": "juguete-1", "proyecto": "juguete", "python": "3.8.3",
            "comandos_test": ["python -m pytest -q -p no:cacheprovider tests/test_mod.py::test_objetivo"],
            "archivos_test": ["tests/test_mod.py"], "modulos_test": ["tests/test_mod.py"]}))

    def lanzar(self, escenario, **env):
        return subprocess.Popen([sys.executable, __file__, "--agente", str(self.carpeta), escenario],
                                cwd=AGENTE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", **env))

    def correr(self, escenario, **env):
        p = self.lanzar(escenario, **env)
        self.salida = p.communicate(timeout=120)[0]
        self.assertEqual(p.returncode, 0, self.salida)
        self.assertNotIn("Traceback", self.salida)

    def comprobar_json(self):
        loc = json.loads((self.carpeta / "localizacion.json").read_text())
        self.assertTrue(loc["regiones"])
        for r in loc["regiones"]:
            self.assertFalse(codigo.es_test(r["archivo"]))
            self.assertTrue(1 <= r["inicio"] <= r["fin"])
        m = json.loads((self.carpeta / "metricas.json").read_text())
        self.assertEqual(set(m), {"bug", "modelo", "llamadas", "tokens_entrada", "tokens_salida", "segundos"})
        self.assertEqual(m["bug"], "juguete-1")
        self.assertEqual(m["modelo"], "gemini-3.5-flash-lite")
        return m

    def reparado(self):
        return "x * 2" in (self.repo / "pkg/mod.py").read_text()

    def repo_limpio(self):
        return subprocess.run(["git", "status", "--short"], cwd=self.repo,
                              capture_output=True, text=True).stdout == ""

    def test_todo_bien(self):
        self.correr("ok")
        m = self.comprobar_json()
        self.assertTrue(self.reparado())
        self.assertEqual(m["llamadas"], 2)
        self.assertEqual(m["tokens_entrada"], 2 * TOKENS_ENTRADA)
        self.assertEqual(m["tokens_salida"], 2 * TOKENS_SALIDA)

    def test_429_a_mitad_reintenta_y_no_cuenta_los_fallidos(self):
        self.correr("429_y_luego_ok")
        m = self.comprobar_json()
        self.assertTrue(self.reparado())
        self.assertIn("falló (429)", self.salida)
        self.assertEqual(m["llamadas"], 2)
        self.assertEqual(m["tokens_entrada"], 2 * TOKENS_ENTRADA)

    def test_503_siempre_termina_sin_caerse(self):
        self.correr("503_siempre")
        m = self.comprobar_json()
        self.assertEqual((m["llamadas"], m["tokens_entrada"]), (0, 0))
        self.assertTrue(self.repo_limpio())

    def test_localizador_saturado_no_le_quita_el_tiempo_al_reparador(self):
        # Sin plazo, los reintentos con espera creciente tomarían más de 60 s.
        self.correr("localizador_saturado", ESPERA_BASE="2", PLAZO_LLM="14")
        m = self.comprobar_json()
        self.assertTrue(self.reparado())
        self.assertLess(m["segundos"], 25)
        self.assertEqual(m["llamadas"], 1)

    def test_plazo_corto_no_llega_a_la_api(self):
        # Con menos de 11 s disponibles no se llama: la API respondería 400 y se perdería el intento.
        self.correr("ok", PLAZO_LLM="6")
        m = self.comprobar_json()
        self.assertEqual(m["llamadas"], 1)
        self.assertNotIn("no recuperable", self.salida)
        self.assertTrue(self.reparado())

    def test_key_invalida_no_reintenta_en_vano(self):
        self.correr("key_invalida")
        m = self.comprobar_json()
        self.assertEqual(m["llamadas"], 0)
        self.assertIn("no recuperable", self.salida)
        self.assertLess(m["segundos"], 30)
        self.assertTrue(self.repo_limpio())

    def test_sin_red(self):
        self.correr("sin_red")
        self.assertEqual(self.comprobar_json()["llamadas"], 0)
        self.assertTrue(self.repo_limpio())

    def test_respuesta_basura(self):
        self.correr("basura")
        m = self.comprobar_json()
        self.assertGreater(m["tokens_entrada"], 0)
        self.assertTrue(self.repo_limpio())

    def test_respuesta_sin_texto(self):
        self.correr("texto_none")
        self.comprobar_json()
        self.assertTrue(self.repo_limpio())

    def test_respuesta_sin_usage_metadata(self):
        self.correr("sin_uso")
        self.comprobar_json()
        self.assertTrue(self.reparado())

    def test_nunca_arregla_deja_el_original(self):
        self.correr("nunca_arregla")
        self.comprobar_json()
        self.assertTrue(self.repo_limpio())

    def test_sigterm_escribe_los_json(self):
        p = self.lanzar("se_cuelga")
        time.sleep(6)
        p.send_signal(signal.SIGTERM)
        salida = p.communicate(timeout=30)[0]
        self.assertEqual(p.returncode, 0, salida)
        self.assertIn("corte: SIGTERM", salida)
        self.comprobar_json()
        self.assertTrue(self.repo_limpio())

    def test_limite_duro_escribe_los_json(self):
        self.correr("se_cuelga", LIMITE_DURO="6")
        self.assertIn("corte: límite de tiempo", self.salida)
        self.comprobar_json()
        self.assertTrue(self.repo_limpio())

    def test_sin_key_real_no_se_cae(self):
        # Sin simular el cliente: google-genai real y sin variable de entorno.
        env = {k: v for k, v in os.environ.items() if k not in ("GEMINI_API_KEY", "GOOGLE_API_KEY")}
        p = subprocess.run([sys.executable, "main.py", str(self.carpeta)], cwd=AGENTE, env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("no se pudo crear el cliente", p.stdout)
        self.assertEqual(self.comprobar_json()["llamadas"], 0)
        self.assertTrue(self.repo_limpio())


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--agente":
        correr_agente(sys.argv[2], sys.argv[3])
    else:
        unittest.main()
