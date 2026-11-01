# 🔐 Contrato de Seguridad: Roles, Usuarios y Tabla de ACL
> **Sistemas Distribuidos — UNPSJB (Proyecto 2)**  
> *Definición formal de identidades, sesiones y matriz de control de acceso (ACL) en el Broker MQTT.*  


---

## 1. Principios de Seguridad y Diseño

El esquema de seguridad del broker se basa en cuatro pilares:

1. **Principio de Menor Privilegio (Least Privilege):** Ningún cliente tiene permisos por encima de lo estrictamente necesario para su función. Cualquier acción no permitida de manera explícita cae en la regla final de denegación (`{deny, all}`).
2. **Identidad Forzada en el Tópico (`${username}` y `${clientid}`):** La identidad de quien publica viaja en la ruta del tópico, no en el payload. La ACL de EMQX reemplaza `${username}` o `${clientid}` dinámicamente según las credenciales validadas, imposibilitando que un cliente publique o suplante a otro.
3. **Aislamiento Total del Público:** Los usuarios civiles autenticados con la cuenta compartida `publico` tienen acceso segregado por su `${clientid}` aleatorio. No pueden ver la flota policial/médica, no pueden ver emergencias de otros usuarios y solo pueden recibir avisos hacia su propio ID y leer el estado global del servicio.
4. **Validación de Prefijo de Rol en Unidades:** Las credenciales de los móviles fijan su tipo de fuerza (`amb-` para ambulancias, `pol-` para policías, `bom-` para bomberos). Una ambulancia jamás puede emitir reportes bajo la jerarquía de bomberos o policía.

---

## 2. Matriz de Roles, Credenciales y Sesiones MQTT

A continuación se detalla la configuración técnica de conexión y sesión por cada rol del sistema (conforme a las decisiones de diseño y dudas 1, 15 y 16):

| Rol | Usuario MQTT (`username`) | Client ID (`client_id`) | Autenticación / Clave | Tipo de Sesión | Justificación de Sesión |
|---|---|---|---|---|---|
| **Unidad (Móvil)** | Uno por móvil (`amb-01..05`, `pol-01..05`, `bom-01..05`) | Idéntico al usuario (`${username}`) | Propia de cada unidad (archivo `emqx/usuarios`) | **No persistente** (`clean_start=True`) | Solo publica telemetría y presencia. Si se reconecta, no requiere encolado de mensajes en el broker (su historial lo maneja el Servicio Central). |
| **Despachador** | `desp-01` (general) | Idéntico al usuario (`desp-01`) | Propia del despachador | **Persistente** (`clean_start=False`, `session_expiry_interval=3600`) | Recibe alertas críticas y respuestas a sus comandos. Si se desconecta momentáneamente, el broker le guarda los eventos pendientes. |
| **Público (Civil)** | `publico` (compartido) | `publico-<uuid>` (único generado por cada app) | Misma clave para todos (embebida en la app cliente) | **No persistente** (`clean_start=True`) | Solo envía solicitudes de auxilio y consulta el estado del servicio. No requiere cola persistente en el broker. |
| **Servicio Central** | `servicio` | `servicio` | Clave fuerte configurada por variable de entorno | **Persistente** (`clean_start=False`, `session_expiry_interval=86400`) | Núcleo del sistema. Si se reinicia, el broker le retiene y entrega todos los comandos, pedidos y presencias acumulados. |
| **Receptor HTTP** | *Sin usuario MQTT* | *N/A* | Secreto compartido en header HTTP (`X-Webhook-Secret`) | *N/A* | No es un cliente MQTT; es invocado por EMQX vía webhook HTTP. |

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
* Client IDs de ejemplo generados por cliente:
  * `publico-550e8400-e29b-41d4-a716-446655440001`
  * `publico-550e8400-e29b-41d4-a716-446655440002`
  * `publico-550e8400-e29b-41d4-a716-446655440003`
  * `publico-550e8400-e29b-41d4-a716-446655440004`
  * `publico-550e8400-e29b-41d4-a716-446655440005`

### 1 Servicio Backend
* `servicio`

---

## 4. Matriz de Control de Acceso (ACL por Rol)

Esta tabla resume qué tópicos puede **publicar** y a cuáles puede **suscribirse** cada rol:

| Rol | Publica en | Se suscribe a | Restricciones de Seguridad |
|---|---|---|---|
| **Unidad Ambulancia** (`^amb-`) | `reportes/ambulancia/${username}/#` | *(Ninguno)* | Solo puede publicar su posición y presencia. No puede leer nada ni publicar en otras fuerzas. |
| **Unidad Policía** (`^pol-`) | `reportes/policia/${username}/#` | *(Ninguno)* | Aislado a la fuerza policial. No lee tópicos ajenos. |
| **Unidad Bomberos** (`^bom-`) | `reportes/bomberos/${username}/#` | *(Ninguno)* | Aislado a bomberos. No lee tópicos ajenos. |
| **Público** (`publico`) | `pedidos/${clientid}/#`<br/>*(ej. `pedidos/${clientid}/emergencia`)* | `usuarios/${clientid}/#`<br/>`servicio/estado` | **No ve la flota ni emergencias ajenas.** Solo publica con su propio `clientid` y solo recibe respuestas directas a su ID. |
| **Despachador** (`^desp-`) | `comandos/${username}/#` | `flota/#`<br/>`emergencias/#`<br/>`alertas/#`<br/>`usuarios/${username}/#`<br/>`respuestas/${username}`<br/>`servicio/estado` | Solo despacha comandos firmados con su propio `${username}`. Puede monitorear la totalidad de flota, incidentes y alertas del sistema. |
| **Servicio Central** (`servicio`) | `flota/#`<br/>`emergencias/#`<br/>`alertas/#`<br/>`respuestas/#`<br/>`usuarios/#`<br/>`servicio/estado` | `reportes/+/+/posicion`<br/>`reportes/+/+/presencia`<br/>`comandos/+/+/+`<br/>`pedidos/+/+` | Único emisor de la "verdad oficial" del sistema. Consume todas las entradas sin validar. |
| **Cualquier otro** | *(Denegado)* | *(Denegado)* | **Deny final:** Cualquier intento fuera de esta lista es rechazado por el broker. |

---

## 5. Reglas de ACL en Formato EMQX (`acl.conf`)

A continuación se presenta la sintaxis exacta de reglas de autorización que utiliza el motor de ACL basado en archivo de EMQX (`etc/acl.conf`).

El broker evalúa las reglas **de arriba hacia abajo** y aplica la primera que coincida con el usuario, la acción y el tópico.

```erlang
%% ====================================================================
%% REGLAS DEL SERVICIO CENTRAL
%% ====================================================================
%% El servicio central publica la información oficial y procesa entradas
{allow, {username, "servicio"}, publish, [
    "flota/#",
    "emergencias/#",
    "alertas/#",
    "respuestas/#",
    "usuarios/#",
    "servicio/estado"
]}.

{allow, {username, "servicio"}, subscribe, [
    "reportes/+/+/posicion",
    "reportes/+/+/presencia",
    "comandos/+/+/+",
    "pedidos/+/+"
]}.

%% ====================================================================
%% REGLAS PARA UNIDADES MÓVILES (Prefijo determina la fuerza)
%% ====================================================================
%% Ambulancias: solo publican bajo reportes/ambulancia/${username}/...
{allow, {username, {re, "^amb-"}}, publish, [
    "reportes/ambulancia/${username}/#"
]}.

%% Policías: solo publican bajo reportes/policia/${username}/...
{allow, {username, {re, "^pol-"}}, publish, [
    "reportes/policia/${username}/#"
]}.

%% Bomberos: solo publican bajo reportes/bomberos/${username}/...
{allow, {username, {re, "^bom-"}}, publish, [
    "reportes/bomberos/${username}/#"
]}.

%% ====================================================================
%% REGLAS PARA EL DESPACHADOR
%% ====================================================================
%% El despachador solo puede emitir comandos bajo su propio nombre de usuario
{allow, {username, {re, "^desp-"}}, publish, [
    "comandos/${username}/#"
]}.

%% El despachador monitorea la flota, emergencias, alertas y estado global
{allow, {username, {re, "^desp-"}}, subscribe, [
    "flota/#",
    "emergencias/#",
    "alertas/#",
    "usuarios/${username}/#",
    "respuestas/${username}",
    "servicio/estado"
]}.

%% ====================================================================
%% REGLAS PARA EL PÚBLICO GENERAL (CIVILES)
%% ====================================================================
%% El usuario compartido 'publico' solo publica en su propio namespace clientid
{allow, {username, "publico"}, publish, [
    "pedidos/${clientid}/#"
]}.

%% El civil solo puede escuchar respuestas a su ID y el estado del servicio
{allow, {username, "publico"}, subscribe, [
    "usuarios/${clientid}/#",
    "servicio/estado"
]}.

%% ====================================================================
%% REGLA POR DEFECTO: DENEGAR TODO LO DEMÁS
%% ====================================================================
{deny, all}.
```

