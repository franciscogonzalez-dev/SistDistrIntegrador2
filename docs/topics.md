La logica es: todo cliente (client_id) se autentica con un usuario, esto permite que definamos reglas de ACL para cada usuario, o sea a que topico se puede suscribir/publicar. Varios clientes pueden autenticarse con el mismo usuario, esto pasa por ejemplo con el publico/civiles.
Lo malo que tiene el compartir un usuario entre varios clientes, es que un cliente puede hacerse pasar por otro (tomando su client id), inclusive puede desconectar al real. Por eso las unidades/moviles no comparten un usuario por cada tipo, sino que cada unidad tiene su propio usuario.

### Diseño de topicos

ENTRADA (sin validar) → lee solo el servicio
  reportes/{tipo}/{username id}/posicion      unidad {id}                 QoS 0
  reportes/{tipo}/{username id}/presencia     unidad {id} + Last Will     QoS 1  retained (topico que dice si la unidad esta conectada o no)
  comandos/{despachador id}/{tipo}/{unidad id} despachador   QoS 1  (id único en el payload, por ej comandos/desp-01/ambulancia/amb-07)
  pedidos/{clientid}/{tipo}      público                     QoS 1  (id único, lat, lon)

SALIDA (oficial) → publica solo el servicio
  flota/{tipo}/{unidad id}/estado           despachador                 QoS 1  retained
  emergencias/{tipo}/{emergencia id}           despachador                 QoS 1  retained (vacío al cerrar)
  alertas/{tipo}                     despachador                 QoS 1
  respuestas/{despachador id}      un solo usuario             QoS 1
  servicio/estado                    todos (Last Will del servicio) QoS 1 retained

### Porque importa el orden de los niveles de un topico
cual seria la diferencia entre `pedidos/{clientid}/{tipo}` (el que usamos) vs `pedidos/{tipo}/{clientid}`

el orden importa por los wildcards (`+`,`#`)
- `+` reemplaza un nivel, en cualquier posición.
- `#` reemplaza todo lo que sigue y solo puede ir al final.

Comparando las dos opciones:

1. `pedidos/{clientid}/{tipo}`: la identidad va primero.
    * ACL: `allow publish pedidos/${clientid}/+` cubre todo lo que publique esa persona con una sola regla (o `pedidos/${clientid}/#`).
    * El servicio lee todo con `pedidos/+/+`, o un solo tipo con `pedidos/+/bomberos`.

2. `pedidos/{tipo}/{clientid}`: el tipo va primero.
    * Filtrar por tipo es natural: `pedidos/bomberos/#`.
    * La ACL tiene que usar `+` en el medio (`pedidos/+/${clientid}`) y no se puede usar `#` para "todo lo de esta persona".

Regla practica: ir de lo general a lo especifico, y poner antes lo que mas se quiera agrupar con `#`.

### Identidad (username) en el topico, no en el payload
Regla del contrato: **quien publica se identifica por el topico, nunca por el payload**. Por eso los topicos de entrada llevan el usuario (`reportes/{tipo}/{username}/...`, `comandos/{despachador}/...`) o el client id en el caso del publico (`pedidos/{clientid}/...`), y el payload solo lleva datos. Ej: la posicion es `{v, ts, lat, lon, rumbo}`, sin id de unidad.

Son dos controles distintos que trabajan juntos:
1. **Las reglas de ACL definen que usuarios pueden publicar/suscribirse a que topicos.** Cada regla dice a quien se aplica (un usuario, o un grupo por regex, ej `^desp-`) y que permite. El broker sabe que usuario esta conectado porque lo autentico con la clave, recorre las reglas en orden y aplica la primera que coincide; si ninguna permite, el `deny` final lo rechaza. Asi solo los despachadores pueden publicar en `comandos/`.
2. **Un topico que incluye el username en su ruta garantiza que solo ese usuario publique en esa ruta**, siempre que la regla lo permita y use `${username}`. El broker reemplaza `${username}` por el usuario autenticado, entonces `desp-01` solo puede publicar en `comandos/desp-01/...` y no en `comandos/desp-02/...`. Ojo: si la regla fuera `comandos/#` (sin `${username}`), el username del topico seria texto comun y cualquier despachador podria poner otro.

