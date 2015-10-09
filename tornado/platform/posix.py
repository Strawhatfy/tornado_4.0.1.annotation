#!/usr/bin/env python
#
# Copyright 2011 Facebook
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

"""Posix implementations of platform-specific functionality."""

from __future__ import absolute_import, division, print_function, with_statement

import fcntl
import os

from tornado.platform import interface

# fcntl 模块为 Unix 上的 ioctl（input/output control，I/O 控制） 和 fcntl（file control，文件控制） 函数提供了一个接口。
# 它们用于文件句柄和 I/O 句柄的 “out of band” 操作，包括读取扩展属性，控制阻塞，更改终端行为等等（
# out of band management: 指使用分离的渠道进行设备管理。 这使系统管理员能在机器关机的时候对服务器, 网络进行监视和管理。
# 出处: http://en.wikipedia.org/wiki/Out-of-band_management ）
#
# 其中的 fcntl 函数签名为 fcntl.fcntl(fd, op[, arg])，共有 5 中功能：
#   1.复制一个现有的描述符（op=F_DUPFD）；
#   2.获得／设置文件描述符标记(op=F_GETFD 或 F_SETFD)；
#   3.获得／设置文件状态标记(op=F_GETFL 或 F_SETFL)；
#   4.获得／设置异步I/O所有权(op=F_GETOWN 或 F_SETOWN)；
#   5.获得／设置记录锁(op=F_GETLK,F_SETLK 或 F_SETLKW)；
# 其他更多函数请参考文档：https://docs.python.org/2/library/fcntl.html

# 通过 fork() 创建子进程时，子进程以写时复制（COW,Copy-On-Write）方式获得父进程的数据空间、堆和栈副本，这其中也包括文件描述符。
# 刚刚 fork() 成功时，父子进程中相同的文件描述符指向系统文件表中的同一项（这也意味着他们共享同一文件偏移量）。一般我们会在子进程中调
# 用 exec 一族的函数执行另一个程序，此时便会用全新的程序替换子进程的正文，数据，堆栈等，当然从父进程拷贝过来的文件描述就没法访问也就
# 无法关闭。为文件描述符设置 FD_CLOEXEC（close-on-exec）标识后，当子进程调用 exec 一族函数成功后便会自动（原子地）关闭该文件描述符。
def set_close_exec(fd):
    flags = fcntl.fcntl(fd, fcntl.F_GETFD)
    fcntl.fcntl(fd, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)


# 将文件描述符设置为非阻塞模式。
#
# O_NONBLOCK 和 O_NDELAY 的区别：
# O_NONBLOCK 和 O_NDELAY 所产生的结果都是使 I/O 变成非阻塞模式(non-blocking)，在读取不到数据或是写入缓冲区已满会马上 return ，而不会
# 阻塞程序，直到有数据或写入完成。二者差别在于设立 O_NDELAY 会使 I/O 函数返回 0 ，于是又导致另外一个问题，因为读取到文件结尾时所返回的值也是0，
# 这样无法得知是哪中情况。因此，O_NONBLOCK 就产生出来，它在读取不到数据时会回传 -1 ，并且设置 errno 为 EAGAIN 。
#
# 不过需要注意的是，在 GNU C 中 O_NDELAY 只是为了与 BSD 的程序兼容，实际上是使用 O_NONBLOCK 作为宏定义，而且 O_NONBLOCK 除了在 ioctl
# 中使用，还可以在 open 时设定。
def _set_nonblocking(fd):
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


class Waker(interface.Waker):
    def __init__(self):
        r, w = os.pipe()
        _set_nonblocking(r)
        _set_nonblocking(w)
        set_close_exec(r)
        set_close_exec(w)
        self.reader = os.fdopen(r, "rb", 0)
        self.writer = os.fdopen(w, "wb", 0)

    # 这个方法不命名为 reader_fileno 是为了提供 file-like object（有 fileno() 方法），这个在 IOLoop.split_fd 方法注释中有提到。
    def fileno(self):
        return self.reader.fileno()

    def write_fileno(self):
        return self.writer.fileno()

    def wake(self):
        try:
            self.writer.write(b"x")
        except IOError:
            pass

    def consume(self):
        try:
            while True:
                result = self.reader.read()
                if not result:
                    break
        except IOError:
            pass

    def close(self):
        self.reader.close()
        self.writer.close()
