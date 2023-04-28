"""Backend that runs targets on a local cluster.

To use this backend you must activate the `local` backend and start a local
cluster (with one or more workers) that the backend can submit targets to. To
start a cluster with two workers run the command::

    gwf -b local workers -n 2

in the working directory of your project. The workflow file must be accessible
to *gwf*. Thus, if your workflow file is not called `workflow.py` or the
workflow object is not called `gwf`, you must specify this so that *gwf* can
locate the workflow::

    gwf -f myworkflow.py:wf1 -b local workers -n 2

If the local backend is your default backend you can of course omit the
``-b local`` option.

If the ``-n`` option is omitted, *gwf* will detect the number of cores available
and use all of them.

To run your workflow, open another terminal and then type::

    gwf -b local run

To stop the pool of workers press :kbd:`Control-c`.

**Backend options:**

* **local.host (str):** Set the host that the workers are running on
    (default: localhost).
* **local.port (int):** Set the port used to connect to the workers
    (default: 12345).

**Target options:**

None available.
"""

import json
import logging
import os
import os.path
import selectors
import socket
import subprocess
import time
import uuid
from enum import Enum
from io import TextIOWrapper
from threading import Thread

import attrs

from .base import BackendStatus, TrackingBackend
from .exceptions import BackendError, UnsupportedOperationError

__all__ = ("Client", "Server")


DEFAULT_HOST = "localhost"
DEFAULT_PORT = 12345

logger = logging.getLogger(__name__)


def _gen_task_id():
    return uuid.uuid4().hex


class LocalStatus(Enum):
    UNKNOWN = 0
    SUBMITTED = 1
    RUNNING = 2
    FAILED = 3
    COMPLETED = 4


@attrs.frozen
class Connection:
    sock: socket.socket = attrs.field(hash=False)
    reader: TextIOWrapper = attrs.field(repr=False)
    writer: TextIOWrapper = attrs.field(repr=False)

    @classmethod
    def connect(cls, hostname, port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((hostname, port))
        return cls.from_socket(sock)

    @classmethod
    def from_socket(cls, sock):
        reader = sock.makefile(encoding="utf-8", mode="r")
        writer = sock.makefile(encoding="utf-8", mode="w")
        return cls(sock=sock, reader=reader, writer=writer)

    def send(self, msg_type, **msg):
        payload = dict(_type=msg_type, **msg)
        data = json.dumps(payload, cls=CustomEncoder) + "\n"
        self.writer.write(data)
        self.writer.flush()

    def recv(self):
        data = self.reader.readline().strip()
        msg = json.loads(data)
        msg_type = msg.pop("_type")
        return msg_type, msg

    def close(self):
        self.send("close")
        self.sock.close()


@attrs.define
class Client:
    """A client for communicating with the local backend server."""

    conn: Connection = attrs.field(repr=False)

    @classmethod
    def connect(cls, hostname=DEFAULT_HOST, port=DEFAULT_PORT):
        return cls(Connection.connect(hostname, port))

    def submit(self, target, stdout_path, stderr_path, deps=None):
        task_id = _gen_task_id()
        self.conn.send(
            "submit_task",
            task_id=task_id,
            name=target.name,
            spec=target.spec,
            working_dir=target.working_dir,
            dependencies=deps or [],
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        return task_id

    def status(self):
        self.conn.send("get_task_states")
        msg_type, response = self.conn.recv()
        assert msg_type == "task_states", "invalid response received"
        return {k: LocalStatus[v] for k, v in response["tasks"].items()}

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc):
        self.close()


@attrs.define
class LocalOps:
    working_dir: str = attrs.field()
    host: str = attrs.field()
    port: int = attrs.field()
    target_defaults: dict = attrs.field()

    _client: Client = attrs.field(init=False, repr=False)

    @_client.default
    def _create_client(self):
        try:
            return Client.connect(self.host, self.port)
        except ConnectionRefusedError:
            raise BackendError(
                f"Local backend could not connect to workers at {self.host} port {self.port}. "
                f"Workers can be started by running `gwf workers`."
            )

    def get_job_states(self, tracked_jobs):
        return {
            k: BackendStatus[v.name]
            for k, v in self._client.status().items()
            if k in tracked_jobs
        }

    def submit_target(self, target, dependency_ids):
        return self._client.submit(
            target,
            deps=dependency_ids,
            stdout_path=os.path.join(
                self.working_dir, ".gwf", "logs", f"{target.name}.stdout"
            ),
            stderr_path=os.path.join(
                self.working_dir, ".gwf", "logs", f"{target.name}.stderr"
            ),
        )

    def cancel_job(self, job_id):
        raise UnsupportedOperationError("cancel")

    def close(self):
        self._client.close()


def create_backend(working_dir, host=DEFAULT_HOST, port=DEFAULT_PORT):
    return TrackingBackend(
        working_dir,
        name="local",
        ops=LocalOps(working_dir, host, port, target_defaults={}),
    )


class CustomEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (set, frozenset)):
            return list(obj)
        if isinstance(obj, LocalStatus):
            return obj.name
        return json.JSONEncoder.default(self, obj)


