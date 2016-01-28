#!/usr/bin/env python
#
# Copyright 2014 Facebook
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

"""Client and server implementations of HTTP/1.x.

.. versionadded:: 4.0
"""

from __future__ import absolute_import, division, print_function, with_statement

import re

from tornado.concurrent import Future
from tornado.escape import native_str, utf8
from tornado import gen
from tornado import httputil
from tornado import iostream
from tornado.log import gen_log, app_log
from tornado import stack_context
from tornado.util import GzipDecompressor


class _QuietException(Exception):
    def __init__(self):
        pass

class _ExceptionLoggingContext(object):
    """Used with the ``with`` statement when calling delegate methods to
    log any exceptions with the given logger.  Any exceptions caught are
    converted to _QuietException
    """
    def __init__(self, logger):
        self.logger = logger

    def __enter__(self):
        pass

    def __exit__(self, typ, value, tb):
        if value is not None:
            self.logger.error("Uncaught exception", exc_info=(typ, value, tb))
            raise _QuietException

class HTTP1ConnectionParameters(object):
    """Parameters for `.HTTP1Connection` and `.HTTP1ServerConnection`.
    """
    def __init__(self, no_keep_alive=False, chunk_size=None,
                 max_header_size=None, header_timeout=None, max_body_size=None,
                 body_timeout=None, decompress=False):
        """
        :arg bool no_keep_alive: If true, always close the connection after
            one request.
        :arg int chunk_size: how much data to read into memory at once
        :arg int max_header_size:  maximum amount of data for HTTP headers
        :arg float header_timeout: how long to wait for all headers (seconds)
        :arg int max_body_size: maximum amount of data for body
        :arg float body_timeout: how long to wait while reading body (seconds)
        :arg bool decompress: if true, decode incoming
            ``Content-Encoding: gzip``
        """
        self.no_keep_alive = no_keep_alive
        self.chunk_size = chunk_size or 65536
        self.max_header_size = max_header_size or 65536
        self.header_timeout = header_timeout
        self.max_body_size = max_body_size
        self.body_timeout = body_timeout
        self.decompress = decompress


