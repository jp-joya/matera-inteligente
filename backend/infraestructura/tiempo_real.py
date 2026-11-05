"""Tiempo real: las pantallas abiertas de cada usuario, el aviso de cada medicion nueva y el canal de cada placa.

CanalesDispositivos y AvisosDispositivosWebSocket (al final) son el canal de
las placas: por ahi reciben su clave y se enteran si las quitan de su matera.
La ruta esta en presentacion/canal_dispositivos.py.


CanalesTiempoReal guarda las conexiones abiertas, agrupadas por usuario.
NotificadorWebSocket implementa el puerto NotificadorDiagnosticos del dominio:
convierte la medicion en un mensaje JSON y se lo manda solo a las conexiones
del dueno de la planta. La ruta que acepta las conexiones esta en
presentacion/tiempo_real.py; main.py le pasa estos mismos canales.
"""
import json
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Protocol

from dominio.diagnostico import Diagnostico
from dominio.historial import RegistroMedicion
from dominio.plantas import Planta
from dominio.puertos_usuarios import AvisosDispositivos, NotificadorDiagnosticos
from dominio.sensores import CodigoSensor

_log = logging.getLogger(__name__)

FormatoMedicion = Callable[[Planta, RegistroMedicion, Diagnostico], dict[str, Any]]


class Canal(Protocol):
    """Una conexion abierta. Lo unico que hace falta de ella es poder mandarle un texto."""

    def enviar(self, texto: str) -> None: ...


class CanalesTiempoReal:
    """Las conexiones abiertas de cada usuario.

    Se usa desde varios hilos a la vez: cada pantalla abierta ocupa uno y cada
    medicion llega por otro. Por eso todo cambio pasa por un candado.
    """

    def __init__(self):
        self._candado = threading.Lock()
        self._por_usuario: dict[int, set[Canal]] = {}

    @contextmanager
    def conectado(self, usuario_id: int, canal: Canal) -> Iterator[None]:
        """Mientras dure el bloque, el canal recibe las mediciones de las plantas del usuario."""
        with self._candado:
            self._por_usuario.setdefault(usuario_id, set()).add(canal)
        try:
            yield
        finally:
            self._quitar(usuario_id, canal)

    def conectados(self, usuario_id: int) -> int:
        with self._candado:
            return len(self._por_usuario.get(usuario_id, ()))

    def enviar(self, usuario_id: int, texto: str) -> int:
        """Manda el texto a cada conexion del usuario y devuelve a cuantas llego.

        Una conexion que falla (la pestana se cerro sin avisar) se quita, y las
        demas igual reciben el texto. Se envia fuera del candado, para que una
        conexion lenta no frene a quien se conecta o se desconecta.
        """
        with self._candado:
            canales = list(self._por_usuario.get(usuario_id, ()))
        entregados = 0
        for canal in canales:
            try:
                canal.enviar(texto)
            except Exception:
                _log.info("Se quita una conexion en vivo del usuario %s que ya no responde.", usuario_id)
                self._quitar(usuario_id, canal)
            else:
                entregados += 1
        return entregados

    def _quitar(self, usuario_id: int, canal: Canal) -> None:
        with self._candado:
            canales = self._por_usuario.get(usuario_id)
            if canales is not None:
                canales.discard(canal)
                if not canales:
                    del self._por_usuario[usuario_id]


class NotificadorWebSocket(NotificadorDiagnosticos):
    """El aviso en vivo por WebSocket. El formato del mensaje lo decide presentacion y llega desde main.py."""

    def __init__(self, canales: CanalesTiempoReal, formato: FormatoMedicion):
        self._canales = canales
        self._formato = formato

    def publicar(self, usuario_id: int, planta: Planta, registro: RegistroMedicion, diagnostico: Diagnostico) -> None:
        if not self._canales.conectados(usuario_id):
            return
        mensaje = json.dumps(self._formato(planta, registro, diagnostico), ensure_ascii=False)
        self._canales.enviar(usuario_id, mensaje)


class CanalReemplazable(Canal, Protocol):
    """Una conexion que tambien se puede cortar desde afuera, cuando otra la reemplaza."""

    def cortar(self) -> None: ...


class CanalesDispositivos:
    """Una conexion por placa: la mas reciente reemplaza a la anterior.

    Una placa que se reinicia abre otra conexion antes de que el servidor note
    que la anterior murio. Si la nueva no reemplazara a la vieja, la clave se le
    mandaria a una conexion muerta.
    """

    def __init__(self):
        self._candado = threading.Lock()
        self._por_codigo: dict[str, CanalReemplazable] = {}

    @contextmanager
    def conectado(self, codigo: str, canal: CanalReemplazable) -> Iterator[None]:
        with self._candado:
            anterior = self._por_codigo.get(codigo)
            self._por_codigo[codigo] = canal
        if anterior is not None:
            _log.info("La placa %s abrio otra conexion: se corta la anterior.", codigo)
            anterior.cortar()
        try:
            yield
        finally:
            self._quitar(codigo, canal)

    def conectado_ahora(self, codigo: str) -> bool:
        with self._candado:
            return codigo in self._por_codigo

    def enviar(self, codigo: str, texto: str) -> bool:
        """True si el texto quedo en camino a la placa. Una conexion que falla se quita."""
        with self._candado:
            canal = self._por_codigo.get(codigo)
        if canal is None:
            return False
        try:
            canal.enviar(texto)
        except Exception:
            _log.info("Se quita la conexion de la placa %s, que ya no responde.", codigo)
            self._quitar(codigo, canal)
            return False
        return True

    def _quitar(self, codigo: str, canal: CanalReemplazable) -> None:
        with self._candado:
            if self._por_codigo.get(codigo) is canal:
                del self._por_codigo[codigo]


class AvisosDispositivosWebSocket(AvisosDispositivos):
    """Los avisos a cada placa por su WebSocket. El formato de los mensajes lo decide presentacion."""

    def __init__(
        self,
        canales: CanalesDispositivos,
        formato_vinculado: Callable[[Planta, str], dict[str, Any]],
        formato_desvinculado: Callable[[], dict[str, Any]],
    ):
        self._canales = canales
        self._formato_vinculado = formato_vinculado
        self._formato_desvinculado = formato_desvinculado

    def conectado(self, codigo: CodigoSensor) -> bool:
        return self._canales.conectado_ahora(codigo.valor)

    def vinculado(self, codigo: CodigoSensor, planta: Planta, clave: str) -> bool:
        mensaje = json.dumps(self._formato_vinculado(planta, clave), ensure_ascii=False)
        return self._canales.enviar(codigo.valor, mensaje)

    def desvinculado(self, codigo: CodigoSensor) -> None:
        self._canales.enviar(codigo.valor, json.dumps(self._formato_desvinculado(), ensure_ascii=False))
