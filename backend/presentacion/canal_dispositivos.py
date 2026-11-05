"""El canal en vivo de cada placa: ws://<api>/api/v1/dispositivos/canal?codigo=<MAC>.

La placa lo abre al encender y lo mantiene abierto. Solo dice su MAC: no tiene
ningun secreto grabado. Por aqui recibe:

    {"tipo": "estado", "vinculado": false}                  sin dueno: espera que la reclamen con el QR
    {"tipo": "estado", "vinculado": true, "planta": {...}}  ya canjeo su clave
    {"tipo": "vinculado", "planta": {...}, "clave": "..."}  clave nueva: guardarla y canjearla por su token
    {"tipo": "desvinculado"}                                la quitaron de su matera: borrar clave y token
    {"tipo": "error", "error": "...", "mensaje": "..."}     y se cierra

La placa no manda nada por aqui: confirma su clave al canjearla por su token
(POST /dispositivos/<codigo>/token). Los mensajes que mande se ignoran.

No se revisa el Origin, a diferencia del WebSocket del front: este canal no usa
cookies ni la sesion del navegador, y la libreria WebSocket del ESP32 manda su
propio Origin (file://).

Riesgo reconocido: como el canal no exige un secreto, alguien en la red que
conozca la MAC podria abrirlo antes que la placa y recibir la clave de una placa
recien reclamada. Por eso la clave solo se entrega mientras la placa no la
canjee, y la conexion mas reciente reemplaza a la anterior. En produccion iria
sobre TLS y con una placa que pruebe su identidad.
"""
import logging
from contextlib import AbstractContextManager
from typing import Protocol

from flask import Blueprint, request
from flask_sock import Sock
from simple_websocket import ConnectionClosed

from dominio import puertos_entrada as entrada
from dominio.errores_usuarios import CodigoSensorInvalido, DispositivoNoRegistrado
from dominio.sensores import CodigoSensor
from presentacion import esquemas_usuarios
from presentacion.tiempo_real import CIERRE_POR_ERROR, ESPERA_MAXIMA, RUTAS_WEBSOCKET, CanalWebSocket

_log = logging.getLogger(__name__)

RUTA = "/api/v1/dispositivos/canal"
RUTAS_WEBSOCKET.add(RUTA)


class ConexionesDispositivos(Protocol):
    """Lo que esta ruta necesita de quien guarda las conexiones de las placas.

    Lo cumple infraestructura.tiempo_real.CanalesDispositivos, y main.py se lo pasa:
    presentacion no importa infraestructura.
    """

    def conectado(self, codigo: str, canal: CanalWebSocket) -> AbstractContextManager[None]: ...


def crear_blueprint_canal_dispositivos(
    atender: entrada.AtenderDispositivo, conexiones: ConexionesDispositivos
) -> Blueprint:
    bp = Blueprint("api_v1_canal_dispositivos", __name__)

    @Sock().route(RUTA, bp=bp)
    def canal_dispositivo(ws):
        canal = CanalWebSocket(ws)
        try:
            _atender(canal, request.args.get("codigo", ""), atender, conexiones)
        except ConnectionClosed:
            raise  # la placa cerro: flask-sock termina la conexion
        except Exception:
            _log.exception("Error inesperado en el canal de una placa.")
            canal.cerrar_con_error("ERROR_INTERNO", "Ocurrió un error inesperado en el servidor.", CIERRE_POR_ERROR)
        finally:
            canal.terminar()

    return bp


def _atender(
    canal: CanalWebSocket, texto: str, atender: entrada.AtenderDispositivo, conexiones: ConexionesDispositivos
) -> None:
    try:
        codigo = CodigoSensor(texto)
    except CodigoSensorInvalido as error:
        canal.cerrar_con_error("CODIGO_SENSOR_INVALIDO", str(error))
        return
    # Primero se registra la conexion: un reclamo que llegue desde aqui ya le encuentra el canal.
    with conexiones.conectado(codigo.valor, canal):
        try:
            estado = atender.ejecutar(codigo)
        except DispositivoNoRegistrado as error:
            canal.cerrar_con_error("DISPOSITIVO_NO_REGISTRADO", str(error))
            return
        canal.enviar_json(esquemas_usuarios.estado_dispositivo_a_json(estado))
        # Hasta que la placa cierre (recibir lanza ConnectionClosed) o que otra conexion la reemplace.
        while not canal.cortado:
            canal.recibir(ESPERA_MAXIMA)
