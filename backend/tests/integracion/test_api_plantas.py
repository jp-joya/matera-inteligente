"""Materas, sensores e historial de punta a punta: HTTP, casos de uso y SQLite.

La matera se crea sin sensor y la placa se reclama con el QR de su etiqueta.
El canal en vivo de la placa (por donde recibe su clave) no se puede abrir con
el test_client: aqui la clave se deja en la base con api.dar_clave(), y el canal
de verdad se prueba en test_canal_dispositivos.py.
"""
import pytest

from tests.integracion.conftest import ORIGEN_FRONT, es_error

MEDICION = {"humedad": 18, "luz": 800, "temperatura": 25}


@pytest.fixture
def ana(api):
    return api.cuenta()


def _crear(api, token, apodo="Sala", especie="Sansevieria"):
    return api.pedir("POST", "/plantas", token, json={"apodo": apodo, "especie": especie})


@pytest.fixture
def matera(api, ana):
    """La Sala de Ana con la placa A4CF12000001 reclamada y con su clave: {planta, codigo, clave}."""
    return api.matera(ana, "Sala", "Sansevieria")


@pytest.fixture
def planta(matera):
    return matera["planta"]


def _desde_sensor(api, codigo, clave, lecturas=MEDICION):
    """Como la placa: canjea la clave y reporta la lectura. La ruta de dispositivos la prueba test_api_dispositivos.py."""
    return api.medir(codigo, clave, lecturas)


def _historial(api, token, planta_id):
    return api.pedir("GET", f"/plantas/{planta_id}/historial", token).get_json()["registros"]


def _tarjetas(api, token):
    return api.pedir("GET", "/plantas", token).get_json()


# --- Materas ---


def test_crear_una_matera_devuelve_la_planta_sin_sensor(api, ana):
    respuesta = _crear(api, ana, especie="  SANSEVIERIA ")

    assert respuesta.status_code == 201
    assert respuesta.get_json() == {"id": 1, "apodo": "Sala", "especie": "sansevieria"}
    assert _tarjetas(api, ana) == [
        {"id": 1, "apodo": "Sala", "especie": "sansevieria", "sensor": None, "ultimaMedicion": None}
    ]


@pytest.mark.parametrize(
    ("cuerpo", "estado", "codigo_error"),
    [
        ({"apodo": "Patio", "especie": "cactus"}, 404, "ESPECIE_NO_SOPORTADA"),
        ({"apodo": "sala", "especie": "potos"}, 409, "APODO_REPETIDO"),
        ({"apodo": "  ", "especie": "potos"}, 400, "PARAMETRO_INVALIDO"),
        ({"apodo": "x" * 41, "especie": "potos"}, 400, "PARAMETRO_INVALIDO"),
        ({"apodo": "Patio"}, 400, "PARAMETRO_INVALIDO"),
    ],
    ids=["especie", "apodo repetido", "apodo vacio", "apodo largo", "sin especie"],
)
def test_una_matera_invalida_responde_con_su_error_y_no_se_crea(api, ana, planta, cuerpo, estado, codigo_error):
    assert es_error(api.pedir("POST", "/plantas", ana, json=cuerpo), estado, codigo_error)
    assert [t["id"] for t in _tarjetas(api, ana)] == [planta["id"]]


def test_las_rutas_de_plantas_y_el_reclamo_exigen_sesion(api):
    assert es_error(api.pedir("GET", "/plantas"), 401, "SESION_INVALIDA")
    assert es_error(_crear(api, None), 401, "SESION_INVALIDA")
    assert es_error(api.reclamar(None, 1, codigo_reclamo="x"), 401, "SESION_INVALIDA")


def test_mis_plantas_muestra_la_placa_reclamada_sin_confirmar_y_sin_mediciones(api, ana, planta):
    assert _tarjetas(api, ana) == [{**planta, "sensor": {"codigo": "A4CF12000001", "listo": False}, "ultimaMedicion": None}]


@pytest.mark.parametrize(
    ("metodo", "ruta"),
    [("POST", "/plantas/{id}/mediciones"), ("PUT", "/plantas/{id}/sensor")],
    ids=["medicion a mano", "cambiar el sensor con un codigo"],
)
def test_las_rutas_que_ya_no_existen_responden_en_json_y_no_cambian_nada(api, ana, matera, metodo, ruta):
    respuesta = api.pedir(metodo, ruta.format(id=matera["planta"]["id"]), ana, json={"codigo": "A4CF12000002"})

    assert respuesta.status_code in (404, 405) and respuesta.mimetype == "application/json"
    assert _tarjetas(api, ana)[0]["sensor"]["codigo"] == "A4CF12000001"


