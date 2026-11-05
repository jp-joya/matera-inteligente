"""Un cliente de la API armado con main.construir_app sobre una base temporal, para las pruebas del corte 2.

Tambien levanta el servidor real, con hilos, para las pruebas que necesitan
hablarle por la red: el recorrido completo, el WebSocket y el simulador del ESP32.
"""
import hashlib
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest
from simple_websocket import Client, ConnectionClosed
from werkzeug.serving import make_server

from crear_admin import main as crear_admin
from infraestructura.configuracion import Configuracion
from infraestructura.sqlite import BaseDatos
from main import construir_app

ORIGEN_FRONT = "http://localhost:5500"


def ahora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ClienteApi:
    """Atajos sobre el test_client de Flask: registrar, entrar, crear un admin, pedir rutas con token
    y reportar lecturas como un dispositivo."""

    def __init__(self, config: Configuracion):
        self.config = config
        self.http = construir_app(config).test_client()
        self._qr: dict[str, str] = {}  # el codigo de reclamo de cada placa que paso por fabrica, como su etiqueta

    def pedir(self, metodo, ruta, token=None, json=None, cabeceras=None):
        cabeceras = dict(cabeceras or {})
        if token:
            cabeceras["Authorization"] = f"Bearer {token}"
        return self.http.open(f"/api/v1{ruta}", method=metodo, json=json, headers=cabeceras)

    def registrar(self, nombre="Ana", correo="ana@correo.co", contrasena="clave-segura"):
        return self.pedir("POST", "/usuarios", json={"nombre": nombre, "correo": correo, "contrasena": contrasena})

    def entrar(self, correo="ana@correo.co", contrasena="clave-segura") -> str:
        respuesta = self.pedir("POST", "/sesiones", json={"correo": correo, "contrasena": contrasena})
        assert respuesta.status_code == 201, respuesta.get_json()
        return respuesta.get_json()["token"]

    def cuenta(self, nombre="Ana", correo="ana@correo.co") -> str:
        """Registra la cuenta y devuelve un token de sesion."""
        assert self.registrar(nombre, correo).status_code == 201
        return self.entrar(correo)

    def admin(self, nombre="Ada", correo="ada@correo.co") -> str:
        """Registra la cuenta, la vuelve admin con crear_admin.py y devuelve su token."""
        assert self.registrar(nombre, correo).status_code == 201
        assert crear_admin([correo], self.config) == 0
        return self.entrar(correo)

    def fabricar(self, codigo="A4CF12000001") -> str:
        """El paso de fabrica, con una cuenta admin propia. Devuelve el codigo de reclamo del QR."""
        if not hasattr(self, "_fabrica"):
            self._fabrica = self.admin("Fabrica", "fabrica@correo.co")
        respuesta = self.pedir("POST", "/admin/dispositivos", self._fabrica, json={"codigo": codigo})
        assert respuesta.status_code == 201, respuesta.get_json()
        self._qr[respuesta.get_json()["codigo"]] = respuesta.get_json()["codigoReclamo"]
        return respuesta.get_json()["codigoReclamo"]

    def reclamar(self, token, planta_id, codigo="A4CF12000001", codigo_reclamo=None):
        """Como el dueno que escanea el QR. Sin codigo_reclamo, registra la placa en fabrica primero."""
        codigo_reclamo = codigo_reclamo if codigo_reclamo is not None else self.fabricar(codigo)
        cuerpo = {"codigoReclamo": codigo_reclamo, "plantaId": planta_id}
        return self.pedir("POST", f"/dispositivos/{codigo}/reclamo", token, json=cuerpo)

    def dar_clave(self, codigo="A4CF12000001", clave="clave-de-prueba") -> str:
        """Como si la placa ya hubiera recibido su clave por su canal en vivo.

        El test_client no abre WebSocket: el canal de verdad se prueba en test_canal_dispositivos.py.
        """
        huella = hashlib.sha256(clave.encode()).hexdigest()
        with BaseDatos(self.config.ruta_bd).conexion() as conexion:
            cambiadas = conexion.execute(
                "UPDATE sensor SET hash_clave = ?, clave_confirmada = 0, token_id = NULL WHERE codigo = ?",
                (huella, codigo),
            ).rowcount
        assert cambiadas == 1, "la placa no esta vinculada"
        return clave

    def matera(self, token, apodo="Sala", especie="potos", codigo="A4CF12000001") -> dict:
        """Una matera con su placa ya reclamada y con su clave: {planta, codigo, clave}."""
        respuesta = self.pedir("POST", "/plantas", token, json={"apodo": apodo, "especie": especie})
        assert respuesta.status_code == 201, respuesta.get_json()
        planta = respuesta.get_json()
        reclamo = self.reclamar(token, planta["id"], codigo)
        assert reclamo.status_code == 201, reclamo.get_json()
        return {"planta": planta, "codigo": reclamo.get_json()["sensor"]["codigo"], "clave": self.dar_clave(codigo)}

    def canjear(self, codigo, clave):
        """Como la placa: cambia la clave del sensor por su token."""
        return self.pedir("POST", f"/dispositivos/{codigo}/token", json={"clave": clave})

    def reportar(self, codigo, token, lecturas, momento=None, dispositivo=None):
        cuerpo = {"dispositivo": dispositivo or codigo, "momento": momento or ahora_iso(), "lecturas": lecturas}
        return self.pedir("POST", f"/dispositivos/{codigo}/lecturas", token, json=cuerpo)

    def medir(self, codigo, clave, lecturas):
        """Canje y lectura, como la placa la primera vez. Si el canje falla, devuelve esa respuesta."""
        canje = self.canjear(codigo, clave)
        if canje.status_code != 200:
            return canje
        return self.reportar(codigo, canje.get_json()["token"], lecturas)


