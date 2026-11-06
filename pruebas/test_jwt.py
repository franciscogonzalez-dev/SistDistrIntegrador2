"""
Pruebas de la sesion local con JWT del despachador (docs/jwt.md).
Verifica:
1. Emision y validacion del token: firma, algoritmo fijado, exp e iss obligatorios.
2. Login: solo despachadores, sin credenciales.example.json, con credenciales propias del test.
3. El token no viaja en los comandos: el contrato de docs/topics.md no cambia.
4. Sesion vencida: no se publica, se pide login de nuevo y el badge lo refleja.
5. La ventana de login real: no precarga la clave, acepta la correcta y rechaza la incorrecta.
"""

import base64
import hashlib
import hmac
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from comun import jwt_auth, payloads
from clientes.credenciales import autenticar_despachador
from clientes.despachador import DespachadorBasico, DialogoLogin

SECRETO_TEST = "clave-solo-para-tests"
HS256 = {"alg": "HS256", "typ": "JWT"}


def _b64(datos):
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode()


def _firmar(header, claims, digest=hashlib.sha256):
    h = _b64(json.dumps(header).encode())
    p = _b64(json.dumps(claims).encode())
    firma = _b64(hmac.new(SECRETO_TEST.encode(), f"{h}.{p}".encode(), digest).digest())
    return f"{h}.{p}.{firma}"


def _claims(**cambios):
    ahora = int(time.time())
    claims = {"sub": "desp-01", "iss": jwt_auth.EMISOR_DEFAULT, "iat": ahora, "exp": ahora + 60}
    claims.update(cambios)
    return claims


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

    def test_token_firmado_afuera_no_vale_con_la_clave_del_proceso(self):
        """Un token de otra app (otra clave) no se acepta, aunque tenga rol de sobra."""
        token = _firmar(HS256, _claims(rol="admin", permisos=["*"]))
        with self.assertRaises(jwt_auth.FirmaInvalida):
            jwt_auth.verificar_token(token)

    def test_decodificar_sin_verificar(self):
        """Inspección de header y payload para depuración visual."""
        token = jwt_auth.generar_token(usuario="operador-test")
        header, payload = jwt_auth.decodificar_sin_verificar(token)
        self.assertEqual(header["alg"], "HS256")
        self.assertEqual(header["typ"], "JWT")
        self.assertEqual(payload["sub"], "operador-test")

    def test_algoritmo_fijado(self):
        """El algoritmo no lo elige el token: se rechaza none y cualquier otro."""
        h_none = _b64(json.dumps({"alg": "none"}).encode())
        p = _b64(json.dumps(_claims()).encode())
        casos = {
            "none sin firma": (f"{h_none}.{p}.", jwt_auth.FirmaInvalida),
            "none firmado": (_firmar({"alg": "none"}, _claims()), jwt_auth.TokenInvalido),
            "HS512": (_firmar({"alg": "HS512"}, _claims(), hashlib.sha512), jwt_auth.FirmaInvalida),
            "sin alg": (_firmar({"typ": "JWT"}, _claims()), jwt_auth.TokenInvalido),
        }
        for nombre, (token, error) in casos.items():
            with self.subTest(nombre), self.assertRaises(error):
                jwt_auth.verificar_token(token, secreto=SECRETO_TEST)

    def test_exp_e_iss_obligatorios(self):
        sin_exp = _claims()
        del sin_exp["exp"]
        casos = {
            "sin exp": sin_exp,
            "exp NaN": _claims(exp=float("nan")),
            "exp bool": _claims(exp=True),
            "exp texto": _claims(exp="mañana"),
            "iss ajeno": _claims(iss="otro"),
        }
        for nombre, claims in casos.items():
            with self.subTest(nombre), self.assertRaises(jwt_auth.TokenInvalido):
                jwt_auth.verificar_token(_firmar(HS256, claims), secreto=SECRETO_TEST)

    def test_vence_justo_en_exp(self):
        ahora = int(time.time())
        with patch("comun.jwt_auth.time.time", return_value=ahora):
            with self.assertRaises(jwt_auth.TokenExpirado):
                jwt_auth.verificar_token(_firmar(HS256, _claims(exp=ahora)), secreto=SECRETO_TEST)


