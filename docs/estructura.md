### Estructura del repositorio

Al lado de cada archivo esta el issue que lo crea o lo usa. Si un issue nuevo necesita un archivo que no esta aca, se agrega a este documento.

```
SistDistrIntegrador2/
├── comun/                        # lo que usan todos los componentes Python
│   ├── publisher.py              # cliente MQTT base: TLS, usuario, Last Will, sesion, subscribe (#3)
│   ├── topics.py                 # funciones que arman y parsean los topicos, nadie los arma a mano (#1)
│   ├── payloads.py               # campos obligatorios/opcionales y version "v" de cada mensaje (#1)
│   └── zona.geojson              # poligono de Comodoro Rivadavia, lo usan el servicio y el simulador (#9, #13)
│
├── broker/                       # notebook A (la del broker)
│   ├── compose.yaml              # EMQX + receptor, misma red (#5, #7)
│   ├── .env.example              # claves de ejemplo; el .env real no se sube (#5, #25)
│   ├── emqx/                     # configuracion versionada, sobrevive a borrar el container
│   │   ├── usuarios              # usuarios y claves (las reales fuera del repo) (#6)
│   │   ├── acl.conf              # reglas por patron de usuario + deny final (#6)
│   │   └── webhooks/reglas       # webhook directo y regla SQL de emergencia nueva (#17)
│   └── certs/                    # ca.crt y server.crt (las .key no se suben) (#4)
│
├── receptor/                     # servidor HTTP que recibe los webhooks (#7)
│   ├── Dockerfile
│   └── app.py                    # endpoints, chequeo del secreto, log y pagina de avisos
│
├── servicio/                     # notebook B (la del servicio)
│   ├── compose.yaml              # volumen para la base, restart, BROKER_HOST por env (#11)
│   ├── Dockerfile                # contexto de build en la raiz, para copiar comun/ (#11)
│   ├── src/
│   │   ├── main.py               # conexion, Last Will en servicio/estado, suscripciones (#8)
│   │   ├── validador.py          # zona, salto imposible, ts (#9)
│   │   ├── estado.py             # SQLite: flota, emergencias, ids procesados (#11)
│   │   └── handlers/             # un handler por tipo de mensaje (#8, #9, #10, #12)
│   │       ├── posicion.py       # (#9)
│   │       ├── presencia.py      # (#10)
│   │       ├── comandos.py       # (#12)
│   │       └── pedidos.py        # (#12)
│   └── tests/                    # tests de validacion que no necesitan broker (#9)
│
├── clientes/
│   ├── unidad.py                 # clase Unidad(MQTTPub) y simulador de recorrido (#13)
│   ├── flota_demo.py             # lanza las 15 unidades de la demo (#13)
│   └── mapa.py                   # mapa en modo despachador y en modo publico (#14, #15, #16)
│
├── pruebas/                      # scripts de cliente tramposo para la demo (#19)
│
├── scripts/
│   ├── generar_certs.sh          # CA propia y certificado del broker (#4)
│   └── autodeploy.sh             # opcional: redespliega cuando cambia main (#25)
│
├── docs/
│   ├── segundo_proyecto.md       # consigna
│   ├── dudas_resueltas.md        # decisiones del grupo
│   ├── topics.md                 # contrato de topicos, payloads, QoS y retained (#1)
│   ├── acl.md                    # usuarios y tabla de ACL (#2)
│   ├── estructura.md             # este documento
│   ├── arquitectura.drawio       # diagrama para el informe (#23)
│   └── capturas/                 # .pcapng y capturas de Wireshark con y sin TLS (#20)
│
├── requirements.txt              # dependencias de los clientes y el servicio (#22)
├── .gitignore                    # *.key, .env, bases SQLite (#4, #25)
└── README.md                     # comandos exactos para levantar todo (#22, #25)
```

#### Notas
* **Handlers por tipo de mensaje, no por tipo de unidad.** La #8 dice "handler por tipo de unidad", pero una ambulancia y un patrullero se procesan igual: lo que cambia es el tipo de mensaje (posicion, presencia, comando, pedido), y cada uno tiene su issue (#9, #10, #12). El tipo de unidad sale del topico y es un dato mas.
* **El tipo y el id salen solo del topico** (ver `topics.md`, seccion de identidad). El validador no compara el topico contra el payload, porque el payload no lleva id.
* **Lo que no se sube:** las `.key`, el `.env` con las claves reales y las bases SQLite. Para cada uno hay un ejemplo versionado (`.env.example`) o un script que lo genera (`generar_certs.sh`).
* **`comun/` se copia en el container del servicio**, por eso el contexto de build del Dockerfile es la raiz del repo (#11).