---

## 6. Coincidencia y Trazabilidad con `docs/topics.md`

| Tópico en `docs/topics.md` | Publicador Permitido en ACL | Suscriptor Permitido en ACL | Coherencia de Contrato |
|---|---|---|---|
| `reportes/{tipo}/{unidad}/posicion` | Móvil con username coincidente (`^amb-`, `^pol-`, `^bom-`) | Servicio Central | Exacta: nadie más puede publicar ni leer posiciones en crudo. |
| `reportes/{tipo}/{unidad}/presencia` | Móvil con username coincidente (incluyendo Last Will) | Servicio Central | Exacta: Last Will validado con el mismo usuario. |
| `comandos/{despachador}/{tipo}/{unidad}` | Despachador autenticado como `${despachador}` | Servicio Central | Exacta: un despachador no puede ordenar en nombre de otro. |
| `pedidos/{clientid}/{tipo}` *(ej. emergencia)* | Público con `${clientid}` coincidente | Servicio Central | Exacta: el civil no puede emitir pedidos haciéndose pasar por otro celular. |
| `flota/{tipo}/{unidad}/estado` | Exclusivo del Servicio Central | Despachador (`flota/#`) | Exacta: el público tiene vedado el acceso a la flota. |
| `emergencias/{tipo}/{eid}` | Exclusivo del Servicio Central | Despachador (`emergencias/#`) | Exacta: solo operadores ven el listado oficial de incidentes. |
| `alertas/{tipo}` | Exclusivo del Servicio Central | Despachador (`alertas/#`) | Exacta: notificaciones operativas dirigidas a despachadores. |
| `respuestas/{despachador}` / `usuarios/{despachador}/#` | Exclusivo del Servicio Central | Despachador individual | Exacta: respuesta privada al operador que envió la orden. |
| `usuarios/{clientid}/#` | Exclusivo del Servicio Central | Público individual (`${clientid}`) | Exacta: confirmación privada al civil que solicitó asistencia. |
| `servicio/estado` | Exclusivo del Servicio Central (incluyendo Last Will) | Todos (Despachador y Público) | Exacta: semáforo de disponibilidad global del sistema. |

---

## 7. Análisis de Vectores de Ataque y Mitigación

* **Ataque: Suplantación de Móvil (Spoofing):**
  * *Escenario:* Un atacante intercepta la red y conoce que existe la ambulancia `amb-01`. Con su cliente intenta publicar `reportes/ambulancia/amb-01/posicion` pero autenticándose como `amb-02` o `pol-01`.
  * *Mitigación:* La regla `{allow, {username, {re, "^amb-"}}, publish, ["reportes/ambulancia/${username}/#"]}` exige que el `${username}` en el tópico sea exactamente el del usuario autenticado. Como `amb-02 != amb-01`, el broker descarta la publicación y aplica `{deny, all}`.
* **Ataque: Espionaje Ciudadano de la Flota:**
  * *Escenario:* Un usuario civil conecta su aplicación y se suscribe a `flota/#` o `reportes/#` para rastrear patrulleros o ambulancias en tiempo real.
  * *Mitigación:* El usuario `publico` solo tiene permiso de suscripción para `usuarios/${clientid}/#` y `servicio/estado`. Cualquier intento de suscripción a `flota/#` es rechazado con error de autorización.
* **Ataque: Secuestro de Órdenes a Móviles:**
  * *Escenario:* Una unidad o un civil intenta enviar comandos a otra unidad en `comandos/...`.
  * *Mitigación:* Solo los usuarios con regex `^desp-` pueden publicar en `comandos/${username}/#`. Ni las unidades ni el público tienen regla de publicación sobre ese árbol, cayendo en el `deny` final.
