#!/usr/bin/env python
'''
[respawn.py]

is a user level implementation of inittab respawning functionality.  Script
uses syslog to report its activities. It can wrap itself around some process,
spawned from the command line executable, and restore it once it has been
killed. Send HUP signal to the parent process so that the underlying child
process will be killed and restarted.
'''
# Copyright (c) 2012 Yauhen Yakimovich <yauhen.yakimovich@uzh.ch>
# Copyright (c) 2006 Red Hat, Inc. Original code written by
# Gary Benson <gbenson@redhat.com for vpnc-watch.py
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# TODO:
#  - add optional lock for pids in /var/run/*
#  - add option to turn on dettach
#
import os
import signal
import sys
import syslog
import time
import traceback


__author__ = 'Yauhen Yakimovich'
__copyright__ = 'Copyright 2012 Yauhen Yakimovich'
__credits__ = ['Yauhen Yakimovich', 'Gary Benson']
__license__ = 'GPL'
__version__ = '1.0.1'


APP_NAME = 'respawn'
WAIT4PID_TIMEOUT = 5


class Error(Exception):
    pass


def which(cmd):
    for path in os.environ["PATH"].split(":"):
        path = os.path.join(path, cmd)
        if os.access(path, os.X_OK):
            return path
    raise Error("no %s in (%s)" % (cmd, os.environ["PATH"]))


def pidof(cmd):
    if not os.path.dirname(cmd):
        cmd = which(cmd)
    cmd = os.path.realpath(cmd)
    pids = []
    seen = False
    for pid in os.listdir("/proc"):
        try:
            pid = int(pid)
        except ValueError:
            continue
        seen = True
        path = os.path.join("/proc", str(pid), "exe")
        try:
            if os.path.realpath(path) == cmd:
                pids.append(pid)
        except OSError:
            pass
    assert seen
    return pids


def tellpid(pid, path):
    pidfile = open(path, 'w')
    pidfile.write('%d' % pid)
    pidfile.close()


def wait4pid(pid):
    signal.alarm(WAIT4PID_TIMEOUT)
    status = os.waitpid(pid, 0)[1]
    signal.alarm(0)
    if os.WIFSIGNALED(status):
        raise Error("process killed with signal %d" % os.WTERMSIG(status))
    if not os.WIFEXITED(status):
        raise RuntimeError("os.waitpid returned %d" % status)
    status = os.WEXITSTATUS(status)
    if status:
        raise Error("process exited with code %d" % status)


class Watcher(object):
    def __init__(self, cmd, args, pidpath=None):
        self.name = os.path.basename(cmd)
        if not os.path.dirname(cmd):
            cmd = which(cmd)
        self.cmd = os.path.realpath(cmd)
        self.args = args
        self.pidpath = pidpath

    def start(self):
        syslog.syslog(syslog.LOG_NOTICE, "starting %s" % self.name)
        pid = os.fork()
        if pid == 0:
            if self.pidpath is not None:
                syslog.syslog(syslog.LOG_NOTICE,
                    'saving child\'s pid into: %s' % self.pidpath)
                tellpid(os.getpid(), self.pidpath)
            os.execv(self.cmd, self.args)
        pids = pidof(self.cmd)
        if not pids or pid not in pids:
            raise Error("%s is not running!" % self.cmd)
        self.pid = pid
        syslog.syslog(syslog.LOG_NOTICE, "%s started" % self.name)

    def detach(self):
        if os.fork():
            sys.exit()
        os.setsid()
        if os.fork():
            sys.exit()
        null = os.open("/dev/null", os.O_RDWR)
        for fd in range(3):
            os.dup2(null, fd)

    def isrunning(self):
        return os.access(os.path.join("/proc", str(self.pid)), os.F_OK)

    def stop(self):
        syslog.syslog(syslog.LOG_NOTICE,
            "stopping %s (pid: %d)" % (self.name, self.pid))
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(self.pid, sig)
            except OSError:
                continue
        if self.isrunning():
            os.system('kill -9 %d' % self.pid)
            try:
                wait4pid(self.pid)
            except Error, e:
                syslog.syslog(syslog.LOG_NOTICE, str(e))
        if self.isrunning():
            raise Error("%s (%d) didn't die!" % (self.name, self.pid))
        syslog.syslog(syslog.LOG_NOTICE, "stopped %s (pid: %d)" %\
            (self.name, self.pid))

    def signal(self, num, frame):
        if num == signal.SIGHUP:
            self.do_restart = True
        if num == signal.SIGTERM:
            self.do_exit = True

    def run(self):
        syslog.openlog(APP_NAME, syslog.LOG_PID, syslog.LOG_DAEMON)
        self.start()
        #self.detach()
        try:
            signal.signal(signal.SIGHUP, self.signal)
            signal.signal(signal.SIGTERM, self.signal)

            self.do_exit = False
            while not self.do_exit:
                self.do_restart = False
                time.sleep(1)

                running = self.isrunning()
                if not running:
                    syslog.syslog(syslog.LOG_WARNING, "%s died" % self.name)
                elif self.do_exit or self.do_restart:
                    self.stop()
                if self.do_restart or not running:
                    self.start()
            syslog.syslog(syslog.LOG_INFO, "exiting")
        except Error, e:
            syslog.syslog(syslog.LOG_ERR, "error: " + str(e))
            sys.exit(1)
        except:
            msg = "".join(apply(traceback.format_exception, sys.exc_info()))
            for line in msg.split("\n"):
                if line:
                    syslog.syslog(syslog.LOG_ERR, line)
            sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print >>sys.stderr, "usage: %s COMMAND [ARGS]" % sys.argv[0]
        sys.exit(1)
    try:
        APP_NAME = os.path.basename(sys.argv[0])
        if sys.argv[1] == '--tell-pid':
            pidpath = sys.argv[2]
            Watcher(sys.argv[3], sys.argv[4:], pidpath).run()
        else:
            Watcher(sys.argv[1], sys.argv[2:]).run()
    except Error, e:
        print >>sys.stderr, "error:", e
        sys.exit(1)