@pytest.mark.parametrize(
    ("metodo", "ruta"),
    [("DELETE", "/plantas/{id}"), ("DELETE", "/plantas/{id}/sensor"), ("GET", "/plantas/{id}/historial")],
)
def test_beto_recibe_404_con_la_planta_de_ana_igual_que_con_una_que_no_existe(api, planta, metodo, ruta):
    beto = api.cuenta("Beto", "beto@correo.co")

    ajena = api.pedir(metodo, ruta.format(id=planta["id"]), beto)
    inexistente = api.pedir(metodo, ruta.format(id=999), beto)

    assert es_error(ajena, 404, "PLANTA_NO_ENCONTRADA")
    assert ajena.get_json()["mensaje"] == inexistente.get_json()["mensaje"].replace("999", str(planta["id"]))
    assert _tarjetas(api, beto) == []


# --- Fabrica y reclamo ---


def test_el_admin_registra_la_placa_en_fabrica_y_recibe_el_codigo_del_qr(api):
    admin = api.admin()

    respuesta = api.pedir("POST", "/admin/dispositivos", admin, json={"codigo": "a4:cf:12:00:00:01"})

    assert respuesta.status_code == 201
    cuerpo = respuesta.get_json()
    assert cuerpo["codigo"] == "A4CF12000001" and len(cuerpo["codigoReclamo"]) >= 43
    repetida = api.pedir("POST", "/admin/dispositivos", admin, json={"codigo": "A4CF12000001"})
    assert es_error(repetida, 409, "DISPOSITIVO_YA_REGISTRADO")


@pytest.mark.parametrize(
    ("cuerpo", "codigo_error"),
    [({"codigo": "MT-0001-AA"}, "CODIGO_SENSOR_INVALIDO"), ({}, "PARAMETRO_INVALIDO")],
    ids=["no es mac", "sin codigo"],
)
def test_un_codigo_que_no_es_una_mac_no_se_registra(api, cuerpo, codigo_error):
    assert es_error(api.pedir("POST", "/admin/dispositivos", api.admin(), json=cuerpo), 400, codigo_error)


def test_un_usuario_comun_no_registra_placas(api, ana):
    respuesta = api.pedir("POST", "/admin/dispositivos", ana, json={"codigo": "A4CF12000001"})

    assert es_error(respuesta, 403, "PERMISO_DENEGADO")


def test_reclamar_con_la_placa_apagada_la_vincula_y_la_clave_espera_a_que_se_conecte(api, ana):
    planta = _crear(api, ana).get_json()

    respuesta = api.reclamar(ana, planta["id"])

    assert respuesta.status_code == 201
    assert respuesta.get_json() == {"planta": planta, "sensor": {"codigo": "A4CF12000001"}, "claveEntregada": False}
    assert _tarjetas(api, ana)[0]["sensor"] == {"codigo": "A4CF12000001", "listo": False}


@pytest.mark.parametrize(
    ("codigo", "codigo_reclamo"),
    [("A4CF12000001", "codigo-inventado"), ("A4CF12009999", None), ("no-es-mac", None)],
    ids=["codigo de reclamo malo", "placa sin fabrica", "codigo mal escrito"],
)
def test_el_reclamo_da_el_mismo_403_sin_importar_que_fallo(api, ana, codigo, codigo_reclamo):
    planta = _crear(api, ana).get_json()
    real = api.fabricar("A4CF12000001")

    respuesta = api.reclamar(ana, planta["id"], codigo, codigo_reclamo or real)

    assert es_error(respuesta, 403, "CODIGO_RECLAMO_INVALIDO")
    assert _tarjetas(api, ana)[0]["sensor"] is None


def test_una_placa_con_dueno_no_la_reclama_otro_aunque_tenga_el_qr(api, ana):
    sala = _crear(api, ana).get_json()
    codigo_reclamo = api.fabricar()
    api.reclamar(ana, sala["id"], codigo_reclamo=codigo_reclamo)
    beto = api.cuenta("Beto", "beto@correo.co")
    patio = _crear(api, beto, "Patio", "potos").get_json()

    respuesta = api.reclamar(beto, patio["id"], codigo_reclamo=codigo_reclamo)

    assert es_error(respuesta, 409, "SENSOR_YA_VINCULADO")


