"""Authentication and listener policy for the host submission/transfer service."""

import hmac
import ipaddress

import grpc


def is_loopback(host):
    host = host.strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_rpc_security(settings):
    if bool(settings.rpc_tls_cert) != bool(settings.rpc_tls_key):
        raise ValueError("RPC_TLS_CERT and RPC_TLS_KEY must be configured together")
    validate_token(settings.api_token)
    validate_token(settings.agent_token)
    from compiler_optimizer.shared.agents_rpc import AGENT_NAMES

    if settings.role in AGENT_NAMES and not is_loopback(settings.grpc_host):
        if not (settings.api_token and settings.rpc_tls_cert and settings.rpc_tls_key):
            raise ValueError("non-loopback agents require TLS and AGENT_API_TOKEN")
    if settings.role == "coordinator" and (
        settings.transfer_enabled or settings.execution_enabled or settings.fleet_enabled
    ):
        if not is_loopback(settings.grpc_host) and not (
            settings.api_token and settings.rpc_tls_cert and settings.rpc_tls_key
        ):
            raise ValueError("non-loopback execution/transfer requires TLS and OPTIMIZER_API_TOKEN")


def validate_token(token):
    if token and not (32 <= len(token) <= 512 and all(33 <= ord(c) <= 126 for c in token)):
        raise ValueError("bearer token must contain 32–512 printable non-space ASCII characters")


class BearerAuth(grpc.ServerInterceptor):
    """One operator token defines one trusted deployment, not per-user tenancy."""

    def __init__(self, token):
        self.expected = ("Bearer " + token).encode("ascii") if token else None

    def intercept_service(self, continuation, details):
        handler = continuation(details)
        if handler is None or self.expected is None:
            return handler
        values = [value for key, value in details.invocation_metadata if key == "authorization"]
        authorized = (
            len(values) == 1
            and isinstance(values[0], str)
            and values[0].isascii()
            and hmac.compare_digest(values[0].encode("ascii"), self.expected)
        )
        if authorized:
            return handler

        def denied(request, context):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "valid bearer authentication required")

        factory = (
            (
                grpc.stream_stream_rpc_method_handler
                if handler.response_streaming
                else grpc.stream_unary_rpc_method_handler
            )
            if handler.request_streaming
            else (
                grpc.unary_stream_rpc_method_handler
                if handler.response_streaming
                else grpc.unary_unary_rpc_method_handler
            )
        )
        return factory(
            denied,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
