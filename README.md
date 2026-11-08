# SistDistrIntegrador2: cómo levantar el sistema

Guía para levantar el proyecto repartido en 4 computadoras. Todos los comandos son de **Git Bash**, parado en la carpeta del repo, salvo que se indique otra cosa.

| Computadora | Qué corre |
|---|---|
| **A** | Broker EMQX (Docker). Opcional: Servicio central |
| **B** | Despachador + Receptor de webhooks (Docker) + carpeta compartida |
| **C** | Unidades (ambulancias y policías) + Público. Opcional: Servicio central |
| **D** | Unidades (bomberos) + Público |

En todos los comandos, reemplazá `IP_A`, `IP_B`, etc. por la IP real de cada computadora. Para verla: `ipconfig | grep -i ipv4`.

> **Lo más importante:** el despachador le pasa las órdenes a las unidades por el archivo `clientes/despachos.txt`, no por MQTT. Como el despachador corre en B y las unidades en C y D, las unidades de C y D se ejecutan **desde una carpeta compartida de B** (paso 0.8). Si no, las asignaciones se confirman pero las unidades no se mueven en el mapa.

> **Comandos de Windows en Git Bash:** Git Bash convierte los argumentos que empiezan con `/` en rutas, y eso rompe comandos como `taskkill /F` o `w32tm /resync`. Por eso, esos comandos van precedidos de `MSYS_NO_PATHCONV=1`. Los pasos marcados **(admin)** se hacen en un Git Bash abierto con "Ejecutar como administrador".

---

## 0. Preparación (una sola vez por computadora)

### 0.1 Red
- Las 4 computadoras en la **misma red**, con el perfil de red en **Privada** (Configuración → Red → Propiedades).
- Anotar las 4 IPs. Si la red es por DHCP, revisarlas cada vez que se levanta el sistema: si la IP de A cambia, hay que regenerar el certificado (0.4).

### 0.2 Repo y Python (en las 4)
```bash
git clone <url-del-repo> SistDistrIntegrador2    # o git pull en main si ya está
cd SistDistrIntegrador2
git switch main
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
```
Docker Desktop tiene que estar instalado y prendido en **A** y **B** (y en **C** si el servicio va en Docker ahí).

### 0.3 Secretos (no van al repo, se copian a mano con un pendrive o la red)
| Archivo | Se crea en | Se copia a |
|---|---|---|
| `clientes/credenciales.json` | A | B, C, D |
| `broker/.env` | A | B (el receptor necesita el mismo `WEBHOOK_SECRET`) |
| `broker/certs/ca.crt` | A (paso 0.4) | B, C, D |

### 0.4 Certificado TLS con la IP de A (en A)
El certificado del broker tiene que incluir la IP de A. Si no, los clientes rechazan la conexión.
```bash
bash scripts/generar_certs.sh IP_A localhost 127.0.0.1
```
Después copiar `broker/certs/ca.crt` de A a B, C y D, pisando el que tengan.
⚠️ Los `.crt` regenerados **no se commitean**.

### 0.5 Usuarios de EMQX (en A)
```bash
.venv/Scripts/python.exe scripts/sincronizar_usuarios.py
```
Genera `broker/emqx/usuarios.csv` con las mismas claves que `credenciales.json`. EMQX importa ese archivo **solo la primera vez que arranca con un volumen vacío**. Si cambiaste alguna clave, hacé el reinicio limpio del paso 1.1.

### 0.6 Firewall (admin)
En **A**:
```bash
netsh advfirewall firewall add rule name="EMQX TLS 8883" dir=in action=allow protocol=TCP localport=8883
netsh advfirewall firewall add rule name="EMQX Dashboard 18083" dir=in action=allow protocol=TCP localport=18083
```
En **B**:
```bash
netsh advfirewall firewall add rule name="Receptor webhooks 5000" dir=in action=allow protocol=TCP localport=5000
```

### 0.7 Relojes sincronizados (en las 4, admin)
El servicio **descarta las posiciones cuyo `ts` difiera más de 10 s de su reloj** (`ts_desfasado`). Activá "Establecer la hora automáticamente" y sincronizá:
```bash
MSYS_NO_PATHCONV=1 w32tm /resync
date +%T     # comparar la hora en las 4: no puede haber más de unos pocos segundos de diferencia
```

