# Autenticación y Autorización con JSON Web Tokens (JWT)

> **Módulo:** Proyecto 2 - Sistemas Distribuidos (UNPSJB)  
> **Estándar:** RFC 7519 / RFC 7515  
> **Algoritmo de Firma:** HMAC-SHA256 (`HS256`)

---

## 1. Motivación y Arquitectura

En este proyecto, los componentes distribuidos (unidades de emergencia, servicio central y despachador) se comunican a través de un broker MQTT seguro con TLS y control de acceso (ACL).

Para complementar la seguridad a nivel de transporte y protocolo (TLS + credenciales MQTT por usuario/rol), se incorporó **JWT (JSON Web Tokens)** en la capa de aplicación:
1. **Inicio de Sesión del Operador:** El despachador solicita credenciales de operador antes de iniciar la jornada operativa.
2. **Generación de Token Firmado (HS256):** Al validar la identidad contra las credenciales autorizadas, se expide un token JWT autocontenido con los roles y permisos del despachador.
3. **Control de Autorización Local:** Antes de ejecutar acciones sensibles (asignar móviles a incidentes o liberar móviles), la interfaz verifica criptográficamente que el operador tenga un token activo y no expirado.
4. **Propagación Distribuida del Token por MQTT:** Las órdenes publicadas en `comandos/desp-01/{tipo}/{unidad}` viajan con el token JWT adjunto en el payload JSON.
5. **Validación en el Servicio Central:** El servicio central valida la firma y el tiempo de vida del JWT antes de registrar o delegar la orden.

---

## 2. Estructura del Token JWT

El token sigue el formato estándar `header.payload.signature` codificado en **Base64URL**:

### Encabezado (Header)
```json
{
  "alg": "HS256",
  "typ": "JWT"
}
```

### Claims (Payload)
| Claim | Nombre estándar | Ejemplo | Descripción |
|---|---|---|---|
| `sub` | Subject | `"desp-01"` | Identificador único del operador autenticado |
| `rol` | Custom Claim | `"despachador"` | Rol operativo del usuario |
| `permisos` | Custom Claim | `["despacho:asignar", "despacho:liberar", "monitoreo:ver"]` | Lista de acciones permitidas |
| `iat` | Issued At | `1730851200` | Timestamp Unix del momento de emisión |
| `exp` | Expiration Time | `1730854800` | Timestamp Unix de caducidad (1 hora por defecto) |
| `iss` | Issuer | `"SistDistr-CentroDespacho"` | Entidad emisora del token |
| `jti` | JWT ID | `"c9d8a4f1-..."` | UUID único del token para prevenir ataques de replay y trazabilidad |

### Firma (Signature)
Calculada mediante HMAC-SHA256 sobre `Base64URL(Header) + "." + Base64URL(Payload)` utilizando la clave secreta compartida (`JWT_SECRET`).

---

## 3. Módulos y Modificaciones Realizadas

### A. Núcleo JWT (`comun/jwt_auth.py`)
- Implementación completa en Python estándar (`hmac`, `hashlib`, `base64`, `json`, `time`), sin requerir librerías externas obligatorias (100% portable y compatible con PyJWT y jwt.io).
- Funciones principales:
  - `generar_token(...)`: Crea y firma el token con los claims correspondientes.
  - `verificar_token(...)`: Comprueba firma criptográfica, vigencia temporal (`exp`) y estructura.
  - `autenticar_despachador(usuario, password)`: Valida las credenciales contra `clientes/credenciales.json` y emite el token.
  - `decodificar_sin_verificar(...)`: Permite inspeccionar los claims para visualización en pantalla.

### B. Ventana de Login y Sesión en el Despachador (`clientes/despachador.py`)
- **Diálogo Modal de Login (`DialogoLogin`):** Se muestra al arrancar el despachador de forma interactiva. Permite ingresar usuario y contraseña.
- **Indicador de Sesión Activa:** En la barra superior se añadió un botón/badge:
  `🔑 desp-01 (JWT Activo 🟢)`
- **Inspector de Token JWT:** Al hacer clic en el badge, se abre una ventana modal con:
  - Token compacto en Base64URL.
  - Botón **"Copiar Token para jwt.io"** para pegarlo en vivo durante la defensa.
  - Claims decodificados (JSON) y tiempo restante de expiración.
- **Validación previa a despachar:** En `asignar_unidad()` y `liberar_unidad()`, el despachador valida su token antes de publicar la orden.
- **Comandos enriquecidos:** El comando MQTT incluye el campo `"jwt": self.token_jwt`.

### C. Contrato de Payloads (`comun/payloads.py`)
- El esquema `COMANDO` acepta opcionalmente `"jwt": (_texto, False)`, manteniendo total retrocompatibilidad con clientes o pruebas existentes.

### D. Verificación en el Servicio Central (`servicio/src/handlers/comandos.py`)
- Al recibir una orden en `comandos/+/+/+`, si el payload contiene un token JWT, el servicio valida la firma y el operador emisor, registrándolo en los logs del sistema.

---

## 4. Guía para Demostración y Defensa

### 1. Iniciar con Login Interactivo
Ejecutar el despachador:
```bash
py clientes/despachador.py
```
Aparecerá la ventana de inicio de sesión. Iniciar con:
- **Usuario:** `desp-01`
- **Contraseña:** `Desp#Cr2026.00!` (la registrada en `clientes/credenciales.json`)

### 2. Inspeccionar el Token en Vivo
1. Hacer clic en el badge verde `🔑 desp-01 (JWT Activo 🟢)` en la barra superior.
2. Observar los claims (`sub`, `rol`, `permisos`, `exp`, `jti`).
3. Hacer clic en **"Copiar Token para jwt.io"**.
4. Abrir en el navegador [https://jwt.io](https://jwt.io) y pegar el token: se verá exactamente la misma estructura y firma válida con la clave secreta.

### 3. Ejecutar la Suite de Pruebas Automatizadas
Para demostrar la robustez y cobertura de pruebas:
```bash
py -m unittest discover -s pruebas
```
Se ejecutan 25 pruebas unitarias, incluyendo:
- Verificación de firma y estructura.
- Rechazo de tokens expirados (`TokenExpirado`).
- Detección de firmas adulteradas (`FirmaInvalida`).
- Autenticación correcta y fallida de credenciales.
- Inclusión del JWT en las órdenes de despacho.
