"""An executor which limits the number of running futures."""
from concurrent.futures import Executor
from threading import Event, Thread, Lock, Semaphore
from collections import namedtuple, deque
import logging
import weakref

from more_executors._common import _MAX_TIMEOUT
from more_executors._wrap import CanCustomizeBind
from more_executors.map import _MapFuture

__pdoc__ = {}
__pdoc__['ThrottleExecutor.submit'] = None
__pdoc__['ThrottleExecutor.map'] = None
__pdoc__['ThrottleExecutor.shutdown'] = None


class _ThrottleFuture(_MapFuture):
    def __init__(self, executor):
        self._executor_ref = weakref.ref(executor)
        super(_ThrottleFuture, self).__init__(delegate=None, map_fn=lambda x: x)

    def _me_cancel(self):
        if self._delegate:
            return self._delegate.cancel()
        executor = self._executor_ref()
        return executor and executor._do_cancel(self)


_ThrottleJob = namedtuple('_ThrottleJob', ['future', 'fn', 'args', 'kwargs'])


class ThrottleExecutor(CanCustomizeBind, Executor):
    """An `Executor` which delegates to another `Executor` while enforcing
    a limit on the number of futures running concurrently.

    - Callables are submitted to the delegate executor, from a different
      thread than the calling thread.

    - Where `count` is used to initialize this executor, if there
      are already `count` futures submitted to the delegate executor and not
      yet `done()`, additional callables will be queued and only submitted
      to the delegate executor once there are less than `count` futures
      in progress.

    *Since version 1.9.0*
    """
    def __init__(self, delegate, count, logger=None):
        """Create a new executor.

        - `delegate`: `Executor` instance to which callables will be submitted
        - `count`: maximum number of concurrently running futures
        - `logger`: a `Logger` used for messages from this executor
        """
        self._log = logger if logger else logging.getLogger('ThrottleExecutor')
        self._delegate = delegate
        self._to_submit = deque()
        self._lock = Lock()
        self._event = Event()
        self._sem = Semaphore(count)
        self._shutdown = False

        event = self._event
        self_ref = weakref.ref(self, lambda _: event.set())

        self._thread = Thread(name='ThrottleExecutor', target=_submit_loop, args=(self_ref,))
        self._thread.daemon = True
        self._thread.start()

    def submit(self, fn, *args, **kwargs):
        out = _ThrottleFuture(self)
        job = _ThrottleJob(out, fn, args, kwargs)
        with self._lock:
            self._to_submit.append(job)
            self._log.debug("Enqueued: %s", job)
        self._event.set()
        return out

    def shutdown(self, wait=True):
        self._log.debug("Shutting down")
        self._shutdown = True
        self._delegate.shutdown(wait)
        self._event.set()
        if wait:
            self._thread.join(_MAX_TIMEOUT)

    def _do_submit(self, job):
        delegate_future = self._delegate.submit(job.fn, *job.args, **job.kwargs)
        self._log.debug("Submitted %s yielding %s", job, delegate_future)

        delegate_future.add_done_callback(self._delegate_future_done)
        job.future._set_delegate(delegate_future)

    def _do_cancel(self, future):
        with self._lock:
            for job in self._to_submit:
                if job.future is future:
                    self._to_submit.remove(job)
                    self._log.debug("Cancelled %s", job)
                    return True
        self._log.debug("Could not find for cancel: %s", future)
        return False

    def _delegate_future_done(self, future):
        # Whenever an inner future completes, one more execution slot becomes
        # available, and the thread should wake up in case there's something to
        # be submitted
        self._log.debug("Delegate future done: %s", future)
        self._sem.release()
        self._event.set()


def _submit_loop(executor_ref):
    while True:
        executor = executor_ref()

        if not executor:
            break

        if executor._shutdown:
            break

        to_submit = []
        with executor._lock:
            while executor._to_submit:
                if not executor._sem.acquire(False):
                    executor._log.debug("Throttled")
                    break
                job = executor._to_submit.popleft()
                executor._log.debug("Will submit: %s", job)
                to_submit.append(job)

            executor._log.debug("Submitting %s, throttling %s",
                                len(to_submit), len(executor._to_submit))

        for job in to_submit:
            executor._do_submit(job)

        event = executor._event
        del executor

        event.wait()
        event.clear()