### 0.8 Carpeta compartida en B (para que las unidades reciban las órdenes)
En **B** (admin), parado en la carpeta del repo:
```bash
touch clientes/despachos.txt
MSYS_NO_PATHCONV=1 net share SistDistr="$(cygpath -w "$PWD")" /GRANT:Todos,READ
```
(En un Windows en inglés, el grupo es `Everyone` en vez de `Todos`.)

En **C** y **D**, conectarse a la carpeta. Si pide usuario, poné el de B (`NOMBRE-PC-B\usuario`) y su clave:
```bash
MSYS_NO_PATHCONV=1 net use '\\IP_B\SistDistr'
.venv/Scripts/python.exe -c "import os,sys,time;print(time.ctime(os.path.getmtime(sys.argv[1])))" '\\IP_B\SistDistr\clientes\despachos.txt'
```
Prueba de que llegan los cambios: en B corré `echo prueba >> clientes/despachos.txt`, y en C repetí el último comando. La hora tiene que cambiar.

### 0.9 Prueba de conexión al broker (desde B, C y D, con el broker ya levantado)
```bash
openssl s_client -connect IP_A:8883 -CAfile broker/certs/ca.crt -verify_ip IP_A </dev/null 2>/dev/null | grep "Verify return code"
```
- `0 (ok)`: la conexión y el certificado están bien.
- `64 (IP address mismatch)`: el certificado no tiene la IP de A (0.4).
- No muestra nada: no hay conexión (firewall 0.6, IP equivocada o broker apagado).

Desde **A**, con el receptor levantado: `curl -s http://IP_B:5000/health` tiene que devolver `{"status":"healthy"}`.

---

## 1. Arranque (en este orden)

### 1.1 A: Broker EMQX
EMQX tiene configurado el receptor como `http://receptor:5000`, un nombre que solo existe si los dos corren en la misma máquina. Como el receptor va en B, se le cambia la URL con un archivo **fuera del repo**, en el Escritorio de A, sin tocar `emqx.conf`:
```bash
OV="$(cygpath -D)/override-receptor.yaml"
cat > "$OV" <<'EOF'
services:
  emqx:
    environment:
      - EMQX_CONNECTORS__HTTP__RECEPTOR__URL=http://IP_B:5000
EOF
```
(Cambiá `IP_B` adentro del archivo por la IP real.)

Levantar **solo** EMQX, sin el receptor:
```bash
cd broker
docker rm -f receptor_http 2>/dev/null     # por si quedó uno viejo en A
docker compose -f compose.yaml -f "$OV" up -d emqx
docker logs emqx_broker 2>&1 | grep "is running"
```
**Reinicio limpio (para empezar de cero):** borra los mensajes retenidos, las sesiones y las emergencias de corridas anteriores, y vuelve a importar los usuarios. Hacelo **junto con** el reinicio de la base del servicio (1.3); si no, el mapa queda desincronizado.
```bash
OV="$(cygpath -D)/override-receptor.yaml"
docker compose -f compose.yaml -f "$OV" down -v
docker compose -f compose.yaml -f "$OV" up -d emqx
```
Dashboard de EMQX: `http://IP_A:18083` (usuario y clave de `broker/.env`).

### 1.2 B: Receptor de webhooks
```bash
cd broker
docker compose up -d --build receptor
curl -s http://localhost:5000/health
```
Página de avisos: `http://localhost:5000` en el navegador de B. Al conectarse cualquier cliente tienen que aparecer avisos "se conectó".

### 1.3 Servicio central: elegir **una** de las dos opciones

**Opción A: Servicio en la computadora A (Docker)**
Crear `servicio/.env`:
```bash
cat > servicio/.env <<EOF
MQTT_HOST=IP_A
MQTT_PORT=8883
MQTT_USUARIO=servicio
MQTT_PASSWORD=$(.venv/Scripts/python.exe -c "import json;print(json.load(open('clientes/credenciales.json'))['servicio'])")
MQTT_CA_CERTS=broker/certs/ca.crt
SERVICIO_DB=servicio/data/bd_servicio.db
EOF
```
⚠️ `MQTT_HOST` va con la IP de A, **no** `127.0.0.1`: dentro del contenedor, `127.0.0.1` es el propio contenedor.
```bash
cd servicio
docker compose up -d --build
docker logs -f servicio        # "Conexion aceptada: Success"; Ctrl+C sale del log, no apaga el servicio
```
Reinicio limpio de la base: `docker compose down -v` y después `docker compose up -d --build`.

