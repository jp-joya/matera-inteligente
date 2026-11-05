"""Casos de uso del sensor: fabrica, reclamo con el QR, su canal en vivo, el canje de su clave y sus lecturas.

El recorrido completo, sin ninguna clave grabada en la placa:

1. Fabrica (un admin): RegistrarDispositivo guarda la MAC de la placa y la
   huella de un codigo de reclamo, que va impreso en el QR de su etiqueta.
2. La placa enciende y abre su canal en vivo solo con su MAC:
   AtenderDispositivo le dice si tiene dueno.
3. El dueno escanea el QR: ReclamarDispositivo comprueba el codigo, vincula la
   placa a su matera y genera la clave. Si la placa esta conectada, la recibe
   ahi mismo; si no, AtenderDispositivo le da otra al conectarse.
4. La placa guarda la clave y la canjea por su token (CanjearClaveDispositivo),
   igual que antes. El primer canje confirma la clave: desde ahi, conectarse ya
   no entrega otra.
5. Quitar el sensor (QuitarSensor) o borrar la matera deja sin efecto su clave
   y su token, y se le avisa a la placa por su canal.
"""
from datetime import timedelta

from aplicacion.diagnosticar_planta_de_usuario import DiagnosticarPlantaDeUsuario
from aplicacion.plantas import buscar_planta_propia
from dominio import puertos_entrada as entrada
from dominio.errores_usuarios import (
    ClaveSensorInvalida,
    CodigoReclamoInvalido,
    CodigoSensorInvalido,
    CredencialDispositivoInvalida,
    DispositivoAjeno,
    DispositivoNoRegistrado,
    PlantaSinSensor,
    SensorYaVinculado,
)
from dominio.identidad import IdentidadDispositivo
from dominio.puertos_usuarios import (
    AvisosDispositivos,
    ConsultaCuentas,
    ConsultaPlantas,
    CredencialesSensor,
    EmisorTokens,
    GeneradorSecretos,
    RegistroDispositivos,
    Reloj,
    SensoresVinculados,
)
from dominio.reportes import ReporteDeDispositivo
from dominio.resultados import (
    DispositivoRegistrado,
    EstadoDispositivo,
    ResultadoMedicion,
    ResultadoReclamo,
    TokenDispositivo,
)
from dominio.sensores import CodigoSensor, Dispositivo, Sensor
from dominio.usuarios import Usuario


class RegistrarDispositivo(entrada.RegistrarDispositivo):
    """El paso de fabrica. Solo un ADMIN lo hace."""

    def __init__(self, dispositivos: RegistroDispositivos, secretos: GeneradorSecretos):
        self._dispositivos = dispositivos
        self._secretos = secretos

    def ejecutar(self, admin: Usuario, codigo: str) -> DispositivoRegistrado:
        admin.exigir_admin()
        codigo_valido = CodigoSensor(codigo)
        codigo_reclamo = self._secretos.nuevo()
        self._dispositivos.registrar(Dispositivo(codigo_valido, self._secretos.huella(codigo_reclamo)))
        return DispositivoRegistrado(codigo_valido, codigo_reclamo)


class ReclamarDispositivo(entrada.ReclamarDispositivo):
    """El dueno escanea el QR de la etiqueta y elige la matera.

    Reclamar otra vez la misma placa para la misma matera le da una clave nueva:
    sirve si la placa perdio la suya. La clave nunca sale en la respuesta: viaja
    solo por el canal de la placa.
    """

    def __init__(
        self,
        dispositivos: RegistroDispositivos,
        plantas: ConsultaPlantas,
        sensores: SensoresVinculados,
        secretos: GeneradorSecretos,
        avisos: AvisosDispositivos,
    ):
        self._dispositivos = dispositivos
        self._plantas = plantas
        self._sensores = sensores
        self._secretos = secretos
        self._avisos = avisos

    def ejecutar(self, usuario: Usuario, codigo: str, codigo_reclamo: str, planta_id: int) -> ResultadoReclamo:
        # El mismo error si el codigo esta mal escrito, si la placa no existe o si el codigo de reclamo no coincide.
        try:
            codigo_valido = CodigoSensor(codigo)
        except CodigoSensorInvalido:
            raise CodigoReclamoInvalido() from None
        dispositivo = self._dispositivos.por_codigo(codigo_valido)
        if dispositivo is None or not self._secretos.coincide(codigo_reclamo, dispositivo.hash_reclamo):
            raise CodigoReclamoInvalido()

        planta = buscar_planta_propia(self._plantas, usuario.id, planta_id)
        en_uso = self._sensores.por_codigo(codigo_valido)
        if en_uso is not None and en_uso.planta_id != planta.id:
            raise SensorYaVinculado(codigo_valido.valor)
        anterior = self._sensores.de_planta(planta.id)

        clave = self._secretos.nuevo()
        self._sensores.reemplazar(planta, Sensor(codigo_valido, planta.id, self._secretos.huella(clave)))
        if anterior is not None and anterior.codigo != codigo_valido:
            self._avisos.desvinculado(anterior.codigo)
        entregada = self._avisos.conectado(codigo_valido) and self._avisos.vinculado(codigo_valido, planta, clave)
        return ResultadoReclamo(planta, codigo_valido, entregada)


