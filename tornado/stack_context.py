#!/usr/bin/env python
#
# Copyright 2010 Facebook
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

"""`StackContext` allows applications to maintain threadlocal-like state
that follows execution as it moves to other execution contexts.

The motivating examples are to eliminate the need for explicit
``async_callback`` wrappers (as in `tornado.web.RequestHandler`), and to
allow some additional context to be kept for logging.

This is slightly magic, but it's an extension of the idea that an
exception handler is a kind of stack-local state and when that stack
is suspended and resumed in a new context that state needs to be
preserved.  `StackContext` shifts the burden of restoring that state
from each call site (e.g.  wrapping each `.AsyncHTTPClient` callback
in ``async_callback``) to the mechanisms that transfer control from
one context to another (e.g. `.AsyncHTTPClient` itself, `.IOLoop`,
thread pools, etc).

Example usage::

    @contextlib.contextmanager
    def die_on_error():
        try:
            yield
        except Exception:
            logging.error("exception in asynchronous operation",exc_info=True)
            sys.exit(1)

    with StackContext(die_on_error):
        # Any exception thrown here *or in callback and its desendents*
        # will cause the process to exit instead of spinning endlessly
        # in the ioloop.
        http_client.fetch(url, callback)
    ioloop.start()

Most applications shouln't have to work with `StackContext` directly.
Here are a few rules of thumb for when it's necessary:

* If you're writing an asynchronous library that doesn't rely on a
  stack_context-aware library like `tornado.ioloop` or `tornado.iostream`
  (for example, if you're writing a thread pool), use
  `.stack_context.wrap()` before any asynchronous operations to capture the
  stack context from where the operation was started.

* If you're writing an asynchronous library that has some shared
  resources (such as a connection pool), create those shared resources
  within a ``with stack_context.NullContext():`` block.  This will prevent
  ``StackContexts`` from leaking from one request to another.

* If you want to write something like an exception handler that will
  persist across asynchronous calls, create a new `StackContext` (or
  `ExceptionStackContext`), and make your asynchronous calls in a ``with``
  block that references your `StackContext`.
"""

from __future__ import absolute_import, division, print_function, with_statement

import sys
import threading

from tornado.util import raise_exc_info


class StackContextInconsistentError(Exception):
    pass


class _State(threading.local):
    def __init__(self):
        # self.contexts[0],tuple 中包含的是普通的 StackContext 上下文，用于异步调用时恢复上下文状态，
        # 具有完整的 StackContext protocol 实现，即 enter, exit ;
        # self.contexts[1]为 Head Context ，用于处理异步调用抛出的异常。
        self.contexts = (tuple(), None)

# 当前线程的上下文状态
_state = _State()


class StackContext(object):
    """Establishes the given context as a StackContext that will be transferred.

    Note that the parameter is a callable that returns a context
    manager, not the context itself.  That is, where for a
    non-transferable context manager you would say::

      with my_context():

    StackContext takes the function itself rather than its result::

      with StackContext(my_context):

    The result of ``with StackContext() as cb:`` is a deactivation
    callback.  Run this callback when the StackContext is no longer
    needed to ensure that it is not propagated any further (note that
    deactivating a context does not affect any instances of that
    context that are currently pending).  This is an advanced feature
    and not necessary in most applications.

    StackContext 是一个 context 的 wrapper，它接受一个 context manager（一个
    可调用对象，调用该对象可以返回一个 context） 作为参数。通过 with 语句使用
    StackContext 将 StackContext 对象加入到当前线程的上下文中（_state.contexts）。
    注：StackContext 封装的 context manager 才能在未来执行回调函数时重新建立新的上下文
    环境（new context object）。使用“同一个上下文环境”和使用“相同的上下文环境”是有区别的，
    StackContext 是为了重建“相同的上下文环境”而不是使用“同一个上下文环境”。

    ``with StackContext() as cb:`` 返回的是一个 deactivation 回调，执行这个回调后
    会将该 StackContext 设置为非活动的（active = False）。非活动的 StackContext 不
    会被传递，也就是说该 StackContext 封装的上下文不会在后续执行回调函数时作为回调函数的上
    下文环境而重建（注：函数 _remove_deactivated 会忽略非活动的 StackContext）。但是这个
    高级特性在大多数的应用中都不需要。
    """
    def __init__(self, context_factory):
        self.context_factory = context_factory
        self.contexts = []
        self.active = True

    def _deactivate(self):
        self.active = False

    # StackContext protocol
    def enter(self):
        context = self.context_factory()
        self.contexts.append(context)
        context.__enter__()

    def exit(self, type, value, traceback):
        context = self.contexts.pop()
        context.__exit__(type, value, traceback)

    # Note that some of this code is duplicated in ExceptionStackContext
    # below.  ExceptionStackContext is more common and doesn't need
    # the full generality of this class.
    def __enter__(self):
        self.old_contexts = _state.contexts
        # 将当前 StackContext “加入”上下文栈进行传播，并将其设置为 Head StackContext 。
        self.new_contexts = (self.old_contexts[0] + (self,), self)
        _state.contexts = self.new_contexts

        try:
            self.enter()
        except:
            _state.contexts = self.old_contexts
            raise

        return self._deactivate

    def __exit__(self, type, value, traceback):
        try:
            self.exit(type, value, traceback)
        finally:
            final_contexts = _state.contexts
            _state.contexts = self.old_contexts

            # Generator coroutines and with-statements with non-local
            # effects interact badly.  Check here for signs of
            # the stack getting out of sync.
            # Note that this check comes after restoring _state.context
            # so that if it fails things are left in a (relatively)
            # consistent state.
            #
            # 回调函数上下文环境的正确重建和销毁依赖于 StackContext 的 enter 与 exit，
            # 在一个 ``with StackContext`` 语句块中使用 yield 语句交回代码控制权
            # 是不安全的，有可能导致上下文不能按照正确顺序进入和退出。如果必须在这种情况下使
            # 用 yield 语句，请使用 run_with_stack_context(context, func) 函数。
            if final_contexts is not self.new_contexts:
                raise StackContextInconsistentError(
                    'stack_context inconsistency (may be caused by yield '
                    'within a "with StackContext" block)')

            # Break up a reference to itself to allow for faster GC on CPython.
            self.new_contexts = None