*(Alternativa sin Docker en A: igual que la Opción C, pero con `MQTT_HOST=127.0.0.1`.)*

**Opción C: Servicio en la computadora C (Python)**
```bash
export MQTT_HOST=IP_A MQTT_PORT=8883 MQTT_USUARIO=servicio MQTT_CA_CERTS=broker/certs/ca.crt
export MQTT_PASSWORD="$(.venv/Scripts/python.exe -c "import json;print(json.load(open('clientes/credenciales.json'))['servicio'])")"
.venv/Scripts/python.exe servicio/src/main.py
```
Dejar esa terminal abierta; Ctrl+C lo apaga. Reinicio limpio de la base: con el servicio apagado, `rm servicio/data/bd_servicio.db`.
*(También puede ir en Docker en C, con el mismo `servicio/.env` de la Opción A, incluido `MQTT_HOST=IP_A`.)*

### 1.4 B: Despachador
```bash
.venv/Scripts/python.exe clientes/despachador.py --broker IP_A
```
Se abre el login: usuario `desp-01`, y la clave de `desp-01` que está en `credenciales.json`. La sesión JWT dura 1 hora; si vence mientras se usa, la app vuelve a pedir el login.

### 1.5 C y D: Unidades (desde la carpeta compartida de B)
Las unidades quedan en segundo plano. Cada una escribe su log en `~/unidades/<id>.log` y su PID de Windows en `~/unidades/<id>.pid`, que después sirve para simular una caída. **No cierres esa terminal** mientras las unidades estén corriendo.
**Cada unidad tiene que correr una sola vez en toda la red**: si dos computadoras lanzan la misma, EMQX desconecta a una de las dos. No usar `flota-demo.py` en C y en D a la vez.

En **C**, parado en el repo local (se usa su `.venv`):
```bash
R='\\IP_B\SistDistr\clientes\unidad.py'
mkdir -p ~/unidades
for u in amb-01 amb-02 amb-03 amb-04 amb-05 pol-01 pol-02 pol-03 pol-04 pol-05; do
  .venv/Scripts/python.exe "$R" --id $u --broker IP_A --silencioso > ~/unidades/$u.log 2>&1 &
  cat /proc/$!/winpid > ~/unidades/$u.pid
done
```
En **D**, lo mismo con los bomberos:
```bash
R='\\IP_B\SistDistr\clientes\unidad.py'
mkdir -p ~/unidades
for u in bom-01 bom-02 bom-03 bom-04 bom-05; do
  .venv/Scripts/python.exe "$R" --id $u --broker IP_A --silencioso > ~/unidades/$u.log 2>&1 &
  cat /proc/$!/winpid > ~/unidades/$u.pid
done
```
- Para ver qué hace una unidad: `tail -f ~/unidades/amb-01.log`
- Para mostrar el validador, agregá `--invalidas 20` al comando: cada 20 reportes la unidad manda una posición falsa.
- Las rutas reales usan OSRM, que necesita **internet**. Sin internet, las unidades se mueven en línea recta.

### 1.6 C y D: Público
```bash
.venv/Scripts/python.exe clientes/publico.py --broker IP_A --prueba
```
`--prueba` muestra un selector de 10 puntos de Comodoro, incluidos uno en el mar y uno fuera de la ciudad. Si querés dos ventanas de público en la misma computadora, la segunda va con `--perfil 2`.

---

## 2. Chequeo rápido antes de empezar
- [ ] Despachador: servicio **online** y 15 unidades en el mapa (verdes = libres).
- [ ] Público: el banner muestra que el servicio está disponible.
- [ ] Receptor (`http://localhost:5000` en B): avisos de conexión de las unidades.
- [ ] Un pedido de prueba desde Público genera una emergencia ⚠️ en el despachador.
- [ ] Al asignarla, la unidad sale hacia la emergencia (si no se mueve, revisar 0.8).

