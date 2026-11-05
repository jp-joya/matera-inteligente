"""Sensor de una planta: la placa ESP32, vendida como un producto terminado.

La placa no lleva ningun secreto grabado. Su codigo es su MAC, que viene de
fabrica en el chip. En fabrica se registra (Dispositivo) con la huella de un
codigo de reclamo que va impreso en el QR de su etiqueta. Cuando el dueno la
reclama con ese QR, queda vinculada a su matera (Sensor) y recibe una clave
por su canal en vivo; la canjea por su token como antes. Aqui solo se guardan
huellas: la del codigo de reclamo y la de la clave.
"""
import re
from dataclasses import dataclass

from dominio.errores_usuarios import CodigoSensorInvalido

_FORMA_CODIGO = re.compile(r"[0-9A-F]{12}")
_SEPARADORES = re.compile(r"[:\-\s]")


@dataclass(frozen=True)
class CodigoSensor:
    """La MAC de la placa: 12 cifras hexadecimales. Acepta minusculas y separadores (a4:cf:12:...)."""

    valor: str

    def __post_init__(self) -> None:
        normalizado = _SEPARADORES.sub("", self.valor).upper()
        if not _FORMA_CODIGO.fullmatch(normalizado):
            raise CodigoSensorInvalido(self.valor)
        object.__setattr__(self, "valor", normalizado)


@dataclass(frozen=True)
class Dispositivo:
    """Una placa registrada en fabrica. Existe tenga o no dueno."""

    codigo: CodigoSensor
    hash_reclamo: str


@dataclass(frozen=True)
class Sensor:
    """Una placa vinculada a una planta. Existe solo mientras esta vinculada.

    clave_confirmada dice si la placa ya canjeo la clave que se le entrego por su
    canal: mientras no, cada vez que se conecta recibe una clave nueva.
    """

    codigo: CodigoSensor
    planta_id: int
    hash_clave: str
    clave_confirmada: bool = False
