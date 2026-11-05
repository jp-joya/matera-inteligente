"""El canal en vivo de la placa, /api/v1/dispositivos/canal, con el servidor real y un cliente WebSocket de verdad.

La placa solo dice su MAC. Por este canal recibe su clave cuando el dueno la
reclama con el QR, y se entera si la quitan de su matera. Confirma la clave al
canjearla por su token.
"""
import pytest
from simple_websocket import ConnectionClosed

from tests.integracion.conftest import Placa, cuenta_http, es_error, fabricar_http, medir_http, pedir_http

LECTURAS = {"humedad": 50, "luz": 800, "temperatura": 20}
PLACA = "A4CF12000001"
OTRA_PLACA = "A4CF12000002"


@pytest.fixture
def conectar(base_url):
    """conectar(codigo) abre el canal como lo haria la placa; todos se cierran al terminar la prueba."""
    abiertas = []

    def abrir(codigo=PLACA, cabeceras=None):
        placa = Placa(base_url, codigo, cabeceras)
        abiertas.append(placa)
        return placa

    yield abrir
    for placa in abiertas:
        placa.cerrar()


@pytest.fixture
def ana(base_url):
    """(token, planta) de Ana, con una matera sin sensor."""
    token = cuenta_http(base_url)
    _, planta = pedir_http(base_url, "POST", "/plantas", {"apodo": "Sala", "especie": "potos"}, token)
    return token, planta


def _reclamar(base_url, token, planta_id, codigo_reclamo, codigo=PLACA):
    cuerpo = {"codigoReclamo": codigo_reclamo, "plantaId": planta_id}
    return pedir_http(base_url, "POST", f"/dispositivos/{codigo}/reclamo", cuerpo, token)


def _canjear(base_url, clave, codigo=PLACA):
    return pedir_http(base_url, "POST", f"/dispositivos/{codigo}/token", {"clave": clave})


def _tarjeta(base_url, token):
    return pedir_http(base_url, "GET", "/plantas", token=token)[1][0]


def test_una_placa_conectada_recibe_su_clave_al_reclamarla_y_con_ella_mide(base_url, config, ana, conectar):
    token, planta = ana
    codigo_reclamo = fabricar_http(base_url, config)
    placa = conectar()
    assert placa.mensaje() == {"tipo": "estado", "vinculado": False}

    estado, reclamo = _reclamar(base_url, token, planta["id"], codigo_reclamo)

    assert (estado, reclamo["claveEntregada"]) == (201, True)
    mensaje = placa.mensaje()
    assert (mensaje["tipo"], mensaje["planta"]) == ("vinculado", {"id": planta["id"], "apodo": "Sala"})
    assert mensaje["clave"] not in str(reclamo)  # el dueno nunca ve la clave
    assert _tarjeta(base_url, token)["sensor"] == {"codigo": PLACA, "listo": False}
    estado, medicion = medir_http(base_url, PLACA, mensaje["clave"], LECTURAS)
    assert (estado, medicion["planta"]["id"]) == (201, planta["id"])
    assert _tarjeta(base_url, token)["sensor"] == {"codigo": PLACA, "listo": True}  # el canje la confirmo


def test_una_placa_apagada_al_reclamarla_recibe_su_clave_al_conectarse(base_url, config, ana, conectar):
    token, planta = ana
    estado, reclamo = _reclamar(base_url, token, planta["id"], fabricar_http(base_url, config))
    assert (estado, reclamo["claveEntregada"]) == (201, False)

    mensaje = conectar().mensaje()

    assert mensaje["tipo"] == "vinculado"
    assert medir_http(base_url, PLACA, mensaje["clave"], LECTURAS)[0] == 201


def test_una_placa_que_ya_canjeo_su_clave_no_recibe_otra_al_reconectarse(base_url, config, ana, conectar):
    token, planta = ana
    _reclamar(base_url, token, planta["id"], fabricar_http(base_url, config))
    primera = conectar()
    clave = primera.mensaje()["clave"]
    assert _canjear(base_url, clave)[0] == 200
    primera.cerrar()

    mensaje = conectar().mensaje()

    assert mensaje == {"tipo": "estado", "vinculado": True, "planta": {"id": planta["id"], "apodo": "Sala"}}
    assert _canjear(base_url, clave)[0] == 200