## 3. Recorrido del circuito completo
1. **Pedido → emergencia:** desde Público, pedir una ambulancia. Aparece ⚠️ en el despachador.
2. **Asignar:** elegir la emergencia y una ambulancia libre. El servicio confirma ✅, la unidad se pone roja y viaja por calles reales.
3. **Rechazos del servicio:** asignar una unidad ocupada, o de otra fuerza, muestra ❌ con el motivo.
4. **Liberar:** "Marcar libre". La emergencia se cierra y la unidad vuelve a su base.
5. **Validador:** un pedido "En el Mar" o "Fuera de Comodoro" no genera emergencia; las posiciones con `--invalidas` se descartan y aparecen alertas.
6. **Caída de una unidad ocupada:** en la computadora de esa unidad, terminarla a la fuerza (por ejemplo bom-01, en D):
   ```bash
   MSYS_NO_PATHCONV=1 taskkill /F /T /PID $(cat ~/unidades/bom-01.pid)
   ```
   Llega una alerta, la emergencia vuelve a "sin asignar" y el botón 🔁 permite reasignarla. Para volver a levantarla, usá el mismo comando del paso 1.5, con `u=bom-01`.
7. **Caída del servicio:** en Docker, `docker kill servicio`; con Python, Ctrl+C en su terminal. El despachador y el público muestran el servicio **offline**. Al volver a levantarlo (`docker start servicio` o el mismo comando de 1.3), retoma con el estado guardado.
8. **Seguridad:** TLS en 8883 (el 1883 está apagado), capturas de Wireshark en `docs/capturas/`, el JWT del despachador (botón 🔑) y los avisos del receptor, con conexiones y publicaciones denegadas por ACL.

⚠️ **No corras `pruebas/test_seguridad_emqx.py` con el sistema en marcha.** Se conecta como `servicio` y `desp-01` (los echa y borra la sesión del servicio) y deja amb-01 como offline. Si querés mostrarlo, corrélo **antes** de levantar el servicio y las unidades:
```bash
MQTT_HOST=IP_A .venv/Scripts/python.exe pruebas/test_seguridad_emqx.py    # 37/37
```

## 4. Apagar todo
- **C y D, unidades:**
  ```bash
  for f in ~/unidades/*.pid; do MSYS_NO_PATHCONV=1 taskkill /F /T /PID $(cat "$f"); done
  ```
- **C y D, público:** cerrar su ventana.
- **B:** cerrar el despachador; `cd broker && docker compose down`.
- **Servicio:** `cd servicio && docker compose down`, o Ctrl+C si corre con Python. Sin `-v`, la base se conserva.
- **A:** `cd broker && docker compose -f compose.yaml -f "$(cygpath -D)/override-receptor.yaml" down`.
- **B (admin):** sacar la carpeta compartida: `MSYS_NO_PATHCONV=1 net share SistDistr /DELETE`.

## 5. Problemas comunes
| Síntoma | Causa probable / solución |
|---|---|
| `Bad user name or password` | `usuarios.csv` desactualizado respecto de `credenciales.json`: correr 0.5 y el reinicio limpio de 1.1. |
| Error de certificado / `CERTIFICATE_VERIFY_FAILED` | El certificado no tiene la IP de A, o el `ca.crt` no es el de A (0.4); comprobalo con 0.9. |
| Una unidad se conecta y se desconecta sin parar | La misma unidad está corriendo en dos computadoras. |
| Las unidades no se mueven al asignarlas | No corren desde `\\IP_B\SistDistr` o la carpeta compartida no anda (0.8). |
| Muchas posiciones `ts_desfasado` en el log del servicio | Relojes desincronizados (0.7). |
| El receptor no recibe avisos | Falta el override de 1.1, el firewall de B bloquea el 5000, o `WEBHOOK_SECRET` distinto en A y B. |
| El despachador muestra emergencias viejas | Retenidos de una corrida anterior: reinicio limpio de broker **y** servicio. |
| El servicio en Docker no conecta | `MQTT_HOST` quedó en `127.0.0.1`: tiene que ser `IP_A`. |
| `taskkill` dice que el argumento no es válido | Faltó el `MSYS_NO_PATHCONV=1` delante. |
| El público ve "enviado" pero no aparece la emergencia | Punto fuera de zona, o más de 5 pedidos por minuto desde esa instancia. El público no recibe ese rechazo (por ACL solo lee `servicio/estado`). |
