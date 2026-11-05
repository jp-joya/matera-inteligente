"""Plantas, sensores y las placas registradas en fabrica, en SQLite.

Las consultas no deciden quien puede ver una planta: eso lo decide
Planta.exigir_dueno, en el dominio. Las escrituras si exigen que la fila siga
siendo la planta que se leyo (el mismo id y el mismo dueno), porque SQLite
reutiliza el id mas alto despues de borrarlo: sin eso, una planta borrada por
otra pestana podria confundirse con la planta nueva de otro usuario.

SQLite tampoco dice que restriccion fallo en un error de llave foranea. Por eso
las escrituras que dependen de otra fila la revisan dentro de la misma operacion.
"""
import sqlite3
from dataclasses import replace

from dominio.errores import EspecieNoSoportada
from dominio.errores_usuarios import ApodoRepetido, PlantaNoEncontrada, SensorYaVinculado, SesionInvalida
from dominio.plantas import Planta
from dominio.errores_usuarios import DispositivoYaRegistrado
from dominio.puertos_usuarios import (
    ConsultaPlantas,
    CredencialesSensor,
    RegistroDispositivos,
    RegistroPlantas,
    SensoresVinculados,
)
from dominio.sensores import CodigoSensor, Dispositivo, Sensor
from infraestructura.sqlite.base_datos import LA_MISMA_PLANTA, BaseDatos, cabe_en_sqlite

_COLUMNAS_PLANTA = "p.id, p.usuario_id, p.apodo, p.especie"
_COLUMNAS_SENSOR = "codigo, planta_id, hash_clave, clave_confirmada"
# La misma planta que se leyo: mismo id y mismo dueno.
def _planta(fila: sqlite3.Row) -> Planta:
    return Planta(fila["id"], fila["usuario_id"], fila["apodo"], fila["especie"])


class RepositorioPlantasSqlite(ConsultaPlantas, RegistroPlantas):
    def __init__(self, bd: BaseDatos):
        self._bd = bd

    def guardar(self, planta: Planta) -> Planta:
        try:
            with self._bd.conexion() as conexion:
                cursor = conexion.execute(
                    "INSERT INTO planta (usuario_id, especie, apodo) VALUES (?, ?, ?)",
                    (planta.usuario_id, planta.especie, planta.apodo),
                )
        except sqlite3.IntegrityError as error:
            # La base respalda las reglas que ya revisa RegistrarPlanta.
            mensaje = str(error)
            if "planta.usuario_id, planta.apodo" in mensaje:
                raise ApodoRepetido(planta.apodo) from None
            if "FOREIGN KEY" in mensaje:
                # Fallo la especie o el usuario: si la especie existe, a la cuenta la borraron
                # mientras se atendia la peticion.
                if self._especie_existe(planta.especie):
                    raise SesionInvalida() from None
                raise EspecieNoSoportada(planta.especie) from None
            raise
        return replace(planta, id=cursor.lastrowid)

    def _especie_existe(self, nombre: str) -> bool:
        with self._bd.conexion() as conexion:
            return conexion.execute("SELECT 1 FROM especie WHERE nombre = ?", (nombre,)).fetchone() is not None

    def de_usuario(self, usuario_id: int) -> list[Planta]:
        with self._bd.conexion() as conexion:
            filas = conexion.execute(
                f"SELECT {_COLUMNAS_PLANTA} FROM planta p WHERE p.usuario_id = ? ORDER BY p.id", (usuario_id,)
            ).fetchall()
        return [_planta(fila) for fila in filas]

    def por_id(self, planta_id: int) -> Planta | None:
        if not cabe_en_sqlite(planta_id):
            return None
        with self._bd.conexion() as conexion:
            fila = conexion.execute(f"SELECT {_COLUMNAS_PLANTA} FROM planta p WHERE p.id = ?", (planta_id,)).fetchone()
        return _planta(fila) if fila else None

    def eliminar(self, planta: Planta) -> None:
        # El historial y el vinculo con su sensor se borran en cascada: la placa queda libre.
        with self._bd.conexion() as conexion:
            conexion.execute("DELETE FROM planta WHERE id = ? AND usuario_id = ?", (planta.id, planta.usuario_id))


