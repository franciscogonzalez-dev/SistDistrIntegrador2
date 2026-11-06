# Sesion del despachador con JWT

## Que es y que no es
El despachador pide usuario y clave al abrir, y mantiene la sesion con un JWT (RFC 7519, HS256) que dura 1 hora. Si vence, no deja asignar ni liberar hasta que el operador vuelva a iniciar sesion, sin cerrar la app.

**Es un control local de la app, no un control de seguridad del sistema.** El token no viaja por MQTT y el servicio no lo valida. Lo que impide que alguien actue en nombre de otro sigue siendo lo de siempre:
* El broker autentica el usuario y la clave de MQTT (#6).
* La ACL solo deja publicar en `comandos/${username}/+/+`, asi que el despachador del topico es el usuario autenticado.
* El servicio saca la identidad del topico, nunca del payload (`docs/topics.md`). El payload de `comandos` no cambia.

### Por que no viaja en los comandos
Con HS256 la misma clave firma y verifica. Para que el despachador emita el token tendria que tener la clave, y entonces podria firmar cualquier `sub`: el servicio no aprenderia nada que la ACL no le garantice ya. Ademas, un comando puede esperar hasta 24 h en la sesion persistente del servicio (`docs/topics.md`), mas que lo que dura el token, y se rechazaria al llegar.

Si hiciera falta validarlo en el servicio, el token lo tiene que emitir un componente que no sea el despachador (por ejemplo el servicio, con un topico y una regla de ACL nuevos), y el despachador no tiene que tener la clave.

## Como funciona
| Paso | Donde | Que hace |
|------|-------|----------|
| Login | `DialogoLogin` (`clientes/despachador.py`) | Pide usuario y clave. No la precarga |
| Validacion | `autenticar_despachador` (`clientes/credenciales.py`) | Solo usuarios `desp-*`. Compara contra `--password` o, si no se paso, contra `clientes/credenciales.json`. Nunca contra `credenciales.example.json` |
| Emision y verificacion | `comun/jwt_auth.py` | Firma con una clave aleatoria generada al abrir la app, que queda solo en memoria. Rechaza otro algoritmo que no sea HS256, y exige `exp` e `iss` |
| Vencimiento | `_verificar_autorizacion_jwt` | Antes de asignar o liberar revisa el token; si vencio pide login de nuevo con el mismo usuario |
| Indicador | badge de la barra superior | Se revisa cada 30 s y pasa de "JWT Activo" a "JWT Vencido" |

La clave de la conexion MQTT la sigue validando el broker: si la del login no coincide con la de EMQX, la app abre pero no conecta.

## Claims
| Claim | Ejemplo | Descripcion |
|-------|---------|-------------|
| `sub` | `"desp-01"` | Operador de la sesion |
| `rol` | `"despachador"` | Rol del operador |
| `permisos` | `["despacho:asignar", "despacho:liberar", "monitoreo:ver"]` | Informativo: hoy no se chequea |
| `iat` | `1730851200` | Emision, en segundos Unix |
| `exp` | `1730854800` | Vencimiento, 1 hora despues |
| `iss` | `"SistDistr-CentroDespacho"` | Emisor, tiene que coincidir |
| `jti` | `"c9d8a4f1-..."` | Id del token, para trazabilidad. No previene replay: no se guarda en ningun lado |

## Como mostrarlo en la demo
1. `py clientes/despachador.py` e iniciar sesion con `desp-01` y la clave de `clientes/credenciales.json`.
2. Hacer clic en el badge de la barra superior para ver los claims y el tiempo restante.
3. "Copiar Token para jwt.io" permite pegarlo en jwt.io para ver como se decodifica. La firma no se puede comprobar ahi: la clave no sale de la app, y no se pega ninguna clave en un sitio externo.

## Pruebas
`py -m unittest discover pruebas`. Las de este modulo estan en `pruebas/test_jwt.py` y no necesitan `credenciales.json`.
