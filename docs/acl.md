# 🔐 Contrato de Seguridad: Roles, Usuarios y Tabla de ACL
> **Sistemas Distribuidos — UNPSJB (Proyecto 2)**  
> *Definición formal de identidades, sesiones y matriz de control de acceso (ACL) en el Broker MQTT.*  


---

## 1. Principios de Seguridad y Diseño

El esquema de seguridad del broker se basa en cuatro pilares:

1. **Principio de Menor Privilegio (Least Privilege):** Ningún cliente tiene permisos por encima de lo estrictamente necesario para su función. Cualquier acción no permitida de manera explícita cae en la regla final de denegación (`{deny, all}`).
2. **Identidad Forzada en el Tópico (`${username}` y `${clientid}`):** La identidad de quien publica viaja en la ruta del tópico, no en el payload. La ACL de EMQX reemplaza `${username}` o `${clientid}` dinámicamente según las credenciales validadas, imposibilitando que un cliente publique o suplante a otro.
3. **Aislamiento Total del Público:** Los usuarios civiles autenticados con la cuenta compartida `publico` publican segregados por su `${clientid}` aleatorio. No ven ninguna unidad (ni siquiera la asignada) ni ninguna emergencia: lo único que pueden leer es `servicio/estado`, para avisar si el sistema está caído (duda 4 y `topics.md`).
4. **Validación de Prefijo de Rol en Unidades:** Las credenciales de los móviles fijan su tipo de fuerza (`amb-` para ambulancias, `pol-` para policías, `bom-` para bomberos). Una ambulancia jamás puede emitir reportes bajo la jerarquía de bomberos o policía.

---

## 2. Matriz de Roles, Credenciales y Sesiones MQTT

A continuación se detalla la configuración técnica de conexión y sesión por cada rol del sistema (conforme a las decisiones de diseño y dudas 1, 15 y 16):

| Rol | Usuario MQTT (`username`) | Client ID (`client_id`) | Autenticación / Clave | Tipo de Sesión | Justificación de Sesión |
|---|---|---|---|---|---|
| **Unidad (Móvil)** | Uno por móvil (`amb-01..05`, `pol-01..05`, `bom-01..05`) | Idéntico al usuario (`${username}`) | Propia de cada unidad (archivo `emqx/usuarios`) | **No persistente** (`clean_start=True`) | Solo publica telemetría y presencia. Si se reconecta, no requiere encolado de mensajes en el broker (su historial lo maneja el Servicio Central). |
| **Despachador** | `desp-01` (general) | Idéntico al usuario (`desp-01`) | Propia del despachador | **Persistente** (`clean_start=False`, `session_expiry_interval=3600`) | Recibe alertas críticas y respuestas a sus comandos. Si se desconecta momentáneamente, el broker le guarda los eventos pendientes. |
| **Público (Civil)** | `publico` (compartido) | `publico-<32 hex>`: `"publico-" + uuid4().hex`, generado una vez por cada app | Misma clave para todos (embebida en la app cliente) | **No persistente** (`clean_start=True`) | Solo envía solicitudes de auxilio y consulta el estado del servicio. No requiere cola persistente en el broker. |
| **Servicio Central** | `servicio` | `servicio` | Clave fuerte configurada por variable de entorno | **Persistente** (`clean_start=False`, `session_expiry_interval=86400`) | Núcleo del sistema. Si se reinicia, el broker le retiene y entrega todos los comandos, pedidos y presencias acumulados. |
| **Receptor HTTP** | *Sin usuario MQTT* | *N/A* | Secreto compartido en header HTTP (`X-Webhook-Secret`) | *N/A* | No es un cliente MQTT; es invocado por EMQX vía webhook HTTP. |

### Cómo se hace cumplir (issue #6)
La ACL sola no alcanza: solo decide qué puede hacer un usuario **ya autenticado**. Faltan dos piezas que configura #6:
* **Sin anónimos.** Si EMQX no tiene un autenticador configurado, acepta cualquier conexión sin clave, y alguien podría conectarse con usuario `servicio` y la ACL lo trataría como el servicio. Por eso va la autenticación con usuario y clave (base interna cargada desde archivo, claves reales fuera del repo).
* **Client ID atado al usuario.** La columna "Client ID" de la tabla no se cumple sola: si nada lo controla, cualquiera con la clave del público (que va dentro de la app) se conecta con client ID `servicio`, EMQX desconecta al servicio real y le entrega su sesión persistente, con los comandos y pedidos encolados. Por eso va el autenticador `cinfo` primero en la cadena: usuario `publico` → client ID `publico-<32 hex>`; el resto → client ID igual al usuario.

---

## 3. Usuarios de Demostración

Para la versión de demo y evaluación (mínimo exigido en la consigna y duda 15):

### 15 Unidades Operativas (Móviles)
* **Ambulancias (5):** `amb-01`, `amb-02`, `amb-03`, `amb-04`, `amb-05`
* **Policías (5):** `pol-01`, `pol-02`, `pol-03`, `pol-04`, `pol-05`
* **Bomberos (5):** `bom-01`, `bom-02`, `bom-03`, `bom-04`, `bom-05`

