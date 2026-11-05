// Matera inteligente: firmware del sensor, para la ESP32 con DHT11 y modulo LDR (DevKit con CH340) en el Arduino IDE 2.
//
// En la placa no se graba ninguna clave. Su codigo es su MAC, que viene de fabrica en el chip, y la clave
// le llega de la API cuando su dueno la reclama con el QR de su etiqueta. Habla con la API igual que el
// simulador (simulador/simulador_esp32.py):
//   0. Canal en vivo (WebSocket), abierto todo el tiempo, solo con su MAC:
//        ws://{API}/dispositivos/canal?codigo=<MAC>
//      Sin dueno, espera. Al reclamarla recibe {"tipo":"vinculado","clave":"..."}: la guarda en su memoria
//      (NVS) y la canjea. Si la quitan de su matera recibe {"tipo":"desvinculado"} y la borra.
//   1. Canje: cambia la clave por un token de alcance «lecturas», que vence a las 24 horas. El primer
//      canje tambien le confirma a la API que la placa guardo la clave.
//        POST {API_BASE}/dispositivos/<MAC>/token     {"clave":"..."}
//        -> {"token":"<JWT>","venceEn":"..."}
//   2. Cada INTERVALO_MS, una lectura con ese token y la hora en que se tomo:
//        POST {API_BASE}/dispositivos/<MAC>/lecturas
//        Authorization: Bearer <JWT>
//        {"dispositivo":"A4CF12345678","momento":"2026-10-22T15:04:05Z","lecturas":{"humedad":53.0,"luz":1500,"temperatura":25.5}}
// Si la API responde que el token ya no sirve (vencio, o lo reemplazo otro canje), vuelve a canjear la clave.
// La API diagnostica, guarda la lectura y la muestra en Mis materas. Aqui no hay reglas: el estado y las
// recomendaciones los calcula el backend.
//
// Sensores de esta placa:
//   DHT11 en GPIO26: temperatura y humedad DEL AIRE. La API espera la humedad del sustrato; para la demo se
//     manda la del aire, y se avisa en la presentacion.
//   Modulo LDR (LM393) en GPIO18: solo dice si hay luz o no. Se manda en lux, con LUX_CON_LUZ o LUX_SIN_LUZ.
// La hora sale de NTP: sin hora no se envia nada. La red y la API van en config.h (copia de config.example.h).
// Boton BOOT apretado 5 s: borra la clave guardada; para volver a medir, el dueno la reclama otra vez.

#include <HTTPClient.h>
#include <Preferences.h>
#include <WebSocketsClient.h>  // WebSockets, de Markus Sattler (Links2004)
#include <WiFi.h>
#include <esp_task_wdt.h>
#include <time.h>

#include <DHT.h>  // DHT sensor library, de Adafruit

#if __has_include("config.h")
#include "config.h"
#else
#error "Falta config.h: copie config.example.h como config.h, en esta misma carpeta, y complete sus datos."
#endif

// Lo que config.h puede omitir: los valores de la demo.
#ifndef INTERVALO_MS
#define INTERVALO_MS 15000
#endif
#ifndef LDR_CON_LUZ
#define LDR_CON_LUZ LOW  // el LM393 baja su salida cuando hay mas luz que la que marca su perilla
#endif
#ifndef LUX_CON_LUZ
#define LUX_CON_LUZ 1500
#endif
#ifndef LUX_SIN_LUZ
#define LUX_SIN_LUZ 50
#endif

// ---- pines de esta placa ----
const int PIN_DHT = 26;
const int PIN_LUZ = 18;  // la salida digital (DO) del modulo LDR
const int PIN_LED = 2;   // el LED azul de la placa
const int PIN_BOOT = 0;  // el boton BOOT: apretado 5 s borra la clave guardada

// ---- tiempos ----
const unsigned long ESPERA_WIFI_MS = 20000;  // cuanto se intenta conectar antes de rendirse un rato
const unsigned long ESPERA_HORA_MS = 15000;  // cuanto se espera la hora de NTP
const unsigned long REINTENTO_MS = 30000;    // si falla el WiFi, la hora, el sensor o el envio
// Si algo traba el programa mas de este tiempo, la placa se reinicia sola. Tiene margen sobre las
// esperas normales: el WiFi (20 s), la hora (15 s) y una peticion HTTP (15 s).
const int SEGUNDOS_VIGILANCIA = 60;
const unsigned long REINTENTO_CANAL_MS = 10000;  // si el canal se cae, se vuelve a abrir cada 10 s
const unsigned long BOTON_BORRAR_MS = 5000;
// Cualquier hora anterior a esta (2024-01-01) es la de una placa que todavia no sincronizo con NTP.
const time_t HORA_MINIMA = 1704067200;

