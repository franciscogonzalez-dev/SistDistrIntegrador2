**UNIVERSIDAD NACIONAL DE LA PATAGONIA SAN JUAN BOSCO**

**SISTEMAS DISTRIBUIDOS**

# Proyecto 2

Objetivo: construir, en grupo, un sistema en el que todos los componentes se comunican solo a través de un broker MQTT, desplegarlo con Docker en un servidor que no es su notebook, dejarlo cerrado para que nadie pueda escuchar, mentir ni hacerse pasar por otro, e integrarlo con un sistema externo por webhooks. Todo lo de las unidades 6, 7 y 8 (administración de servidores, comunicación indirecta y seguridad) en un proyecto.

## El problema

En este proyecto ningún componente le habla directamente a otro. Los componentes publican y se suscriben a topics, y el broker reparte. Eso resuelve algunos problemas y trae otros. ¿Qué pasa con un mensaje cuando se corta la red? ¿Cómo se entera el sistema de que un usuario se desconectó? ¿Quién puede publicar en cada topic? Y si cualquiera puede conectarse al broker, ¿qué impide que un usuario publique en nombre de otro?

El día de la defensa, se espera que puedan demostrar que en su sistema ningún usuario puede escuchar lo que no le corresponde, actuar en nombre de otro ni mentir sobre el estado.

## La arquitectura mínima

- Un broker EMQX.
- Un servicio central, que es el único que guarda el estado del sistema y decide qué es válido. Por ejemplo, en un juego de preguntas sabe qué pregunta está abierta, quién respondió y cuál es el puntaje de cada uno; un usuario puede mandar una respuesta, pero es el servicio el que decide si cuenta.
- Un receptor HTTP, que recibe los webhooks del broker (requerimiento 6).
- Al menos dos clientes, que solo hablan MQTT, corriendo en dispositivos distintos. Pueden ser notebooks, una aplicación en un teléfono o placas como un ESP32 o una Raspberry Pi.

El broker y el servicio corren en containers, cada uno en un servidor distinto. El receptor HTTP también corre en un container, en cualquiera de los servidores del grupo. Se usan los servidores que se le asignaron a cada integrante durante la práctica. El servicio se conecta al broker por la red, como un cliente más, con su usuario y su clave; así se puede detener uno sin el otro. Los clientes llegan a los servidores por Tailscale. Un dispositivo que no puede correr Tailscale (un ESP32, por ejemplo) necesita otro camino hasta el broker; resolverlo es parte del diseño, y los requisitos de seguridad valen igual para él. Los demás servidores del grupo se pueden usar libremente (una réplica del servicio, un cliente automático, un entorno de prueba).

No se pide replicación del servicio. Se puede sumar, y se valora.

## Seis requerimientos

El sistema tiene que cumplir con lo siguiente.

1. **Diseño de topics.** El árbol de topics completo, con quién publica y quién se suscribe a cada uno, y con qué wildcards. ¿Cómo hace el servicio para mandarle algo a un solo usuario y cómo para mandarle a todos? ¿Cómo se identifica a cada usuario en el topic? ¿Cómo se separan dos grupos de usuarios que usan el sistema a la vez (dos salas, dos rondas)?

2. **Garantías de entrega.** El QoS de cada topic, justificado. No vale "QoS 2 en todo por las dudas". ¿Qué pasa en su sistema si un mismo pedido llega dos veces? (QoS 1 puede entregar un mensaje repetido.) ¿Qué topics llevan retained y por qué? ¿Qué clientes usan sesión persistente? Un cliente que se cae y vuelve tiene que poder seguir donde estaba sin que nadie haga nada a mano.

3. **Presencia y caídas.** El sistema tiene que saber quién está conectado y enterarse solo cuando alguien se cae, sin pings propios, usando el Last Will de MQTT. ¿Qué hace el sistema cuando se cae un usuario que estaba participando? ¿Y cuando se cae el servicio? ¿Y el broker?

4. **Despliegue.** Todo el lado servidor (broker y servicio) se levanta con `docker compose up -d` en los servidores asignados al grupo. Si un container se muere, ¿vuelve solo? ¿Se pierde el estado? ¿Y si se borra el container del broker y se vuelve a crear?

5. **Seguridad.**

   - Nada es anónimo. Cada componente tiene su usuario y su clave. El panel tampoco queda con admin / public.
   - La ACL sigue los roles. Un usuario solo publica en sus propios topics y solo lee lo que le corresponde; el servicio es el único que publica el estado del sistema. Nadie puede actuar en nombre de otro.
   - El client ID está atado al usuario, para que nadie pueda robarle la sesión a otro.
   - Las conexiones de los clientes van por TLS (en EMQX es el listener ssl, puerto 8883 adentro del container), con un certificado generado por ustedes. EMQX ya trae un certificado de ejemplo en ese puerto, pero su clave privada está publicada en internet. Ese no vale.
   - El informe muestra qué ve alguien que captura el tráfico, con una captura con TLS y otra sin TLS.

