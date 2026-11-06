"""
Pruebas unitarias para la autenticación y validación de JSON Web Tokens (JWT).
Verifica:
1. Generación de tokens estándar HS256 con claims (sub, rol, permisos, iat, exp, jti).
2. Verificación criptográfica con HMAC-SHA256 y detección de alteraciones.
3. Expiración de tokens (TokenExpirado).
4. Autenticación de operadores de despacho con emisión de token.
5. Inclusión y verificación de JWT en los comandos del despachador y el servicio.
"""

import unittest
import time
import json
from unittest.mock import MagicMock, patch

from comun import jwt_auth, payloads
from clientes.despachador import DespachadorBasico
from servicio.src.handlers import comandos


class TestJWTBasico(unittest.TestCase):

    def test_generacion_y_verificacion_exitosa(self):
        """Verifica emisión y decodificación correcta con firma intacta."""
        token = jwt_auth.generar_token(
            usuario="desp-01",
            rol="despachador",
            expiracion_segundos=1800,
        )
        self.assertIsInstance(token, str)
        partes = token.split(".")
        self.assertEqual(len(partes), 3)

        claims = jwt_auth.verificar_token(token)
        self.assertEqual(claims["sub"], "desp-01")
        self.assertEqual(claims["rol"], "despachador")
        self.assertIn("despacho:asignar", claims["permisos"])
        self.assertIn("despacho:liberar", claims["permisos"])
        self.assertTrue(claims["exp"] > claims["iat"])

    def test_token_expirado_falla(self):
        """Un token cuyo exp ya pasó debe lanzar TokenExpirado."""
        token = jwt_auth.generar_token(
            usuario="desp-01",
            expiracion_segundos=-5,  # Expirado hace 5 segundos
        )
        with self.assertRaises(jwt_auth.TokenExpirado):
            jwt_auth.verificar_token(token)

    def test_firma_alterada_falla(self):
        """Modificar cualquier caracter del token debe invalidar la firma."""
        token = jwt_auth.generar_token(usuario="desp-01")
        partes = token.split(".")
        # Alterar el payload Base64
        partes_corruptas = [partes[0], partes[1] + "X", partes[2]]
        token_corrupto = ".".join(partes_corruptas)

        with self.assertRaises(jwt_auth.FirmaInvalida):
            jwt_auth.verificar_token(token_corrupto)

    def test_secreto_distinto_falla(self):
        """Verificar con una clave secreta distinta debe fallar."""
        token = jwt_auth.generar_token(usuario="desp-01", secreto="clave_secreta_A")
        with self.assertRaises(jwt_auth.FirmaInvalida):
            jwt_auth.verificar_token(token, secreto="clave_secreta_B")

    def test_decodificar_sin_verificar(self):
        """Inspección de header y payload para depuración visual."""
        token = jwt_auth.generar_token(usuario="operador-test")
        header, payload = jwt_auth.decodificar_sin_verificar(token)
        self.assertEqual(header["alg"], "HS256")
        self.assertEqual(header["typ"], "JWT")
        self.assertEqual(payload["sub"], "operador-test")


class TestAutenticacionDespachador(unittest.TestCase):

    def test_autenticacion_correcta(self):
        """Verifica que con las credenciales de desp-01 se emita un JWT válido."""
        from clientes.credenciales import clave
        pwd = clave("desp-01")
        token, claims = jwt_auth.autenticar_despachador("desp-01", pwd)
        self.assertIsNotNone(token)
        self.assertEqual(claims["sub"], "desp-01")
        self.assertEqual(claims["rol"], "despachador")

    def test_autenticacion_fallida(self):
        """Contraseña errónea debe levantar ValueError."""
        with self.assertRaises(ValueError):
            jwt_auth.autenticar_despachador("desp-01", "clave_totalmente_incorrecta")


class TestIntegracionJWTDespachadorServicio(unittest.TestCase):

    def setUp(self):
        with patch("clientes.despachador.MQTTPub"), patch("tkintermapview.TkinterMapView.set_tile_server"):
            self.desp = DespachadorBasico(
                ip_broker="127.0.0.1",
                puerto=8883,
                client_id="desp-01",
                usuario="desp-01",
                password="test_password",
                ca_certs=None,
                pedir_login=False,
            )
            self.desp.root.withdraw()
            self.mock_cliente = MagicMock()
            self.desp.cliente = self.mock_cliente

    def tearDown(self):
        try:
            self.desp.root.destroy()
        except Exception:
            pass

    def test_despachador_incluye_jwt_en_comando_asignar(self):
        """El despachador incluye su JWT activo al publicar una orden de asignación."""
        self.assertIsNotNone(self.desp.token_jwt)
        self.desp.asignar_unidad("amb-01", "ambulancia", "E-100")

        self.mock_cliente.publish.assert_called_once()
        args, kwargs = self.mock_cliente.publish.call_args
        payload_enviado = args[0]
        self.assertEqual(payload_enviado["accion"], "asignar")
        self.assertEqual(payload_enviado["eid"], "E-100")
        self.assertEqual(payload_enviado["jwt"], self.desp.token_jwt)

        # Verificar que el servicio procese el comando y valide el JWT correctamente
        datos_procesados = comandos.procesar(
            cliente=self.mock_cliente,
            despachador="desp-01",
            tipo="ambulancia",
            unidad="amb-01",
            raw_payload=payload_enviado,
        )
        self.assertIsNotNone(datos_procesados)
        self.assertEqual(datos_procesados["jwt"], self.desp.token_jwt)

    def test_comando_con_jwt_expirado_es_rechazado_por_despachador(self):
        """Si el token del despachador expiró, no se debe publicar el comando."""
        # Forzar token expirado
        self.desp.token_jwt = jwt_auth.generar_token("desp-01", expiracion_segundos=-10)

        with patch("tkinter.messagebox.showwarning") as mock_warning:
            self.desp.asignar_unidad("amb-01", "ambulancia", "E-100")
            mock_warning.assert_called_once()
            self.mock_cliente.publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