### 1 Despachador General
* `desp-01`

### 5+ Instancias de Usuarios del Público
* Usuario: `publico`
* Client IDs de ejemplo generados por cliente (`"publico-" + uuid4().hex`: 32 hex sin guiones, lo que exige el autenticador `cinfo` de #6):
  * `publico-550e8400e29b41d4a716446655440001`
  * `publico-550e8400e29b41d4a716446655440002`
  * `publico-550e8400e29b41d4a716446655440003`
  * `publico-550e8400e29b41d4a716446655440004`
  * `publico-550e8400e29b41d4a716446655440005`

### 1 Servicio Backend
* `servicio`

---

## 4. Matriz de Control de Acceso (ACL por Rol)

Esta tabla resume qué tópicos puede **publicar** y a cuáles puede **suscribirse** cada rol:

| Rol | Publica en | Se suscribe a | Restricciones de Seguridad |
|---|---|---|---|
| **Unidad Ambulancia** (`^amb-`) | `reportes/ambulancia/${username}/posicion`<br/>`reportes/ambulancia/${username}/presencia` | *(Ninguno)* | Solo puede publicar su posición y presencia. No puede leer nada ni publicar en otras fuerzas. |
| **Unidad Policía** (`^pol-`) | `reportes/policia/${username}/posicion`<br/>`reportes/policia/${username}/presencia` | *(Ninguno)* | Aislado a la fuerza policial. No lee tópicos ajenos. |
| **Unidad Bomberos** (`^bom-`) | `reportes/bomberos/${username}/posicion`<br/>`reportes/bomberos/${username}/presencia` | *(Ninguno)* | Aislado a bomberos. No lee tópicos ajenos. |
| **Público** (`publico`) | `pedidos/${clientid}/+`<br/>*(ej. `pedidos/publico-3f2a…/bomberos`)* | `servicio/estado` | **No ve ninguna unidad ni emergencia.** Solo publica con su propio `clientid` y solo lee si el servicio está caído. |
| **Despachador** (`^desp-`) | `comandos/${username}/+/+` | `flota/#`<br/>`emergencias/#`<br/>`alertas/#`<br/>`respuestas/${username}`<br/>`servicio/estado` | Solo despacha comandos firmados con su propio `${username}`. Puede monitorear la totalidad de flota, incidentes y alertas del sistema. |
| **Servicio Central** (`servicio`) | `flota/#`<br/>`emergencias/#`<br/>`alertas/#`<br/>`respuestas/#`<br/>`servicio/estado` | `reportes/+/+/posicion`<br/>`reportes/+/+/presencia`<br/>`comandos/+/+/+`<br/>`pedidos/+/+` | Único emisor de la "verdad oficial" del sistema. Consume todas las entradas sin validar. |
| **Cualquier otro** | *(Denegado)* | *(Denegado)* | **Deny final:** Cualquier intento fuera de esta lista es rechazado por el broker. |

---

## 5. Reglas de ACL en Formato EMQX (`acl.conf`)

A continuación se presenta la sintaxis exacta de reglas de autorización que utiliza el motor de ACL basado en archivo de EMQX (`etc/acl.conf`).

El broker evalúa las reglas **de arriba hacia abajo** y aplica la primera que coincida con el usuario, la acción y el tópico.

```erlang
%% 1. servicio central: verdad oficial del sistema y consumo de entradas

{allow, {username, "servicio"}, publish, [
    "flota/#",
    "emergencias/#",
    "alertas/#",
    "respuestas/#",
    "servicio/estado"
]}.

{allow, {username, "servicio"}, subscribe, [
    "reportes/+/+/posicion",
    "reportes/+/+/presencia",
    "comandos/+/+/+",
    "pedidos/+/+"
]}.

%% 2. unidades móviles: el prefijo del usuario fija la fuerza permitida
%% solo sus dos topicos (posicion y presencia, que incluye el Last Will), no reportes/.../#
%% retain solo en presencia: en los demas topicos de entrada un mensaje retenido
%% se le volveria a entregar al servicio en cada reconexion

%% ambulancias
{allow, {username, {re, "^amb-"}}, {publish, [{retain, false}]}, [
    "reportes/ambulancia/${username}/posicion"
]}.
{allow, {username, {re, "^amb-"}}, publish, [
    "reportes/ambulancia/${username}/presencia"
]}.

%% policías
{allow, {username, {re, "^pol-"}}, {publish, [{retain, false}]}, [
    "reportes/policia/${username}/posicion"
]}.
{allow, {username, {re, "^pol-"}}, publish, [
    "reportes/policia/${username}/presencia"
]}.

%% bomberos
{allow, {username, {re, "^bom-"}}, {publish, [{retain, false}]}, [
    "reportes/bomberos/${username}/posicion"
]}.
{allow, {username, {re, "^bom-"}}, publish, [
    "reportes/bomberos/${username}/presencia"
]}.

%% 3. despachador: emite comandos propios y monitorea la flota y emergencias

{allow, {username, {re, "^desp-"}}, {publish, [{retain, false}]}, [
    "comandos/${username}/+/+"
]}.

{allow, {username, {re, "^desp-"}}, subscribe, [
    "flota/#",
    "emergencias/#",
    "alertas/#",
    "respuestas/${username}",
    "servicio/estado"
]}.

%% 4. público (civiles): pide emergencias con su ${clientid}, un nivel por servicio
%% no tiene topico de salida propio: no ve la flota ni las emergencias, solo si el sistema esta caido
{allow, {username, "publico"}, {publish, [{retain, false}]}, [
    "pedidos/${clientid}/+"
]}.

{allow, {username, "publico"}, subscribe, [
    "servicio/estado"
]}.

%% 5. denegación explícita por defecto (principio de menor privilegio)
{deny, all}.
```

---

## 6. Coincidencia y Trazabilidad con `docs/topics.md`

| Tópico en `docs/topics.md` | Publicador Permitido en ACL | Suscriptor Permitido en ACL | Coherencia de Contrato |
|---|---|---|---|
| `reportes/{tipo}/{unidad}/posicion` | Móvil con username coincidente (`^amb-`, `^pol-`, `^bom-`) | Servicio Central | Exacta: nadie más puede publicar ni leer posiciones en crudo. |
| `reportes/{tipo}/{unidad}/presencia` | Móvil con username coincidente (incluyendo Last Will) | Servicio Central | Exacta: Last Will validado con el mismo usuario. |
| `comandos/{despachador}/{tipo}/{unidad}` | Despachador autenticado como `${despachador}` | Servicio Central | Exacta: un despachador no puede ordenar en nombre de otro. |
| `pedidos/{clientid}/{tipo}` | Público con `${clientid}` coincidente | Servicio Central | Exacta: el civil no puede emitir pedidos haciéndose pasar por otro celular. |
| `flota/{tipo}/{unidad}/estado` | Exclusivo del Servicio Central | Despachador (`flota/#`) | Exacta: el público tiene vedado el acceso a la flota. |
| `emergencias/{tipo}/{eid}` | Exclusivo del Servicio Central | Despachador (`emergencias/#`) | Exacta: solo operadores ven el listado oficial de incidentes. |
| `alertas/{tipo}` | Exclusivo del Servicio Central | Despachador (`alertas/#`) | Exacta: notificaciones operativas dirigidas a despachadores. |
| `respuestas/{despachador}` | Exclusivo del Servicio Central | Despachador individual | Exacta: respuesta privada al operador que envió la orden. |
| `servicio/estado` | Exclusivo del Servicio Central (incluyendo Last Will) | Todos (Despachador y Público) | Exacta: semáforo de disponibilidad global del sistema. |

---

## 7. Análisis de Vectores de Ataque y Mitigación

* **Ataque: Suplantación de Móvil (Spoofing):**
  * *Escenario:* Un atacante intercepta la red y conoce que existe la ambulancia `amb-01`. Con su cliente intenta publicar `reportes/ambulancia/amb-01/posicion` pero autenticándose como `amb-02` o `pol-01`.
  * *Mitigación:* La regla `{allow, {username, {re, "^amb-"}}, publish, ["reportes/ambulancia/${username}/#"]}` exige que el `${username}` en el tópico sea exactamente el del usuario autenticado. Como `amb-02 != amb-01`, el broker descarta la publicación y aplica `{deny, all}`.
* **Ataque: Espionaje Ciudadano de la Flota:**
  * *Escenario:* Un usuario civil conecta su aplicación y se suscribe a `flota/#` o `reportes/#` para rastrear patrulleros o ambulancias en tiempo real.
  * *Mitigación:* El usuario `publico` solo tiene permiso de suscripción para `servicio/estado`. Cualquier intento de suscripción a `flota/#` es rechazado con error de autorización.
* **Ataque: Secuestro de Órdenes a Móviles:**
  * *Escenario:* Una unidad o un civil intenta enviar comandos a otra unidad en `comandos/...`.
  * *Mitigación:* Solo los usuarios con regex `^desp-` pueden publicar en `comandos/${username}/+/+`. Ni las unidades ni el público tienen regla de publicación sobre ese árbol, cayendo en el `deny` final.
* **Ataque: Mensaje hecho para romper al servicio:**
  * *Escenario:* Un cliente autenticado publica en su propio topic, que la ACL permite, un payload armado para que el servicio falle (un entero de cientos de dígitos, un JSON muy anidado), incluso con retained para que se repita en cada reconexión.
  * *Mitigación:* `payloads.validar` lo descarta como cualquier payload inválido, y `MQTTPub` no deja que una excepción de un callback mate el hilo de red (issue #45). La ACL prohíbe `retain` en los topics de entrada, salvo en `presencia`, que lo necesita. Se demuestra con `pruebas/tramposo_mensaje_hostil.py`.
* **Ataque: Inundar al despachador de alertas:**
  * *Escenario:* Una unidad manda posiciones inválidas a toda velocidad; cada alerta es QoS 1 y se encola en la sesión del despachador, hasta desplazar a las alertas reales.
  * *Mitigación:* El servicio descarta todas, pero publica como mucho una alerta cada 5 s por unidad (`ALERTA_CADA_SEG`).