@attrs.frozen
class Task:
    task_id: str = attrs.field()
    name: str = attrs.field()
    working_dir: str = attrs.field(repr=False)
    spec: str = attrs.field(repr=False)
    dependencies: frozenset = attrs.field(repr=False)
    stdout_path: str = attrs.field(repr=False)
    stderr_path: str = attrs.field(repr=False)

    def __str__(self):
        return self.task_id


@attrs.define
class Worker:
    worker_id: str = attrs.field()
    conn: Connection = attrs.field()

    _logger: logging.Logger = attrs.field(repr=False, init=False)
    _shutdown_requested: bool = attrs.field(default=False, init=False)

    @_logger.default
    def _create_logger(self):
        return logging.getLogger(self.worker_id)

    @classmethod
    def connect(cls, worker_id, hostname=DEFAULT_HOST, port=DEFAULT_PORT):
        conn = Connection.connect(hostname, port)
        conn.send("join_worker", worker_id=worker_id)
        return cls(worker_id, conn)

    def _update_state(self, task_id, new_state):
        self.conn.send("update_task_state", task_id=task_id, new_state=new_state.name)

    def run_task(self, task):
        self._logger.debug("Task %s started", task.task_id)
        self._update_state(task.task_id, LocalStatus.RUNNING)
        try:
            env = os.environ.copy()
            env["GWF_TARGET_NAME"] = task.name

            with open(task.stdout_path, mode="w") as stdout_fp, open(
                task.stderr_path, mode="w"
            ) as stderr_fp:
                process = subprocess.Popen(
                    ["bash"],
                    stdin=subprocess.PIPE,
                    stdout=stdout_fp,
                    stderr=stderr_fp,
                    universal_newlines=True,
                    cwd=task.working_dir,
                    env=env,
                )
                assert process.stdin is not None
                process.stdin.write(task.spec)
                process.stdin.flush()
                process.stdin.close()

                while process.poll() is None:
                    if self._shutdown_requested:
                        raise Exception("Worker received shutdown signal")
                    time.sleep(0.01)

                process.wait(timeout=60)
                if process.returncode != 0:
                    raise Exception(
                        f"Task {task.task_id} ({task.name}) exited with "
                        f"return code {process.returncode}."
                    )
        except Exception:
            self._logger.error("Task %s failed", task.task_id, exc_info=True)
            self._update_state(task.task_id, LocalStatus.FAILED)
        else:
            self._logger.debug("Task %s completed", task.task_id)
            self._update_state(task.task_id, LocalStatus.COMPLETED)

    def handle_run_task(self, task_id, **kwargs):
        task = Task(task_id=task_id, **kwargs)
        self._logger.debug("Worker received task %s (%s)", task_id, kwargs.get("name"))
        self.run_task(task)
        self._logger.debug("Worker is now idle")

    def handle_shutdown(self):
        self._logger.debug("Shutdown requested for worker %s", self.worker_id)
        self._shutdown_requested = True

    def start(self):
        while not self._shutdown_requested:
            msg_type, msg = self.conn.recv()
            getattr(self, f"handle_{msg_type}")(**msg)
        self.conn.send("leave_worker", worker_id=self.worker_id)
        self.conn.close()


@attrs.frozen
class JoinedWorker:
    worker_id: str = attrs.field()
    conn: Connection = attrs.field(hash=False, repr=False)

    def run_task(self, task):
        self.conn.send("run_task", **attrs.asdict(task))

    def shutdown(self):
        self.conn.send("shutdown")

    def __str__(self):
        return self.worker_id


# @attrs.define
# class Scheduler:
#     sched_interval: int = attrs.field(default=0.1)