class TestAutenticacionDespachador(unittest.TestCase):

    def setUp(self):
        #credenciales propias del test: no depende de clientes/credenciales.json, que no esta en el repo
        carpeta = tempfile.TemporaryDirectory()
        self.addCleanup(carpeta.cleanup)
        self.archivo = Path(carpeta.name) / "credenciales.json"
        self.archivo.write_text(json.dumps({"desp-01": "clave-desp", "amb-01": "clave-amb"}), encoding="utf-8")

    def test_autenticacion_correcta(self):
        """Con la clave correcta se emite un JWT válido."""
        token, claims = autenticar_despachador("desp-01", "clave-desp", archivo=self.archivo)
        self.assertIsNotNone(token)
        self.assertEqual(claims["sub"], "desp-01")
        self.assertEqual(claims["rol"], "despachador")

    def test_autenticacion_fallida(self):
        """Contraseña errónea debe levantar ValueError."""
        with self.assertRaisesRegex(ValueError, "incorrectos"):
            autenticar_despachador("desp-01", "clave_totalmente_incorrecta", archivo=self.archivo)

    def test_usuario_desconocido_da_el_mismo_error(self):
        """El mensaje no revela si el usuario existe."""
        with self.assertRaisesRegex(ValueError, "incorrectos"):
            autenticar_despachador("desp-99", "cualquiera", archivo=self.archivo)

    def test_una_unidad_no_recibe_token_de_despachador(self):
        with self.assertRaisesRegex(ValueError, "despachador"):
            autenticar_despachador("amb-01", "clave-amb", archivo=self.archivo)

    def test_sin_archivo_no_usa_el_de_ejemplo(self):
        with self.assertRaisesRegex(ValueError, "Falta"):
            autenticar_despachador("desp-01", "cambiar", archivo=self.archivo.with_name("no-existe.json"))

    def test_clave_por_cli_no_necesita_archivo(self):
        no_existe = self.archivo.with_name("no-existe.json")
        token, _ = autenticar_despachador("desp-01", "clave-cli", clave_esperada="clave-cli", archivo=no_existe)
        self.assertIsNotNone(token)
        with self.assertRaises(ValueError):
            autenticar_despachador("desp-01", "otra", clave_esperada="clave-cli", archivo=no_existe)


class TestContratoComandos(unittest.TestCase):

    def test_el_comando_no_tiene_campo_jwt(self):
        """La identidad sale del topico: el payload de comandos no cambia (docs/topics.md)."""
        self.assertNotIn("jwt", payloads.COMANDO)
        self.assertEqual(set(payloads.comando(accion="asignar", eid="E1")), {"v", "id", "ts", "accion", "eid"})


class _ConDespachador(unittest.TestCase):

    def _crear(self, **kwargs):
        with patch("clientes.despachador.MQTTPub"), patch("tkintermapview.TkinterMapView.set_tile_server"):
            return DespachadorBasico(
                ip_broker="127.0.0.1",
                puerto=8883,
                client_id="desp-01",
                usuario="desp-01",
                ca_certs=None,
                **kwargs,
            )


