"""La herramienta de fabrica contra el servidor de verdad: registra la placa y genera la etiqueta con su QR.

Vive fuera del backend (fabrica/ en la raiz del repositorio) y no importa nada de el:
se carga desde su archivo, como lo correria cualquiera.
"""
import importlib.util
from pathlib import Path

import pytest

from crear_admin import main as crear_admin
from tests.integracion.conftest import cuenta_http, pedir_http

RAIZ = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("fabricar_dispositivo", RAIZ / "fabrica" / "fabricar_dispositivo.py")
fabrica = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fabrica)


@pytest.fixture
def admin(base_url, config, monkeypatch):
    cuenta_http(base_url, "Ada")
    assert crear_admin(["ada@correo.co"], config) == 0
    monkeypatch.setenv("MATERA_CONTRASENA_ADMIN", "clave-segura")
    return "ada@correo.co"


def test_registra_la_placa_y_genera_la_etiqueta_con_el_enlace_de_reclamo(base_url, admin, tmp_path, capsys):
    argumentos = ["a4:cf:12:00:00:01", "--correo", admin, "--api", base_url, "--front", "http://192.168.1.10:5500/"]

    assert fabrica.main([*argumentos, "--etiquetas", str(tmp_path)]) == 0

    enlace = next(linea for linea in capsys.readouterr().out.splitlines() if linea.startswith("http://192.168.1.10"))
    assert enlace.startswith("http://192.168.1.10:5500/#vincular?d=A4CF12000001&c=")
    assert (tmp_path / "A4CF12000001.svg").read_text().startswith("<?xml")
    # El codigo del QR sirve para reclamar la placa.
    token = cuenta_http(base_url, "Ana")
    _, planta = pedir_http(base_url, "POST", "/plantas", {"apodo": "Sala", "especie": "potos"}, token)
    cuerpo = {"codigoReclamo": enlace.split("&c=")[1], "plantaId": planta["id"]}
    assert pedir_http(base_url, "POST", "/dispositivos/A4CF12000001/reclamo", cuerpo, token)[0] == 201


def test_la_misma_placa_dos_veces_muestra_el_error_de_la_api(base_url, admin, tmp_path, capsys):
    argumentos = ["A4CF12000001", "--correo", admin, "--api", base_url, "--etiquetas", str(tmp_path)]
    assert fabrica.main(argumentos) == 0

    assert fabrica.main(argumentos) == 1

    assert "409 DISPOSITIVO_YA_REGISTRADO" in capsys.readouterr().err


def test_una_cuenta_que_no_es_admin_no_registra_nada(base_url, monkeypatch, tmp_path, capsys):
    cuenta_http(base_url, "Ana")
    monkeypatch.setenv("MATERA_CONTRASENA_ADMIN", "clave-segura")

    estado = fabrica.main(["A4CF12000001", "--correo", "ana@correo.co", "--api", base_url, "--etiquetas", str(tmp_path)])

    assert estado == 1
    assert "403 PERMISO_DENEGADO" in capsys.readouterr().err


def test_sin_api_dice_que_no_la_encontro(monkeypatch, tmp_path):
    monkeypatch.setenv("MATERA_CONTRASENA_ADMIN", "x")

    assert fabrica.main(["A4CF12000001", "--correo", "a@b.co", "--api", "http://127.0.0.1:9/api/v1"]) == 2
