"""El simulador del ESP32 (R3) contra el servidor de verdad: lecturas normales, fuera de rango e invalidas.

El simulador vive fuera del backend (simulador/ en la raiz del repositorio) y no importa
nada de el: se carga desde su archivo, como lo correria cualquiera.
"""
import importlib.util
import random
import threading
import time
from pathlib import Path

import pytest

from tests.integracion.conftest import cuenta_http, fabricar_http, matera_http, pedir_http

RAIZ = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("simulador_esp32", RAIZ / "simulador" / "simulador_esp32.py")
simulador = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(simulador)


@pytest.fixture
def matera(base_url, config):
    """La cuenta de Ana con la matera del simulador ya reclamada: (token de Ana, {planta, codigo, clave}).

    La clave es la que recibio la placa al reclamarla. El simulador, que todavia no la tiene en su
    memoria, recibe otra al abrir su canal: mientras no la canjee, cada conexion trae una nueva.
    """
    token = cuenta_http(base_url)
    return token, matera_http(base_url, config, token, apodo="Simulada", codigo="A4CF12000012")


@pytest.fixture
def memoria(tmp_path):
    return tmp_path / "memoria"


@pytest.fixture
def correr(base_url, memoria):
    """Corre el simulador como desde la consola, con su memoria en una carpeta temporal."""

    def correr_simulador(datos, *opciones):
        lineas = []
        argumentos = [datos["codigo"], f"--api={base_url}", f"--memoria={memoria}", "--intervalo", "0", "--semilla", "7"]
        codigo = simulador.main([*argumentos, "--espera", "10", *opciones], salida=lineas.append)
        return codigo, lineas

    return correr_simulador


def _historial(base_url, token, matera):
    return pedir_http(base_url, "GET", f"/plantas/{matera['planta']['id']}/historial?limite=50", token=token)[1]["registros"]


def test_el_modo_continuo_manda_lecturas_que_quedan_en_el_historial_con_el_codigo_del_sensor(base_url, matera, correr):
    token, datos = matera

    codigo, lineas = correr(datos, "--veces", "3")

    assert codigo == 0
    assert lineas[-1] == "Se mandaron 3 lecturas: 3 normales, 0 fuera de rango y 0 invalidas."
    registros = _historial(base_url, token, datos)
    assert [r["origen"] for r in registros] == ["A4CF12000012"] * 3


def test_las_lecturas_fuera_de_rango_se_guardan_con_su_diagnostico(base_url, matera, correr):
    token, datos = matera

    codigo, lineas = correr(datos, "--veces", "3", "--fuera-de-rango", "1")

    assert codigo == 0
    assert lineas[-1] == "Se mandaron 3 lecturas: 0 normales, 3 fuera de rango y 0 invalidas."
    assert all(r["estado"] != "SALUDABLE" for r in _historial(base_url, token, datos))


def test_los_datos_invalidos_se_rechazan_como_se_esperaba_y_no_se_guardan(base_url, matera, correr):
    token, datos = matera

    codigo, lineas = correr(datos, "--veces", "6", "--invalidas", "1")

    assert codigo == 0
    # Las dos primeras dicen que llego la clave y que el token esta listo; la ultima es el resumen.
    assert all("como se esperaba" in linea for linea in lineas[2:-1])
    assert _historial(base_url, token, datos) == []


def test_el_modo_casos_recorre_cada_error_y_la_api_responde_lo_esperado(base_url, matera, correr):
    token, datos = matera

    codigo, lineas = correr(datos, "--casos")

    assert codigo == 0
    assert lineas[-1] == f"{len(simulador.CASOS) + 1} de {len(simulador.CASOS) + 1} como se esperaba."
    assert _historial(base_url, token, datos) == []


def test_el_simulador_recibe_su_clave_por_el_canal_la_guarda_y_la_vuelve_a_usar(base_url, matera, memoria, correr):
    token, datos = matera

    assert correr(datos, "--veces", "1")[0] == 0
    guardada = simulador.Memoria(memoria, "A4CF12000012").leer()
    assert guardada and guardada != datos["clave"]  # la que trajo el canal al conectarse

    codigo, lineas = correr(datos, "--veces", "1")

    assert codigo == 0
    assert "con su clave guardada" in lineas[0]
    assert len(_historial(base_url, token, datos)) == 2