class HTTP1Connection(httputil.HTTPConnection):
    """Implements the HTTP/1.x protocol.

    This class can be on its own for clients, or via `HTTP1ServerConnection`
    for servers.
    """
    def __init__(self, stream, is_client, params=None, context=None):
        """
        :arg stream: an `.IOStream`
        :arg bool is_client: client or server
        :arg params: a `.HTTP1ConnectionParameters` instance or ``None``
        :arg context: an opaque application-defined object that can be accessed
            as ``connection.context``.
        """
        self.is_client = is_client
        self.stream = stream
        if params is None:
            params = HTTP1ConnectionParameters()
        self.params = params
        self.context = context
        self.no_keep_alive = params.no_keep_alive
        # The body limits can be altered by the delegate, so save them
        # here instead of just referencing self.params later.
        self._max_body_size = (self.params.max_body_size or
                               self.stream.max_buffer_size)
        self._body_timeout = self.params.body_timeout
        # _write_finished is set to True when finish() has been called,
        # i.e. there will be no more data sent.  Data may still be in the
        # stream's write buffer.
        self._write_finished = False
        # True when we have read the entire incoming body.
        self._read_finished = False
        # _finish_future resolves when all data has been written and flushed
        # to the IOStream.
        self._finish_future = Future()
        # If true, the connection should be closed after this request
        # (after the response has been written in the server side,
        # and after it has been read in the client)
        self._disconnect_on_finish = False
        self._clear_callbacks()
        # Save the start lines after we read or write them; they
        # affect later processing (e.g. 304 responses and HEAD methods
        # have content-length but no bodies)
        self._request_start_line = None
        self._response_start_line = None
        self._request_headers = None
        # True if we are writing output with chunked encoding.
        self._chunking_output = None
        # While reading a body with a content-length, this is the
        # amount left to read.
        self._expected_content_remaining = None
        # A Future for our outgoing writes, returned by IOStream.write.
        self._pending_write = None

    def read_response(self, delegate):
        """Read a single HTTP response.

        Typical client-mode usage is to write a request using `write_headers`,
        `write`, and `finish`, and then call ``read_response``.

        :arg delegate: a `.HTTPMessageDelegate`

        Returns a `.Future` that resolves to None after the full response has
        been read.
        """
        if self.params.decompress:
            delegate = _GzipMessageDelegate(delegate, self.params.chunk_size)
        return self._read_message(delegate)

    @gen.coroutine
    def _read_message(self, delegate):
        need_delegate_close = False
        try:
            # 消息头与消息体之间由一个空行分开
            header_future = self.stream.read_until_regex(
                b"\r?\n\r?\n",
                max_bytes=self.params.max_header_size)
            if self.params.header_timeout is None:
                header_data = yield header_future
            else:
                try:
                    header_data = yield gen.with_timeout(
                        self.stream.io_loop.time() + self.params.header_timeout,
                        header_future,
                        io_loop=self.stream.io_loop)
                except gen.TimeoutError:
                    self.close()
                    raise gen.Return(False)
            # 解析消息头，分离头字段和首行（request-line/status-line）
            start_line, headers = self._parse_headers(header_data)
            # 作为 client 解析的是 server 的 response，作为 server 解析的是 client 的 request。
            # response 与 request 的 start_line(status-line/request-line) 的字段内容不同：
            # 1. response's status-line: HTTP-Version SP Status-Code SP Reason-Phrase CRLF
            # 2. request's request-line：Method SP Request-URI SP HTTP-Version CRLF
            # start_line 的值是一个 namedtuple。
            if self.is_client:
                start_line = httputil.parse_response_start_line(start_line)
                self._response_start_line = start_line
            else:
                start_line = httputil.parse_request_start_line(start_line)
                self._request_start_line = start_line
                self._request_headers = headers

            # 非 keep-alive 的请求或响应处理完成后要关闭连接。
            self._disconnect_on_finish = not self._can_keep_alive(
                start_line, headers)
            need_delegate_close = True
            with _ExceptionLoggingContext(app_log):
                header_future = delegate.headers_received(start_line, headers)
                if header_future is not None:
                    # 如果 header_future 是一个 `Future` 实例，则要等到完成才读取 body。
                    yield header_future
            # websocket ？？？
            if self.stream is None:
                # We've been detached.
                need_delegate_close = False
                raise gen.Return(False)
            skip_body = False
            if self.is_client:
                # 作为 client 如果发起的是 HEAD 请求，那么 server response 应该无消息体
                if (self._request_start_line is not None and
                        self._request_start_line.method == 'HEAD'):
                    skip_body = True
                code = start_line.code
                if code == 304:
                    # 304 responses may include the content-length header
                    # but do not actually have a body.
                    # http://tools.ietf.org/html/rfc7230#section-3.3
                    skip_body = True
                if code >= 100 and code < 200:
                    # 1xx responses should never indicate the presence of
                    # a body.
                    if ('Content-Length' in headers or
                        'Transfer-Encoding' in headers):
                        raise httputil.HTTPInputError(
                            "Response code %d cannot have body" % code)
                    # TODO: client delegates will get headers_received twice
                    # in the case of a 100-continue.  Document or change?
                    yield self._read_message(delegate)
            else:
                # 100-continue 这个状态码是在 HTTP/1.1 中为了提高传输效率而设置的。当
                # client 需要 POST 较大数据给 WebServer 时，可以在发送 HTTP 请求时带上
                # Expect: 100-continue，WebServer 如果接受这个请求则应答一个
                # ``HTTP/1.1 100 (Continue)``，那么 client 就继续传输 request body，
                # 否则应答 ``HTTP/1.1 417 Expectation Failed`` client 就放弃传输剩余
                # 的数据。（注：Expect 头部域，用于指出客户端要求的特殊服务器行为采用扩展语法
                # 定义，以方便扩展。）
                if (headers.get("Expect") == "100-continue" and
                        not self._write_finished):
                    self.stream.write(b"HTTP/1.1 100 (Continue)\r\n\r\n")
            if not skip_body:
                body_future = self._read_body(
                    start_line.code if self.is_client else 0, headers, delegate)
                if body_future is not None:
                    if self._body_timeout is None:
                        yield body_future
                    else:
                        try:
                            yield gen.with_timeout(
                                self.stream.io_loop.time() + self._body_timeout,
                                body_future, self.stream.io_loop)
                        except gen.TimeoutError:
                            gen_log.info("Timeout reading body from %s",
                                         self.context)
                            self.stream.close()
                            raise gen.Return(False)
            self._read_finished = True
            # 对 client mode ，response 解析完成就调用 HTTPMessageDelegate.finish() 方法是合适的；
            # 对 server mode ，_write_finished 表示 response 是否发送完成，未完成前调用
            # HTTPMessageDelegate.finish() 方法让 delegate 执行请求响应；
            if not self._write_finished or self.is_client:
                need_delegate_close = False
                with _ExceptionLoggingContext(app_log):
                    delegate.finish()
            # If we're waiting for the application to produce an asynchronous
            # response, and we're not detached, register a close callback
            # on the stream (we didn't need one while we were reading)
            #
            # NOTE:_finish_future resolves when all data has been written and flushed
            # to the IOStream.
            #
            # hold 住执行流程，直到异步响应完成，所有数据都写入 fd，才继续后续处理，通常调用方执行 `finish` 方法
            # 设置 `_finish_future` 完成，详细见 `finish` 和 `_finish_request` 方法实现。
            if (not self._finish_future.done() and
                    self.stream is not None and
                    not self.stream.closed()):
                self.stream.set_close_callback(self._on_connection_close)
                yield self._finish_future
            # 对于 client mode，处理完响应后如果不是 keep-alive 就断开连接。
            # 对于 server mode，需要在 response 完成后才断开连接，详细见 _finish_request/finish 方法实现。
            if self.is_client and self._disconnect_on_finish:
                self.close()
            if self.stream is None:
                raise gen.Return(False)
        except httputil.HTTPInputError as e:
            gen_log.info("Malformed HTTP message from %s: %s",
                         self.context, e)
            self.close()
            raise gen.Return(False)
        finally:
            # 连接 “关闭” 前还没能结束处理请求（call HTTPMessageDelegate.finish()），则
            # call  HTTPMessageDelegate.on_connection_close()
            if need_delegate_close:
                with _ExceptionLoggingContext(app_log):
                    delegate.on_connection_close()
            self._clear_callbacks()
        raise gen.Return(True)

    def _clear_callbacks(self):
        """Clears the callback attributes.

        This allows the request handler to be garbage collected more
        quickly in CPython by breaking up reference cycles.
        """
        self._write_callback = None
        self._write_future = None
        self._close_callback = None
        if self.stream is not None:
            self.stream.set_close_callback(None)

    def set_close_callback(self, callback):
        """Sets a callback that will be run when the connection is closed.

        .. deprecated:: 4.0
            Use `.HTTPMessageDelegate.on_connection_close` instead.
        """
        self._close_callback = stack_context.wrap(callback)

    def _on_connection_close(self):
        # Note that this callback is only registered on the IOStream
        # when we have finished reading the request and are waiting for
        # the application to produce its response.
        if self._close_callback is not None:
            callback = self._close_callback
            self._close_callback = None
            callback()
        if not self._finish_future.done():
            self._finish_future.set_result(None)
        self._clear_callbacks()

    def close(self):
        if self.stream is not None:
            self.stream.close()
        self._clear_callbacks()
        if not self._finish_future.done():
            self._finish_future.set_result(None)

    def detach(self):
        """Take control of the underlying stream.

        Returns the underlying `.IOStream` object and stops all further
        HTTP processing.  May only be called during
        `.HTTPMessageDelegate.headers_received`.  Intended for implementing
        protocols like websockets that tunnel over an HTTP handshake.
        """
        self._clear_callbacks()
        stream = self.stream
        self.stream = None
        return stream

    def set_body_timeout(self, timeout):
        """Sets the body timeout for a single request.

        Overrides the value from `.HTTP1ConnectionParameters`.
        """
        self._body_timeout = timeout

    def set_max_body_size(self, max_body_size):
        """Sets the body size limit for a single request.

        Overrides the value from `.HTTP1ConnectionParameters`.
        """
        self._max_body_size = max_body_size

    def write_headers(self, start_line, headers, chunk=None, callback=None):
        """Implements `.HTTPConnection.write_headers`."""
        if self.is_client:
            self._request_start_line = start_line
            # Client requests with a non-empty body must have either a
            # Content-Length or a Transfer-Encoding.
            # 不检查是否 Http/1.0 是不完备的。
            self._chunking_output = (
                start_line.method in ('POST', 'PUT', 'PATCH') and
                'Content-Length' not in headers and
                'Transfer-Encoding' not in headers)
        else:
            self._response_start_line = start_line
            # 对于 HTTP/1.0 ``self._chunking_output=False``，不支持分块传输编码。
            self._chunking_output = (
                # TODO: should this use
                # self._request_start_line.version or
                # start_line.version?
                self._request_start_line.version == 'HTTP/1.1' and
                # 304 responses have no body (not even a zero-length body), and so
                # should not have either Content-Length or Transfer-Encoding.
                # headers.
                start_line.code != 304 and
                # No need to chunk the output if a Content-Length is specified.
                'Content-Length' not in headers and
                # Applications are discouraged from touching Transfer-Encoding,
                # but if they do, leave it alone.
                'Transfer-Encoding' not in headers)
            # If a 1.0 client asked for keep-alive, add the header.
            # HTTP/1.1 默认就是持久化连接，不需要单独指定。
            # 假设客户端请求使用 HTTP/1.0 和 `Connection:Keep-Alive`，服务端响应时没有指定
            # `Content-Length` （比如在 handler 中多次调用 flush 方法）,那么响应数据就无法
            # 判断边界，代码中应该对这个条件做特别处理。
            if (self._request_start_line.version == 'HTTP/1.0' and
                (self._request_headers.get('Connection', '').lower()
                 == 'keep-alive')):
                headers['Connection'] = 'Keep-Alive'
        if self._chunking_output:
            headers['Transfer-Encoding'] = 'chunked'
        # 服务端响应 `HEAD` 或者 304 时不需要 body 数据。
        if (not self.is_client and
            (self._request_start_line.method == 'HEAD' or
             start_line.code == 304)):
            self._expected_content_remaining = 0
        elif 'Content-Length' in headers:
            self._expected_content_remaining = int(headers['Content-Length'])
        else:
            self._expected_content_remaining = None
        lines = [utf8("%s %s %s" % start_line)]
        # 通过 add 添加的响应头会输出多个，比如：“Set-Cookie” 响应头。
        lines.extend([utf8(n) + b": " + utf8(v) for n, v in headers.get_all()])
        for line in lines:
            if b'\n' in line:
                raise ValueError('Newline in header: ' + repr(line))
        future = None
        if self.stream.closed():
            future = self._write_future = Future()
            future.set_exception(iostream.StreamClosedError())
        else:
            # "写回调" 是一个实例字段 `_write_callback`，当上一次写操作还没有回调时就再次执行
            # 写操作，那么上一次写操作的回调将被放弃（callback is not None）
            if callback is not None:
                self._write_callback = stack_context.wrap(callback)
            else:
                # 没有 callback 时，返回 Future(self._write_future)
                future = self._write_future = Future()
            # Headers
            data = b"\r\n".join(lines) + b"\r\n\r\n"
            # message-body
            if chunk:
                data += self._format_chunk(chunk)
            self._pending_write = self.stream.write(data)
            self._pending_write.add_done_callback(self._on_write_complete)
        return future

    def _format_chunk(self, chunk):
        if self._expected_content_remaining is not None:
            self._expected_content_remaining -= len(chunk)
            if self._expected_content_remaining < 0:
                # Close the stream now to stop further framing errors.
                self.stream.close()
                raise httputil.HTTPOutputError(
                    "Tried to write more data than Content-Length")
        if self._chunking_output and chunk:
            # Don't write out empty chunks because that means END-OF-STREAM
            # with chunked encoding
            #
            # Each chunk: the number of octets of the data(hex number) + CRLF + chunk data + CRLF
            return utf8("%x" % len(chunk)) + b"\r\n" + chunk + b"\r\n"
        else:
            return chunk

    def write(self, chunk, callback=None):
        """Implements `.HTTPConnection.write`.

        For backwards compatibility is is allowed but deprecated to
        skip `write_headers` and instead call `write()` with a
        pre-encoded header block.
        """
        future = None
        if self.stream.closed():
            future = self._write_future = Future()
            self._write_future.set_exception(iostream.StreamClosedError())
        else:
            if callback is not None:
                self._write_callback = stack_context.wrap(callback)
            else:
                future = self._write_future = Future()
            self._pending_write = self.stream.write(self._format_chunk(chunk))
            self._pending_write.add_done_callback(self._on_write_complete)
        return future

    def finish(self):
        """Implements `.HTTPConnection.finish`."""
        if (self._expected_content_remaining is not None and
                self._expected_content_remaining != 0 and
                not self.stream.closed()):
            self.stream.close()
            raise httputil.HTTPOutputError(
                "Tried to write %d bytes less than Content-Length" %
                self._expected_content_remaining)
        if self._chunking_output:
            if not self.stream.closed():
                # `Transfer-Encoding:chunked`: The terminating chunk is a
                # regular chunk, with the exception that its length is zero.
                self._pending_write = self.stream.write(b"0\r\n\r\n")
                self._pending_write.add_done_callback(self._on_write_complete)
        self._write_finished = True
        # If the app finished the request while we're still reading,
        # divert any remaining data away from the delegate and
        # close the connection when we're done sending our response.
        # Closing the connection is the only way to avoid reading the
        # whole input body.
        if not self._read_finished:
            self._disconnect_on_finish = True
        # No more data is coming, so instruct TCP to send any remaining
        # data immediately instead of waiting for a full packet or ack.
        # 关闭 Nagle 算法，效果相当于让 socket 立即 flush 数据到客户端，随后将在
        # `_finish_request` 中恢复 Nagle 算法。
        self.stream.set_nodelay(True)
        if self._pending_write is None:
            self._finish_request(None)
        else:
            # 最后一次挂起的写操作完成后回调 `_finish_request` 方法。
            self._pending_write.add_done_callback(self._finish_request)

    def _on_write_complete(self, future):
        if self._write_callback is not None:
            callback = self._write_callback
            self._write_callback = None
            self.stream.io_loop.add_callback(callback)
        if self._write_future is not None:
            future = self._write_future
            self._write_future = None
            future.set_result(None)

    def _can_keep_alive(self, start_line, headers):
        if self.params.no_keep_alive:
            return False
        # 在 HTTP/1.0 中没有官方的 keep-alive 操作，HTTP/1.1 所有连接都是默认持
        # 久化，除非特殊声明不支持。通常是在现有协议上添加一个指数。
        # client 若支持 keep-alive，会在请求头中添加：Connection: Keep-Alive，
        # server 响应的时候也要在响应头中增加： Connection: Keep-Alive。参见：
        # https://zh.wikipedia.org/wiki/HTTP%E6%8C%81%E4%B9%85%E8%BF%9E%E6%8E%A5
        connection_header = headers.get("Connection")
        if connection_header is not None:
            connection_header = connection_header.lower()
        # 不论是否 is_client mode， start_line 中都包含 version 字段。
        # ***************************** NOTE ***********************************
        # 1. 假设使用的是 HTTP/1.0 协议，对 is_client=True，start_line 不包含 method 字段，
        # 而当 response empty headers(eg.100-continue) 时，start_line.method 会报错。避
        # 免这种情况只能在 is_client mode 时设置 no_keep_alive=True，自行实现客户端时要注意。
        # 2. 如果 POST 数据时使用 'Connection: Keep-Alive' 和 'Transfer-Encoding: Chunked',
        # 这个时候由于没有 'Content-Length' 字段，所以检查不到 keep-alive 而关闭连接。需要增加：
        # or headers.get("Transfer-Encoding", "").lower() == "chunked"
        # *************************** NOTE END *********************************
        if start_line.version == "HTTP/1.1":
            return connection_header != "close"
        elif ("Content-Length" in headers
              or start_line.method in ("HEAD", "GET")):
            # HTTP/1.0 要支持持久化连接需要能够知道 request body 的大小，才能区分
            # 不同的 HTTP 请求。没有 "Content-Length" 字段，表示没有 request body。
            return connection_header == "keep-alive"
        return False

    def _finish_request(self, future):
        # ``close`` 中还会执行一次，调整到后面执行更好
        self._clear_callbacks()
        # 服务端不需要支持长连接时，执行关闭操作
        if not self.is_client and self._disconnect_on_finish:
            self.close()
            return
        # Turn Nagle's algorithm back on, leaving the stream in its
        # default state for the next request.
        self.stream.set_nodelay(False)
        if not self._finish_future.done():
            self._finish_future.set_result(None)

    def _parse_headers(self, data):
        # HTTP 消息包括 Request（c2s）和 Response(s2c)，消息格式为：
        # 起始行(Request-Line/Status-Line) + 0 个或多个头域(((general-header |
        # (request-header | response-header) | entity-header)CRLF)) +
        # 头域结束行(\r\n,CRLF) + 可选的消息体(message-body)，所以读取消息头时以
        # r"\r\n\r\n" 作为匹配字符。
        # 每个头域由一个头域名称（name） + 冒号（:） + 域值(value), 三部分组成，
        # name 是大小写无关的，value 前可以添加任何数量的空格符，头域可以被扩展为
        # 多行，在每行开始处，使用至少一个空格或制表符。相关 RFC：
        # 1. Request：http://www.w3.org/Protocols/rfc2616/rfc2616-sec5.html#sec5
        # 2. Response：http://www.w3.org/Protocols/rfc2616/rfc2616-sec6.html#sec6
        data = native_str(data.decode('latin1'))
        eol = data.find("\r\n")
        start_line = data[:eol]
        try:
            headers = httputil.HTTPHeaders.parse(data[eol:])
        except ValueError:
            # probably form split() if there was no ':' in the line
            raise httputil.HTTPInputError("Malformed HTTP headers: %r" %
                                          data[eol:100])
        return start_line, headers

    def _read_body(self, code, headers, delegate):
        if "Content-Length" in headers:
            if "," in headers["Content-Length"]:
                # Proxies sometimes cause Content-Length headers to get
                # duplicated.  If all the values are identical then we can
                # use them but if they differ it's an error.
                pieces = re.split(r',\s*', headers["Content-Length"])
                if any(i != pieces[0] for i in pieces):
                    raise httputil.HTTPInputError(
                        "Multiple unequal Content-Lengths: %r" %
                        headers["Content-Length"])
                headers["Content-Length"] = pieces[0]
            content_length = int(headers["Content-Length"])

            if content_length > self._max_body_size:
                raise httputil.HTTPInputError("Content-Length too long")
        else:
            content_length = None

        # 204 No Content，表示服务器已经完成了请求，但是返回的信息不包括 message-body，但是可以通过
        # header fields 返回一些用于更新的元数据。
        if code == 204:
            # This response code is not allowed to have a non-empty body,
            # and has an implicit length of zero instead of read-until-close.
            # http://www.w3.org/Protocols/rfc2616/rfc2616-sec4.html#sec4.3
            if ("Transfer-Encoding" in headers or
                    content_length not in (None, 0)):
                raise httputil.HTTPInputError(
                    "Response with code %d should not have body" % code)
            content_length = 0

        # 持久连接： Content-Length or Transfer-Encoding
        if content_length is not None:
            return self._read_fixed_body(content_length, delegate)
        if headers.get("Transfer-Encoding") == "chunked":
            return self._read_chunked_body(delegate)
        # 非持久连接
        if self.is_client:
            return self._read_body_until_close(delegate)
        return None

    @gen.coroutine
    def _read_fixed_body(self, content_length, delegate):
        while content_length > 0:
            body = yield self.stream.read_bytes(
                min(self.params.chunk_size, content_length), partial=True)
            content_length -= len(body)
            if not self._write_finished or self.is_client:
                with _ExceptionLoggingContext(app_log):
                    yield gen.maybe_future(delegate.data_received(body))

    @gen.coroutine
    def _read_chunked_body(self, delegate):
        # TODO: "chunk extensions" http://tools.ietf.org/html/rfc2616#section-3.6.1
        #
        # *************************** chunk extensions *************************
        # 使用分块传输编码（chunked transfer encoding）时，消息体由数量未定的块组成，并以最
        # 后一个大小为 0 的块结束。
        # 1. 每一个非空的块都以该块包含数据的字节数（十六进制表示）开始，跟随一个 CRLF，然后是数
        # 据本身，最后跟 CRLF 结束。在一些实现中，块大小和 CRLF 之间填充有白空格（0x20）。
        # 2. 最后一块由块大小（0），一些可选的填充白空格，以及 CRLF。最后一块不包含任何数据，但
        # 是可以发送包含消息头字段的可选尾部（注：以下代码实现不支持可选尾部），最后以 CRLF 结尾。
        # ----------------------------eg. start--------------------------------
        # HTTP/1.1 200 OK\r\n
        # Content-Type: text/plain\r\n
        # Transfer-Encoding: chunked\r\n
        # \r\n
        # 25\r\n
        # This is the data in the first chunk\r\n
        # 1C\r\n
        # and this is the second one\r\n
        # 3\r\n
        # con\r\n
        # 8\r\n
        # sequence\r\n
        # 0\r\n
        # \r\n
        # ----------------------------eg. end--------------------------------
        # **********************************************************************
        total_size = 0
        while True:
            chunk_len = yield self.stream.read_until(b"\r\n", max_bytes=64)
            chunk_len = int(chunk_len.strip(), 16)
            if chunk_len == 0:
                return
            total_size += chunk_len
            if total_size > self._max_body_size:
                raise httputil.HTTPInputError("chunked body too large")
            bytes_to_read = chunk_len
            while bytes_to_read:
                chunk = yield self.stream.read_bytes(
                    min(bytes_to_read, self.params.chunk_size), partial=True)
                bytes_to_read -= len(chunk)
                if not self._write_finished or self.is_client:
                    with _ExceptionLoggingContext(app_log):
                        yield gen.maybe_future(delegate.data_received(chunk))
            # chunk ends with \r\n
            crlf = yield self.stream.read_bytes(2)
            # 如果最后一个 chunk 中包含了可选的尾部，断言会失败。可选尾部由 Trailer 头域支持，
            # 参考：http://tools.ietf.org/html/rfc2616#section-14.40。
            # 目前 tornado 中的实现不支持这个可选尾部，如果发生异常的话，可尝试判断是否是 last chunk，
            # 然后吞掉可选尾部。
            # eg.
            # if bytes_to_read == 0 and crlf != b"\r\n":
            #     yield self.stream.read_until(b"\r\n", max_bytes=self._max_body_size - total_size)
            # else:
            #     assert crlf == b"\r\n"
            assert crlf == b"\r\n"

    @gen.coroutine
    def _read_body_until_close(self, delegate):
        body = yield self.stream.read_until_close()
        if not self._write_finished or self.is_client:
            with _ExceptionLoggingContext(app_log):
                delegate.data_received(body)


