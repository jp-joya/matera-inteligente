"""Fabrica: registra una placa en la API y genera la etiqueta con su QR.

Es el paso que en un producto real se hace en la fabrica, una vez por placa:

1. Se lee la MAC de la placa. El ESP32 la muestra por el monitor serie al
   encender («Codigo del sensor (MAC): A4CF12345678»).
2. Una cuenta ADMIN la registra (POST /api/v1/admin/dispositivos) y recibe el
   codigo de reclamo. La API solo guarda su huella: si se pierde la etiqueta,
   no se puede recuperar.
3. Se arma el enlace de reclamo, <front>/#vincular?d=<MAC>&c=<codigo>, y se
   guarda como QR en un SVG para imprimir y pegar en la caja de la matera.

En la placa no se graba nada: el codigo de reclamo vive solo en la etiqueta.
No importa nada del backend: le habla a la API por HTTP.

Uso, desde la raiz del repositorio:
    python fabrica/fabricar_dispositivo.py A4CF12345678 --correo admin@correo.co
    python fabrica/fabricar_dispositivo.py A4CF12345678 --correo admin@correo.co --front http://192.168.1.10:5500

La contrasena se pide en la consola sin mostrarla, o sale de MATERA_CONTRASENA_ADMIN.
Para el QR hace falta la libreria qrcode (pip install -r backend/requirements-dev.txt);
sin ella, la herramienta igual registra la placa y muestra el enlace.
"""
import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API_POR_DEFECTO = "http://127.0.0.1:5000/api/v1"
FRONT_POR_DEFECTO = "http://127.0.0.1:5500"
ETIQUETAS_POR_DEFECTO = Path(__file__).resolve().parent / "etiquetas"


def _argumentos(argv: list[str] | None) -> argparse.Namespace:
    lector = argparse.ArgumentParser(description="Registra una placa y genera la etiqueta con su QR de reclamo.")
    lector.add_argument("placa", help="MAC de la placa, como la muestra el monitor serie")
    lector.add_argument("--correo", required=True, help="correo de una cuenta ADMIN")
    lector.add_argument("--api", default=os.environ.get("MATERA_API", API_POR_DEFECTO), help="URL base de la API")
    lector.add_argument(
        "--front",
        default=os.environ.get("MATERA_FRONT", FRONT_POR_DEFECTO),
        help="URL del front que abrira el celular al escanear el QR (con la IP del PC, no 127.0.0.1)",
    )
    lector.add_argument("--etiquetas", type=Path, default=ETIQUETAS_POR_DEFECTO, help="carpeta para el SVG")
    return lector.parse_args(argv)


def _pedir(api: str, metodo: str, ruta: str, cuerpo: dict, token: str | None = None) -> tuple[int, dict]:
    cabeceras = {"Content-Type": "application/json"}
    if token:
        cabeceras["Authorization"] = f"Bearer {token}"
    peticion = urllib.request.Request(
        f"{api.rstrip('/')}{ruta}", data=json.dumps(cuerpo).encode(), headers=cabeceras, method=metodo
    )
    try:
        with urllib.request.urlopen(peticion, timeout=10) as respuesta:
            return respuesta.status, json.load(respuesta)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def enlace_de_reclamo(front: str, placa: str, codigo: str) -> str:
    return f"{front.rstrip('/')}/#vincular?d={placa}&c={codigo}"


def guardar_etiqueta(enlace: str, placa: str, carpeta: Path) -> Path | None:
    """El QR en un SVG. None si no esta instalada la libreria qrcode."""
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return None
    carpeta.mkdir(parents=True, exist_ok=True)
    archivo = carpeta / f"{placa}.svg"
    qrcode.make(enlace, image_factory=qrcode.image.svg.SvgPathImage, box_size=12).save(archivo)
    return archivo


def _mostrar_qr(enlace: str) -> None:
    try:
        import qrcode
    except ImportError:
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(enlace)
    qr.print_ascii(invert=True)


def main(argv: list[str] | None = None) -> int:
    args = _argumentos(argv)
    contrasena = os.environ.get("MATERA_CONTRASENA_ADMIN") or getpass.getpass(f"Contraseña de {args.correo}: ")
    try:
        estado, sesion = _pedir(args.api, "POST", "/sesiones", {"correo": args.correo, "contrasena": contrasena})
        if estado != 201:
            print(f"{estado} {sesion.get('error')}: {sesion.get('mensaje')}", file=sys.stderr)
            return 1
        estado, registrado = _pedir(args.api, "POST", "/admin/dispositivos", {"codigo": args.placa}, sesion["token"])
    except urllib.error.URLError as error:
        print(f"No se pudo contactar la API en {args.api}: {error.reason}", file=sys.stderr)
        return 2
    if estado != 201:
        print(f"{estado} {registrado.get('error')}: {registrado.get('mensaje')}", file=sys.stderr)
        return 1

    placa = registrado["codigo"]
    enlace = enlace_de_reclamo(args.front, placa, registrado["codigoReclamo"])
    print(f"Placa {placa} registrada. Enlace de reclamo (va en el QR de la etiqueta):\n{enlace}")
    archivo = guardar_etiqueta(enlace, placa, args.etiquetas)
    if archivo is None:
        print("Sin la librería qrcode no se generó el QR: guarde el enlace, no se puede volver a pedir.")
    else:
        _mostrar_qr(enlace)
        print(f"Etiqueta para imprimir: {archivo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