```
{allow, {username, {re, "^desp-"}}, publish, ["comandos/${username}/#"]}.
{allow, {username, {re, "^amb-"}},  publish, ["reportes/ambulancia/${username}/#"]}.
{deny, all}.

amb-01  publica en comandos/desp-01/...  → la regla de desp- no se aplica a amb-01, la de amb- no matchea el topico → deny
desp-01 publica en comandos/desp-02/...  → la regla de desp- se aplica, pero comandos/desp-01/# no matchea → deny
desp-01 publica en comandos/desp-01/...  → permitido
```

**Ojo: `${username}` solo existe dentro de las reglas de ACL**, no en los topicos. Los clientes siempre publican en topicos concretos (texto comun), ej `reportes/ambulancia/amb-01/posicion`; es el broker el que, al chequear el permiso, convierte la regla `reportes/ambulancia/${username}/#` en `reportes/ambulancia/amb-01/#` y compara. Poner un username en la ruta de un topico, por si solo, no protege nada: es solo una convencion de nombres. Lo que la vuelve una garantia es que la regla de ACL use `${username}` en esa posicion.
* No confundir con los `{id}`, `{tipo}`, `{eid}` del arbol de arriba: esos son marcadores para que lo lea una persona ("aca va el id de la unidad"), y los reemplaza nuestro codigo al armar el topico (`comun/topics.py`).
* Ejemplo: `flota/{tipo}/{id}/estado` tambien lleva el username de la unidad, pero la regla es `{allow, {username, "servicio"}, publish, ["flota/#"]}` (sin `${username}`). Ahi el username es solo un dato: sirve para tener un retained por unidad y para que el despachador sepa de que unidad es el estado. Que una unidad no pueda publicar su propio estado lo garantiza que no tiene ninguna regla que le permita `flota/`, y cae en el `deny`.

Por que:
* La ACL solo mira el topico, nunca el contenido del mensaje. Y el suscriptor (el servicio) no recibe el usuario de quien publico, solo el topico y el payload. Entonces lo unico que el broker garantiza sobre la identidad de quien publica es el topico.
* Si todas las unidades publican en un topico compartido (`reportes/posicion`) con el id en el payload, la ACL puede limitar quien entra al topico (incluso con regex, ej `^unidad-`), pero no en nombre de quien publica:
    ```
    amb-01 publica en reportes/posicion  {"id": "pol-03", ...}
    → ACL: ¿amb-01 puede publicar en reportes/posicion? si → pasa
    → el servicio cree que es una posicion de pol-03, no tiene forma de saber que la mando amb-01
    ```
* Con el usuario en el topico y la ACL atada a `${username}`, cada unidad solo puede publicar como si misma:
    ```
    {allow, {username, {re, "^amb-"}}, publish, ["reportes/ambulancia/${username}/#"]}.

    amb-01 publica en reportes/ambulancia/amb-01/posicion  → pasa
    amb-01 publica en reportes/policia/pol-03/posicion     → el broker lo rechaza
    ```
  La regex agrupa por tipo (una ambulancia no puede publicar como policia) y `${username}` ata cada mensaje a su autor.
* Si el payload trae un campo de id, el servicio lo ignora: no es parte del contrato. La identidad sale solo del topico.

