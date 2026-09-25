# Detector de restaurantes

Market research para encontrar restaurantes que gestionan sus reservas a mano (teléfono, WhatsApp) y que, por lo tanto, son clientes potenciales de la recepcionista telefónica con IA.

## Language

### Clasificación

**Sistema de reservas**:
Plataforma de reservas online (widget o enlace de un proveedor como CoverManager o TheFork) que confirma la reserva sin que intervenga una persona. Un formulario que envía un email y un bot o número de WhatsApp **no** son sistemas de reservas.
_Avoid_: software, herramienta, plataforma

**Candidato**:
Restaurante sin sistema de reservas conocido: estado `Sin sistema detectado`, `Sin web` o `Solo redes sociales`. Si usa un sistema de reservas, deja de ser candidato aunque también reserve por teléfono (por ejemplo, para grupos).
_Avoid_: lead, prospecto, oportunidad

**Reservable en Google**:
Google indica que el local acepta reservas online. Como Reserve with Google solo funciona a través de partners, casi siempre implica un sistema de reservas cuyo proveedor no se identificó. Tiene prioridad Baja fija hasta que la validación manual confirme o descarte esta suposición.

**No acepta reservas**:
Restaurante que declara explícitamente que no toma reservas. No es candidato. Solo se asigna en la validación manual, nunca se deduce de la falta de señales.

**Cadena**:
Grupo de locales que comparten web o teléfono (3 o más). Cada local se trata como candidato por separado, pero se valida a mano porque la decisión de compra es del grupo.

### Priorización

**Prioridad**:
Volumen relativo del restaurante respecto de toda su zona (percentiles de reseñas sobre todos los restaurantes encontrados, incluidos los que usan sistema). `Usa sistema` siempre es Descartar.

**Validación manual**:
Veredicto humano sobre un restaurante (por ejemplo, confirmar que no tiene sistema o que no acepta reservas), identificado por su Place ID. Se acumula entre ejecuciones y nunca se borra. Registra hechos sobre el restaurante, no el seguimiento comercial (visitas, interés), que vive en una hoja aparte.

**Veredicto**:
Resultado de una validación manual, de una lista cerrada: `Confirmado sin sistema`, `Tiene sistema`, `No acepta reservas`, `Cerrado`. Prevalece sobre la clasificación automática, salvo cuando el script detecta después un sistema de reservas: en ese caso gana el script y el veredicto queda **desactualizado**.