// Lo que paso con una lectura. Va aqui arriba: el IDE pone los prototipos de las funciones antes que el resto.
enum Resultado { ENVIADA, TOKEN_VENCIDO, FALLO };

DHT dht(PIN_DHT, DHT11);
String token;  // el JWT del ultimo canje; vacio hasta canjear
WebSocketsClient canal;
Preferences memoria;   // la NVS: sobrevive a apagar la placa y a subir otra vez el programa
String codigoSensor;   // la MAC sin separadores, por ejemplo A4CF12345678
String clave;          // la que le llego por el canal; vacia mientras nadie la reclame
unsigned long proximoEnvio = 0;

// ---- LED: parpadeo lento = conectando al WiFi, destello cada 2 s = esperando dueño,
//      fijo = vinculada, 5 parpadeos rapidos = algo fallo ----

void led(bool encendido) {
  digitalWrite(PIN_LED, encendido ? HIGH : LOW);
}

void avisarFallo() {
  for (int i = 0; i < 5; i++) {
    led(true);
    delay(100);
    led(false);
    delay(100);
  }
  ledDeEstado();
}

// Entre envios: fijo si tiene su clave; un destello cada 2 s si espera dueño; apagado sin WiFi.
void ledDeEstado() {
  if (WiFi.status() != WL_CONNECTED) {
    led(false);
  } else if (clave.length() > 0) {
    led(true);
  } else {
    led(millis() % 2000 < 100);
  }
}

// ---- red ----

const char *motivoWifi(wl_status_t estado) {
  switch (estado) {
    case WL_NO_SSID_AVAIL:
      return "no ve la red: revise WIFI_SSID y que sea de 2,4 GHz";
    case WL_CONNECT_FAILED:
      return "la red rechazó la conexión: revise WIFI_CLAVE";
    case WL_CONNECTION_LOST:
      return "se perdió la conexión";
    case WL_DISCONNECTED:
      return "la red no la dejó entrar: suele ser WIFI_CLAVE o el límite de dispositivos del hotspot";
    default:
      return "estado desconocido";
  }
}

// Las redes que ve la placa: sirve para saber si el hotspot esta al alcance y en 2,4 GHz.
void mostrarRedes() {
  esp_task_wdt_reset();
  int cuantas = WiFi.scanNetworks();
  Serial.printf("La placa ve %d redes:\n", cuantas);
  for (int i = 0; i < cuantas && i < 10; i++) {
    Serial.printf("  «%s», canal %d, señal %d dBm\n", WiFi.SSID(i).c_str(), WiFi.channel(i), WiFi.RSSI(i));
  }
  WiFi.scanDelete();
}

bool conectarWifi() {
  if (WiFi.status() == WL_CONNECTED) return true;
  Serial.printf("Conectando al WiFi «%s»", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_CLAVE);
  bool encendido = false;
  unsigned long inicio = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - inicio < ESPERA_WIFI_MS) {
    esp_task_wdt_reset();
    encendido = !encendido;
    led(encendido);
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() != WL_CONNECTED) {
    Serial.printf("No se pudo conectar: %s.\n", motivoWifi(WiFi.status()));
    WiFi.disconnect();
    mostrarRedes();
    led(false);
    return false;
  }
  Serial.printf("Conectado. IP de la placa: %s\n", WiFi.localIP().toString().c_str());
  led(true);
  return true;
}

