"""L'import du serveur ne doit jamais toucher aux données : seule la mise en route les initialise."""

import os
import subprocess
import sys
from pathlib import Path


def test_import_du_serveur_ne_cree_ni_ne_migre_de_base(tmp_path: Path) -> None:
    env = {**os.environ, "JOBRADAR_DATA_DIR": str(tmp_path)}
    subprocess.run([sys.executable, "-c", "import jobradar.server"], check=True, env=env,
                   cwd=Path(__file__).resolve().parent.parent)

    assert not (tmp_path / "jobradar.db").exists()