6. **Webhooks.** El broker avisa a un servidor HTTP externo cuando pasa algo en el sistema, de dos maneras:

   - Un webhook directo (en el panel de EMQX, Integración → Webhook), disparado por un evento o un topic sin mirar el contenido del mensaje. Por ejemplo, cada vez que un cliente se conecta o se desconecta.
   - Un webhook disparado por una regla (Integración → Reglas, con una acción HTTP Server), en el que una consulta SQL decide cuándo se dispara y qué datos se mandan. Por ejemplo, solo cuando la ACL le niega una publicación a alguien, o cuando un sensor pasa un umbral.

   El receptor HTTP es un container más del lado servidor, y lo que recibe tiene que poder verse (un log, una página, un aviso en un canal). El que llama es el broker, así que los componentes siguen sin hablarse directamente. ¿Por qué cada aviso va por el camino que eligieron? ¿Qué hace el broker si el receptor está caído? ¿Cómo sabe el receptor que el aviso viene del broker y no de cualquiera? ¿El webhook y la regla siguen ahí si se borra el container del broker y se vuelve a crear?

Las preguntas son orientativas. Muestran a qué apunta cada requerimiento y son también las que se pueden hacer en la defensa.

## Temática

Se espera un tema nuevo, que aproveche lo que MQTT hace bien, como un servicio que les habla a muchos a la vez y clientes que entran y salen. Por ejemplo, un juego de preguntas con tiempo (tipo Kahoot), un chat con salas, un tablero de sensores con alarmas o un sistema de turnos. El tema tiene que estar definido para el jueves 22/10.

También se puede rehacer sobre MQTT el tema del Proyecto 1. Se puede reutilizar la lógica, pero la comunicación se rehace entera, sin sockets, Pyro ni gRPC entre componentes.

## Entregables y fechas

Grupos de 4 integrantes. Pueden ser los mismos del Proyecto 1 o grupos nuevos.

- Repositorio git con el código, el `compose.yaml` y un README que diga exactamente cómo levantar el lado servidor y los clientes (comandos concretos; si no se puede levantar, no tiene nota). Las claves y la clave privada del certificado no van al repositorio.
- Informe técnico que conteste las preguntas de los seis requisitos, con el árbol de topics, la ACL y un diagrama de la arquitectura. Respuestas concretas con decisiones justificadas.
- Demostración en vivo el día de la defensa.

**Domingo 8/11:** entrega en el aula virtual del link al repositorio y del informe. Desde la entrega y hasta la defensa, el sistema queda funcionando en los servidores asignados, para que los docentes puedan probarlo como un usuario más. Para eso, la entrega incluye los datos necesarios para conectarse.

**Jueves 12/11:** defensa. La defensa es el Parcial 2.

**Jueves 19/11:** recuperatorio.

## La defensa y la evaluación

La defensa tiene tres momentos:

- El camino normal: el sistema funcionando de principio a fin con al menos dos clientes.
- Las fallas y la seguridad: cómo responde el sistema ante caídas y ante usuarios que intentan hacer trampa.
- Preguntas individuales: cualquier integrante tiene que poder explicar cualquier parte del sistema, la haya escrito o no.

| Criterio | Qué miramos | Puntos |
| --- | --- | --- |
| Camino normal | El sistema funciona de principio a fin con varios clientes, solo por MQTT | 10 |
| Topics y QoS | El árbol tiene sentido, el QoS de cada topic está justificado y un pedido repetido no rompe el estado | 10 |
| Presencia | Se cae un cliente y el sistema se entera solo; vuelve y sigue | 10 |
| Despliegue | `docker compose up -d` levanta todo en los servidores asignados y sobrevive a la muerte de un container | 10 |
| Seguridad | Un cliente tramposo no puede escuchar, actuar en nombre de otro, publicar estado falso ni robar una sesión | 15 |
| TLS | Los clientes se conectan solo por TLS con su certificado, y las dos capturas muestran la diferencia | 5 |
| Webhooks | Los dos avisos llegan al receptor: el directo con cada evento y el de la regla solo cuando se cumple su condición | 10 |
| Informe | Las decisiones están justificadas con argumentos técnicos y coinciden con lo que hace el código | 10 |
| Defensa individual | Cada integrante entiende el sistema completo | 10 |