// La hora en UTC por NTP. La API rechaza una lectura sin zona horaria o del futuro, asi que sin hora no se envia.
bool sincronizarHora() {
  if (time(nullptr) >= HORA_MINIMA) return true;
  Serial.print("Pidiendo la hora por NTP");
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  unsigned long inicio = millis();
  while (time(nullptr) < HORA_MINIMA && millis() - inicio < ESPERA_HORA_MS) {
    esp_task_wdt_reset();
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  if (time(nullptr) < HORA_MINIMA) {
    Serial.println("No llegó la hora: la red tiene que dar salida a internet (puerto 123 de NTP).");
    return false;
  }
  Serial.printf("Hora sincronizada: %s\n", momentoAhora().c_str());
  return true;
}

// Por ejemplo 2026-10-22T15:04:05Z, como lo pide la API.
String momentoAhora() {
  time_t ahora = time(nullptr);
  struct tm utc;
  gmtime_r(&ahora, &utc);
  char texto[25];
  strftime(texto, sizeof texto, "%Y-%m-%dT%H:%M:%SZ", &utc);
  return String(texto);
}

void prepararPeticion(HTTPClient &http, const String &url) {
  http.setConnectTimeout(5000);
  http.setTimeout(10000);
  http.begin(url);
  http.addHeader("Content-Type", "application/json");  // sin ella, la API responde 400
}

// El texto de un campo de un JSON de la API, buscado despues de `desde`: un texto entre comillas, o
// true, false o un numero. Sin libreria de JSON: la clave y el token no llevan comillas, y los mensajes
// de la API son cortos y conocidos. "" si no esta.
String campoDespues(const String &json, const char *desde, const char *nombre) {
  int pos = json.indexOf(desde);
  if (pos >= 0) pos = json.indexOf(String("\"") + nombre + "\"", pos);
  if (pos >= 0) pos = json.indexOf(':', pos);
  if (pos < 0) return String();
  int inicio = pos + 1;
  while (inicio < (int)json.length() && json[inicio] == ' ') inicio++;
  if (inicio >= (int)json.length()) return String();
  if (json[inicio] == '"') {
    int fin = json.indexOf('"', inicio + 1);
    return fin > inicio ? json.substring(inicio + 1, fin) : String();
  }
  int fin = inicio;
  while (fin < (int)json.length() && json[fin] != ',' && json[fin] != '}') fin++;
  return json.substring(inicio, fin);
}

String campo(const String &json, const char *nombre) {
  return campoDespues(json, "{", nombre);
}

// ---- la clave, en la memoria de la placa ----

// La MAC del WiFi, sin los dos puntos. Es unica y viene de fabrica en el chip: no hay que grabarla.
String leerCodigoSensor() {
  String mac = WiFi.macAddress();
  mac.replace(":", "");
  mac.toUpperCase();
  return mac;
}

void guardarClave(const String &nueva) {
  clave = nueva;
  token = "";  // la clave nueva deja sin efecto el token anterior: hay que canjearla
  memoria.putString("clave", clave);
}

void borrarClave() {
  token = "";
  if (clave.length() == 0) return;
  clave = "";
  memoria.remove("clave");
  Serial.println("Clave borrada: la placa espera que su dueño la reclame con el QR.");
}

void revisarBotonBorrar() {
  static unsigned long apretadoDesde = 0;
  if (digitalRead(PIN_BOOT) == HIGH) {
    apretadoDesde = 0;
    return;
  }
  if (apretadoDesde == 0) apretadoDesde = millis();
  if (millis() - apretadoDesde >= BOTON_BORRAR_MS) {
    Serial.println("BOOT apretado 5 s.");
    borrarClave();
    apretadoDesde = millis();  // no repetir mientras siga apretado
  }
}

// ---- canal en vivo (WebSocket) ----

void atenderMensaje(const String &texto) {
  String tipo = campo(texto, "tipo");
  String apodo = campoDespues(texto, "\"planta\"", "apodo");
  if (tipo == "estado" && campo(texto, "vinculado") != "true") {
    borrarClave();
    Serial.printf("Sin dueño. Reclámela desde el front con el QR de su etiqueta (código %s).\n", codigoSensor.c_str());
  } else if (tipo == "estado") {
    if (clave.length() > 0) {
      Serial.printf("Vinculada a «%s».\n", apodo.c_str());
    } else {
      Serial.printf("Vinculada a «%s», pero sin su clave: reclámela otra vez para esa matera.\n", apodo.c_str());
    }
  } else if (tipo == "vinculado") {
    String nueva = campo(texto, "clave");
    if (nueva.length() == 0) return;
    guardarClave(nueva);
    Serial.printf("Vinculada a «%s»: clave recibida y guardada. Se canjea por el token.\n", apodo.c_str());
    proximoEnvio = millis();  // el canje y la primera lectura salen enseguida
  } else if (tipo == "desvinculado") {
    Serial.println("La quitaron de su matera.");
    borrarClave();
  } else if (tipo == "error") {
    String error = campo(texto, "error");
    Serial.printf("La API cerró el canal: %s. %s\n", error.c_str(), campo(texto, "mensaje").c_str());
    if (error == "DISPOSITIVO_NO_REGISTRADO") {
      Serial.printf("Registre la placa en fábrica: python fabrica/fabricar_dispositivo.py %s --correo <admin>\n",
                    codigoSensor.c_str());
    }
  }
}

void eventoCanal(WStype_t tipo, uint8_t *datos, size_t largo) {
  if (tipo == WStype_CONNECTED) {
    Serial.println("Canal en vivo abierto.");
  } else if (tipo == WStype_DISCONNECTED) {
    Serial.println("Canal en vivo cerrado: se vuelve a abrir solo.");
  } else if (tipo == WStype_TEXT) {
    atenderMensaje(String((const char *)datos).substring(0, largo));
  }
}

// API_BASE es "http://<host>:<puerto>/api/v1": de ahi salen el host, el puerto y la ruta del canal.
void abrirCanal() {
  String base = API_BASE;
  base.replace("http://", "");
  int barra = base.indexOf('/');
  String hostPuerto = barra >= 0 ? base.substring(0, barra) : base;
  String prefijo = barra >= 0 ? base.substring(barra) : String();
  int dosPuntos = hostPuerto.indexOf(':');
  String host = dosPuntos >= 0 ? hostPuerto.substring(0, dosPuntos) : hostPuerto;
  uint16_t puerto = dosPuntos >= 0 ? hostPuerto.substring(dosPuntos + 1).toInt() : 80;

  canal.begin(host, puerto, prefijo + "/dispositivos/canal?codigo=" + codigoSensor, "");
  canal.onEvent(eventoCanal);
  canal.setReconnectInterval(REINTENTO_CANAL_MS);
  canal.enableHeartbeat(15000, 3000, 2);  // detecta un servidor que se fue sin cerrar
}

// ---- canje de la clave por el token ----

bool canjearClave() {
  HTTPClient http;
  prepararPeticion(http, String(API_BASE) + "/dispositivos/" + codigoSensor + "/token");
  int estado = http.POST(String("{\"clave\":\"") + clave + "\"}");
  String respuesta = estado > 0 ? http.getString() : String();
  http.end();
  if (estado == 200) {
    token = campo(respuesta, "token");
    if (token.length() == 0) {
      token = "";
      Serial.println("Canje: la API respondió 200 pero sin token.");
      return false;
    }
    Serial.printf("Canje listo: token del sensor hasta %s.\n", campo(respuesta, "venceEn").c_str());
    return true;
  }
  token = "";
  if (estado < 0) {
    Serial.printf("No se pudo contactar la API en %s (%s).\n", API_BASE, HTTPClient::errorToString(estado).c_str());
    Serial.println("Revise API_BASE, que el backend corra con MATERA_HOST=0.0.0.0 y que el firewall deje pasar el puerto.");
  } else if (estado == 401) {
    Serial.println("401: la clave ya no sirve (la quitaron de su matera o la reclamaron otra vez).");
    borrarClave();
    canal.disconnect();  // al volver a abrir el canal, la API dice si tiene dueño y, si hace falta, da otra clave
  } else {
    Serial.printf("Canje: %d %s\n", estado, respuesta.c_str());
  }
  return false;
}

// ---- sensores ----

// El DHT11 da temperatura y humedad del aire. La libreria no lo deja leer mas de una vez cada 2 s.
bool leerAire(float &humedad, float &temperatura) {
  humedad = dht.readHumidity();
  temperatura = dht.readTemperature();
  if (isnan(humedad) || isnan(temperatura)) {
    Serial.printf("El DHT11 no respondió: revise que esté en GPIO%d y alimentado con 3V3.\n", PIN_DHT);
    return false;
  }
  return true;
}

// El modulo LDR solo dice si hay luz o no; se manda en lux con dos valores fijos.
int leerLuz() {
  bool hayLuz = digitalRead(PIN_LUZ) == LDR_CON_LUZ;
  return hayLuz ? LUX_CON_LUZ : LUX_SIN_LUZ;
}

// ---- envio ----

Resultado enviarLectura(float humedad, int lux, float temperatura) {
  String cuerpo = String("{\"dispositivo\":\"") + codigoSensor + "\",\"momento\":\"" + momentoAhora() +
                  "\",\"lecturas\":{\"humedad\":" + String(humedad, 1) + ",\"luz\":" + String(lux) +
                  ",\"temperatura\":" + String(temperatura, 1) + "}}";
  HTTPClient http;
  prepararPeticion(http, String(API_BASE) + "/dispositivos/" + codigoSensor + "/lecturas");
  http.addHeader("Authorization", String("Bearer ") + token);
  int estado = http.POST(cuerpo);
  String respuesta = estado > 0 ? http.getString() : String();
  http.end();

  Serial.printf("Enviado %s -> ", cuerpo.c_str());
  if (estado == 201) {
    Serial.printf("201, la planta está %s.\n", campoDespues(respuesta, "\"diagnostico\"", "estado").c_str());
    return ENVIADA;
  }
  if (estado < 0) {
    Serial.printf("sin respuesta (%s). Revise la red y API_BASE.\n", HTTPClient::errorToString(estado).c_str());
    return FALLO;
  }
  String error = campo(respuesta, "error");
  if (estado == 401 && error == "CREDENCIAL_DISPOSITIVO_INVALIDA") {
    Serial.println("401: el token venció o lo reemplazó otro canje. Se canjea la clave otra vez.");
    return TOKEN_VENCIDO;
  }
  Serial.printf("%d %s: %s\n", estado, error.c_str(), campo(respuesta, "mensaje").c_str());
  return FALLO;
}

// ---- programa ----

void setup() {
  Serial.begin(115200);
  delay(500);
  pinMode(PIN_LED, OUTPUT);
  pinMode(PIN_LUZ, INPUT);
  pinMode(PIN_BOOT, INPUT_PULLUP);
  led(false);
  dht.begin();
  esp_task_wdt_init(SEGUNDOS_VIGILANCIA, true);  // reinicia la placa si loop() queda trabado
  esp_task_wdt_add(NULL);

  WiFi.mode(WIFI_STA);  // la MAC se lee con el WiFi encendido
  codigoSensor = leerCodigoSensor();
  memoria.begin("matera", false);
  clave = memoria.getString("clave", "");

  Serial.println();
  Serial.println("Matera inteligente · sensor");
  Serial.printf("Codigo del sensor (MAC): %s\n", codigoSensor.c_str());
  Serial.println("API: " API_BASE);
  Serial.println(clave.length() > 0 ? "Tiene una clave guardada." : "Sin clave: espera que su dueño la reclame.");
  // La primera lectura del DHT11 despues de encenderlo suele salir mal: se descarta.
  delay(2000);
  dht.readTemperature();
  // El canje dice si llega al backend; el canal le dice si tiene dueño.
  conectarWifi();
  abrirCanal();
}

void loop() {
  esp_task_wdt_reset();
  canal.loop();  // atiende el canal en vivo: mensajes, reconexion y latidos
  revisarBotonBorrar();
  // Resta con signo: sigue funcionando cuando millis() da la vuelta, a los 49 dias.
  if ((long)(millis() - proximoEnvio) < 0) {
    ledDeEstado();
    delay(20);
    return;
  }
  proximoEnvio = millis() + REINTENTO_MS;  // si algo falla abajo, se reintenta en REINTENTO_MS
  if (!conectarWifi() || !sincronizarHora()) {
    avisarFallo();
    return;
  }
  if (clave.length() == 0) {
    // Sin dueño no hay nada que canjear: se espera en el canal a que la reclamen.
    proximoEnvio = millis() + 1000;
    return;
  }
  if (token.length() == 0 && !canjearClave()) {
    avisarFallo();
    return;
  }

  float humedad, temperatura;
  if (!leerAire(humedad, temperatura)) {
    avisarFallo();
    return;
  }
  int lux = leerLuz();

  Resultado resultado = enviarLectura(humedad, lux, temperatura);
  if (resultado == TOKEN_VENCIDO && canjearClave()) resultado = enviarLectura(humedad, lux, temperatura);
  if (resultado != ENVIADA) {
    if (resultado == TOKEN_VENCIDO) token = "";
    avisarFallo();
    return;
  }
  proximoEnvio = millis() + (unsigned long)INTERVALO_MS;
}