@pytest.mark.parametrize(
    "cuerpo",
    [{"plantaId": 1}, {"codigoReclamo": "x"}, {"codigoReclamo": "x", "plantaId": "1"}, {"codigoReclamo": "x", "plantaId": True}],
    ids=["sin codigo", "sin planta", "planta en texto", "planta booleana"],
)
def test_un_reclamo_incompleto_responde_400(api, ana, cuerpo):
    respuesta = api.pedir("POST", "/dispositivos/A4CF12000001/reclamo", ana, json=cuerpo)

    assert es_error(respuesta, 400, "PARAMETRO_INVALIDO")


def test_la_respuesta_del_reclamo_nunca_trae_la_clave(api, ana):
    planta = _crear(api, ana).get_json()

    cuerpo = api.reclamar(ana, planta["id"]).get_json()

    assert "clave" not in cuerpo and "clave" not in cuerpo["sensor"]


# --- Lecturas ---


def test_el_sensor_con_su_clave_guarda_la_medicion_con_su_codigo(api, ana, matera):
    respuesta = _desde_sensor(api, "A4CF12000001", matera["clave"])

    assert respuesta.status_code == 201
    cuerpo = respuesta.get_json()
    assert cuerpo["planta"] == matera["planta"]
    assert cuerpo["diagnostico"]["estado"] == "EN_RIESGO"
    assert len(cuerpo["diagnostico"]["recomendaciones"]) == 1
    [tarjeta] = _tarjetas(api, ana)
    assert tarjeta["sensor"] == {"codigo": "A4CF12000001", "listo": True}  # el canje confirmo la clave
    assert tarjeta["ultimaMedicion"]["origen"] == "A4CF12000001"
    assert tarjeta["ultimaMedicion"]["lecturas"][0] == {"nombre": "humedad", "valor": 18.0, "unidad": "%", "estado": "BAJO"}
    assert tarjeta["ultimaMedicion"]["momento"] == cuerpo["momento"]


def test_una_medicion_imposible_del_sensor_responde_400_y_no_se_guarda(api, ana, matera):
    respuesta = _desde_sensor(api, "A4CF12000001", matera["clave"], {**MEDICION, "humedad": 150})

    assert es_error(respuesta, 400, "PARAMETRO_INVALIDO")
    assert respuesta.get_json()["detalle"]["motivo"] == "fuera_de_rango_fisico"
    assert _historial(api, ana, matera["planta"]["id"]) == []


@pytest.mark.parametrize(
    ("codigo", "clave"),
    [("A4CF12000001", "clave-inventada"), ("A4CF12009999", "clave-inventada"), ("cualquiera", "x")],
)
def test_el_sensor_recibe_el_mismo_401_sin_importar_que_fallo_y_no_se_guarda_nada(api, ana, planta, codigo, clave):
    assert es_error(_desde_sensor(api, codigo, clave), 401, "CLAVE_SENSOR_INVALIDA")
    assert _historial(api, ana, planta["id"]) == []


# --- Cambiar o quitar el sensor ---


def test_reclamar_otra_placa_para_la_matera_conserva_el_historial_y_la_clave_vieja_deja_de_servir(api, ana, matera):
    planta = matera["planta"]
    assert _desde_sensor(api, "A4CF12000001", matera["clave"]).status_code == 201

    respuesta = api.reclamar(ana, planta["id"], "a4cf12000002")

    assert respuesta.status_code == 201
    assert respuesta.get_json()["sensor"] == {"codigo": "A4CF12000002"}
    assert es_error(_desde_sensor(api, "A4CF12000001", matera["clave"]), 401, "CLAVE_SENSOR_INVALIDA")
    assert _desde_sensor(api, "A4CF12000002", api.dar_clave("A4CF12000002", "otra-clave")).status_code == 201
    assert [r["origen"] for r in _historial(api, ana, planta["id"])] == ["A4CF12000002", "A4CF12000001"]
    assert _tarjetas(api, ana)[0]["sensor"]["codigo"] == "A4CF12000002"


