"""Plantas, sensores e historial con dobles en memoria. Incluye las pruebas de aislamiento entre usuarios.

La placa no trae ninguna clave: en fabrica se registra su MAC, el dueno la
reclama con el codigo del QR y la clave le llega por su canal en vivo (aqui,
AvisosEnMemoria). Las claves salen de un generador predecible: secreto-1, secreto-2...
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from aplicacion.diagnosticar_planta_de_usuario import DiagnosticarPlantaDeUsuario
from aplicacion.historial import ConsultarHistorial
from aplicacion.plantas import EliminarPlanta, ListarPlantas, RegistrarPlanta
from aplicacion.sensores import (
    AtenderDispositivo,
    CanjearClaveDispositivo,
    QuitarSensor,
    ReclamarDispositivo,
    RegistrarDispositivo,
    RegistrarLectura,
)
from dominio.errores import EspecieNoSoportada
from dominio.errores_usuarios import (
    AlcanceInvalido,
    ApodoRepetido,
    ClaveSensorInvalida,
    CodigoReclamoInvalido,
    CodigoSensorInvalido,
    CredencialDispositivoInvalida,
    CuentaDesactivada,
    DispositivoAjeno,
    DispositivoNoRegistrado,
    DispositivoYaRegistrado,
    MomentoInvalido,
    PermisoDenegado,
    PlantaNoEncontrada,
    PlantaSinSensor,
    SensorYaVinculado,
)
from dominio.especie import Especie
from dominio.estados import EstadoGlobal
from dominio.evaluador import EvaluadorPlanta
from dominio.parametros import HUMEDAD, LUZ, TEMPERATURA
from dominio.plantas import Planta
from dominio.rango import Rango
from dominio.recomendaciones import GeneradorRecomendaciones
from dominio.reglas import ReglaHibrida
from dominio.reportes import ReporteDeDispositivo
from dominio.sensores import CodigoSensor, Sensor
from dominio.servicio_diagnostico import ServicioDiagnostico
from dominio.usuarios import Correo, Rol, Usuario
from tests.dobles import medicion, sansevieria
from tests.dobles_usuarios import (
    AvisosEnMemoria,
    EspeciesEditablesEnMemoria,
    GeneradorSecretosFalso,
    HistorialEnMemoria,
    NotificadorEnMemoria,
    NotificadorQueFalla,
    RelojFijo,
    RepositorioDispositivosEnMemoria,
    RepositorioPlantasEnMemoria,
    RepositorioSensoresEnMemoria,
    RepositorioUsuariosEnMemoria,
    TokensFalsos,
)

AHORA = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
ANA = Usuario(1, Correo("ana@correo.co"), "Ana", "h")
BETO = Usuario(2, Correo("beto@correo.co"), "Beto", "h")
ADA = Usuario(3, Correo("ada@correo.co"), "Ada", "h", Rol.ADMIN)


def potos() -> Especie:
    return Especie("potos", {HUMEDAD: Rango(40, 70), LUZ: Rango(270, 2150), TEMPERATURA: Rango(16, 29)})


@dataclass(frozen=True)
class Vinculada:
    """Una matera con su placa reclamada: la clave es la que le llego a la placa por su canal."""

    planta: Planta
    codigo: CodigoSensor
    clave: str


class Sistema:
    """Los casos de uso de plantas y sensores conectados a los mismos dobles y al ServicioDiagnostico real."""

    def __init__(self, notificador=None):
        self.sensores = RepositorioSensoresEnMemoria()
        self.plantas = RepositorioPlantasEnMemoria(self.sensores)
        self.dispositivos = RepositorioDispositivosEnMemoria()
        self.avisos = AvisosEnMemoria()
        self.cuentas = RepositorioUsuariosEnMemoria(self.plantas)
        self.cuentas.guardar(ANA)
        self.cuentas.guardar(BETO)
        self.historial = HistorialEnMemoria()
        self.reloj = RelojFijo(AHORA)
        especies = EspeciesEditablesEnMemoria(sansevieria(), potos())
        secretos = GeneradorSecretosFalso()
        servicio = ServicioDiagnostico(especies, EvaluadorPlanta(ReglaHibrida(), GeneradorRecomendaciones()))

        self.registrar = RegistrarPlanta(self.plantas, self.plantas, especies)
        self.listar = ListarPlantas(self.plantas, self.sensores, self.historial)
        self.eliminar = EliminarPlanta(self.plantas, self.plantas, self.sensores, self.avisos)
        self.consultar_historial = ConsultarHistorial(self.plantas, self.historial)
        self.notificador = notificador or NotificadorEnMemoria(self.historial)
        self.diagnosticar = DiagnosticarPlantaDeUsuario(self.plantas, servicio, self.historial, self.notificador)
        # La fabrica con su propio generador: los codigos de reclamo no corren la numeracion de las claves.
        self.fabrica = RegistrarDispositivo(self.dispositivos, GeneradorSecretosFalso())
        self.reclamar = ReclamarDispositivo(self.dispositivos, self.plantas, self.sensores, secretos, self.avisos)
        self.atender = AtenderDispositivo(self.dispositivos, self.sensores, self.plantas, secretos)
        self.quitar = QuitarSensor(self.plantas, self.sensores, self.avisos)
        self.tokens = TokensFalsos()
        # Su propio generador: asi los ids de token no corren la numeracion de las claves (secreto-1, secreto-2...).
        self.canjear = CanjearClaveDispositivo(
            self.sensores,
            self.sensores,
            self.plantas,
            self.cuentas,
            GeneradorSecretosFalso(),
            self.tokens,
            self.reloj,
            timedelta(hours=24),
        )
        self.registrar_lectura = RegistrarLectura(self.sensores, self.cuentas, self.diagnosticar, self.reloj)
        self.qr: dict[str, str] = {}

    def placa(self, codigo: str) -> str:
        """La registra en fabrica, si no lo estaba, y devuelve el codigo de reclamo de su QR."""
        valor = CodigoSensor(codigo).valor
        if valor not in self.qr:
            self.qr[valor] = self.fabrica.ejecutar(ADA, valor).codigo_reclamo
        return self.qr[valor]

    def reclamar_para(self, usuario: Usuario, planta_id: int, codigo: str) -> Vinculada:
        """Como el dueno con la placa encendida: escanea el QR y la placa recibe su clave por su canal."""
        valor = CodigoSensor(codigo).valor
        qr = self.placa(valor)
        self.avisos.conectadas.add(valor)
        resultado = self.reclamar.ejecutar(usuario, codigo, qr, planta_id)
        return Vinculada(resultado.planta, resultado.codigo, self.avisos.claves[-1][2])

    def matera(self, usuario: Usuario, apodo: str, especie: str, codigo: str) -> Vinculada:
        planta = self.registrar.ejecutar(usuario, apodo, especie)
        return self.reclamar_para(usuario, planta.id, codigo)

    def medir(self, codigo: str, clave: str, medida, momento=None):
        """Como la placa: canjea su clave por un token y reporta la medicion con la identidad de ese token."""
        identidad = self.tokens.verificar_dispositivo(self.canjear.ejecutar(codigo, clave).token)
        reporte = ReporteDeDispositivo(identidad.codigo, momento or self.reloj.ahora(), medida)
        return self.registrar_lectura.ejecutar(identidad, reporte)

    def codigo_de(self, planta) -> str | None:
        sensor = self.sensores.de_planta(planta.id)
        return sensor.codigo.valor if sensor else None


@pytest.fixture
def sistema():
    return Sistema()


@pytest.fixture
def matera_de_ana(sistema):
    """La Sala de Ana con la placa A4CF12000001. La clave que le llego a la placa es secreto-1."""
    return sistema.matera(ANA, "Sala", "Sansevieria", "A4CF12000001")


@pytest.fixture
def planta_de_ana(matera_de_ana):
    return matera_de_ana.planta


# --- Plantas ---


def test_la_matera_se_crea_sin_sensor(sistema):
    planta = sistema.registrar.ejecutar(ANA, "Sala", "  SANSEVIERIA ")

    assert (planta.id, planta.usuario_id, planta.especie) == (1, ANA.id, "sansevieria")
    [tarjeta] = sistema.listar.ejecutar(ANA)
    assert (tarjeta.sensor, tarjeta.ultima_medicion) == (None, None)


@pytest.mark.parametrize(
    ("apodo", "especie", "error"),
    [("Cocina", "cactus", EspecieNoSoportada), (" sala ", "potos", ApodoRepetido)],
    ids=["especie", "apodo repetido"],
)
def test_una_matera_invalida_no_se_guarda(sistema, planta_de_ana, apodo, especie, error):
    with pytest.raises(error):
        sistema.registrar.ejecutar(ANA, apodo, especie)

    assert sistema.plantas.de_usuario(ANA.id) == [planta_de_ana]


def test_otro_usuario_puede_repetir_el_apodo_pero_no_reclamar_una_placa_con_dueno(sistema, planta_de_ana):
    patio = sistema.registrar.ejecutar(BETO, "Sala", "potos")
    assert patio.usuario_id == BETO.id

    # Aunque tenga el QR de la etiqueta: primero Ana tiene que quitarla de su matera.
    with pytest.raises(SensorYaVinculado):
        sistema.reclamar.ejecutar(BETO, "A4CF12000001", sistema.placa("A4CF12000001"), patio.id)
    assert sistema.codigo_de(planta_de_ana) == "A4CF12000001"


def test_listar_devuelve_solo_las_plantas_propias_con_su_sensor_y_su_ultima_medicion(sistema, matera_de_ana):
    sistema.matera(BETO, "Patio", "potos", "A4CF12000002")
    sistema.medir("A4CF12000001", matera_de_ana.clave, medicion(18, 800, 22))

    [tarjeta] = sistema.listar.ejecutar(ANA)

    assert tarjeta.planta.apodo == "Sala"
    assert tarjeta.sensor.codigo.valor == "A4CF12000001"
    assert tarjeta.ultima_medicion.estado is EstadoGlobal.EN_RIESGO


def test_una_matera_recien_reclamada_aparece_con_su_sensor_sin_confirmar_y_sin_mediciones(sistema, planta_de_ana):
    [tarjeta] = sistema.listar.ejecutar(ANA)

    assert (tarjeta.sensor.codigo.valor, tarjeta.sensor.clave_confirmada, tarjeta.ultima_medicion) == (
        "A4CF12000001",
        False,
        None,
    )


def test_eliminar_borra_la_planta_propia_suelta_su_placa_y_le_avisa(sistema, matera_de_ana):
    sistema.eliminar.ejecutar(ANA, matera_de_ana.planta.id)

    assert sistema.listar.ejecutar(ANA) == []
    assert sistema.avisos.desvinculadas == ["A4CF12000001"]
    with pytest.raises(ClaveSensorInvalida):
        sistema.canjear.ejecutar("A4CF12000001", matera_de_ana.clave)
    # La placa queda libre: el mismo QR sirve para otra matera.
    assert sistema.matera(ANA, "Cocina", "potos", "A4CF12000001").codigo.valor == "A4CF12000001"


@pytest.mark.parametrize(
    "accion",
    [
        lambda s, p: s.eliminar.ejecutar(BETO, p.id),
        lambda s, p: s.quitar.ejecutar(BETO, p.id),
        lambda s, p: s.reclamar.ejecutar(BETO, "A4CF12000009", s.placa("A4CF12000009"), p.id),
        lambda s, p: s.consultar_historial.ejecutar(BETO, p.id),
        lambda s, p: s.diagnosticar.ejecutar(BETO.id, p.id, medicion(30, 800, 22), "A4CF12000009", AHORA),
    ],
    ids=["eliminar", "quitar sensor", "reclamar para la planta de otro", "historial", "medicion"],
)
def test_beto_no_puede_tocar_la_planta_de_ana_y_recibe_el_mismo_error_que_si_no_existiera(
    sistema, planta_de_ana, accion
):
    # El doble de plantas, como la base, se la devuelve a cualquiera: la que la niega es la regla del dominio.
    assert sistema.plantas.por_id(planta_de_ana.id) == planta_de_ana
    with pytest.raises(PlantaNoEncontrada):
        accion(sistema, planta_de_ana)

    assert sistema.listar.ejecutar(ANA)[0].planta == planta_de_ana
    assert sistema.historial.registros == []
    assert sistema.codigo_de(planta_de_ana) == "A4CF12000001"


def test_beto_no_ve_las_plantas_de_ana(sistema, planta_de_ana):
    assert sistema.listar.ejecutar(BETO) == []


# --- Fabrica, reclamo y el canal de la placa ---


def test_en_fabrica_se_registra_la_mac_y_el_codigo_de_reclamo_sale_una_sola_vez(sistema):
    registrado = sistema.fabrica.ejecutar(ADA, "a4:cf:12:00:00:01")

    assert (registrado.codigo.valor, registrado.codigo_reclamo) == ("A4CF12000001", "secreto-1")
    assert sistema.dispositivos.por_codigo(CodigoSensor("A4CF12000001")).hash_reclamo == "huella(secreto-1)"


def test_solo_un_admin_registra_placas_y_cada_mac_una_vez(sistema):
    with pytest.raises(PermisoDenegado):
        sistema.fabrica.ejecutar(ANA, "A4CF12000001")
    sistema.placa("A4CF12000001")
    with pytest.raises(DispositivoYaRegistrado):
        sistema.fabrica.ejecutar(ADA, "A4CF12000001")
    with pytest.raises(CodigoSensorInvalido):
        sistema.fabrica.ejecutar(ADA, "MT-0001-AA")


def test_reclamar_con_la_placa_conectada_le_entrega_la_clave_por_su_canal_y_no_al_dueno(sistema):
    planta = sistema.registrar.ejecutar(ANA, "Sala", "potos")
    sistema.avisos.conectadas.add("A4CF12000001")

    resultado = sistema.reclamar.ejecutar(ANA, "a4cf12000001", sistema.placa("A4CF12000001"), planta.id)

    assert (resultado.planta, resultado.codigo.valor, resultado.clave_entregada) == (planta, "A4CF12000001", True)
    assert sistema.avisos.claves == [("A4CF12000001", planta.id, "secreto-1")]
    assert sistema.sensores.de_planta(planta.id) == Sensor(CodigoSensor("A4CF12000001"), planta.id, "huella(secreto-1)")


def test_una_placa_apagada_al_reclamarla_recibe_su_clave_al_conectarse(sistema):
    planta = sistema.registrar.ejecutar(ANA, "Sala", "potos")

    resultado = sistema.reclamar.ejecutar(ANA, "A4CF12000001", sistema.placa("A4CF12000001"), planta.id)
    assert not resultado.clave_entregada
    assert sistema.avisos.claves == []

    estado = sistema.atender.ejecutar(CodigoSensor("A4CF12000001"))

    assert (estado.planta, estado.clave) == (planta, "secreto-2")
    assert sistema.medir("A4CF12000001", estado.clave, medicion(30, 800, 22)).planta == planta


def test_mientras_la_placa_no_canjee_cada_conexion_le_da_otra_clave_y_la_anterior_deja_de_servir(
    sistema, matera_de_ana
):
    nueva = sistema.atender.ejecutar(CodigoSensor("A4CF12000001")).clave

    assert nueva != matera_de_ana.clave
    with pytest.raises(ClaveSensorInvalida):
        sistema.canjear.ejecutar("A4CF12000001", matera_de_ana.clave)
    assert sistema.medir("A4CF12000001", nueva, medicion(30, 800, 22)).planta == matera_de_ana.planta


def test_el_canje_confirma_la_clave_y_desde_ahi_conectarse_ya_no_entrega_otra(sistema, matera_de_ana):
    sistema.canjear.ejecutar("A4CF12000001", matera_de_ana.clave)

    estado = sistema.atender.ejecutar(CodigoSensor("A4CF12000001"))

    assert (estado.planta, estado.clave) == (matera_de_ana.planta, None)
    assert sistema.listar.ejecutar(ANA)[0].sensor.clave_confirmada
    assert sistema.medir("A4CF12000001", matera_de_ana.clave, medicion(30, 800, 22))


def test_una_placa_sin_dueno_solo_sabe_que_espera_y_una_sin_fabrica_no_se_atiende(sistema):
    sistema.placa("A4CF12000001")

    estado = sistema.atender.ejecutar(CodigoSensor("A4CF12000001"))

    assert (estado.vinculado, estado.clave) == (False, None)
    with pytest.raises(DispositivoNoRegistrado):
        sistema.atender.ejecutar(CodigoSensor("A4CF12009999"))


@pytest.mark.parametrize(
    ("codigo", "qr"),
    [("A4CF12000001", "codigo-malo"), ("A4CF12009999", None), ("no-es-mac", None)],
    ids=["codigo de reclamo malo", "placa sin fabrica", "codigo mal escrito"],
)
def test_el_reclamo_da_el_mismo_error_sin_importar_que_fallo_y_no_vincula_nada(sistema, codigo, qr):
    planta = sistema.registrar.ejecutar(ANA, "Sala", "potos")
    qr_real = sistema.placa("A4CF12000001")

    with pytest.raises(CodigoReclamoInvalido):
        sistema.reclamar.ejecutar(ANA, codigo, qr or qr_real, planta.id)

    assert sistema.codigo_de(planta) is None


def test_reclamar_otra_placa_para_la_misma_matera_conserva_el_historial_y_suelta_la_anterior(
    sistema, matera_de_ana
):
    planta = matera_de_ana.planta
    sistema.medir("A4CF12000001", matera_de_ana.clave, medicion(30, 800, 22))

    nuevo = sistema.reclamar_para(ANA, planta.id, "a4cf12000002")

    assert (nuevo.planta, nuevo.codigo.valor, nuevo.clave) == (planta, "A4CF12000002", "secreto-2")
    assert sistema.avisos.desvinculadas == ["A4CF12000001"]
    with pytest.raises(ClaveSensorInvalida):
        sistema.medir("A4CF12000001", matera_de_ana.clave, medicion(31, 800, 22))
    sistema.medir("A4CF12000002", nuevo.clave, medicion(32, 800, 22))
    assert [r.origen for r in sistema.historial.registros] == ["A4CF12000001", "A4CF12000002"]
    assert [t.sensor.codigo.valor for t in sistema.listar.ejecutar(ANA)] == ["A4CF12000002"]


def test_la_placa_que_se_suelta_queda_libre_para_otra_matera(sistema, planta_de_ana):
    sistema.reclamar_para(ANA, planta_de_ana.id, "A4CF12000002")

    assert sistema.matera(BETO, "Patio", "potos", "A4CF12000001").codigo.valor == "A4CF12000001"


def test_reclamar_otra_vez_la_misma_placa_le_da_una_clave_nueva_si_perdio_la_suya(sistema, matera_de_ana):
    nuevo = sistema.reclamar_para(ANA, matera_de_ana.planta.id, "A4CF12000001")

    assert nuevo.clave == "secreto-2"
    assert sistema.avisos.desvinculadas == []
    with pytest.raises(ClaveSensorInvalida):
        sistema.medir("A4CF12000001", matera_de_ana.clave, medicion(30, 800, 22))
    assert sistema.medir("A4CF12000001", nuevo.clave, medicion(30, 800, 22)).planta == matera_de_ana.planta


def test_quitar_el_sensor_revoca_su_clave_y_su_token_le_avisa_y_conserva_el_historial(sistema, matera_de_ana):
    identidad = _identidad(sistema, "A4CF12000001", matera_de_ana.clave)
    sistema.registrar_lectura.ejecutar(identidad, ReporteDeDispositivo(identidad.codigo, AHORA, medicion(30, 800, 22)))

    assert sistema.quitar.ejecutar(ANA, matera_de_ana.planta.id).valor == "A4CF12000001"

    assert sistema.avisos.desvinculadas == ["A4CF12000001"]
    with pytest.raises(CredencialDispositivoInvalida):
        sistema.registrar_lectura.ejecutar(identidad, ReporteDeDispositivo(identidad.codigo, AHORA, medicion(31, 800, 22)))
    with pytest.raises(ClaveSensorInvalida):
        sistema.canjear.ejecutar("A4CF12000001", matera_de_ana.clave)
    assert len(sistema.historial.registros) == 1
    assert sistema.listar.ejecutar(ANA)[0].sensor is None
    with pytest.raises(PlantaSinSensor):
        sistema.quitar.ejecutar(ANA, matera_de_ana.planta.id)


# --- Lecturas ---


def test_el_sensor_con_su_clave_guarda_la_medicion_en_su_planta_con_su_codigo(sistema, matera_de_ana):
    resultado = sistema.medir("a4cf12000001", matera_de_ana.clave, medicion(30, 800, 22))

    assert resultado.planta == matera_de_ana.planta
    assert resultado.diagnostico.estado is EstadoGlobal.SALUDABLE
    assert sistema.historial.registros[0].origen == "A4CF12000001"


@pytest.mark.parametrize(
    ("codigo", "clave"),
    [("A4CF12000001", "clave-mala"), ("A4CF12009999", "secreto-1"), ("no-es-codigo", "secreto-1")],
)
def test_el_sensor_recibe_el_mismo_error_sin_importar_que_fallo_y_no_se_guarda_nada(
    sistema, matera_de_ana, codigo, clave
):
    with pytest.raises(ClaveSensorInvalida):
        sistema.medir(codigo, clave, medicion(30, 800, 22))
    assert sistema.historial.registros == []


def test_una_clave_revocada_mientras_llegaba_la_medicion_no_escribe_en_la_planta_que_tomo_el_codigo(
    sistema, matera_de_ana, monkeypatch
):
    viejo = sistema.sensores.por_codigo(CodigoSensor("A4CF12000001"))
    # Ana cambia de placa y Beto reclama la que quedo libre justo despues de que se verifico la clave vieja.
    sistema.reclamar_para(ANA, matera_de_ana.planta.id, "A4CF12000002")
    sistema.matera(BETO, "Patio", "potos", "A4CF12000001")
    monkeypatch.setattr(sistema.sensores, "por_codigo", lambda codigo: viejo)

    with pytest.raises(ClaveSensorInvalida):
        sistema.medir("A4CF12000001", matera_de_ana.clave, medicion(30, 800, 22))

    assert sistema.historial.registros == []
    assert sistema.notificador.avisos == []


def test_no_se_vincula_una_placa_a_una_planta_que_se_borro_mientras_tanto(sistema, matera_de_ana):
    # buscar_planta_propia ya la encontro, pero otra pestana la borra antes de guardar el sensor.
    sistema.plantas.eliminar(matera_de_ana.planta)

    with pytest.raises(PlantaNoEncontrada):
        sistema.sensores.reemplazar(matera_de_ana.planta, Sensor(CodigoSensor("A4CF12000002"), matera_de_ana.planta.id, "h"))


def test_el_historial_trae_lo_mas_reciente_primero_y_respeta_el_limite(sistema, matera_de_ana):
    for humedad in (30, 31, 32):
        sistema.medir("A4CF12000001", matera_de_ana.clave, medicion(humedad, 800, 22))
        sistema.reloj.avanzar(timedelta(hours=1))

    historial = sistema.consultar_historial.ejecutar(ANA, matera_de_ana.planta.id, limite=2)

    assert historial.planta == matera_de_ana.planta
    assert [r.lecturas[0].valor for r in historial.registros] == [32, 31]


def test_la_medicion_del_sensor_se_avisa_al_dueno_despues_de_guardarla(sistema, matera_de_ana):
    resultado = sistema.medir("A4CF12000001", matera_de_ana.clave, medicion(18, 800, 22))

    assert sistema.notificador.avisos == [(ANA.id, resultado.planta, resultado.registro, resultado.diagnostico)]
    assert sistema.notificador.guardada_al_avisar == [True]


def test_cada_medicion_se_avisa_solo_al_dueno_de_su_planta(sistema, matera_de_ana):
    de_beto = sistema.matera(BETO, "Patio", "potos", "A4CF12000002")

    sistema.medir("A4CF12000002", de_beto.clave, medicion(50, 800, 22))
    sistema.medir("A4CF12000001", matera_de_ana.clave, medicion(30, 800, 22))

    assert [(usuario, planta.apodo) for usuario, planta, _, _ in sistema.notificador.avisos] == [
        (BETO.id, "Patio"),
        (ANA.id, "Sala"),
    ]


def test_si_el_aviso_falla_la_medicion_queda_guardada_y_el_sensor_recibe_su_diagnostico():
    sistema = Sistema(notificador=NotificadorQueFalla())
    matera = sistema.matera(ANA, "Sala", "Sansevieria", "A4CF12000001")

    resultado = sistema.medir("A4CF12000001", matera.clave, medicion(30, 800, 22))

    assert resultado.diagnostico.estado is EstadoGlobal.SALUDABLE
    assert sistema.historial.registros == [resultado.registro]


def test_una_medicion_rechazada_no_se_avisa(sistema, matera_de_ana):
    with pytest.raises(ClaveSensorInvalida):
        sistema.medir("A4CF12000001", "clave-mala", medicion(30, 800, 22))

    assert sistema.notificador.avisos == []


# --- El dispositivo: canje de la clave por un token y registro de lecturas ---


def test_el_canje_entrega_un_token_de_dispositivo_que_vence_en_24_horas(sistema, matera_de_ana):
    canjeado = sistema.canjear.ejecutar("a4cf12000001", matera_de_ana.clave)

    assert canjeado.token == "token(dispositivo A4CF12000001, secreto-1)"
    assert canjeado.vence_en == AHORA + timedelta(hours=24)
    with pytest.raises(AlcanceInvalido):
        sistema.tokens.verificar_usuario(canjeado.token)  # no sirve en las rutas de usuario


def test_la_lectura_se_guarda_con_la_hora_que_trae_el_reporte_no_con_la_de_llegada(sistema, matera_de_ana):
    tomada = AHORA - timedelta(minutes=10)

    resultado = sistema.medir("A4CF12000001", matera_de_ana.clave, medicion(30, 800, 22), momento=tomada)

    assert resultado.registro.momento == tomada
    assert sistema.historial.registros == [resultado.registro]


def _identidad(sistema, codigo, clave):
    return sistema.tokens.verificar_dispositivo(sistema.canjear.ejecutar(codigo, clave).token)


def test_un_canje_nuevo_deja_sin_efecto_el_token_anterior(sistema, matera_de_ana):
    vieja = _identidad(sistema, "A4CF12000001", matera_de_ana.clave)
    nueva = _identidad(sistema, "A4CF12000001", matera_de_ana.clave)

    with pytest.raises(CredencialDispositivoInvalida):
        sistema.registrar_lectura.ejecutar(vieja, ReporteDeDispositivo(vieja.codigo, AHORA, medicion(30, 800, 22)))
    assert sistema.registrar_lectura.ejecutar(nueva, ReporteDeDispositivo(nueva.codigo, AHORA, medicion(30, 800, 22)))


def test_reclamar_otra_vez_la_placa_deja_sin_efecto_su_token_aunque_no_haya_vencido(sistema, matera_de_ana):
    identidad = _identidad(sistema, "A4CF12000001", matera_de_ana.clave)

    sistema.reclamar_para(ANA, matera_de_ana.planta.id, "A4CF12000001")

    with pytest.raises(CredencialDispositivoInvalida):
        sistema.registrar_lectura.ejecutar(identidad, ReporteDeDispositivo(identidad.codigo, AHORA, medicion(30, 800, 22)))
    assert sistema.historial.registros == []


def test_un_sensor_no_puede_reportar_lecturas_de_otro(sistema, matera_de_ana):
    sistema.matera(BETO, "Patio", "potos", "A4CF12000002")
    identidad = _identidad(sistema, "A4CF12000001", matera_de_ana.clave)

    with pytest.raises(DispositivoAjeno):
        sistema.registrar_lectura.ejecutar(
            identidad, ReporteDeDispositivo(CodigoSensor("A4CF12000002"), AHORA, medicion(30, 800, 22))
        )
    assert sistema.historial.registros == []


def test_los_sensores_de_una_cuenta_desactivada_no_canjean_ni_reportan(sistema, matera_de_ana):
    identidad = _identidad(sistema, "A4CF12000001", matera_de_ana.clave)

    sistema.cuentas.guardar(ANA.con_estado(False))

    with pytest.raises(CuentaDesactivada):
        sistema.canjear.ejecutar("A4CF12000001", matera_de_ana.clave)
    with pytest.raises(CuentaDesactivada):
        sistema.registrar_lectura.ejecutar(identidad, ReporteDeDispositivo(identidad.codigo, AHORA, medicion(30, 800, 22)))
    assert sistema.historial.registros == []
    assert sistema.notificador.avisos == []


def test_al_reactivar_la_cuenta_su_sensor_vuelve_a_reportar(sistema, matera_de_ana):
    sistema.cuentas.guardar(ANA.con_estado(False))
    sistema.cuentas.guardar(ANA)

    assert sistema.medir("A4CF12000001", matera_de_ana.clave, medicion(30, 800, 22))


def test_una_clave_incorrecta_no_revela_que_la_cuenta_esta_desactivada(sistema, matera_de_ana):
    sistema.cuentas.guardar(ANA.con_estado(False))

    with pytest.raises(ClaveSensorInvalida):
        sistema.canjear.ejecutar("A4CF12000001", "otra-clave")


def test_una_lectura_del_futuro_no_se_guarda_ni_se_avisa(sistema, matera_de_ana):
    with pytest.raises(MomentoInvalido):
        sistema.medir("A4CF12000001", matera_de_ana.clave, medicion(30, 800, 22), momento=AHORA + timedelta(minutes=5))

    assert sistema.historial.registros == []
    assert sistema.notificador.avisos == []


def test_un_token_de_un_sensor_que_nunca_canjeo_no_sirve(sistema, matera_de_ana):
    inventada = sistema.tokens.verificar_dispositivo("token(dispositivo A4CF12000001, inventado)")

    with pytest.raises(CredencialDispositivoInvalida):
        sistema.registrar_lectura.ejecutar(inventada, ReporteDeDispositivo(inventada.codigo, AHORA, medicion(30, 800, 22)))
