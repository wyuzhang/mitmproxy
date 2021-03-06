import os
import socket
import struct
from OpenSSL import SSL

from mitmproxy import exceptions
from mitmproxy import flow
from mitmproxy.proxy.protocol import base
from mitmproxy.net import tcp
from mitmproxy.net import websockets
from mitmproxy.websocket import WebSocketFlow, WebSocketBinaryMessage, WebSocketTextMessage


class WebSocketLayer(base.Layer):
    """
        WebSocket layer to intercept, modify, and forward WebSocket messages.

        Only version 13 is supported (as specified in RFC6455).
        Only HTTP/1.1-initiated connections are supported.

        The client starts by sending an Upgrade-request.
        In order to determine the handshake and negotiate the correct protocol
        and extensions, the Upgrade-request is forwarded to the server.
        The response from the server is then parsed and negotiated settings are extracted.
        Finally the handshake is completed by forwarding the server-response to the client.
        After that, only WebSocket frames are exchanged.

        PING/PONG frames pass through and must be answered by the other endpoint.

        CLOSE frames are forwarded before this WebSocketLayer terminates.

        This layer is transparent to any negotiated extensions.
        This layer is transparent to any negotiated subprotocols.
        Only raw frames are forwarded to the other endpoint.

        WebSocket messages are stored in a WebSocketFlow.
    """

    def __init__(self, ctx, handshake_flow):
        super().__init__(ctx)
        self.handshake_flow = handshake_flow
        self.flow = None  # type: WebSocketFlow

        self.client_frame_buffer = []
        self.server_frame_buffer = []

    def _handle_frame(self, frame, source_conn, other_conn, is_server):
        if frame.header.opcode & 0x8 == 0:
            return self._handle_data_frame(frame, source_conn, other_conn, is_server)
        elif frame.header.opcode in (websockets.OPCODE.PING, websockets.OPCODE.PONG):
            return self._handle_ping_pong(frame, source_conn, other_conn, is_server)
        elif frame.header.opcode == websockets.OPCODE.CLOSE:
            return self._handle_close(frame, source_conn, other_conn, is_server)
        else:
            return self._handle_unknown_frame(frame, source_conn, other_conn, is_server)

    def _handle_data_frame(self, frame, source_conn, other_conn, is_server):
        fb = self.server_frame_buffer if is_server else self.client_frame_buffer
        fb.append(frame)

        if frame.header.fin:
            if frame.header.opcode == websockets.OPCODE.TEXT:
                t = WebSocketTextMessage
            else:
                t = WebSocketBinaryMessage

            payload = b''.join(f.payload for f in fb)
            fb.clear()

            websocket_message = t(self.flow, not is_server, payload)
            self.flow.messages.append(websocket_message)
            self.channel.ask("websocket_message", self.flow)

            # chunk payload into multiple 10kB frames, and send them
            payload = websocket_message.content
            chunk_size = 10240  # 10kB
            chunks = range(0, len(payload), chunk_size)
            frms = [
                websockets.Frame(
                    payload=payload[i:i + chunk_size],
                    opcode=frame.header.opcode,
                    mask=(False if is_server else 1),
                    masking_key=(b'' if is_server else os.urandom(4))) for i in chunks
            ]
            frms[-1].header.fin = 1

            for frm in frms:
                other_conn.send(bytes(frm))

        return True

    def _handle_ping_pong(self, frame, source_conn, other_conn, is_server):
        # just forward the ping/pong to the other side
        other_conn.send(bytes(frame))
        return True

    def _handle_close(self, frame, source_conn, other_conn, is_server):
        self.flow.close_sender = "server" if is_server else "client"
        if len(frame.payload) >= 2:
            code, = struct.unpack('!H', frame.payload[:2])
            self.flow.close_code = code
            self.flow.close_message = websockets.CLOSE_REASON.get_name(code, default='unknown status code')
        if len(frame.payload) > 2:
            self.flow.close_reason = frame.payload[2:]

        other_conn.send(bytes(frame))

        # close the connection
        return False

    def _handle_unknown_frame(self, frame, source_conn, other_conn, is_server):
        # unknown frame - just forward it
        other_conn.send(bytes(frame))

        sender = "server" if is_server else "client"
        self.log("Unknown WebSocket frame received from {}".format(sender), "info", [repr(frame)])

        return True

    def __call__(self):
        self.flow = WebSocketFlow(self.client_conn, self.server_conn, self.handshake_flow, self)
        self.flow.metadata['websocket_handshake'] = self.handshake_flow
        self.handshake_flow.metadata['websocket_flow'] = self.flow
        self.channel.ask("websocket_start", self.flow)

        client = self.client_conn.connection
        server = self.server_conn.connection
        conns = [client, server]

        try:
            while not self.channel.should_exit.is_set():
                r = tcp.ssl_read_select(conns, 1)
                for conn in r:
                    source_conn = self.client_conn if conn == client else self.server_conn
                    other_conn = self.server_conn if conn == client else self.client_conn
                    is_server = (conn == self.server_conn.connection)

                    frame = websockets.Frame.from_file(source_conn.rfile)

                    if not self._handle_frame(frame, source_conn, other_conn, is_server):
                        return
        except (socket.error, exceptions.TcpException, SSL.Error) as e:
            self.flow.error = flow.Error("WebSocket connection closed unexpectedly: {}".format(repr(e)))
            self.channel.tell("websocket_error", self.flow)
        finally:
            self.channel.tell("websocket_end", self.flow)