def test_si_la_placa_no_canjea_al_reconectarse_recibe_otra_clave_y_la_anterior_deja_de_servir(
    base_url, config, ana, conectar
):
    token, planta = ana
    _reclamar(base_url, token, planta["id"], fabricar_http(base_url, config))
    primera = conectar()
    vieja = primera.mensaje()["clave"]
    primera.cerrar()

    nueva = conectar().mensaje()["clave"]

    assert nueva != vieja
    assert _canjear(base_url, vieja)[1]["error"] == "CLAVE_SENSOR_INVALIDA"
    assert _canjear(base_url, nueva)[0] == 200


@pytest.mark.parametrize("accion", ["quitar sensor", "borrar matera"])
def test_la_placa_se_entera_en_vivo_si_la_quitan_y_su_clave_deja_de_servir(base_url, config, ana, conectar, accion):
    token, planta = ana
    _reclamar(base_url, token, planta["id"], fabricar_http(base_url, config))
    placa = conectar()
    clave = placa.mensaje()["clave"]

    ruta = f"/plantas/{planta['id']}/sensor" if accion == "quitar sensor" else f"/plantas/{planta['id']}"
    assert pedir_http(base_url, "DELETE", ruta, token=token)[0] == 200

    assert placa.mensaje() == {"tipo": "desvinculado"}
    assert _canjear(base_url, clave)[1]["error"] == "CLAVE_SENSOR_INVALIDA"


def test_reclamar_otra_placa_para_la_misma_matera_le_avisa_a_la_anterior(base_url, config, ana, conectar):
    token, planta = ana
    _reclamar(base_url, token, planta["id"], fabricar_http(base_url, config))
    vieja = conectar()
    assert vieja.mensaje()["tipo"] == "vinculado"
    codigo_reclamo = fabricar_http(base_url, config, OTRA_PLACA)
    nueva = conectar(OTRA_PLACA)
    nueva.mensaje()

    _reclamar(base_url, token, planta["id"], codigo_reclamo, OTRA_PLACA)

    assert vieja.mensaje() == {"tipo": "desvinculado"}
    assert nueva.mensaje()["tipo"] == "vinculado"


def test_una_conexion_nueva_de_la_misma_placa_reemplaza_a_la_anterior(base_url, config, ana, conectar):
    token, planta = ana
    codigo_reclamo = fabricar_http(base_url, config)
    vieja = conectar()
    vieja.mensaje()
    nueva = conectar()
    nueva.mensaje()

    with pytest.raises(ConnectionClosed):
        vieja.ws.receive(timeout=5)
    _reclamar(base_url, token, planta["id"], codigo_reclamo)
    assert nueva.mensaje()["tipo"] == "vinculado"


@pytest.mark.parametrize(
    ("codigo", "error"),
    [("FFFFFFFFFFFF", "DISPOSITIVO_NO_REGISTRADO"), ("MT-0001-AA", "CODIGO_SENSOR_INVALIDO")],
    ids=["sin pasar por fabrica", "codigo que no es mac"],
)
def test_una_placa_desconocida_recibe_un_error_en_json_y_se_cierra(conectar, codigo, error):
    placa = conectar(codigo)

    mensaje = placa.mensaje()

    assert (mensaje["tipo"], mensaje["error"]) == ("error", error)
    with pytest.raises(ConnectionClosed) as cierre:
        placa.ws.receive(timeout=5)
    assert cierre.value.reason == 1008


def test_el_canal_acepta_el_origin_que_manda_la_libreria_del_esp32(base_url, config, conectar):
    fabricar_http(base_url, config)

    placa = conectar(cabeceras={"Origin": "file://"})

    assert placa.mensaje() == {"tipo": "estado", "vinculado": False}


def test_lo_que_mande_la_placa_por_el_canal_se_ignora(base_url, config, ana, conectar):
    token, planta = ana
    _reclamar(base_url, token, planta["id"], fabricar_http(base_url, config))
    placa = conectar()
    clave = placa.mensaje()["clave"]

    for texto in ["hola", "[1]", '{"tipo": "vinculado"}']:
        placa.ws.send(texto)

    assert placa.mensaje(0.5) is None
    assert _canjear(base_url, clave)[0] == 200


def test_un_get_normal_al_canal_responde_400_en_json(api):
    respuesta = api.pedir("GET", f"/dispositivos/canal?codigo={PLACA}")

    assert es_error(respuesta, 400, "SOLICITUD_INVALIDA")
    assert "es un WebSocket" in respuesta.get_json()["mensaje"]
