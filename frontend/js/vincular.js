// Vincular un sensor: el dueño escanea el QR de la etiqueta (#vincular?d=<MAC>&c=<código>) y elige la matera.
// El front solo manda el código de reclamo. La clave de la placa nunca pasa por aquí: la API se la
// entrega a la placa por su propio canal en vivo, y la placa la canjea por su token.
import { obtenerPlantas, reclamarDispositivo, registrarPlanta } from "./api.js";
import { capitalizar } from "./formato.js";
import { botonDe, datosDe, mostrarError, mostrarExito, ocupado } from "./formularios.js";

const NUEVA = "nueva";

export function crearPantallaVincular(contexto) {
  const $ = (id) => document.getElementById(id);
  const formulario = $("form-vincular");
  let plantas = [];

  $("vincular-planta").addEventListener("change", pintarNueva);
  $("vincular-ver-materas").addEventListener("click", () => contexto.navegar("materas"));

  formulario.addEventListener("submit", async (evento) => {
    evento.preventDefault();
    const datos = datosDe(formulario);
    mostrarExito($("exito-vincular"), null);
    let resultado;
    try {
      resultado = await ocupado(botonDe(evento), async () => {
        const plantaId = await plantaElegida(datos);
        return reclamarDispositivo(datos.dispositivo.trim(), datos.codigo.trim(), plantaId);
      });
    } catch (error) {
      mostrarError($("error-vincular"), error);
      return;
    }
    mostrarError($("error-vincular"), null);
    const { sensor, planta, claveEntregada } = resultado;
    mostrarExito(
      $("exito-vincular"),
      claveEntregada
        ? `Listo: el sensor ${sensor.codigo} quedó en «${planta.apodo}» y ya recibió su clave.`
        : `El sensor ${sensor.codigo} quedó en «${planta.apodo}». Recibirá su clave apenas se encienda y se conecte al WiFi.`,
    );
  });

  // Una matera nueva se crea primero. Si después falla el reclamo, queda elegida en la lista: al
  // corregir el código y reintentar no se crea otra.
  async function plantaElegida(datos) {
    if (datos.planta !== NUEVA) return Number(datos.planta);
    const nueva = await registrarPlanta({ apodo: datos.apodo, especie: datos.especie });
    plantas.push(nueva);
    pintarPlantas(nueva.id);
    return nueva.id;
  }

  async function cargarPlantas(elegida) {
    try {
      plantas = await obtenerPlantas();
    } catch (error) {
      mostrarError($("error-vincular"), error);
      return;
    }
    // Sin materas, la única opción es crear una.
    pintarPlantas(elegida ?? plantas.find((p) => !p.sensor)?.id ?? (plantas.length ? plantas[0].id : NUEVA));
  }

  function pintarPlantas(elegida) {
    const opciones = plantas.map((p) => {
      const sensor = p.sensor ? ` · tiene el sensor ${p.sensor.codigo}` : "";
      return new Option(`${p.apodo} (${capitalizar(p.especie)})${sensor}`, p.id);
    });
    $("vincular-planta").replaceChildren(...opciones, new Option("Una matera nueva…", NUEVA));
    $("vincular-planta").value = String(elegida);
    pintarNueva();
  }

  function pintarNueva() {
    $("vincular-nueva").hidden = $("vincular-planta").value !== NUEVA;
  }

  return {
    mostrar() {
      // Los datos del QR se pasan al formulario y se quitan de la dirección: el código no queda en el historial.
      const datos = new URLSearchParams(location.hash.split("?")[1] ?? "");
      if (datos.has("d") || datos.has("c")) {
        $("vincular-dispositivo").value = datos.get("d") ?? "";
        $("vincular-codigo").value = datos.get("c") ?? "";
        history.replaceState(null, "", "#vincular");
      }
      mostrarError($("error-vincular"), null);
      mostrarExito($("exito-vincular"), null);
      // La matera desde la que se pidió vincular, si se llegó con «Vincular sensor» de su tarjeta.
      const elegida = contexto.plantaParaVincular;
      contexto.plantaParaVincular = null;
      cargarPlantas(elegida);
      ($("vincular-dispositivo").value ? $("vincular-planta") : $("vincular-dispositivo")).focus();
    },
    alCargarCatalogos() {
      const elegida = $("vincular-especie").value;
      $("vincular-especie").replaceChildren(...contexto.especies.map((e) => new Option(capitalizar(e.nombre), e.nombre)));
      if (elegida) $("vincular-especie").value = elegida;
    },
  };
}