def test_sin_dueno_el_simulador_espera_y_mide_apenas_lo_reclaman(base_url, config, correr):
    token = cuenta_http(base_url)
    _, planta = pedir_http(base_url, "POST", "/plantas", {"apodo": "Simulada", "especie": "potos"}, token)
    codigo_reclamo = fabricar_http(base_url, config, "A4CF12000012")
    resultado = {}
    hilo = threading.Thread(target=lambda: resultado.update(salida=correr({"codigo": "A4CF12000012"}, "--veces", "1")))
    hilo.start()
    time.sleep(0.5)

    cuerpo = {"codigoReclamo": codigo_reclamo, "plantaId": planta["id"]}
    assert pedir_http(base_url, "POST", "/dispositivos/A4CF12000012/reclamo", cuerpo, token)[1]["claveEntregada"]
    hilo.join(timeout=10)

    codigo, lineas = resultado["salida"]
    assert codigo == 0
    assert "sin dueno" in lineas[0]
    assert len(_historial(base_url, token, {"planta": planta})) == 1


def test_si_nadie_lo_reclama_a_tiempo_el_simulador_termina(base_url, config, correr):
    fabricar_http(base_url, config, "A4CF12000012")

    codigo, lineas = correr({"codigo": "A4CF12000012"}, "--espera", "0.5")

    assert codigo == 3
    assert lineas[-1] == "Nadie reclamo la placa a tiempo."


def test_una_placa_que_no_paso_por_fabrica_recibe_el_error_y_termina(base_url, correr):
    codigo, lineas = correr({"codigo": "A4CF12000012"})

    assert codigo == 1
    assert "DISPOSITIVO_NO_REGISTRADO" in lineas[-1]


def test_si_la_quitaron_de_su_matera_borra_la_clave_guardada_y_espera(base_url, matera, memoria, correr):
    token, datos = matera
    assert correr(datos, "--veces", "1")[0] == 0
    pedir_http(base_url, "DELETE", f"/plantas/{datos['planta']['id']}/sensor", token=token)

    codigo, lineas = correr(datos, "--espera", "0.5")

    assert codigo == 3
    assert "sin dueno" in lineas[0]
    assert simulador.Memoria(memoria, "A4CF12000012").leer() is None


def test_si_la_api_deja_de_responder_el_modo_continuo_lo_dice_y_sigue(base_url, matera, monkeypatch, correr):
    token, datos = matera
    pedir = simulador._pedir
    lecturas_pedidas = []

    def con_un_corte(url, cuerpo, token=None):
        if url.endswith("/lecturas"):
            lecturas_pedidas.append(url)
            if len(lecturas_pedidas) == 2:
                raise simulador.SinConexion("el backend se esta reiniciando")
        return pedir(url, cuerpo, token)

    monkeypatch.setattr(simulador, "_pedir", con_un_corte)

    codigo, lineas = correr(datos, "--veces", "3")

    assert codigo == 0
    assert any("sin conexion con la API (el backend se esta reiniciando)" in linea for linea in lineas)
    assert lineas[-2:] == [
        "Se mandaron 2 lecturas: 2 normales, 0 fuera de rango y 0 invalidas.",
        "1 no se pudieron mandar porque la API no respondio.",
    ]
    assert len(_historial(base_url, token, datos)) == 2


def test_un_caso_de_error_con_el_token_vencido_canjea_y_prueba_el_dato(base_url, matera):
    _, datos = matera
    sim = simulador.Simulador(base_url, datos["codigo"], datos["clave"], salida=lambda _: None)
    assert sim.canjear()[0] == 200
    # Otro canje del mismo sensor deja sin efecto el token de sim, como si hubiera vencido.
    assert simulador.Simulador(base_url, datos["codigo"], datos["clave"]).canjear()[0] == 200
    [humedad_imposible] = [caso for caso in simulador.CASOS if caso.nombre.startswith("humedad imposible")]

    estado, respuesta = sim.probar(humedad_imposible, {"humedad": 50, "luz": 800, "temperatura": 22})

    assert (estado, respuesta["error"]) == (400, "PARAMETRO_INVALIDO")


def test_si_la_api_no_responde_el_simulador_lo_dice():
    lineas = []

    codigo = simulador.main(["A4CF12000012", "--api=http://127.0.0.1:9/api/v1"], salida=lineas.append)

    assert codigo == 2
    assert "No se pudo contactar la API" in lineas[-1]


def test_los_valores_de_la_matera_simulada_cambian_poco_a_poco_y_no_pasan_los_limites_fisicos():
    matera = simulador.MateraSimulada(random.Random(1))
    lecturas = [matera.siguiente() for _ in range(300)]

    for anterior, actual in zip(lecturas, lecturas[1:]):
        regada = actual["humedad"] > anterior["humedad"]
        assert regada or anterior["humedad"] - actual["humedad"] <= 0.8 + 0.05
        assert abs(actual["temperatura"] - anterior["temperatura"]) < 1.5
    assert all(0 <= l["humedad"] <= 100 and 0 <= l["luz"] <= 150000 and -50 <= l["temperatura"] <= 60 for l in lecturas)
    assert any(actual["humedad"] > anterior["humedad"] for anterior, actual in zip(lecturas, lecturas[1:]))  # hubo riego