Por que importa, si una unidad legitima no se va a hacer pasar por otra:
* Credenciales robadas: si se filtra la clave de `amb-01`, con el topico compartido se podria mover a cualquier unidad del mapa. Con el usuario en el topico el daño queda limitado a `amb-01` (minimo privilegio).
* Errores: dos simuladores con la misma config copiada. Con el topico compartido el servicio mezcla las posiciones sin enterarse; con la ACL el que esta mal configurado es rechazado y se nota enseguida.
* Lo pide la consigna ("nadie puede actuar en nombre de otro", req. 5) y lo probamos en la demo (#19).

Como saca el servicio el usuario si se suscribe con wildcard: cada mensaje llega con el topico real, no con el patron de la suscripcion. Se suscribe a `reportes/+/+/posicion` y recibe `msg.topic == "reportes/ambulancia/amb-01/posicion"`, de ahi saca tipo y unidad. Ese parseo vive en `comun/topics.py` (nadie hace `split` a mano).

### Explicacion de algunos topicos:
##### De entrada: (el servidor lee en todos estos)
* comandos/{despachador}/{tipo}/{unidad id} <- aca en el ACL iria el usuario (`${username}`), esto permite autenticación para identificar al despachador que publica un comando para que lo procese el servicio. El Broker intermediario permite ver si ese despchador esta autorizado lo controlamos mediante el ACL
* reportes/{tipo}/{unidad id}/* <- estos 2 topicos la publicacion pertenece a las unidades/moviles, cada unidad se autentica con su propio usuario y solo puede publicar en estos 2. El id se refiere a su usuario, no a su client_id
* pedidos/{clientid}/{tipo} <- en este topico publica el publico/civil que necesita una emergencia. El tipo de la emergencia se direcciona por la jearquia del {tipo}
* reportes/{tipo}/{unidad id}/presencia <- dice si la unidad esta conectada o no, usando el Last Will de MQTT (sin pings propios, como pide el req. 3):
    * Al conectarse, la unidad le deja registrado al broker un Last Will en este topico con payload `offline`, y apenas conecta publica `online`.
    * Si la unidad se cae sin desconectarse bien (se corta la red, se mata el proceso, vence el keepalive), el broker publica solo el `offline`.
    * El servicio lo lee y actualiza la conexion de la unidad en `flota/.../estado`. El despachador nunca lee `reportes/` directo.
    * Si se cae una unidad ocupada: el servicio la desasigna, la emergencia vuelve a "sin asignar" y publica una alerta. Cuando la unidad vuelve, queda libre.
    * Esta separado de `posicion` porque el Last Will tiene topico y payload fijos (se definen al conectar) y porque necesita QoS 1, mientras que la posicion va en QoS 0.
    * Conviene que lleve retained (tanto el `online` como el Last Will): asi el servicio, si arranca despues que las unidades, sabe al instante quien esta conectado.

##### De salida
* flota/{tipo}/{unidad id}/estado <- estado oficial de cada unidad, lo publica solo el servicio despues de validar lo que reportan las unidades. Junta la conexion (sale de `presencia`), la disponibilidad libre/ocupada (solo la cambia el despachador) y la ultima posicion valida.
    * Va con retained: un retained por unidad (por eso el `{unidad id}` en el topico), asi el mapa del despachador al conectarse muestra la flota completa sin esperar a que cada unidad vuelva a reportar.
    * El `{unidad id}` aca no es por ACL: la regla del servicio es `flota/#`, y ninguna unidad puede publicar en `flota/` (ver seccion de identidad).
* alertas/{tipo} <- avisos puntuales para el despachador, ej: "amb-07 se desconecto con la emergencia E42 en curso".
    * Sin retained: es un aviso del momento. El estado que importa ya queda en `flota/` y `emergencias/` (ej: la emergencia vuelve a "sin asignar"), asi que un despachador que se conecta despues no necesita la alerta vieja para saber que hacer.
    * QoS 1 para que no se pierda, y como el despachador tiene sesion persistente, si estaba caido la recibe al volver.
* emergencias/{tipo}/{eid} <- topico para que el servidor publique las emergencias ya validadas (el eid lo genera el servicio, no se reutiliza el id del pedido, asi un cliente no puede pisar la emergencia de otro). Se utiliza el {eid} ya que el broker guarda un solo mensaje retained por topic: con un topico por emergencia, si el despachador se conecta por primera vez, desde otra PC o con la sesion vencida, recibe al instante todas las emergencias abiertas. Si fuera un solo topico emergencias/{tipo}, cada emergencia nueva pisaria a la anterior.
    * La sesion persistente sola no alcanza: solo guarda lo que llego mientras el cliente ya estaba suscripto, y entrega la historia (creada, asignada, cerrada) en vez del estado actual.
    * No se acumulan mensajes en el broker: al cerrar la emergencia el servicio publica un payload vacio con retained y el broker borra ese retained. Siempre hay un retained por emergencia abierta, nada mas (los topicos en si no ocupan lugar, solo los mensajes retained).
* respuestas/{despachador} <- despachador manda ordenes y necesita saber si se aplicaron o fueron rechazadas
  * Ejemplo: desp-01 → comandos/desp-01/ambulancia/amb-07   {id: "c-123", accion: "asignar", eid: "E42"}
servicio: amb-07 está ocupada → rechaza
servicio → respuestas/desp-01                  {id: "c-123", ok: false, motivo: "amb-07 ocupada"}
* servicio/estado <- dice si el servicio central esta vivo. Funciona igual que la presencia de las unidades: el servicio registra un Last Will `offline` (QoS 1, retained) y al conectarse publica `online` (retained).
    * Lo leen todos. Al ser retained, el que se conecta sabe al instante si el servicio esta andando.
    * Despachador: si esta caido, sabe que sus comandos no se van a procesar hasta que vuelva (no se pierden: van con QoS 1 y el servicio tiene sesion persistente).
    * Publico: es lo unico que puede leer. Si el servicio esta caido, la app avisa "el sistema esta con problemas, llama al 107 / 101 / 100", asi la persona no se queda esperando.
    * Responde la pregunta del req. 3 "¿y cuando se cae el servicio?".
* El publico/civil no tiene topico de salida propio: no ve ninguna unidad, ni siquiera la asignada. Solo publica su pedido y lee `servicio/estado`.

### ACL: define quien puede publicar/suscribirse a que topico
La tabla completa de usuarios y reglas esta en `docs/acl.md` (issue #2), y tiene que coincidir con este documento.

### Quien se suscribe y con qué wildcard
* `+`: reemplaza un nivel, cualquier sea su valor
* `#`: reemplaza cualquier cant de niveles, solo se puede poner al final
| Topic | Publica | Se suscribe | Patrón de suscripción |
|-------|---------|-------------|----------------------|
| `reportes/{tipo}/{unidad}/posicion` | unidad | servicio | `reportes/+/+/posicion` |
| `reportes/{tipo}/{unidad}/presencia` | unidad + Last Will | servicio | `reportes/+/+/presencia` |
| `comandos/{despachador}/{tipo}/{unidad}` | despachador | servicio | `comandos/+/+/+` |
| `pedidos/{clientid}/{tipo}` | público | servicio | `pedidos/+/+` |
| `flota/{tipo}/{unidad}/estado` | servicio | despachador | `flota/#` |
| `emergencias/{tipo}/{eid}` | servicio | despachador | `emergencias/#` |
| `alertas/{tipo}` | servicio | despachador | `alertas/#` |
| `respuestas/{despachador}` | servicio | ese despachador | `respuestas/desp-01` (sin wildcard) |
| `servicio/estado` | servicio + Last Will | despachador y público | `servicio/estado` (sin wildcard) |

### QoS y retained justificados por cada topic

No usamos QoS 2 ya que puede imponer un delay en los mensajes debido a su handshake de cuatro pasos, y el servicio que tenemos al igual que los payloads recibidos nos permiten deduplicar por id.

| Topic | QoS | Retained | Justificación |
|-------|-----|----------|---------------|
| `posicion` | 0 | no | Llega otra en 1–2 s, así que perder una no importa. Con QoS 1, al reconectar se reenviarían posiciones viejas. No lleva retained porque el estado oficial ya está en `flota/`. |
| `presencia` | 1 | sí | Un cambio de conexión no se puede perder. Un duplicado no hace daño: recibir "online" dos veces deja el mismo estado. Con retained, el servicio sabe quién está conectado aunque arranque sin sesión (ver "Retained y sesion persistente cubren casos distintos"). |
| `comandos` | 1 | no | Una orden no se puede perder, y un duplicado se descarta por id. No lleva retained porque es una acción única: si quedara retenida, el servicio la volvería a ejecutar cada vez que se reconecta. |
| `pedidos` | 1 | no | Igual que comandos. El duplicado se descarta por clientid + id, y el servicio devuelve el mismo eid sin crear otra emergencia. |
| `flota/.../estado` | 1 | sí | Un cambio de estado (libre a ocupada, a sin conexión) no se puede perder. Cada mensaje trae el estado completo, así que un duplicado no hace daño. Con retained, el mapa arranca con la flota completa (#14). |
| `emergencias` | 1 | sí (vacío al cerrar) | No se puede perder una emergencia. Lleva retained para que el despachador vea todas las abiertas al conectarse (ver la explicación de `emergencias` más arriba). |
| `alertas` | 1 | no | Un aviso no se puede perder: si el despachador estaba caído, lo recibe al volver gracias a su sesión persistente. No lleva retained porque es un aviso del momento y el estado ya queda en `flota/` y `emergencias/`. |
| `respuestas` | 1 | no | El despachador tiene que enterarse si un comando fue rechazado. No lleva retained porque cada respuesta corresponde a un comando puntual. |
| `servicio/estado` | 1 | sí | Lo usa el Last Will. Con retained, cualquiera que se conecta sabe al instante si el servicio está andando. |


### Idempotencia
QoS 1 puede entregar un mensaje mas de una vez, entonces el servicio tiene que poder recibir el mismo mensaje dos veces sin que cambie el resultado. Esto es lo que nos permite no usar QoS 2.

| Mensaje | Como se reconoce un duplicado | Que hace el servicio |
|---------|-------------------------------|----------------------|
| `comandos` | mismo `id` de comando ya procesado | No lo vuelve a aplicar. Reenvia la misma respuesta a `respuestas/{despachador}`, por si la primera se perdio. |
| `pedidos` | mismo `clientid` (del topico) + mismo `id` (del payload) | No crea otra emergencia. Guarda a que `eid` corresponde cada pedido y, si llega repetido, lo ignora. |
| `posicion` | `ts` menor o igual al ultimo aceptado de esa unidad | La descarta. Cubre duplicados y posiciones que llegan desordenadas. |
| Mensajes de estado (`presencia`, `flota`, `emergencias`, `servicio/estado`) | — | No hace falta nada: traen el estado completo, recibirlos dos veces deja el mismo resultado. |

* El `id` de comandos y pedidos lo genera el cliente (uuid4), uno por accion. Si el cliente reintenta la misma accion, manda el mismo `id`; si es una accion nueva, uno nuevo.
* El pedido se identifica por `clientid` + `id` y no solo por `id`, asi un civil no puede pisar el pedido de otro mandando el mismo `id`.
* Los ids ya procesados y el ultimo `ts` de cada unidad se guardan en SQLite (#11), no solo en memoria: si el servicio se reinicia y el broker le reentrega mensajes de la sesion persistente, los sigue reconociendo como duplicados.

### Sesión Persistente
La sesión persistente guarda las suscripciones del cliente y los mensajes que le llegan mientras está desconectado. Por eso solo le sirve a un cliente que recibe mensajes que no se pueden perder.

Para que funcione hay que cumplir cuatro condiciones:

1. Client ID fijo (servicio, desp-01). La sesión está atada al client ID: si cambia, el broker crea una sesión nueva.
2. clean_start=False y un session_expiry_interval distinto de 0 al conectar, porque es MQTT 5. Por ejemplo, un día para el servicio y una hora para el despachador.
3. Suscribirse con QoS 1. El broker entrega cada mensaje con el menor QoS entre el de la publicación y el de la suscripción. Si el servicio se suscribe a comandos/+/+/+ con QoS 0, los comandos le llegan como QoS 0 y no se guardan mientras está caído. Es un error fácil de cometer.
4. La cola tiene límite. EMQX guarda hasta max_mqueue_len mensajes por sesión (1000 por defecto). Para la demo alcanza, pero conviene mencionarlo en el informe.


| Cliente | ¿Sesión persistente? | Por qué |
|---------|----------------------|---------|
| Servicio | Sí | Si se cae, el broker le guarda los comandos, pedidos y cambios de presencia (QoS 1) que lleguen mientras tanto, y se los entrega al volver. Las posiciones son QoS 0 y no se guardan, pero no hace falta. |
| Despachador | Sí | Al volver recibe las alertas y respuestas que se perdió. Las emergencias y la flota igual le llegan por retained. |
| Unidades | No | No se suscriben a nada, solo publican. Si se cortan, los mensajes que no salieron quedan en la cola local de paho (la que ya maneja `publisher.py`), no en el broker. |
| Público | No | Solo lee `servicio/estado`, que es retained y le llega al conectarse. |
| Receptor | No aplica | No es un cliente MQTT. |

#### Retained y sesion persistente cubren casos distintos
La sesion persistente no reemplaza al retained, se usan los dos:
* **Sesion persistente = "lo que paso mientras estaba caido"** (eventos: comandos, pedidos, alertas, respuestas). Solo sirve si el cliente ya estaba suscripto y vuelve antes de que venza la sesion.
* **Retained = "como estan las cosas ahora"** (estado: presencia, flota, emergencias, servicio/estado). Funciona siempre, haya sesion o no.

Casos del servicio:
1. **Se cae y vuelve con la sesion viva**: recibe por la sesion lo que paso mientras no estaba. El retained no hace falta.
2. **Arranca sin sesion**: solo el retained le dice que unidades estan conectadas y el estado de cada cosa. Pasa cuando:
    * Es el primer arranque: las unidades se conectaron antes de que el servicio se suscribiera nunca, asi que el broker no tenia donde guardarle nada.
    * La sesion vencio: estuvo caido mas que el `session_expiry_interval`.
    * Se recreo el broker (`docker rm`, como en las pruebas de #21): las sesiones viven en el broker y se pierden.

Al reconectar con la sesion viva, si el cliente vuelve a suscribirse (paho lo hace en `on_connect`, lo pide #3), el broker ademas le manda el retained de cada topico. Puede recibir un mismo estado dos veces (por la cola y por el retained), pero no hace daño porque los mensajes de estado son idempotentes. Si se quiere evitar, MQTT 5 tiene la opcion **Retain Handling** al suscribirse: `1` = solo manda el retained si la suscripcion es nueva.

El Last Will no es un tercer mecanismo de entrega: es un mensaje comun que el broker publica en nombre del cliente caido, y le llega a los suscriptores por los caminos de siempre (en vivo, por la cola de la sesion o por retained).


### Last Will en que topicos y por qué
Van dos: uno para las unidades y otro para el servicio.

| Cliente	| Topic del Last Will	| Payload	| QoS / retained|
|---------|---------------------|---------|---------------|
|Unidad	|reportes/{tipo}/{unidad}/presencia	|{"v":1, "estado":"offline"}	| 1 / sí|
|Servicio	|servicio/estado	|{"v":1, "estado":"offline"}	| 1 / sí|
Por qué esos dos clientes:
* Unidades: el servicio tiene que saber si una unidad se cae para no asignarle emergencias, y para lanzar la alerta si estaba ocupada (duda 8).
* Servicio: el despachador y el público tienen que saber si el sistema está caído.


Dos detalles de MQTT 5 que les conviene saber:
* El Last Will solo sale si la desconexión es inesperada. Si la unidad se apaga bien (disconnect()), tiene que publicar offline ella misma antes de cerrar. La otra opción es desconectarse con el reason code 0x04 ("Disconnect with Will Message"), que le pide al broker que publique el Last Will igual.
* Will Delay Interval: se puede configurar un retraso de unos segundos antes de que el broker publique el Last Will. Si la unidad reconecta dentro de ese tiempo, el "offline" no se publica, y así un microcorte no genera una alerta ni desasigna la emergencia. Para su caso, unos 5–10 segundos estaría bien


### Payloads
* v en todos. Arranca en 1.
* ts en segundos Unix, como en el ejemplo de la duda 13 (1730000000).
* Un campo obligatorio que falta o tiene tipo incorrecto hace que se descarte el mensaje.
* Los campos desconocidos se ignoran.
* Los id son uuid4 en texto.
* La identidad de quien publica sale del topic, nunca del payload.

| Topico | Publica | Obligatorios | Opcionales | Ejemplo | Notas |
|--------|---------|--------------|------------|---------|-------|
| `reportes/{tipo}/{unidad}/posicion` | unidad | `v, ts, lat, lon` | `rumbo` (0–359) | `{"v":1,"ts":1730000000,"lat":-45.8641,"lon":-67.4966,"rumbo":180}` | Tipo y unidad salen del topico |
| `reportes/{tipo}/{unidad}/presencia` | unidad + Last Will | `v, estado` (`online`/`offline`) | `ts` | `{"v":1,"estado":"offline"}` | `ts` opcional: el Last Will se define al conectar, tendria la hora de conexion y no la de la caida |
| `comandos/{despachador}/{tipo}/{unidad}` | despachador | `v, id, ts, accion` (`asignar`/`liberar`) | `eid` (obligatorio si `asignar`) | `{"v":1,"id":"3f2a…","ts":1730000000,"accion":"asignar","eid":"E42"}` | `liberar` deja la unidad libre y cierra su emergencia |
| `pedidos/{clientid}/{tipo}` | publico | `v, id, ts, lat, lon` | — | `{"v":1,"id":"b7e4…","ts":1730000000,"lat":-45.8712,"lon":-67.5023}` | Repetido (mismo `clientid` + `id`) no crea otra emergencia |
| `flota/{tipo}/{unidad}/estado` | servicio | `v, ts, conexion` (`online`/`offline`), `disponibilidad` (`libre`/`ocupada`) | `lat, lon, rumbo`, `eid` (si ocupada) | `{"v":1,"ts":1730000000,"conexion":"online","disponibilidad":"ocupada","lat":-45.8641,"lon":-67.4966,"eid":"E42"}` | `lat`/`lon` opcionales: sin posicion valida todavia no aparece en el mapa |
| `emergencias/{tipo}/{eid}` | servicio | `v, eid, ts, lat, lon, estado` (`sin_asignar`/`asignada`) | `unidad` (si asignada) | `{"v":1,"eid":"E42","ts":1730000000,"lat":-45.8712,"lon":-67.5023,"estado":"asignada","unidad":"amb-07"}` | Al cerrar: payload vacio (no JSON) + retained. No lleva el `clientid` del civil |
| `alertas/{tipo}` | servicio | `v, ts, motivo` (`unidad_desconectada`/`posicion_invalida`), `mensaje` | `unidad, eid` | `{"v":1,"ts":1730000000,"motivo":"unidad_desconectada","mensaje":"amb-07 se desconecto con E42 en curso","unidad":"amb-07","eid":"E42"}` | `motivo` lo usa el codigo, `mensaje` se muestra al despachador |
| `respuestas/{despachador}` | servicio | `v, id, ts, ok` | `motivo` (si `ok` es false) | `{"v":1,"id":"3f2a…","ts":1730000001,"ok":false,"motivo":"amb-07 ocupada"}` | `id` = el del comando que responde |
| `servicio/estado` | servicio + Last Will | `v, estado` (`online`/`offline`) | `ts` | `{"v":1,"estado":"online","ts":1730000000}` | `ts` opcional por lo mismo que `presencia` |