@pytest.fixture
def config(tmp_path):
    # Pocas iteraciones de PBKDF2: con las 600 000 reales cada registro tarda casi un segundo.
    return Configuracion(
        puerto=5000, ruta_bd=tmp_path / "matera.sqlite", origenes_cors=(ORIGEN_FRONT,), iteraciones_contrasena=1_000
    )


@pytest.fixture
def api(config):
    return ClienteApi(config)


@pytest.fixture
def base_url(config):
    """El servidor de verdad, con threaded=True como app.run(), en un puerto libre."""
    servidor = make_server("127.0.0.1", 0, construir_app(config), threaded=True)
    hilo = threading.Thread(target=servidor.serve_forever, daemon=True)
    hilo.start()
    yield f"http://127.0.0.1:{servidor.server_port}/api/v1"
    servidor.shutdown()
    hilo.join()


def pedir_http(base_url, metodo, ruta, cuerpo=None, token=None):
    """Una peticion de verdad por la red. Devuelve (estado, cuerpo JSON)."""
    cabeceras = {"Content-Type": "application/json"}
    if token:
        cabeceras["Authorization"] = f"Bearer {token}"
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    peticion = urllib.request.Request(f"{base_url}{ruta}", data=datos, headers=cabeceras, method=metodo)
    try:
        with urllib.request.urlopen(peticion, timeout=10) as respuesta:
            return respuesta.status, json.load(respuesta)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def medir_http(base_url, codigo, clave, lecturas):
    """Canje y lectura por la red, como la placa. Devuelve (estado, cuerpo JSON) de la lectura."""
    _, canje = pedir_http(base_url, "POST", f"/dispositivos/{codigo}/token", {"clave": clave})
    cuerpo = {"dispositivo": codigo, "momento": ahora_iso(), "lecturas": lecturas}
    return pedir_http(base_url, "POST", f"/dispositivos/{codigo}/lecturas", cuerpo, canje.get("token"))


def es_error(respuesta, estado: int, codigo: str) -> bool:
    """Respuesta de error con el cuerpo uniforme del corte 1: {error, mensaje, detalle}."""
    cuerpo = respuesta.get_json(silent=True) or {}
    return (
        respuesta.status_code == estado
        and respuesta.mimetype == "application/json"
        and set(cuerpo) == {"error", "mensaje", "detalle"}
        and cuerpo["error"] == codigo
    )


class Placa:
    """Una placa con su canal en vivo abierto, como el ESP32: solo dice su MAC."""

    def __init__(self, base_url, codigo="A4CF12000001", cabeceras=None):
        url = base_url.replace("http://", "ws://") + f"/dispositivos/canal?codigo={codigo}"
        self.ws = Client.connect(url, headers=cabeceras or {})

    def mensaje(self, espera=5):
        texto = self.ws.receive(timeout=espera)
        return json.loads(texto) if texto is not None else None

    def cerrar(self):
        try:
            self.ws.close()
        except (ConnectionClosed, OSError):  # el servidor ya pudo haber cerrado el socket
            pass


def cuenta_http(base_url, nombre="Ana") -> str:
    """Registra la cuenta por la red y devuelve su token."""
    correo = f"{nombre.lower()}@correo.co"
    pedir_http(base_url, "POST", "/usuarios", {"nombre": nombre, "correo": correo, "contrasena": "clave-segura"})
    return pedir_http(base_url, "POST", "/sesiones", {"correo": correo, "contrasena": "clave-segura"})[1]["token"]


def fabricar_http(base_url, config, codigo="A4CF12000001", admin=None) -> str:
    """El paso de fabrica contra el servidor real. Sin el token de un admin, crea una cuenta de fabrica."""
    if admin is None:
        correo = f"fabrica-{codigo.lower()}@correo.co"
        pedir_http(base_url, "POST", "/usuarios", {"nombre": "Fabrica", "correo": correo, "contrasena": "clave-segura"})
        assert crear_admin([correo], config) == 0
        admin = pedir_http(base_url, "POST", "/sesiones", {"correo": correo, "contrasena": "clave-segura"})[1]["token"]
    estado, registrado = pedir_http(base_url, "POST", "/admin/dispositivos", {"codigo": codigo}, admin)
    assert estado == 201, registrado
    return registrado["codigoReclamo"]


def matera_http(base_url, config, token, apodo="Sala", codigo="A4CF12000001", admin=None) -> dict:
    """El recorrido completo por la red: la matera, la fabrica, la placa conectada, el reclamo con el QR.

    Devuelve {planta, codigo, clave}: la clave es la que le llego a la placa por su canal.
    """
    estado, planta = pedir_http(base_url, "POST", "/plantas", {"apodo": apodo, "especie": "potos"}, token)
    assert estado == 201, planta
    codigo_reclamo = fabricar_http(base_url, config, codigo, admin)
    placa = Placa(base_url, codigo)
    try:
        assert placa.mensaje() == {"tipo": "estado", "vinculado": False}
        cuerpo = {"codigoReclamo": codigo_reclamo, "plantaId": planta["id"]}
        estado, reclamo = pedir_http(base_url, "POST", f"/dispositivos/{codigo}/reclamo", cuerpo, token)
        assert estado == 201 and reclamo["claveEntregada"], reclamo
        mensaje = placa.mensaje()
        assert mensaje["tipo"] == "vinculado", mensaje
        return {"planta": planta, "codigo": codigo, "clave": mensaje["clave"]}
    finally:
        placa.cerrar()
