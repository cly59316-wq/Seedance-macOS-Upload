import hashlib
import hmac
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import OrderedDict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from volcengine.ApiInfo import ApiInfo
    from volcengine.Credentials import Credentials
    from volcengine.ServiceInfo import ServiceInfo
    from volcengine.auth.SignerV4 import SignerV4
    from volcengine.base.Service import Service
    HAS_VOLCENGINE_SDK = True
except Exception:
    HAS_VOLCENGINE_SDK = False


HOST = "127.0.0.1"
PORT = int(os.environ.get("SEEDANCE_APP_PORT", "18765"))
ARK_BASE_URL = os.environ.get("SEEDANCE_ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
AMK_BASE_URL = os.environ.get("SEEDANCE_AMK_BASE_URL", "https://amk.cn-beijing.volces.com/api/v1").rstrip("/")
ASSETS_OPENAPI_HOST_TEMPLATE = os.environ.get("SEEDANCE_ASSETS_OPENAPI_HOST_TEMPLATE", "ark.{region}.volcengineapi.com")
LITTERBOX_UPLOAD_URL = "https://litterbox.catbox.moe/resources/internals/api.php"
NAIXIAI_UPLOAD_URL = "https://naixiai.cn/api/1/upload"
def _get_root_dir():
    """兼容 PyInstaller 打包后的资源路径"""
    if getattr(sys, "frozen", False):
        # PyInstaller 打包后 _MEIPASS 指向资源临时目录
        base = getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)
        return Path(base)
    return Path(__file__).resolve().parent


ROOT_DIR = _get_root_dir()
PROXY_TIMEOUT_SECONDS = 300


def make_json_response(handler, status_code, payload):
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_cors_headers()
    handler.end_headers()
    handler.wfile.write(body)


def resolve_assets_credentials(request_data, region):
    ak = (request_data.get("ak") or "").strip()
    sk = (request_data.get("sk") or "").strip()
    session_token = (request_data.get("sessionToken") or "").strip()

    if ak and sk:
        return ak, sk, session_token, "request"

    if HAS_VOLCENGINE_SDK:
        credentials = Credentials("", "", "ark", region, session_token)
        service_info = ServiceInfo(
            f"ark.{region}.volces.com",
            {},
            credentials,
            PROXY_TIMEOUT_SECONDS,
            PROXY_TIMEOUT_SECONDS,
            "https",
        )
        Service(service_info, {})
        if credentials.ak and credentials.sk:
            return credentials.ak, credentials.sk, credentials.session_token, "sdk-default"

    env_ak = (os.environ.get("VOLC_ACCESSKEY") or "").strip()
    env_sk = (os.environ.get("VOLC_SECRETKEY") or "").strip()
    env_token = (os.environ.get("VOLC_SESSION_TOKEN") or session_token).strip()
    if env_ak and env_sk:
        return env_ak, env_sk, env_token, "environment"

    return "", "", session_token, "missing"


def build_assets_signed_request(action, version, region, ak, sk, session_token, body):
    body_text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    body_bytes = body_text.encode("utf-8")
    query = OrderedDict([("Action", action), ("Version", version)])
    assets_host = ASSETS_OPENAPI_HOST_TEMPLATE.format(region=region)
    target_url = f"https://{assets_host}/?{urllib.parse.urlencode(query)}"

    if HAS_VOLCENGINE_SDK:
        credentials = Credentials(ak, sk, "ark", region, session_token)
        service_info = ServiceInfo(
            assets_host,
            {},
            credentials,
            PROXY_TIMEOUT_SECONDS,
            PROXY_TIMEOUT_SECONDS,
            "https",
        )
        api_info = {"assets": ApiInfo("POST", "/", query, {}, {})}
        service = Service(service_info, api_info)
        request = service.prepare_request(service.api_info["assets"], {})
        request.headers["Content-Type"] = "application/json; charset=utf-8"
        request.body = body_bytes
        SignerV4.sign(request, credentials)
        return {
            "url": request.build(),
            "body_bytes": body_bytes,
            "headers": dict(request.headers),
            "signer": "sdk",
        }

    signer = VolcSignHelper(ak, sk, region=region, service="ark")
    signed_headers = signer.sign(
        method="POST",
        uri="/",
        query=urllib.parse.urlencode(query),
        headers={"content-type": "application/json; charset=utf-8"},
        body_bytes=body_bytes,
    )
    request_headers = {k: v for k, v in signed_headers.items() if not k.startswith("__debug_")}
    return {
        "url": target_url,
        "body_bytes": body_bytes,
        "headers": request_headers,
        "signer": "manual",
        "canonical_request": signed_headers.get("__debug_canonical_request"),
        "string_to_sign": signed_headers.get("__debug_string_to_sign"),
    }


