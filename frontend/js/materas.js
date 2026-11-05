// Mis materas: cada matera muestra su sensor y su última medición, que vienen de GET /plantas. El sensor
// se vincula con el QR de la etiqueta de la placa (vincular.js).
// Se actualiza sola de dos maneras: vuelve a pedir GET /plantas cada 10 s (consulta-periodica.js) y,
// mientras la conexión en vivo funciona, recibe cada medición al instante (tiempo-real.js). El estado
// de cada tarjeta lo calculó la API; aquí solo se ubica en pantalla. Desde el front no se escriben mediciones.
import { eliminarPlanta, obtenerPlantas, quitarSensor, registrarPlanta } from "./api.js";
import { consultarCadaRato, textoConsulta } from "./consulta-periodica.js";
import { dominioVisual, fraccion, porcentaje } from "./escala.js";
import { capitalizar, colorDeEstado, crear, etiqueta, numero, pastillaEstado, tono, unidad } from "./formato.js";
import { botonDe, datosDe, fechaCorta, mostrarError, ocupado } from "./formularios.js";
import { esMasNueva, especieDe, textoOrigen } from "./lecturas.js";

export function crearPantallaMateras(contexto) {
  const $ = (id) => document.getElementById(id);
  const formulario = $("form-planta");
  let plantas = [];
  // Lo que GET /plantas no trae: las recomendaciones de la medición que llegó en vivo, con su momento.
  // Solo se muestran mientras esa siga siendo la última medición de la tarjeta.
  const diagnosticos = new Map();

  formulario.addEventListener("submit", async (evento) => {
    evento.preventDefault();
    try {
      await ocupado(botonDe(evento), () => registrarPlanta(datosDe(formulario)));
    } catch (error) {
      mostrarError($("error-planta"), error);
      return;
    }
    mostrarError($("error-planta"), null);
    formulario.reset();
    cargar();
  });

  $("ir-a-vincular").addEventListener("click", () => contexto.navegar("vincular"));

  consultarCadaRato(() => !$("pantalla-materas").hidden, actualizar);

  contexto.tiempoReal.alRecibir((mensaje) => {
    if ($("pantalla-materas").hidden) return; // al volver a la pantalla se recarga todo
    if (mensaje.tipo === "reconectado") {
      cargar();
      return;
    }
    if (mensaje.tipo !== "medicion") return;
    const planta = plantas.find((p) => p.id === mensaje.planta.id);
    if (!planta) {
      cargar(); // una matera creada en otra pestaña
      return;
    }
    if (esMasNueva(planta.ultimaMedicion, mensaje.medicion)) return; // la consulta ya trajo una posterior
    planta.ultimaMedicion = mensaje.medicion;
    // Si la lectura es de su sensor, la placa ya canjeó su clave: está lista aunque la consulta no lo haya traído.
    if (planta.sensor && mensaje.medicion.origen === planta.sensor.codigo) planta.sensor.listo = true;
    diagnosticos.set(planta.id, { momento: mensaje.medicion.momento, diagnostico: mensaje.diagnostico });
    repintarTarjeta(planta);
    pintarBajada();
  });

  function alCargarCatalogos() {
    const elegida = $("planta-especie").value;
    $("planta-especie").replaceChildren(...contexto.especies.map((e) => new Option(capitalizar(e.nombre), e.nombre)));
    if (elegida) $("planta-especie").value = elegida;
    pintar();
  }

  async function cargar() {
    try {
      plantas = await obtenerPlantas();
      mostrarError($("error-materas"), null);
    } catch (error) {
      mostrarError($("error-materas"), error);
      return;
    }
    $("materas-consulta").textContent = textoConsulta();
    pintar();
  }

  // La consulta periódica: solo se vuelven a pintar las tarjetas que cambiaron, así no se pierde lo que
  // el usuario está escribiendo. Si cambió la lista (una matera nueva o borrada en otra pestaña), se pinta todo.
  async function actualizar() {
    let nuevas;
    try {
      nuevas = await obtenerPlantas();
    } catch (error) {
      mostrarError($("error-materas"), error);
      return;
    }
    mostrarError($("error-materas"), null);
    $("materas-consulta").textContent = textoConsulta();
    const ids = (lista) => lista.map((p) => p.id).join(",");
    if (ids(nuevas) !== ids(plantas)) {
      plantas = nuevas;
      pintar();
      return;
    }
    nuevas.forEach((nueva, indice) => {
      const vieja = plantas[indice];
      // Si mientras se esperaba la respuesta llegó una medición en vivo, esa es más reciente: no se retrocede.
      if (esMasNueva(vieja.ultimaMedicion, nueva.ultimaMedicion)) nueva.ultimaMedicion = vieja.ultimaMedicion;
      const cambio = nueva.ultimaMedicion?.momento !== vieja.ultimaMedicion?.momento
        || nueva.sensor?.codigo !== vieja.sensor?.codigo
        || nueva.sensor?.listo !== vieja.sensor?.listo;
      plantas[indice] = nueva;
      if (cambio) repintarTarjeta(nueva);
    });
    pintarBajada();
  }

  function pintarBajada() {
    const conAtencion = plantas.filter((p) => p.ultimaMedicion && p.ultimaMedicion.estado !== "SALUDABLE").length;
    $("materas-bajada").textContent = plantas.length
      ? `${plantas.length} ${plantas.length === 1 ? "matera" : "materas"} · ` +
        `${conAtencion} ${conAtencion === 1 ? "necesita" : "necesitan"} atención según su última medición`
      : "Todavía no tiene materas. Agregue la primera y vincúlele su sensor con el QR de la etiqueta.";
  }

  function pintar() {
    pintarBajada();
    $("materas").replaceChildren(...plantas.map((planta) => tarjeta(planta)));
  }

  // Solo la tarjeta que cambió, marcada un momento para que se note la medición nueva.
  function repintarTarjeta(planta) {
    const vieja = $("materas").querySelector(`[data-planta="${planta.id}"]`);
    if (!vieja) return;
    const nueva = tarjeta(planta);
    nueva.classList.add("matera--nueva");
    vieja.replaceWith(nueva);
  }

  function tarjeta(planta) {
    const medicion = planta.ultimaMedicion;
    const especie = especieDe(contexto, planta.especie);
    const colores = colorDeEstado(medicion?.estado);

    const nodo = crear("article", { clase: "tarjeta matera" },
      crear("div", { clase: "matera__cabecera" },
        crear("div", { clase: "matera__icono", estilo: { background: colores.fondo } },
          crear("div", { clase: "matera__punto", estilo: { background: colores.texto } })),
        crear("div", { clase: "matera__nombres" },
          crear("p", { clase: "matera__lugar", texto: planta.apodo }),
          crear("p", { clase: "matera__especie", texto: capitalizar(planta.especie) }))),
      pastillaEstado(medicion?.estado),
      crear("div", { clase: "matera__barras" }, ...barras(medicion, especie)),
      crear("p", { clase: "matera__nota", texto: nota(planta) }));
    nodo.dataset.planta = planta.id;
    if (medicion?.estado === "CRITICO") nodo.classList.add("matera--critica");

    const enVivo = diagnosticos.get(planta.id);
    if (enVivo && enVivo.momento === medicion?.momento) nodo.append(recomendaciones(enVivo.diagnostico));
    nodo.append(acciones(planta));
    return nodo;
  }

  function barras(medicion, especie) {
    return contexto.parametros.map((parametro, indice) => {
      const lectura = medicion?.lecturas.find((l) => l.nombre === parametro.nombre);
      const rango = especie?.rangos[parametro.nombre];
      const pista = crear("div", { clase: "matera__pista" });
      if (rango) {
        const dominio = dominioVisual(rango, parametro);
        const desde = fraccion(rango.min, dominio);
        pista.append(crear("div", {
          clase: "matera__banda",
          estilo: { left: porcentaje(desde), width: porcentaje(fraccion(rango.max, dominio) - desde) },
        }));
        if (lectura) {
          pista.append(crear("div", {
            clase: "matera__marca",
            estilo: { left: porcentaje(fraccion(lectura.valor, dominio)), background: tono(indice).linea },
          }));
        }
      }
      return crear("div", { clase: "matera__barra" },
        crear("span", { clase: "matera__etiqueta", texto: etiqueta(parametro.nombre) }),
        pista,
        crear("span", {
          clase: "matera__valor",
          texto: lectura ? `${numero.format(lectura.valor)} ${unidad(parametro.unidad)}` : "—",
        }));
    });
  }

  function nota(planta) {
    // «listo»: la placa ya canjeó su clave. Antes de eso no puede mandar lecturas.
    const sensor = !planta.sensor
      ? "Sin sensor: vincúlele uno con el QR de su etiqueta"
      : planta.sensor.listo
        ? `Sensor ${planta.sensor.codigo}`
        : `Sensor ${planta.sensor.codigo}, esperando que se encienda y reciba su clave`;
    const medicion = planta.ultimaMedicion;
    if (!medicion) return `${sensor} · todavía no llega ninguna medición`;
    // El origen solo se dice si no es el sensor actual: una medición vieja a mano o de un sensor anterior.
    // Sin punto final: la hora ya termina en «p. m.».
    const origen = medicion.origen === planta.sensor?.codigo
      ? ""
      : medicion.origen === "MANUAL" ? `, ${textoOrigen(medicion.origen)}` : `, del sensor anterior ${medicion.origen}`;
    return `${sensor} · última medición ${fechaCorta(medicion.momento)}${origen}`;
  }

  function recomendaciones(diagnostico) {
    const textos = diagnostico.recomendaciones.length
      ? diagnostico.recomendaciones
      : ["Todos los parámetros están en su rango óptimo."];
    return crear("div", { clase: "matera__respuesta" },
      crear("p", { clase: "rotulo", texto: "Recomendaciones de la medición que llegó en vivo" }),
      ...textos.map((texto) => crear("p", { clase: "matera__recomendacion", texto })));
  }

  function acciones(planta) {
    const boton = (texto, clase, accion) => {
      const nodo = crear("button", { clase, texto });
      nodo.type = "button";
      nodo.addEventListener("click", accion);
      return nodo;
    };
    const sensor = planta.sensor
      ? boton("Quitar sensor", "boton-contorno", () => quitar(planta))
      : boton("Vincular sensor", "boton-contorno", () => {
          contexto.plantaParaVincular = planta.id;
          contexto.navegar("vincular");
        });
    return crear("div", { clase: "matera__acciones" },
      sensor,
      boton("Historial", "boton-contorno", () => contexto.verPlanta("historial", planta.id)),
      boton("Eliminar", "boton-contorno boton-contorno--peligro", () => eliminar(planta)));
  }

  // El historial se conserva. La placa se entera en vivo, borra su clave y queda libre para otra matera.
  async function quitar(planta) {
    const { codigo } = planta.sensor;
    const confirmado = window.confirm(`¿Quitar el sensor ${codigo} de «${planta.apodo}»? Su historial se conserva.`);
    if (!confirmado) return;
    try {
      await quitarSensor(planta.id);
    } catch (error) {
      mostrarError($("error-materas"), error);
      return;
    }
    cargar();
  }

  async function eliminar(planta) {
    const aviso = planta.sensor ? " Su sensor queda libre para otra matera." : "";
    const confirmado = window.confirm(`¿Eliminar «${planta.apodo}» y su historial?${aviso}`);
    if (!confirmado) return;
    try {
      await eliminarPlanta(planta.id);
    } catch (error) {
      mostrarError($("error-materas"), error);
      return;
    }
    diagnosticos.delete(planta.id);
    cargar();
  }

  return {
    mostrar() {
      cargar();
    },
    alCargarCatalogos,
    // Al cerrar la sesión: nada de esta cuenta queda para la que entre después en la misma pestaña.
    reiniciar() {
      plantas = [];
      $("materas-consulta").textContent = "";
      diagnosticos.clear();
      pintar();
    },
  };
}
