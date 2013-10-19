"""
Proxy.

This proxy is used to drive the server classes
through the huptime binary, but still have them
accessible from the test harness.
"""

import os
import sys
import uuid
import subprocess
import threading
import traceback
import pickle

import modes
import servers

class ProxyServer(object):

    def __init__(self, mode_name, server_name, cookie_file):
        cookie = open(cookie_file, 'r').read()
        self._mode = getattr(modes, mode_name)()
        self._server = getattr(servers, server_name)(cookie)
        self._cond = threading.Condition()

    def run(self):
        # Open our pipes.
        in_pipe = os.fdopen(3, 'r')
        out_pipe = os.fdopen(4, 'w')

        # Dump our startup message.
        robj = {
            "id": None,
            "result": None
        }
        out_pipe.write(pickle.dumps(robj))
        out_pipe.flush()

        # Get the call from the other side.
        while True:
            try:
                obj = pickle.load(in_pipe)
                sys.stderr.write("proxy: server <- %s\n" % obj)
            except:
                # We're done!
                break
            def process():
                self._process(obj, out_pipe)

            t = threading.Thread(target=process)
            t.start()

        # Wait for processing to finish on
        # the server side. It's possible that
        # there are client connections to finish.
        sys.stderr.write("proxy: waiting...")
        self._server.wait()
        sys.stderr.write("proxy: done")

    def _process(self, obj, out):
        uniq = obj.get("id")
        try:
            if not "method_name" in obj:
                raise ValueError("no method_name?")
            method_name = obj["method_name"]
            args = obj.get("args")
            kwargs = obj.get("kwargs")
            if method_name:
                method = getattr(self._server, method_name)
                self._mode.pre(method_name, self._server)
                result = method(*args, **kwargs)
                self._mode.post(method_name, self._server)
            else:
                result = None
            robj = {
                "id": uniq,
                "result": result
            }
        except Exception as e:
            traceback.print_exc()
            robj = {
                "id": uniq,
                "exception": e
            }

        self._cond.acquire()
        try:
            sys.stderr.write("proxy: server -> %s\n" % robj)
            out.write(pickle.dumps(robj))
            out.flush()
        finally:
            self._cond.release()

class ProxyClient(object):

    def __init__(self, cmdline, mode_class, server_class, cookie_file):
        super(ProxyClient, self).__init__()
        self._cond = threading.Condition()
        self._results = {}
        self._cookie_file = cookie_file

        cmdline = cmdline[:]
        cmdline.extend([
            "python",
            __file__,
            mode_class.__name__,
            server_class.__name__,
            self._cookie_file,
        ])

        r, w = os.pipe()
        self._out = os.fdopen(w, 'w')
        proc_in = os.fdopen(r, 'r')

        r, w = os.pipe()
        self._in = os.fdopen(r, 'r')
        proc_out = os.fdopen(w, 'w')

        def _setup_pipes():
            os.dup2(0, 3)
            os.dup2(1, 4)
            devnull = open("/dev/null", 'r')
            os.dup2(devnull.fileno(), 0)
            devnull.close()
            os.dup2(2, 1)

        sys.stderr.write("exec: %s\n" % " ".join(cmdline))
        proc = subprocess.Popen(
            cmdline,
            stdin=proc_in,
            stdout=proc_out,
            preexec_fn=_setup_pipes,
            close_fds=True)

        proc_in.close()
        proc_out.close()

        # Start the processing thread.
        t = threading.Thread(target=self._run)
        t.daemon = True
        t.start()

        # Start the reaper thread.
        self._reaper = threading.Thread(target=lambda: proc.wait())
        self._reaper.daemon = True
        self._reaper.start()

    def _join(self):
        self._reaper.join()

    def _call(self, method_name=None, args=None, kwargs=None):
        if args is None:
            args = []
        if kwargs is None:
            kwargs = {}

        # Send the call to the other side.
        uniq = str(uuid.uuid4())
        obj = {
            "id": uniq,
            "method_name": method_name,
            "args": args,
            "kwargs": kwargs
        }
        sys.stderr.write("proxy: client -> %s\n" % obj)
        self._out.write(pickle.dumps(obj))
        self._out.flush()

        return uniq

    def _wait(self, uniq=None, method_name=None):
        # Wait for a result to appear.
        self._cond.acquire()
        try:
            while True:
                if uniq in self._results:
                    res = self._results[uniq]
                    del self._results[uniq]
                    if "exception" in res:
                        raise res["exception"]
                    elif "result" in res:
                        return res["result"]
                    else:
                        raise ValueError("no result?")
                sys.stderr.write("proxy: waiting for %s (%s)...\n" %
                    (uniq, method_name))
                self._cond.wait()
        finally:
            self._cond.release()

    def _run(self):
        # Get the return from the other side.
        while True:
            try:
                obj = pickle.load(self._in)
                sys.stderr.write("proxy: client <- %s\n" % obj)
            except:
                # We're done!
                break
            self._cond.acquire()
            try:
                uniq = obj.get("id")
                self._results[uniq] = obj
                self._cond.notifyAll()
            finally:
                self._cond.release()

    def __getattr__(self, method_name):
        def _fn(*args, **kwargs):
            uniq = self._call(method_name, args, kwargs)
            return self._wait(uniq, method_name=method_name)
        _fn.__name__ = method_name
        return _fn

if __name__ == "__main__":
    proxy = ProxyServer(*sys.argv[1:])
    proxy.run()