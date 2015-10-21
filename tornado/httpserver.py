#!/usr/bin/env python
#
# Copyright 2009 Facebook
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""A non-blocking, single-threaded HTTP server.

Typical applications have little direct interaction with the `HTTPServer`
class except to start a server at the beginning of the process
(and even that is often done indirectly via `tornado.web.Application.listen`).

.. versionchanged:: 4.0

   The ``HTTPRequest`` class that used to live in this module has been moved
   to `tornado.httputil.HTTPServerRequest`.  The old name remains as an alias.
"""

from __future__ import absolute_import, division, print_function, with_statement

import socket

from tornado.escape import native_str
from tornado.http1connection import HTTP1ServerConnection, HTTP1ConnectionParameters
from tornado import gen
from tornado import httputil
from tornado import iostream
from tornado import netutil
from tornado.tcpserver import TCPServer


class HTTPServer(TCPServer, httputil.HTTPServerConnectionDelegate):
    r"""A non-blocking, single-threaded HTTP server.

    A server is defined by either a request callback that takes a
    `.HTTPServerRequest` as an argument or a `.HTTPServerConnectionDelegate`
    instance.

    A simple example server that echoes back the URI you requested::

        import tornado.httpserver
        import tornado.ioloop

        def handle_request(request):
           message = "You requested %s\n" % request.uri
           request.connection.write_headers(
               httputil.ResponseStartLine('HTTP/1.1', 200, 'OK'),
               {"Content-Length": str(len(message))})
           request.connection.write(message)
           request.connection.finish()

        http_server = tornado.httpserver.HTTPServer(handle_request)
        http_server.listen(8888)
        tornado.ioloop.IOLoop.instance().start()

    Applications should use the methods of `.HTTPConnection` to write
    their response.

    `HTTPServer` supports keep-alive connections by default
    (automatically for HTTP/1.1, or for HTTP/1.0 when the client
    requests ``Connection: keep-alive``).

    If ``xheaders`` is ``True``, we support the
    ``X-Real-Ip``/``X-Forwarded-For`` and
    ``X-Scheme``/``X-Forwarded-Proto`` headers, which override the
    remote IP and URI scheme/protocol for all requests.  These headers
    are useful when running Tornado behind a reverse proxy or load
    balancer.  The ``protocol`` argument can also be set to ``https``
    if Tornado is run behind an SSL-decoding proxy that does not set one of
    the supported ``xheaders``.

    To make this server serve SSL traffic, send the ``ssl_options`` dictionary
    argument with the arguments required for the `ssl.wrap_socket` method,
    including ``certfile`` and ``keyfile``.  (In Python 3.2+ you can pass
    an `ssl.SSLContext` object instead of a dict)::

       HTTPServer(applicaton, ssl_options={
           "certfile": os.path.join(data_dir, "mydomain.crt"),
           "keyfile": os.path.join(data_dir, "mydomain.key"),
       })

    `HTTPServer` initialization follows one of three patterns (the
    initialization methods are defined on `tornado.tcpserver.TCPServer`):

    1. `~tornado.tcpserver.TCPServer.listen`: simple single-process::

            server = HTTPServer(app)
            server.listen(8888)
            IOLoop.instance().start()

       In many cases, `tornado.web.Application.listen` can be used to avoid
       the need to explicitly create the `HTTPServer`.

    2. `~tornado.tcpserver.TCPServer.bind`/`~tornado.tcpserver.TCPServer.start`:
       simple multi-process::

            server = HTTPServer(app)
            server.bind(8888)
            server.start(0)  # Forks multiple sub-processes
            IOLoop.instance().start()

       When using this interface, an `.IOLoop` must *not* be passed
       to the `HTTPServer` constructor.  `~.TCPServer.start` will always start
       the server on the default singleton `.IOLoop`.

    3. `~tornado.tcpserver.TCPServer.add_sockets`: advanced multi-process::

            sockets = tornado.netutil.bind_sockets(8888)
            tornado.process.fork_processes(0)
            server = HTTPServer(app)
            server.add_sockets(sockets)
            IOLoop.instance().start()

       The `~.TCPServer.add_sockets` interface is more complicated,
       but it can be used with `tornado.process.fork_processes` to
       give you more flexibility in when the fork happens.
       `~.TCPServer.add_sockets` can also be used in single-process
       servers if you want to create your listening sockets in some
       way other than `tornado.netutil.bind_sockets`.

    .. versionchanged:: 4.0
       Added ``decompress_request``, ``chunk_size``, ``max_header_size``,
       ``idle_connection_timeout``, ``body_timeout``, ``max_body_size``
       arguments.  Added support for `.HTTPServerConnectionDelegate`
       instances as ``request_callback``.
    """
    def __init__(self, request_callback, no_keep_alive=False, io_loop=None,
                 xheaders=False, ssl_options=None, protocol=None,
                 decompress_request=False,
                 chunk_size=None, max_header_size=None,
                 idle_connection_timeout=None, body_timeout=None,
                 max_body_size=None, max_buffer_size=None):
        self.request_callback = request_callback
        self.no_keep_alive = no_keep_alive
        self.xheaders = xheaders
        self.protocol = protocol
        self.conn_params = HTTP1ConnectionParameters(
            decompress=decompress_request,
            chunk_size=chunk_size,
            max_header_size=max_header_size,
            header_timeout=idle_connection_timeout or 3600,
            max_body_size=max_body_size,
            body_timeout=body_timeout)
        TCPServer.__init__(self, io_loop=io_loop, ssl_options=ssl_options,
                           max_buffer_size=max_buffer_size,
                           read_chunk_size=chunk_size)
        self._connections = set()

    @gen.coroutine
    def close_all_connections(self):
        while self._connections:
            # Peek at an arbitrary element of the set
            conn = next(iter(self._connections))
            yield conn.close()

    def handle_stream(self, stream, address):
        # 请求的上下文，以属性字段的形式封装请求关联的数据
        context = _HTTPRequestContext(stream, address,
                                      self.protocol)
        # 支持 HTTP/1.x 的服务端持久化的连接对象， 作为 HTTPServerConnectionDelegate.start_request
        # 方法的 server_conn 实参。
        conn = HTTP1ServerConnection(
            stream, self.conn_params, context)
        self._connections.add(conn)
        conn.start_serving(self)

    def start_request(self, server_conn, request_conn):
        # request_callback 可能是以 `HTTPServerRequest` 对象作为参数的函数，也
        # 可能是一个 `HTTPServerConnectionDelegate` 实例。`_ServerRequestAdapter` 需
        # 要正确处理这两种类型的对象，以适配 `HTTPServerConnectionDelegate`。
        return _ServerRequestAdapter(self, request_conn)

    def on_close(self, server_conn):
        self._connections.remove(server_conn)


class _HTTPRequestContext(object):
    def __init__(self, stream, address, protocol):
        self.address = address
        self.protocol = protocol
        # Save the socket's address family now so we know how to
        # interpret self.address even after the stream is closed
        # and its socket attribute replaced with None.
        if stream.socket is not None:
            self.address_family = stream.socket.family
        else:
            self.address_family = None
        # In HTTPServerRequest we want an IP, not a full socket address.
        if (self.address_family in (socket.AF_INET, socket.AF_INET6) and
                address is not None):
            self.remote_ip = address[0]
        else:
            # Unix (or other) socket; fake the remote address.
            self.remote_ip = '0.0.0.0'
        if protocol:
            self.protocol = protocol
        elif isinstance(stream, iostream.SSLIOStream):
            self.protocol = "https"
        else:
            self.protocol = "http"
        self._orig_remote_ip = self.remote_ip
        self._orig_protocol = self.protocol

    def __str__(self):
        if self.address_family in (socket.AF_INET, socket.AF_INET6):
            return self.remote_ip
        elif isinstance(self.address, bytes):
            # Python 3 with the -bb option warns about str(bytes),
            # so convert it explicitly.
            # Unix socket addresses are str on mac but bytes on linux.
            return native_str(self.address)
        else:
            return str(self.address)

    def _apply_xheaders(self, headers):
        """Rewrite the ``remote_ip`` and ``protocol`` fields."""
        # 1. X-Forwarded-For: 记录一个请求从客户端出发到目标服务器过程中经历的代理或
        # 者负载平衡设备的IP。最早由 Squid 的开发人员引入，现已成为事实上的标准被广泛使用。
        # 一般格式为: client1, proxy1, proxy2，其中的值通过一个 “逗号+空格” 把多
        # 个 IP 地址分开，最左边(client1)是最原始客户端的IP地址, 代理服务器每成功收到
        # 一个请求，就把请求来源 IP 地址添加到右边(所以最后一个代理的 IP 不在该字段中，但
        # 可以通过 socket.getpeername() 获取，这个 Ip 是可靠的，伪造 Ip 的是无法通过
        #  TCP 三次握手的)。
        #
        # 2. X-Real-Ip：不是标准的 Http 头，不过 Nginx 在使用。通常被 HTTP 代理用来
        # 表示与它产生 TCP 连接的设备 IP，这个设备可能是其他代理，也可能是真正的请求端。
        #
        # 3. 经过 Nginx 的 Http 请求，X-Forwarded-For 的最后一个 Ip 与 X-Real-Ip
        # 是相同的，记录的是直接与 Nginx 建立连接的端 Ip。
        #
        # 4. 安全问题：伪造 X-Forwarded-For 字段很容易，所以使用该字段信息时要格外注意。
        # 通常 X-Forwarded-For 是最后一个IP地址是最后一个代理服务器的IP地址,这通常是一
        # 个比较可靠的信息来源。
        #
        # Squid uses X-Forwarded-For, others use X-Real-Ip
        ip = headers.get("X-Forwarded-For", self.remote_ip)
        ip = ip.split(',')[-1].strip()
        ip = headers.get("X-Real-Ip", ip)
        if netutil.is_valid_ip(ip):
            self.remote_ip = ip
        # AWS uses X-Forwarded-Proto
        proto_header = headers.get(
            "X-Scheme", headers.get("X-Forwarded-Proto",
                                    self.protocol))
        if proto_header in ("http", "https"):
            self.protocol = proto_header

    def _unapply_xheaders(self):
        """Undo changes from `_apply_xheaders`.

        Xheaders are per-request so they should not leak to the next
        request on the same connection.
        """
        self.remote_ip = self._orig_remote_ip
        self.protocol = self._orig_protocol


class _ServerRequestAdapter(httputil.HTTPMessageDelegate):
    """Adapts the `HTTPMessageDelegate` interface to the interface expected
    by our clients.
    """
    def __init__(self, server, connection):
        self.server = server
        self.connection = connection
        self.request = None
        # 如果 server.request_callback 是一个 HTTPServerConnectionDelegate 实例，
        # 比如 tornado.web.Application 实例，那就委托给它处理请求，否则就自己处理。
        if isinstance(server.request_callback,
                      httputil.HTTPServerConnectionDelegate):
            self.delegate = server.request_callback.start_request(connection)
            self._chunks = None
        else:
            self.delegate = None
            self._chunks = []

    def headers_received(self, start_line, headers):
        # 对这次请求应用 xheaders 支持
        if self.server.xheaders:
            self.connection.context._apply_xheaders(headers)
        if self.delegate is None:
            # 初始化一个 HTTPServerRequest 实例，HTTP 数据解析完成后将为该对象的 body 字段赋值。
            self.request = httputil.HTTPServerRequest(
                connection=self.connection, start_line=start_line,
                headers=headers)
        else:
            return self.delegate.headers_received(start_line, headers)

    def data_received(self, chunk):
        if self.delegate is None:
            self._chunks.append(chunk)
        else:
            return self.delegate.data_received(chunk)

    def finish(self):
        if self.delegate is None:
            # 为 body 字段赋值，并解析成 key/value 的形式，对上传的文件也采用这种方式（value 的类型
            # 为 HTTPFile，由于有 max_body_size 和 max_buffer_szie 的限制，这里倒是不用担心文件
            # 过大的问题）。
            self.request.body = b''.join(self._chunks)
            self.request._parse_body()
            self.server.request_callback(self.request)
        else:
            self.delegate.finish()
        self._cleanup()

    def on_connection_close(self):
        if self.delegate is None:
            self._chunks = None
        else:
            self.delegate.on_connection_close()
        self._cleanup()

    def _cleanup(self):
        # 请求处理正常结束或者连接关闭时进行一些清理工作，如有必要取消对 xheaders 的支持。
        if self.server.xheaders:
            self.connection.context._unapply_xheaders()


HTTPRequest = httputil.HTTPServerRequest