class TestSesionDespachador(_ConDespachador):

    def setUp(self):
        self.desp = self._crear(password="test_password", token_jwt=jwt_auth.generar_token("desp-01"))
        self.desp.root.withdraw()
        self.mock_cliente = MagicMock()
        self.desp.cliente = self.mock_cliente
        self.addCleanup(self._cerrar)

    def _cerrar(self):
        try:
            self.desp.root.destroy()
        except Exception:
            pass

    def _vencer_sesion(self):
        self.desp.token_jwt = jwt_auth.generar_token("desp-01", expiracion_segundos=-10)

    def _dialogo(self, resultado):
        #login simulado: el dialogo real se prueba en TestVentanaLogin
        return (
            patch("clientes.despachador.DialogoLogin", return_value=MagicMock(resultado=resultado)),
            patch.object(self.desp.root, "wait_window"),
            patch("tkinter.messagebox.showwarning"),
        )

    def test_el_comando_publicado_no_lleva_el_token(self):
        self.desp.asignar_unidad("amb-01", "ambulancia", "E-100")
        self.desp.liberar_unidad("amb-02", "ambulancia")

        self.assertEqual(self.mock_cliente.publish.call_count, 2)
        for llamada in self.mock_cliente.publish.call_args_list:
            self.assertNotIn("jwt", llamada.args[0])

    def test_sin_sesion_no_se_publica(self):
        self.desp.token_jwt = None
        with patch("tkinter.messagebox.showerror") as mock_error:
            self.desp.asignar_unidad("amb-01", "ambulancia", "E-100")
        mock_error.assert_called_once()
        self.mock_cliente.publish.assert_not_called()

    def test_sesion_vencida_y_cancela_el_login_no_publica(self):
        self._vencer_sesion()
        d, w, m = self._dialogo(None)
        with d, w, m as mock_warning:
            self.desp.asignar_unidad("amb-01", "ambulancia", "E-100")
        mock_warning.assert_called_once()
        self.mock_cliente.publish.assert_not_called()
        self.assertIn("Vencido", self.desp.btn_sesion_jwt.cget("text"))

    def test_sesion_vencida_y_renueva_publica_sin_reiniciar(self):
        self._vencer_sesion()
        nuevo = jwt_auth.generar_token("desp-01")
        d, w, m = self._dialogo(("desp-01", "clave", nuevo, jwt_auth.verificar_token(nuevo)))
        with d, w, m:
            self.desp.liberar_unidad("amb-01", "ambulancia")
        self.mock_cliente.publish.assert_called_once()
        self.assertEqual(self.desp.token_jwt, nuevo)
        self.assertIn("Activo", self.desp.btn_sesion_jwt.cget("text"))

    def test_renovar_con_otro_usuario_no_cambia_la_sesion(self):
        """La conexion MQTT sigue siendo de desp-01: no se puede renovar como otro."""
        self._vencer_sesion()
        vencido = self.desp.token_jwt
        ajeno = jwt_auth.generar_token("desp-02")
        d, w, m = self._dialogo(("desp-02", "clave", ajeno, jwt_auth.verificar_token(ajeno)))
        with d, w, m, patch("tkinter.messagebox.showerror") as mock_error:
            self.desp.liberar_unidad("amb-01", "ambulancia")
        mock_error.assert_called_once()
        self.mock_cliente.publish.assert_not_called()
        self.assertEqual(self.desp.token_jwt, vencido)

    def test_el_badge_refleja_el_vencimiento(self):
        self.desp._actualizar_badge_jwt()
        self.assertIn("Activo", self.desp.btn_sesion_jwt.cget("text"))
        self._vencer_sesion()
        self.desp._actualizar_badge_jwt()
        self.assertIn("Vencido", self.desp.btn_sesion_jwt.cget("text"))


class TestVentanaLogin(_ConDespachador):
    """Usa la ventana real: se completa y se acepta sola desde el bucle de eventos de Tk."""

    def _operar_login(self, accion):
        original = DialogoLogin.__init__
        vistos = {}

        def init(dialogo, *args, **kwargs):
            original(dialogo, *args, **kwargs)
            vistos["clave_inicial"] = dialogo.ent_clave.get()
            dialogo.after(10, lambda: accion(dialogo))

        return patch.object(DialogoLogin, "__init__", init), vistos

    def test_login_correcto_no_precarga_la_clave_y_emite_token(self):
        def accion(dialogo):
            dialogo.ent_clave.insert(0, "clave-cli")
            dialogo._ingresar()

        parche, vistos = self._operar_login(accion)
        with parche:
            desp = self._crear(password="clave-cli", pedir_login=True)
        self.addCleanup(desp.root.destroy)

        self.assertEqual(vistos["clave_inicial"], "")
        self.assertEqual(jwt_auth.verificar_token(desp.token_jwt)["sub"], "desp-01")

    def test_clave_incorrecta_muestra_error_y_cancelar_cierra(self):
        def accion(dialogo):
            dialogo.ent_clave.insert(0, "mala")
            dialogo._ingresar()
            dialogo._cancelar()

        parche, _ = self._operar_login(accion)
        with parche, patch("tkinter.messagebox.showerror") as mock_error:
            with self.assertRaises(SystemExit):
                self._crear(password="clave-cli", pedir_login=True)
        mock_error.assert_called_once()


if __name__ == "__main__":
    unittest.main()