class RepositorioSensoresSqlite(SensoresVinculados, CredencialesSensor):
    def __init__(self, bd: BaseDatos):
        self._bd = bd

    def por_codigo(self, codigo: CodigoSensor) -> Sensor | None:
        return self._uno(f"SELECT {_COLUMNAS_SENSOR} FROM sensor WHERE codigo = ?", (codigo.valor,))

    def de_planta(self, planta_id: int) -> Sensor | None:
        return self._uno(f"SELECT {_COLUMNAS_SENSOR} FROM sensor WHERE planta_id = ?", (planta_id,))

    def reemplazar(self, planta: Planta, sensor: Sensor) -> None:
        # Una sola transaccion: si el codigo nuevo es de otra planta, esta conserva su sensor. Las dos
        # sentencias exigen que la planta siga siendo la que se leyo: otra pestana pudo borrarla, y su id
        # pudo pasar a la planta nueva de otro usuario.
        try:
            with self._bd.conexion() as conexion:
                conexion.execute(
                    f"DELETE FROM sensor WHERE planta_id IN ({LA_MISMA_PLANTA})", (planta.id, planta.usuario_id)
                )
                insertado = conexion.execute(
                    f"INSERT INTO sensor (codigo, planta_id, hash_clave) SELECT ?, id, ? FROM ({LA_MISMA_PLANTA})",
                    (sensor.codigo.valor, sensor.hash_clave, planta.id, planta.usuario_id),
                )
                if insertado.rowcount == 0:
                    raise PlantaNoEncontrada(planta.id)  # deshace el DELETE
        except sqlite3.IntegrityError as error:
            if "sensor.codigo" in str(error):
                raise SensorYaVinculado(sensor.codigo.valor) from None
            raise

    def quitar(self, planta: Planta) -> None:
        with self._bd.conexion() as conexion:
            conexion.execute(
                f"DELETE FROM sensor WHERE planta_id IN ({LA_MISMA_PLANTA})", (planta.id, planta.usuario_id)
            )

    def nueva_clave(self, sensor: Sensor, hash_clave: str) -> bool:
        # Codigo, planta y huella: si entre tanto se quito o se reclamo otra vez, la fila ya no coincide.
        with self._bd.conexion() as conexion:
            actualizado = conexion.execute(
                "UPDATE sensor SET hash_clave = ?, clave_confirmada = 0, token_id = NULL"
                " WHERE codigo = ? AND planta_id = ? AND hash_clave = ?",
                (hash_clave, sensor.codigo.valor, sensor.planta_id, sensor.hash_clave),
            )
        return actualizado.rowcount == 1

    def renovar(self, sensor: Sensor, token_id: str) -> bool:
        # Codigo, planta y huella: si entre tanto se cambio el sensor, la fila ya no coincide y no se toca.
        # El canje tambien confirma la clave: la placa la guardo.
        with self._bd.conexion() as conexion:
            actualizado = conexion.execute(
                "UPDATE sensor SET token_id = ?, clave_confirmada = 1 WHERE codigo = ? AND planta_id = ? AND hash_clave = ?",
                (token_id, sensor.codigo.valor, sensor.planta_id, sensor.hash_clave),
            )
        return actualizado.rowcount == 1

    def planta_con_token(self, codigo: CodigoSensor, token_id: str) -> Planta | None:
        with self._bd.conexion() as conexion:
            fila = conexion.execute(
                f"SELECT {_COLUMNAS_PLANTA} FROM planta p JOIN sensor s ON s.planta_id = p.id"
                " WHERE s.codigo = ? AND s.token_id = ?",
                (codigo.valor, token_id),
            ).fetchone()
        return _planta(fila) if fila else None

    def _uno(self, consulta: str, parametros: tuple) -> Sensor | None:
        with self._bd.conexion() as conexion:
            fila = conexion.execute(consulta, parametros).fetchone()
        if fila is None:
            return None
        return Sensor(
            CodigoSensor(fila["codigo"]), fila["planta_id"], fila["hash_clave"], bool(fila["clave_confirmada"])
        )


class RepositorioDispositivosSqlite(RegistroDispositivos):
    """Las placas registradas en fabrica. Una placa existe aqui tenga o no dueno."""

    def __init__(self, bd: BaseDatos):
        self._bd = bd

    def registrar(self, dispositivo: Dispositivo) -> None:
        try:
            with self._bd.conexion() as conexion:
                conexion.execute(
                    "INSERT INTO dispositivo (codigo, hash_reclamo) VALUES (?, ?)",
                    (dispositivo.codigo.valor, dispositivo.hash_reclamo),
                )
        except sqlite3.IntegrityError as error:
            if "dispositivo.codigo" in str(error):
                raise DispositivoYaRegistrado(dispositivo.codigo.valor) from None
            raise

    def por_codigo(self, codigo: CodigoSensor) -> Dispositivo | None:
        with self._bd.conexion() as conexion:
            fila = conexion.execute(
                "SELECT codigo, hash_reclamo FROM dispositivo WHERE codigo = ?", (codigo.valor,)
            ).fetchone()
        return Dispositivo(CodigoSensor(fila["codigo"]), fila["hash_reclamo"]) if fila else None