class AtenderDispositivo(entrada.AtenderDispositivo):
    """La placa abrio su canal en vivo. Solo dice su MAC: no tiene ningun secreto.

    Si esta vinculada y todavia no canjeo su clave, recibe una nueva: la anterior,
    si alguna vez le llego, deja de servir.
    """

    def __init__(
        self,
        dispositivos: RegistroDispositivos,
        sensores: SensoresVinculados,
        plantas: ConsultaPlantas,
        secretos: GeneradorSecretos,
    ):
        self._dispositivos = dispositivos
        self._sensores = sensores
        self._plantas = plantas
        self._secretos = secretos

    def ejecutar(self, codigo: CodigoSensor) -> EstadoDispositivo:
        if self._dispositivos.por_codigo(codigo) is None:
            raise DispositivoNoRegistrado(codigo.valor)
        sensor = self._sensores.por_codigo(codigo)
        planta = self._plantas.por_id(sensor.planta_id) if sensor is not None else None
        if planta is None:
            return EstadoDispositivo(None)
        if sensor.clave_confirmada:
            return EstadoDispositivo(planta)
        clave = self._secretos.nuevo()
        if not self._sensores.nueva_clave(sensor, self._secretos.huella(clave)):
            return EstadoDispositivo(None)  # mientras tanto la quitaron o la reclamaron otra vez
        return EstadoDispositivo(planta, clave)


class QuitarSensor(entrada.QuitarSensor):
    """Suelta el sensor de la matera. El historial se conserva; la placa queda libre para otra matera."""

    def __init__(self, plantas: ConsultaPlantas, sensores: SensoresVinculados, avisos: AvisosDispositivos):
        self._plantas = plantas
        self._sensores = sensores
        self._avisos = avisos

    def ejecutar(self, usuario: Usuario, planta_id: int) -> CodigoSensor:
        planta = buscar_planta_propia(self._plantas, usuario.id, planta_id)
        sensor = self._sensores.de_planta(planta.id)
        if sensor is None:
            raise PlantaSinSensor(planta.id)
        self._sensores.quitar(planta)
        self._avisos.desvinculado(sensor.codigo)
        return sensor.codigo


class CanjearClaveDispositivo(entrada.CanjearClaveDispositivo):
    """Aqui se emite el token del dispositivo: la placa cambia su clave por un token de alcance «lecturas».

    La clave le llega a la placa por su canal en vivo, al reclamarla, y solo sirve
    para esto. El canje tambien la confirma: la placa ya la guardo.
    Cada canje deja vigente solo el token nuevo, y cambiar el sensor de la planta
    deja sin efecto la clave y el token. Si el dueno de la planta esta desactivado,
    no hay canje.
    """

    def __init__(
        self,
        sensores: SensoresVinculados,
        credenciales: CredencialesSensor,
        plantas: ConsultaPlantas,
        cuentas: ConsultaCuentas,
        secretos: GeneradorSecretos,
        emisor: EmisorTokens,
        reloj: Reloj,
        duracion: timedelta,
    ):
        self._sensores = sensores
        self._credenciales = credenciales
        self._plantas = plantas
        self._cuentas = cuentas
        self._secretos = secretos
        self._emisor = emisor
        self._reloj = reloj
        self._duracion = duracion

    def ejecutar(self, codigo: str, clave: str) -> TokenDispositivo:
        # El mismo error si el codigo esta mal escrito, si no existe o si la clave no coincide.
        try:
            codigo_valido = CodigoSensor(codigo)
        except CodigoSensorInvalido:
            raise ClaveSensorInvalida() from None
        sensor = self._sensores.por_codigo(codigo_valido)
        if sensor is None or not self._secretos.coincide(clave, sensor.hash_clave):
            raise ClaveSensorInvalida()
        # Solo despues de verificar la clave se dice que la cuenta esta desactivada, como al iniciar sesion.
        planta = self._plantas.por_id(sensor.planta_id)
        dueno = self._cuentas.por_id(planta.usuario_id) if planta is not None else None
        if dueno is None:
            raise ClaveSensorInvalida()
        dueno.exigir_activo()
        token_id = self._secretos.nuevo()
        # Solo si el sensor sigue siendo el que se verifico: si mientras tanto se cambio, la clave ya no vale.
        if not self._credenciales.renovar(sensor, token_id):
            raise ClaveSensorInvalida()
        vence_en = self._reloj.ahora() + self._duracion
        return TokenDispositivo(self._emisor.emitir(IdentidadDispositivo(codigo_valido, token_id), vence_en), vence_en)


class RegistrarLectura(entrada.RegistrarLectura):
    """R2: un dispositivo reporta una lectura de su planta.

    Recibe la identidad que el adaptador saco del token ya verificado, nunca el
    token. No inventa un diagnostico propio: le pasa la medicion a
    DiagnosticarPlantaDeUsuario, con el dueno de la planta, la hora del reporte y
    el codigo del sensor como origen. Si el dueno de la planta esta desactivado,
    la lectura no se guarda, aunque el token siga vigente.
    """

    def __init__(
        self,
        credenciales: CredencialesSensor,
        cuentas: ConsultaCuentas,
        diagnosticar: DiagnosticarPlantaDeUsuario,
        reloj: Reloj,
    ):
        self._credenciales = credenciales
        self._cuentas = cuentas
        self._diagnosticar = diagnosticar
        self._reloj = reloj

    def ejecutar(self, identidad: IdentidadDispositivo, reporte: ReporteDeDispositivo) -> ResultadoMedicion:
        planta = self._credenciales.planta_con_token(identidad.codigo, identidad.token_id)
        if planta is None:
            raise CredencialDispositivoInvalida()  # otro canje o un cambio de sensor lo dejo sin efecto
        dueno = self._cuentas.por_id(planta.usuario_id)
        if dueno is None:
            raise CredencialDispositivoInvalida()
        dueno.exigir_activo()
        if reporte.dispositivo != identidad.codigo:
            raise DispositivoAjeno(identidad.codigo.valor, reporte.dispositivo.valor)
        reporte.exigir_que_no_sea_futuro(self._reloj.ahora())
        return self._diagnosticar.ejecutar(
            planta.usuario_id, planta.id, reporte.medicion, origen=identidad.codigo.valor, momento=reporte.momento
        )
