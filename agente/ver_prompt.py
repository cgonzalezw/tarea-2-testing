"""Arma el prompt real de un bug a partir de su localizacion.json guardado y lo imprime (sin LLM)."""
import json
import sys
from pathlib import Path

import contexto

carpeta = Path(sys.argv[1]).resolve()
info = json.loads((carpeta / "bug.json").read_text())
loc = json.loads((carpeta / "localizacion.json").read_text())
prompt = contexto.construir_prompt(carpeta / "repo", info, loc["falla"], loc["regiones"][:3])
print(f"tokens aproximados: {len(prompt) // 4}")
print(prompt)