def test_reclamar_otra_vez_la_misma_placa_reemplaza_una_clave_perdida(api, ana):
    sala = _crear(api, ana).get_json()
    codigo_reclamo = api.fabricar()
    api.reclamar(ana, sala["id"], codigo_reclamo=codigo_reclamo)
    vieja = api.dar_clave(clave="clave-vieja")

    assert api.reclamar(ana, sala["id"], codigo_reclamo=codigo_reclamo).status_code == 201

    assert es_error(_desde_sensor(api, "A4CF12000001", vieja), 401, "CLAVE_SENSOR_INVALIDA")
    assert _desde_sensor(api, "A4CF12000001", api.dar_clave(clave="clave-nueva")).status_code == 201


def test_quitar_el_sensor_revoca_su_clave_y_conserva_el_historial(api, ana, matera):
    planta_id = matera["planta"]["id"]
    assert _desde_sensor(api, "A4CF12000001", matera["clave"]).status_code == 201

    respuesta = api.pedir("DELETE", f"/plantas/{planta_id}/sensor", ana)

    assert respuesta.status_code == 200
    assert es_error(_desde_sensor(api, "A4CF12000001", matera["clave"]), 401, "CLAVE_SENSOR_INVALIDA")
    assert len(_historial(api, ana, planta_id)) == 1
    assert _tarjetas(api, ana)[0]["sensor"] is None
    otra_vez = api.pedir("DELETE", f"/plantas/{planta_id}/sensor", ana)
    assert es_error(otra_vez, 404, "PLANTA_SIN_SENSOR")


def test_el_historial_trae_lo_mas_reciente_primero_y_respeta_el_limite(api, ana, matera):
    for humedad in (30, 31, 32):
        _desde_sensor(api, "A4CF12000001", matera["clave"], {**MEDICION, "humedad": humedad})

    historial = api.pedir("GET", f"/plantas/{matera['planta']['id']}/historial?limite=2", ana).get_json()

    assert historial["planta"] == matera["planta"]
    assert [r["lecturas"][0]["valor"] for r in historial["registros"]] == [32.0, 31.0]


@pytest.mark.parametrize("limite", ["0", "201", "muchos"])
def test_un_limite_invalido_del_historial_responde_400(api, ana, planta, limite):
    respuesta = api.pedir("GET", f"/plantas/{planta['id']}/historial?limite={limite}", ana)

    assert es_error(respuesta, 400, "PARAMETRO_INVALIDO")
    assert respuesta.get_json()["detalle"]["campo"] == "limite"


def test_eliminar_una_matera_la_quita_de_mis_plantas_y_suelta_su_placa(api, ana, matera):
    assert api.pedir("DELETE", f"/plantas/{matera['planta']['id']}", ana).status_code == 200

    assert _tarjetas(api, ana) == []
    assert api.pedir("GET", "/usuarios/yo", ana).get_json()["plantas"] == 0
    assert es_error(_desde_sensor(api, "A4CF12000001", matera["clave"]), 401, "CLAVE_SENSOR_INVALIDA")


def test_eliminar_una_cuenta_suelta_su_placa_para_que_otro_la_reclame(api, ana):
    sala = _crear(api, ana).get_json()
    codigo_reclamo = api.fabricar()
    api.reclamar(ana, sala["id"], codigo_reclamo=codigo_reclamo)
    admin = api.admin()

    api.pedir("DELETE", "/admin/usuarios/1", admin)

    nueva = _crear(api, admin, "Sala", "potos").get_json()
    assert api.reclamar(admin, nueva["id"], codigo_reclamo=codigo_reclamo).status_code == 201


def test_una_especie_que_agrega_el_admin_sirve_para_las_materas_de_todos(api, ana):
    admin = api.admin()
    rangos = {"humedad": {"min": 30, "max": 60}, "luz": {"min": 1080, "max": 5380}, "temperatura": {"min": 13, "max": 27}}
    api.pedir("POST", "/admin/especies", admin, json={"nombre": "Cinta", "rangos": rangos})

    matera = api.matera(ana, "Baño", "cinta", "A4CF12000003")
    respuesta = _desde_sensor(api, "A4CF12000003", matera["clave"], {"humedad": 45, "luz": 2000, "temperatura": 20})

    assert respuesta.get_json()["diagnostico"]["estado"] == "SALUDABLE"


def test_cors_deja_al_front_quitar_el_sensor_con_delete(api):
    respuesta = api.http.options(
        "/api/v1/plantas/1/sensor",
        headers={
            "Origin": ORIGEN_FRONT,
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "Authorization",
        },
    )

    assert respuesta.headers["Access-Control-Allow-Origin"] == ORIGEN_FRONT
    assert "DELETE" in respuesta.headers["Access-Control-Allow-Methods"]