class ExceptionStackContext(object):
    """Specialization of StackContext for exception handling.

    The supplied ``exception_handler`` function will be called in the
    event of an uncaught exception in this context.  The semantics are
    similar to a try/finally clause, and intended use cases are to log
    an error, close a socket, or similar cleanup actions.  The
    ``exc_info`` triple ``(type, value, traceback)`` will be passed to the
    exception_handler function.

    If the exception handler returns true, the exception will be
    consumed and will not be propagated to other exception handlers.

    ExceptionStackContext 是特化的 StackContext，在语义上类似 try/finally 语句块。
    其设计初衷是为处理上下文中未被处理的异常，关闭套接字以及完成一些清理工作。
    与普通的 StackContext 相比 ExceptionStackContext 没有 enter() 方法，只有 exit()
    方法，所以它不加入上下文 tuple 中进行传播重建，而仅仅提供 exit() 对上下文中未处理的
    异常进行后处理。

    ExceptionStackContext 由一个可调用的对象 exception_handler 初始化， exception_handler
    的调用参数为 ``exc_info`` 元组 ``(type, value, traceback)`` 。其返回值为 boolean 值，
    True 表示异常被处理， False 表示异常未被处理需要 propagated 给其他 exception handlers(指
    上下文链中的其他 StackContext/ExceptionStackContext)。
    """
    def __init__(self, exception_handler):
        self.exception_handler = exception_handler
        self.active = True

    def _deactivate(self):
        self.active = False

    def exit(self, type, value, traceback):
        if type is not None:
            return self.exception_handler(type, value, traceback)

    def __enter__(self):
        self.old_contexts = _state.contexts
        # 这里需要注意一下， ExceptionStackContext 自身不会作为上下文的一部分（tuple）进行传播重建，
        # 仅作为 Head StackContext ，在异步回调时负责处异常。
        self.new_contexts = (self.old_contexts[0], self)
        _state.contexts = self.new_contexts

        return self._deactivate

    def __exit__(self, type, value, traceback):
        try:
            if type is not None:
                return self.exception_handler(type, value, traceback)
        finally:
            final_contexts = _state.contexts
            _state.contexts = self.old_contexts

            if final_contexts is not self.new_contexts:
                raise StackContextInconsistentError(
                    'stack_context inconsistency (may be caused by yield '
                    'within a "with StackContext" block)')

            # Break up a reference to itself to allow for faster GC on CPython.
            self.new_contexts = None


class NullContext(object):
    """Resets the `StackContext`.

    Useful when creating a shared resource on demand (e.g. an
    `.AsyncHTTPClient`) where the stack that caused the creating is
    not relevant to future operations.
    """
    def __enter__(self):
        self.old_contexts = _state.contexts
        _state.contexts = (tuple(), None)

    def __exit__(self, type, value, traceback):
        _state.contexts = self.old_contexts


