// El front se sirve desde otro origen que el backend (RA2), por eso la URL va completa. La API corre en el
// mismo PC que sirve el front, en el puerto 5000: así funciona igual en este PC (localhost) y desde el
// celular que escanea el QR de un sensor (http://<IP del PC>:5500). Abierto como archivo, usa 127.0.0.1.
const PUERTO_API = 5000;
export const API_BASE = `http://${location.hostname || "127.0.0.1"}:${PUERTO_API}/api/v1`;