#     _tasks: dict = attrs.field(factory=dict, repr=False, init=False)
#     _task_states: dict = attrs.field(factory=dict, repr=False, init=False)
#     _pending_tasks: set = attrs.field(factory=set, repr=False, init=False)
#     _scheduled_tasks: set = attrs.field(factory=set, repr=False, init=False)
#     _joined_workers: dict = attrs.field(factory=dict, init=False, repr=False)
#     _used_workers: dict = attrs.field(factory=dict, init=False, repr=False)
#     _lock: Lock = attrs.field(repr=False, init=False, factory=Lock)
#     _logger: logging.Logger = attrs.field(init=False, repr=False)
#     _shutdown_requested: bool = attrs.field(default=False, init=False)

#     @_logger.default
#     def _create_logger(self):
#         return logging.getLogger(self.__class__.__name__)

#     def add_worker(self, worker):
#         with self._lock:
#             self._joined_workers[worker.worker_id] = worker

#     def remove_worker(self, worker_id):
#         with self._lock:
#             del self._joined_workers[worker_id]

#     def submit_task(
#         self, task_id, name, working_dir, spec, dependencies, stdout_path, stderr_path
#     ):
#         task = Task(
#             name=name,
#             working_dir=working_dir,
#             spec=spec,
#             dependencies=frozenset(dependencies),
#             task_id=task_id,
#             stdout_path=stdout_path,
#             stderr_path=stderr_path,
#         )
#         with self._lock:
#             self._tasks[task_id] = task
#             self._task_states[task_id] = LocalStatus.SUBMITTED
#             self._pending_tasks.add(task_id)

#     def get_task_states(self):
#         return {k: v.name for k, v in self._task_states.items()}

#     def set_task_state(self, task_id, new_state):
#         with self._lock:
#             self._task_states[task_id] = new_state
#             if new_state in (LocalStatus.COMPLETED, LocalStatus.FAILED):
#                 del self._used_workers[task_id]

#     def _schedule_once(self):
#         failed_tasks = set()
#         scheduled_tasks = set()
#         used_workers = {}
#         for task_id in self._pending_tasks:
#             task = self._tasks[task_id]

#             if any(
#                 self._task_states.get(dep_id) == LocalStatus.FAILED
#                 for dep_id in task.dependencies
#             ):
#                 failed_tasks.add(task_id)
#                 continue

#             if any(
#                 self._task_states.get(dep_id) != LocalStatus.COMPLETED
#                 for dep_id in task.dependencies
#             ):
#                 continue

#             for worker_id, worker in self._joined_workers.items():
#                 if worker_id not in self._used_workers.values():
#                     used_workers[task_id] = worker_id
#                     scheduled_tasks.add(task_id)
#                     worker.run_task(task)
#                     break

#         self._used_workers.update(used_workers)

#         for task_id in scheduled_tasks:
#             self._pending_tasks.remove(task_id)

#         for task_id in failed_tasks:
#             self._task_states[task_id] = LocalStatus.FAILED
#             self._pending_tasks.remove(task_id)

#     def start(self):
#         while not self._shutdown_requested:
#             with self._lock:
#                 try:
#                     self._schedule_once()
#                 except Exception as exc:
#                     self._logger.exception(exc)
#             time.sleep(self.sched_interval)

#     def shutdown(self):
#         self._shutdown_requested = True
#         with self._lock:
#             for worker in self._joined_workers.values():
#                 worker.shutdown()


