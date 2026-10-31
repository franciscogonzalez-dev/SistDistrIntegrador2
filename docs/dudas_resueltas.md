
Del diseño de topics (issues #1 y #2):
1. ¿Hay un despachador por fuerza o uno general? Si es por fuerza, el {tipo} responde cómo se separan dos grupos a la vez, que la consigna pregunta explícitamente.
RTA: un despachador general
2. ¿Cambiamos el topic de comandos a comandos/{despachador}/{tipo}/{id}? Así el servicio sabe quién mandó el comando.
RTA: un despachador general
3. ¿Las emergencias van por tipo (emergencias/{tipo}/{id})? Así cada despachador ve solo las de su fuerza.
RTA: el tipo de emergencia podria ser a que servicio hace referencia, por ejemplo emergencia Policia?
4. ¿Qué tipos de unidad ve el público? ¿Todas, o sin patrulleros?
RTA: el publico (la persona civil que solicita una emergencia) no ve ninguna unidad, ni siquiera la asignada. Solo publica su pedido.
5. ¿Pedir una emergencia desde el público está en la base o es ampliación? Cambia el tamaño de las issues #12 y #16.
RTA: si el civil/publico puede pedir una emergencia sin estar logueado, la idea es una interfaz que tiene los 3 botones uno por cada servicio que necesita, y al solicictar uno se envia un id que simula ser el id del celular al igual que las coords de su gps.

Del estado de las unidades (#9, #10, #12 y #13):

6. ¿Quién cambia "en servicio / fuera de servicio"? ¿La unidad, en su payload? El alcance lo pone en la base, pero "unidades que controlan su estado" figura como ampliación.
RTA: Queda fuera del alcance del proyecto, la unidad o esta “ocupada” o “libre”.
7. ¿Solo el despachador marca ocupada o libre?
RTA: si, solo el despachador
8. ¿Qué pasa con una unidad ocupada que se cae? ¿Sigue asignada a la emergencia o se libera?
RTA: una alerta al despachador y que permita asignar una nueva, tal que cuando esa unidad caida vuelva se la marque como desocupada para que no vaya al pedo
9. ¿Cuándo se cierra una emergencia? ¿Cuando el despachador libera la unidad?
RTA: si, tal que la unidad asignada le informa por fuera del sistema (no esta en nuestro alcance) que ya termino, el despachador la marca como libre

De la validación y el simulador (#9 y #13):

10. ¿Cuál es la zona válida? Qué ciudad, con sus coordenadas.
RTA: solo la zona cubierta por Comodoro Rivadavia, quizás se podría definir una frontera tal que esporádicamente se envíen unidades fuera de comodoro y que sean inválidas, especificar si es posible a través de coordenadas marcar como una zona cubierta.
11. ¿Qué velocidad máxima define un salto imposible?
RTA: Quizas esto queda fuera del alcance, enrealidad el servicio debería validar las posiciones de los móviles, osea cosas como que los móviles no estén en el medio del mar o cosas así. En todo caso lo que puede validar es que entre las actualizaciones de ubicación de los móviles, aproximadamente cada 2 segundos, no haya una diferencia de más de 150mts.
12. ¿Cada cuánto reporta posición una unidad?
RTA: entre 1-2 segs
13. ¿Qué campos lleva el payload de posición y cuáles son obligatorios?
RTA: si un campo obligatorio falta o es tipo incorrecto, el mensaje descarta. Campos desconocidos se ignoran, no se rechazan, permitiendo agg campos futuros.  {"v": 1, "ts": 1730000000, "lat": -43.2490, "lon": -65.3051, "rumbo": 180} (el “v” es la versión del schema/payload, el rumbo orienta el icono en el mapa)

De seguridad y webhooks (#6 y #17):

14. ¿Qué condición dispara el webhook por regla? ¿ACL denegada, emergencia nueva o las dos?
El TP pide 2 webhooks:
Webhook directo (sin mirar el contenido): Se dispara con cada conexión y desconexión de un cliente, que es el ejemplo de la consigna. En el receptor se vería algo así: "10:42 amb-07 se conectó", "10:55 pol-02 se desconectó". Para su caso sirve como registro de actividad del centro de monitoreo: quién entró y quién salió. No tiene que leer ningún payload, por eso va directo.
Emergencia Nueva (disparado por regla, una consulta SQL decide cuando): el servicio publica una emergencia nueva, permite ver al receptor “Nueva emergencia: se requiere ambulancia en -43.25, -65.30”.
15. ¿Qué usuarios de demo hay? Cuántas unidades, despachadores y usuarios del público.
Rta: en la versión demo hay 1 despachador, al menos 5 usuarios del público y 5 móviles de cada uno (policias, bomberos y ambulancias).
16. ¿Cómo se ata el client ID al usuario en EMQX? Hay que investigarlo.
	RTA: 
Rol: Unidad
Usuario: Uno por unidad (amb-01, pol-03)
Client ID: Igual al usuario
Clave: Propia, en el archivo de usuarios
Sesión: No persistente: solo publica, y el estado lo guarda el servicio
────────────────────────────────────────
Rol: Despachador
Usuario: Uno general (desp-01), ver duda nueva 1
Client ID: Igual al usuario
Clave: Propia
Sesión: Persistente: si se cae, al volver recibe lo que se perdió
────────────────────────────────────────
Rol: Público
Usuario: Compartido: publico
Client ID: publico-<uuid>, generado una vez y guardado en el teléfono (al no se el mismo no hay problemas)
Clave: La misma para todos, dentro de la app
Sesión: No persistente
────────────────────────────────────────
Rol: Servicio
Usuario: servicio
Client ID: servicio
Clave: Fuerte, por variable de entorno
Sesión: Persistente: no pierde reportes si se reinicia
────────────────────────────────────────
Rol: Receptor HTTP
Usuario: Sin usuario MQTT: lo llama el broker
Client ID: –
Clave: Secreto compartido en un header del webhook
Sesión: –



Del despliegue y la entrega (#18):

17. ¿Cómo se conectan los docentes desde el 8/11? ¿Misma red o Tailscale?
	RTA:  usamos local y tailscale, el workflow es:
1. En curso: creás tu rama desde main, programás el issue y probás contra tu broker local.
2. En revisión: abrís el PR (pull request)-. Otra persona lo revisa y, si quiere, lo prueba en su local.
3. Merge a main.
4. Prueba en Tailscale:
Si tocaste el lado servidor (broker, servicio o receptor): en la notebook que corresponda, git pull && docker compose up -d --build.
Si tocaste un cliente (unidad o mapa): no se redespliega nada. Corrés el cliente desde main apuntando al broker de Tailscale.
Probás el camino que tocaste, junto con lo que ya está integrado.
5. Done: recién cuando anda en Tailscale.

18. ¿Qué ponemos en el informe sobre el camino para dispositivos sin Tailscale? Por ejemplo, "no usamos ESP32" y por qué.
RTA: esta simulado para reducir el alcance.
19. ¿Tienen la autorización de los docentes para correr en local por escrito?
RTA: respuesta en 17.

De alcance:

20. ¿Hacemos alguna ampliación? ¿Cuál? La replicación del servicio es la única que la consigna dice que se valora.
Rta: Por ahora no hacemos ninguna aplicación, nos queremos enfocar en que la base del sistema funcione de manera sólida, eventualmente si todo esto funciona podriamos implementar la vista del lado del móvil que acepte la misión y se designe automaticamente como ocupada.

Las primeras cinco son las que más bloquean, porque cambian el árbol de topics y la ACL, y de ahí cuelga todo lo demás. Conviene cerrarlas en la reunión de #1.

Dudas nuevas:

1. ¿Despachador general o uno por fuerza? En la duda 1 dicen general, pero la tabla de la duda 16 dice "uno por persona y fuerza (desp-policia-01)". Puse general (desp-01). ¿Confirman?: despachador es general
2. La pregunta de la consigna sobre cómo se separan dos grupos a la vez sigue sin respuesta. Con un despachador general, el tipo de unidad ya no la responde. RTA: el público no puede suscribirse a nada salvo servicio/estado, así que ningún civil ve datos de otro; las emergencias se separan por servicio (emergencias/{tipo}/...).
3. ¿El topic de comandos lleva el id del despachador? La respuesta a la duda 2 repite "un despachador general", así que no queda claro. Con un solo despachador, comandos/despacho/... funciona, así que lo dejé así y lo marqué como pendiente en la #1. RTA: si
4. ¿Las emergencias por tipo son lo que querían? En la duda 3 preguntaban si el tipo es el servicio (por ejemplo, Policía). Sí, es eso, y lo apliqué como emergencias/{tipo}/{id}.: si pero el id a que hace referencia
5. ¿Los 150 m van como velocidad? Los puse como velocidad máxima de 75 m/s calculada con ts, así el control funciona aunque el intervalo varíe entre 1 y 2 s. Equivale a 150 m cada 2 s.: dale
6. ¿rumbo es opcional? No lo dijeron. Lo dejé opcional, con v, ts, lat y lon obligatorios.; si
7. Hay cosas desactualizadas en el documento:
   - Coordenadas del ejemplo: el payload de ejemplo (-43.249, -65.305) es de Trelew, no de Comodoro (alrededor de -45.86, -67.49).
   - Estados de la unidad: el alcance todavía dice "en servicio / fuera de servicio".
   - Árbol de topics: es la versión anterior. RTA: no importa
8. ¿Quién tiene la notebook del broker? La #25 la tiene que hacer quien tenga esa máquina. La asigné a Nico por rol, pero si es de otro, la cambio.: no existe ahora, porque seria con tailscale