class _GzipMessageDelegate(httputil.HTTPMessageDelegate):
    """Wraps an `HTTPMessageDelegate` to decode ``Content-Encoding: gzip``.
    """
    def __init__(self, delegate, chunk_size):
        self._delegate = delegate
        self._chunk_size = chunk_size
        self._decompressor = None

    def headers_received(self, start_line, headers):
        # 若是 gzip 数据，则提供 GzipDecompressor 实例用于 body 的解压缩。
        # 并删除 Content-Encoding 报文头，用 X-Consumed-Content-Encoding 来代替。
        if headers.get("Content-Encoding") == "gzip":
            self._decompressor = GzipDecompressor()
            # Downstream delegates will only see uncompressed data,
            # so rename the content-encoding header.
            # (but note that curl_httpclient doesn't do this).
            headers.add("X-Consumed-Content-Encoding",
                        headers["Content-Encoding"])
            del headers["Content-Encoding"]
        return self._delegate.headers_received(start_line, headers)

    @gen.coroutine
    def data_received(self, chunk):
        if self._decompressor:
            compressed_data = chunk
            while compressed_data:
                decompressed = self._decompressor.decompress(
                    compressed_data, self._chunk_size)
                if decompressed:
                    yield gen.maybe_future(
                        self._delegate.data_received(decompressed))
                compressed_data = self._decompressor.unconsumed_tail
        else:
            yield gen.maybe_future(self._delegate.data_received(chunk))

    def finish(self):
        if self._decompressor is not None:
            tail = self._decompressor.flush()
            if tail:
                # I believe the tail will always be empty (i.e.
                # decompress will return all it can).  The purpose
                # of the flush call is to detect errors such
                # as truncated input.  But in case it ever returns
                # anything, treat it as an extra chunk
                self._delegate.data_received(tail)
        return self._delegate.finish()

    def on_connection_close(self):
        return self._delegate.on_connection_close()