def _remove_deactivated(contexts):
    """Remove deactivated handlers from the chain"""
    # Clean ctx handlers
    # 从上下文栈(tuple)中移除掉非活动（不需要传播）的 StackContext 上下文。
    stack_contexts = tuple([h for h in contexts[0] if h.active])

    # Find new head 沿着上下文链找到活动的 Head Context
    head = contexts[1]
    while head is not None and not head.active:
        head = head.old_contexts[1]

    # Process chain
    # 处理上下文链，对于不需要传播而被设置为非活动的 StackContext 上下文节点，在 With 调用结束后
    # 并没有从上下文链中移除，这段代码负责清理上下文链。
    # 注：_remove_deactivated 函数会在每次执行回调时调用（详见 wrapped 函数），对于没有非活
    # 动的上下文链，实际上进行了毫无意义的重复遍历处理上下文链，这个逻辑应该可以被优化。比如在上下
    # 文的 _deactivate()方法中调用 _remove_deactivated 而不是在每次执行回调时，这样就可
    # 以按需移除非活动的上下文。但是考虑到实际使用时上下文链不长的情况，这个重复遍历处理也尚可接
    # 受。
    ctx = head
    while ctx is not None:
        parent = ctx.old_contexts[1]

        while parent is not None:
            if parent.active:
                break
            ctx.old_contexts = parent.old_contexts
            parent = parent.old_contexts[1]

        ctx = parent

    return (stack_contexts, head)


def wrap(fn):
    """Returns a callable object that will restore the current `StackContext`
    when executed.

    Use this whenever saving a callback to be executed later in a
    different execution context (either in a different thread or
    asynchronously in the same thread).

    wrap(fn) 函数是上下文调度的“核心”，异步调用前用它获取当前上下文快照，再将快照保
    存在自由变量 cap_contexts中，并返回一个闭包（wrapped 或者 null_wrapper）作为回
    调函数 fn 的 wrapper，在之后回调时（在其他线程或者在相同线程异步调用）再从
    cap_contexts 中恢复保存的上下文状态，然后执行 fn 函数。
    """
    # Check if function is already wrapped
    if fn is None or hasattr(fn, '_wrapped'):
        return fn

    # Capture current stack head
    # TODO: Any other better way to store contexts and update them in wrapped function?
    cap_contexts = [_state.contexts]

    if not cap_contexts[0][0] and not cap_contexts[0][1]:
        # Fast path when there are no active contexts.
        def null_wrapper(*args, **kwargs):
            try:
                current_state = _state.contexts
                _state.contexts = cap_contexts[0]
                return fn(*args, **kwargs)
            finally:
                _state.contexts = current_state
        null_wrapper._wrapped = True
        return null_wrapper

    def wrapped(*args, **kwargs):
        ret = None
        try:
            # Capture old state
            current_state = _state.contexts

            # Remove deactivated items
            cap_contexts[0] = contexts = _remove_deactivated(cap_contexts[0])

            # Force new state
            _state.contexts = contexts

            # Current exception
            exc = (None, None, None)
            top = None

            # Apply stack contexts
            last_ctx = 0
            stack = contexts[0]

            # Apply state
            for n in stack:
                try:
                    n.enter()
                    last_ctx += 1
                except:
                    # Exception happened. Record exception info and store top-most handler
                    exc = sys.exc_info()
                    top = n.old_contexts[1]

            # Execute callback if no exception happened while restoring state
            if top is None:
                try:
                    ret = fn(*args, **kwargs)
                except:
                    exc = sys.exc_info()
                    top = contexts[1]

            # If there was exception, try to handle it by going through the exception chain
            if top is not None:
                exc = _handle_exception(top, exc)
            else:
                # Otherwise take shorter path and run stack contexts in reverse order
                while last_ctx > 0:
                    last_ctx -= 1
                    c = stack[last_ctx]

                    try:
                        c.exit(*exc)
                    except:
                        exc = sys.exc_info()
                        top = c.old_contexts[1]
                        break
                else:
                    top = None

                # If if exception happened while unrolling, take longer exception handler path
                if top is not None:
                    exc = _handle_exception(top, exc)

            # If exception was not handled, raise it
            if exc != (None, None, None):
                raise_exc_info(exc)
        finally:
            _state.contexts = current_state
        return ret

    wrapped._wrapped = True
    return wrapped


def _handle_exception(tail, exc):
    while tail is not None:
        try:
            if tail.exit(*exc):
                exc = (None, None, None)
        except:
            exc = sys.exc_info()

        tail = tail.old_contexts[1]

    return exc


def run_with_stack_context(context, func):
    """Run a coroutine ``func`` in the given `StackContext`.

    It is not safe to have a ``yield`` statement within a ``with StackContext``
    block, so it is difficult to use stack context with `.gen.coroutine`.
    This helper function runs the function in the correct context while
    keeping the ``yield`` and ``with`` statements syntactically separate.

    Example::

        @gen.coroutine
        def incorrect():
            with StackContext(ctx):
                # ERROR: this will raise StackContextInconsistentError
                yield other_coroutine()

        @gen.coroutine
        def correct():
            yield run_with_stack_context(StackContext(ctx), other_coroutine)

    .. versionadded:: 3.1
    """
    with context:
        return func()