def build_assets_auth_error_message(upstream_body):
    try:
        payload = json.loads(upstream_body.decode("utf-8"))
    except Exception:
        return None

    error_info = payload.get("error") or {}
    if error_info.get("code") == "AuthenticationError":
        base_message = error_info.get("message") or "AuthenticationError"
        return (
            f"{base_message}。Assets API 使用的是火山引擎 Access Key / Secret Key (AK/SK)，"
            f"不是 Seedance 生成接口里的 API Key。请检查设置中的 Assets AK/SK，"
            f"或在环境变量 VOLC_ACCESSKEY / VOLC_SECRETKEY 中配置。"
        )
    return None


class VolcSignHelper:
    """Volcengine HMAC-SHA256 签名（兼容 AWS Signature Version 4）"""

    def __init__(self, ak, sk, region="cn-beijing", service="ark"):
        self.ak = ak
        self.sk = sk
        self.region = region
        self.service = service

    @staticmethod
    def _sha256_hash(data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _hmac_sha256(key, msg):
        if isinstance(key, str):
            key = key.encode("utf-8")
        if isinstance(msg, str):
            msg = msg.encode("utf-8")
        return hmac.new(key, msg, hashlib.sha256).digest()

    def sign(self, method, uri, query, headers, body_bytes):
        now = datetime.now(timezone.utc)
        date_stamp = now.strftime("%Y%m%d")
        datetime_stamp = now.strftime("%Y%m%dT%H%M%SZ")

        if body_bytes is None:
            body_bytes = b""
        payload_hash = self._sha256_hash(body_bytes)

        # 标准化为小写用于签名计算
        headers_lower = {k.lower(): v for k, v in headers.items()}
        headers_lower["host"] = f"ark.{self.region}.volces.com"
        headers_lower["x-date"] = datetime_stamp
        headers_lower["x-content-sha256"] = payload_hash

        signed_headers = sorted(headers_lower.keys())
        canonical_headers = "".join(f"{k}:{headers_lower[k]}\n" for k in signed_headers)
        signed_headers_str = ";".join(signed_headers)

        canonical_request = (
            f"{method.upper()}\n"
            f"{uri}\n"
            f"{query}\n"
            f"{canonical_headers}\n"
            f"{signed_headers_str}\n"
            f"{payload_hash}"
        )

        credential_scope = f"{date_stamp}/{self.region}/{self.service}/request"
        string_to_sign = (
            f"HMAC-SHA256\n"
            f"{datetime_stamp}\n"
            f"{credential_scope}\n"
            f"{self._sha256_hash(canonical_request)}"
        )

        signing_key = self._hmac_sha256(self.sk.encode("utf-8"), date_stamp)
        signing_key = self._hmac_sha256(signing_key, self.region)
        signing_key = self._hmac_sha256(signing_key, self.service)
        signing_key = self._hmac_sha256(signing_key, "request")

        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        auth_header = (
            f"HMAC-SHA256 Credential={self.ak}/{credential_scope}, "
            f"SignedHeaders={signed_headers_str}, Signature={signature}"
        )

        # 使用大驼峰键名返回，避免 urllib 发送时的大小写不确定性
        result = {
            "Content-Type": headers_lower.get("content-type", "application/json"),
            "Host": headers_lower["host"],
            "X-Date": headers_lower["x-date"],
            "X-Content-Sha256": headers_lower["x-content-sha256"],
            "Authorization": auth_header,
        }
        # 保留调试信息，方便排查
        result["__debug_canonical_request"] = canonical_request
        result["__debug_string_to_sign"] = string_to_sign
        return result


class SeedanceRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT_DIR), **kwargs)

    def log_message(self, format, *args):
        sys.stdout.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), format % args))

    def end_headers(self):
        self.send_cors_headers()
        super().end_headers()

    def send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Accept, X-API-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            make_json_response(self, HTTPStatus.OK, {
                "status": "ok",
                "service": "seedance-local-proxy",
                "ark_base_url": ARK_BASE_URL,
                "amk_base_url": AMK_BASE_URL,
                "port": PORT,
            })
            return

        if self.path.startswith("/api/v3/"):
            self.proxy_request("GET", self.build_ark_target_url())
            return

        if self.path.startswith("/proxy/amk/"):
            self.proxy_request("GET", self.build_amk_target_url())
            return

        if self.path == "/":
            self.path = "/index.html"

        super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/v3/"):
            self.proxy_request("POST", self.build_ark_target_url())
            return

        if self.path.startswith("/proxy/amk/"):
            self.proxy_request("POST", self.build_amk_target_url())
            return

        if self.path == "/proxy/litterbox":
            self.proxy_request("POST", LITTERBOX_UPLOAD_URL)
            return

        if self.path == "/proxy/naixiai":
            self.proxy_request("POST", NAIXIAI_UPLOAD_URL)
            return

        if self.path == "/proxy/assets":
            self.handle_assets_proxy()
            return

        make_json_response(self, HTTPStatus.NOT_FOUND, {"error": "Unsupported POST endpoint"})

    def build_ark_target_url(self):
        split_result = urllib.parse.urlsplit(self.path)
        upstream_path = split_result.path.removeprefix("/api/v3")
        rebuilt = urllib.parse.urlunsplit(("", "", upstream_path, split_result.query, ""))
        return f"{ARK_BASE_URL}{rebuilt}"

    def build_amk_target_url(self):
        split_result = urllib.parse.urlsplit(self.path)
        upstream_path = split_result.path.removeprefix("/proxy/amk")
        rebuilt = urllib.parse.urlunsplit(("", "", upstream_path, split_result.query, ""))
        return f"{AMK_BASE_URL}{rebuilt}"

    def _read_json_body(self):
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        if content_length == 0:
            return None
        body = self.rfile.read(content_length)
        try:
            return json.loads(body.decode("utf-8"))
        except Exception as e:
            return {"__raw_error__": str(e), "__raw_body__": body}

    def handle_assets_proxy(self):
        data = self._read_json_body()
        if data is None:
            make_json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Missing JSON body"})
            return

        action = data.get("action", "").strip()
        body = data.get("body", {})
        region = data.get("region", "cn-beijing").strip()
        version = data.get("version", "2024-01-01").strip()
        ak, sk, session_token, credentials_source = resolve_assets_credentials(data, region)

        if not ak or not sk:
            make_json_response(self, HTTPStatus.BAD_REQUEST, {
                "error": "assets_credentials_required",
                "message": "Assets API 需要火山引擎 AK/SK。请在设置中填写 Assets AK/SK，或配置环境变量 VOLC_ACCESSKEY / VOLC_SECRETKEY。",
            })
            return
        if not action:
            make_json_response(self, HTTPStatus.BAD_REQUEST, {"error": "action is required"})
            return

        signed_request = build_assets_signed_request(
            action=action,
            version=version,
            region=region,
            ak=ak,
            sk=sk,
            session_token=session_token,
            body=body,
        )

        # 调试日志（打印到控制台）
        sys.stderr.write(f"[AssetsProxy] action={action} url={signed_request['url']} signer={signed_request['signer']} credentials_source={credentials_source}\n")
        if signed_request.get("canonical_request"):
            sys.stderr.write(f"[AssetsProxy] canonical_request=\n{signed_request.get('canonical_request')}\n")
        if signed_request.get("string_to_sign"):
            sys.stderr.write(f"[AssetsProxy] string_to_sign=\n{signed_request.get('string_to_sign')}\n")

        upstream_request = urllib.request.Request(
            signed_request["url"],
            data=signed_request["body_bytes"],
            method="POST",
            headers=signed_request["headers"],
        )

        try:
            with urllib.request.urlopen(upstream_request, timeout=PROXY_TIMEOUT_SECONDS) as response:
                response_body = response.read()
                self.write_proxy_response(response.status, response.headers, response_body)
        except urllib.error.HTTPError as error:
            response_body = error.read()
            sys.stderr.write(f"[AssetsProxy] HTTPError {error.code}: {response_body.decode('utf-8', errors='replace')[:500]}\n")
            auth_error_message = build_assets_auth_error_message(response_body)
            if auth_error_message:
                make_json_response(self, error.code, {
                    "error": {"code": "AuthenticationError", "message": auth_error_message},
                })
                return
            self.write_proxy_response(error.code, error.headers, response_body)
        except urllib.error.URLError as error:
            make_json_response(self, HTTPStatus.BAD_GATEWAY, {
                "error": "ProxyError",
                "message": f"无法连接上游服务: {error.reason}",
                "target": signed_request["url"],
            })

    def proxy_request(self, method, target_url):
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        request_body = self.rfile.read(content_length) if content_length > 0 else None

        upstream_request = urllib.request.Request(
            target_url,
            data=request_body,
            method=method,
            headers=self.build_forward_headers(),
        )

        try:
            with urllib.request.urlopen(upstream_request, timeout=PROXY_TIMEOUT_SECONDS) as response:
                response_body = response.read()
                self.write_proxy_response(response.status, response.headers, response_body)
        except urllib.error.HTTPError as error:
            response_body = error.read()
            self.write_proxy_response(error.code, error.headers, response_body)
        except urllib.error.URLError as error:
            make_json_response(self, HTTPStatus.BAD_GATEWAY, {
                "error": "ProxyError",
                "message": f"无法连接到 {target_url}。当前版本默认通过本地 Python 服务代理请求；若你手动改成了外部直连地址，请检查目标接口、网络策略和浏览器拦截情况。",
                "target": target_url,
            })

    def build_forward_headers(self):
        allowed_headers = ["Authorization", "Content-Type", "Accept", "User-Agent", "X-API-Key"]
        forward_headers = {}
        for header_name in allowed_headers:
            header_value = self.headers.get(header_name)
            if header_value:
                forward_headers[header_name] = header_value

        if "Accept" not in forward_headers:
            forward_headers["Accept"] = "application/json"
        if "User-Agent" not in forward_headers:
            forward_headers["User-Agent"] = "SeedanceLocalProxy/1.0"
        return forward_headers

    def write_proxy_response(self, status_code, response_headers, body):
        self.send_response(status_code)

        excluded_headers = {
            "transfer-encoding",
            "connection",
            "keep-alive",
            "proxy-connection",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailers",
            "upgrade",
            "date",
            "server",
            "access-control-allow-origin",
            "access-control-allow-headers",
            "access-control-allow-methods",
        }

        for header_name, header_value in response_headers.items():
            if header_name.lower() in excluded_headers:
                continue
            if header_name.lower() == "content-length":
                continue
            self.send_header(header_name, header_value)

        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


def port_is_in_use(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def open_browser_delayed(url, delay=1.2):
    """延迟后自动打开浏览器"""
    import threading
    def _open():
        import time
        time.sleep(delay)
        webbrowser.open(url)
    threading.Thread(target=_open, daemon=True).start()


def main():
    if port_is_in_use(HOST, PORT):
        # 如果端口已被占用（可能是上一个实例），直接打开浏览器
        url = f"http://{HOST}:{PORT}"
        if getattr(sys, "frozen", False):
            webbrowser.open(url)
        print(f"端口 {PORT} 已被占用。")
        print(f"可改用环境变量 SEEDANCE_APP_PORT 指定新端口，例如：")
        print(f"  export SEEDANCE_APP_PORT=28765 && python app.py")
        sys.exit(1)

    server = ThreadingHTTPServer((HOST, PORT), SeedanceRequestHandler)
    url = f"http://{HOST}:{PORT}"

    # 打包后自动打开浏览器
    if getattr(sys, "frozen", False):
        open_browser_delayed(url)

    print("Seedance 本地服务已启动")
    print(f"访问地址: {url}")
    print(f"Ark 上游: {ARK_BASE_URL}")
    print("按 Ctrl+C 停止服务")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