@attrs.define
class Server:
    hostname: str = attrs.field()
    port: int = attrs.field()
    sched_interval: int = attrs.field(default=0.1)

    _tasks: dict = attrs.field(factory=dict, repr=False, init=False)
    _task_states: dict = attrs.field(factory=dict, repr=False, init=False)
    _pending_tasks: set = attrs.field(factory=set, repr=False, init=False)
    _scheduled_tasks: set = attrs.field(factory=set, repr=False, init=False)
    _joined_workers: dict = attrs.field(factory=dict, init=False, repr=False)
    _used_workers: dict = attrs.field(factory=dict, init=False, repr=False)

    _sock = attrs.field(init=False, repr=False)
    _logger: logging.Logger = attrs.field(init=False, repr=False)
    _shutdown_requested: bool = attrs.field(default=False, init=False, repr=False)
    _sel: selectors.BaseSelector = attrs.field(
        factory=selectors.DefaultSelector, init=False, repr=False
    )
    _conns: dict = attrs.field(factory=dict, init=False, repr=False)

    @_logger.default
    def _create_logger(self):
        return logging.getLogger(self.__class__.__name__)

    def _schedule_once(self):
        failed_tasks = set()
        scheduled_tasks = set()
        used_workers = {}
        for task_id in self._pending_tasks:
            task = self._tasks[task_id]

            if any(
                self._task_states.get(dep_id) == LocalStatus.FAILED
                for dep_id in task.dependencies
            ):
                failed_tasks.add(task_id)
                continue

            if any(
                self._task_states.get(dep_id) != LocalStatus.COMPLETED
                for dep_id in task.dependencies
            ):
                continue

            for worker_id, worker in self._joined_workers.items():
                if worker_id not in self._used_workers.values():
                    used_workers[task_id] = worker_id
                    scheduled_tasks.add(task_id)
                    worker.run_task(task)
                    break

        self._used_workers.update(used_workers)

        for task_id in scheduled_tasks:
            self._pending_tasks.remove(task_id)

        for task_id in failed_tasks:
            self._task_states[task_id] = LocalStatus.FAILED
            self._pending_tasks.remove(task_id)

    def handle_join_worker(self, conn, worker_id):
        self._logger.info("Worker %s has joined", worker_id)
        self._joined_workers[worker_id] = JoinedWorker(worker_id, conn)
        self._schedule_once()

    def handle_leave_worker(self, conn, worker_id):
        self._logger.info("Worker %s is leaving", worker_id)
        del self._joined_workers[worker_id]

    def handle_update_task_state(self, conn, task_id, new_state):
        new_state = LocalStatus[new_state]
        self._task_states[task_id] = new_state
        if new_state in (LocalStatus.COMPLETED, LocalStatus.FAILED):
            del self._used_workers[task_id]
        self._schedule_once()

    def handle_get_task_states(self, conn):
        task_states = {k: v.name for k, v in self._task_states.items()}
        conn.send("task_states", tasks=task_states)

    def handle_submit_task(
        self,
        conn,
        task_id,
        name,
        working_dir,
        spec,
        dependencies,
        stdout_path,
        stderr_path,
    ):
        task = Task(
            name=name,
            working_dir=working_dir,
            spec=spec,
            dependencies=frozenset(dependencies),
            task_id=task_id,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        self._tasks[task_id] = task
        self._task_states[task_id] = LocalStatus.SUBMITTED
        self._pending_tasks.add(task_id)
        self._schedule_once()

    def handle_close(self, conn):
        del self._conns[conn.sock]
        self._sel.unregister(conn.sock)
        conn.close()

    def prepare(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.hostname, self.port))
        self._sock.listen(1024)

        self._logger.info(
            "Server is listening on %s port %s",
            self.hostname,
            self.port,
        )

    def start(self):
        def _accept(sock):
            client, (host, port) = sock.accept()
            client.setblocking(False)
            self._sel.register(client, selectors.EVENT_READ, _read)
            if client not in self._conns:
                self._conns[client] = Connection.from_socket(client)
            self._logger.debug("Accepted connection from %s port %s", host, port)

        def _read(client):
            conn = self._conns[client]
            msg_type, msg = conn.recv()
            getattr(self, f"handle_{msg_type}")(conn, **msg)

        self._sock.setblocking(False)
        self._sel.register(self._sock, selectors.EVENT_READ, _accept)
        while not self._shutdown_requested or self._joined_workers:
            events = self._sel.select(timeout=0.01)
            for key, _ in events:
                callback = key.data
                callback(key.fileobj)

        self._sel.close()

    def shutdown(self):
        for _, worker in self._joined_workers.items():
            worker.shutdown()
        self._shutdown_requested = True


@attrs.define
class Cluster:
    num_workers: int = attrs.field(default=1)

    hostname: str = attrs.field(default=DEFAULT_HOST)
    port: int = attrs.field(default=DEFAULT_PORT)

    server: Server = attrs.field()

    _logger: logging.Logger = attrs.field(init=False, repr=False)
    _server_thread: Thread = attrs.field(repr=False, init=False)
    _workers: list = attrs.field(factory=list, repr=False, init=False)
    _shutdown_requested: bool = attrs.field(default=False, init=False, repr=False)

    @_logger.default
    def _create_logger(self):
        return logging.getLogger(self.__class__.__name__)

    @server.default
    def _create_server(self):
        server = Server(self.hostname, self.port)
        server.prepare()
        return server

    @_server_thread.default
    def _create_server_thread(self):
        return Thread(target=self.server.start)

    def start(self):
        self._server_thread.start()
        for num in range(self.num_workers):
            worker_id = f"Worker{num}"
            worker = Worker.connect(worker_id, self.hostname, self.port)
            worker_thread = Thread(target=worker.start)
            worker_thread.start()
            self._workers.append((worker, worker_thread))

        while not self._shutdown_requested:
            time.sleep(0.01)

        self.server.shutdown()
        self._server_thread.join()

    def shutdown(self):
        self._shutdown_requested = True


setup = (create_backend, 0)