class HTTP1ServerConnection(object):
    """An HTTP/1.x server."""
    def __init__(self, stream, params=None, context=None):
        """
        :arg stream: an `.IOStream`
        :arg params: a `.HTTP1ConnectionParameters` or None
        :arg context: an opaque application-defined object that is accessible
            as ``connection.context``
        """
        self.stream = stream
        if params is None:
            params = HTTP1ConnectionParameters()
        self.params = params
        self.context = context
        self._serving_future = None

    @gen.coroutine
    def close(self):
        """Closes the connection.

        Returns a `.Future` that resolves after the serving loop has exited.
        """
        self.stream.close()
        # Block until the serving loop is done, but ignore any exceptions
        # (start_serving is already responsible for logging them).
        try:
            yield self._serving_future
        except Exception:
            pass

    def start_serving(self, delegate):
        """Starts serving requests on this connection.

        :arg delegate: a `.HTTPServerConnectionDelegate`
        """
        assert isinstance(delegate, httputil.HTTPServerConnectionDelegate)
        self._serving_future = self._server_request_loop(delegate)
        # Register the future on the IOLoop so its errors get logged.
        self.stream.io_loop.add_future(self._serving_future,
                                       lambda f: f.result())

    @gen.coroutine
    def _server_request_loop(self, delegate):
        try:
            while True:
                conn = HTTP1Connection(self.stream, False,
                                       self.params, self.context)
                request_delegate = delegate.start_request(self, conn)
                try:
                    # 解析 HTTP 数据，解析的结果交由 request_delegate 处理。通常 request_delegate
                    # 会将解析结果保存到 HTTPServerRequest 中，参见 _ServerRequestAdapter 实现。
                    ret = yield conn.read_response(request_delegate)
                except (iostream.StreamClosedError,
                        iostream.UnsatisfiableReadError):
                    # 这两种异常由底层 IOStream 引发，发生时 IOStream 会自动关闭（包括关联的 fd） 并 logged。
                    # 若是其他异常，则需要调用 conn.close() 以关闭 IOStream。
                    return
                except _QuietException:
                    # This exception was already logged.
                    #
                    # HTTP1Connection 中发生异常时由 _ExceptionLoggingContext 捕获并 logged（详细异常），
                    # 然后 _ExceptionLoggingContext 抛出 _QuietException 异常。
                    conn.close()
                    return
                except Exception:
                    gen_log.error("Uncaught exception", exc_info=True)
                    conn.close()
                    return
                # read_response 方法返回 True 表示此次请求正常处理完成，继续处理下一个请求：
                #   1. keep-alive 时，等待下一次 read_response 完成；
                #   2. 非 keep-alive 时，会抛出 StreamClosedError 终止循环；
                # 返回 False 则表示处理请求时发生了异常无法再继续处理下一个请求，立即终止循环是最优的选择而不是通过下
                # 一次抛出异常来终止。
                if not ret:
                    return
                yield gen.moment
        finally:
            # HTTPServer.on_close 方法仅将当前 HTTP1ServerConnection 从其内部连接列表中移除。
            delegate.on_close(self)
