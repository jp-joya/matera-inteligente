"""Casos de uso de las plantas de un usuario: registrar, listar y eliminar (seccion 5.3)."""
from dominio import puertos_entrada as entrada
from dominio.errores import EspecieNoSoportada
from dominio.errores_usuarios import ApodoRepetido, PlantaNoEncontrada
from dominio.plantas import Planta
from dominio.puertos import ConsultaRangos
from dominio.puertos_usuarios import (
    AvisosDispositivos,
    ConsultaPlantas,
    HistorialMediciones,
    RegistroPlantas,
    SensoresVinculados,
)
from dominio.resultados import PlantaConEstado
from dominio.usuarios import Usuario


def buscar_planta_propia(plantas: ConsultaPlantas, usuario_id: int, planta_id: int) -> Planta:
    """La planta, si es de ese usuario. El puerto la busca; quien puede verla lo decide el dominio."""
    planta = plantas.por_id(planta_id)
    if planta is None:
        raise PlantaNoEncontrada(planta_id)
    planta.exigir_dueno(usuario_id)
    return planta


class RegistrarPlanta(entrada.RegistrarPlanta):
    """La matera se crea sin sensor: el sensor se le vincula despues, al reclamar la placa con su QR.

    Usa el mismo puerto ConsultaRangos del corte 1 para saber si la especie existe.
    """

    def __init__(self, plantas: ConsultaPlantas, registro: RegistroPlantas, especies: ConsultaRangos):
        self._plantas = plantas
        self._registro = registro
        self._especies = especies

    def ejecutar(self, usuario: Usuario, apodo: str, especie: str) -> Planta:
        nueva = Planta(None, usuario.id, apodo, especie)
        if self._especies.rangos_de(nueva.especie) is None:
            raise EspecieNoSoportada(especie)
        if any(propia.mismo_apodo(nueva.apodo) for propia in self._plantas.de_usuario(usuario.id)):
            raise ApodoRepetido(nueva.apodo)
        return self._registro.guardar(nueva)


class ListarPlantas(entrada.ListarPlantas):
    def __init__(self, plantas: ConsultaPlantas, sensores: SensoresVinculados, historial: HistorialMediciones):
        self._plantas = plantas
        self._sensores = sensores
        self._historial = historial

    def ejecutar(self, usuario: Usuario) -> list[PlantaConEstado]:
        return [
            PlantaConEstado(planta, self._sensores.de_planta(planta.id), self._historial.ultimo_de(planta.id))
            for planta in self._plantas.de_usuario(usuario.id)
        ]


class EliminarPlanta(entrada.EliminarPlanta):
    """Borra la planta con su historial. Su sensor queda libre para otra matera, y se le avisa a la placa."""

    def __init__(
        self,
        plantas: ConsultaPlantas,
        registro: RegistroPlantas,
        sensores: SensoresVinculados,
        avisos: AvisosDispositivos,
    ):
        self._plantas = plantas
        self._registro = registro
        self._sensores = sensores
        self._avisos = avisos

    def ejecutar(self, usuario: Usuario, planta_id: int) -> None:
        planta = buscar_planta_propia(self._plantas, usuario.id, planta_id)
        sensor = self._sensores.de_planta(planta.id)
        self._registro.eliminar(planta)
        if sensor is not None:
            self._avisos.desvinculado(sensor.codigo)
