"""
    This module is executed in remote subprocesses and helps to
    control a remote testing session and relay back information.
    It assumes that 'py' is importable and does not have dependencies
    on the rest of the xdist code.  This means that the xdist-plugin
    needs not to be installed in remote environments.
"""

import sys
import os
import time

import py
import pytest
from execnet.gateway_base import dumps, DumpError

from _pytest.config import _prepareconfig, Config

try:
    from setproctitle import setproctitle
except ImportError:

    def setproctitle(title):
        pass


def worker_title(title):
    try:
        setproctitle(title)
    except Exception:
        # changing the process name is very optional, no errors please
        pass


class WorkerInteractor:
    def __init__(self, config, channel):
        self.config = config
        self.workerid = config.workerinput.get("workerid", "?")
        self.testrunuid = config.workerinput["testrunuid"]
        self.log = py.log.Producer("worker-%s" % self.workerid)
        if not config.option.debug:
            py.log.setconsumer(self.log._keywords, None)
        self.channel = channel
        config.pluginmanager.register(self)

    def sendevent(self, name, **kwargs):
        self.log("sending", name, kwargs)
        self.channel.send((name, kwargs))

    @pytest.hookimpl
    def pytest_internalerror(self, excrepr):
        formatted_error = str(excrepr)
        for line in formatted_error.split("\n"):
            self.log("IERROR>", line)
        interactor.sendevent("internal_error", formatted_error=formatted_error)

    @pytest.hookimpl
    def pytest_sessionstart(self, session):
        self.session = session
        workerinfo = getinfodict()
        self.sendevent("workerready", workerinfo=workerinfo)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_sessionfinish(self, exitstatus):
        # in pytest 5.0+, exitstatus is an IntEnum object
        self.config.workeroutput["exitstatus"] = int(exitstatus)
        yield
        self.sendevent("workerfinished", workeroutput=self.config.workeroutput)

    @pytest.hookimpl
    def pytest_collection(self, session):
        self.sendevent("collectionstart")

    @pytest.hookimpl
    def pytest_runtestloop(self, session):
        self.log("entering main loop")
        torun = []
        while 1:
            try:
                name, kwargs = self.channel.receive()
            except EOFError:
                return True
            self.log("received command", name, kwargs)
            if name == "runtests":
                torun.extend(kwargs["indices"])
            elif name == "runtests_all":
                torun.extend(range(len(session.items)))
            self.log("items to run:", torun)
            # only run if we have an item and a next item
            while len(torun) >= 2:
                self.run_one_test(torun)
            if name == "shutdown":
                if torun:
                    self.run_one_test(torun)
                break
        return True

    def run_one_test(self, torun):
        items = self.session.items
        self.item_index = torun.pop(0)
        item = items[self.item_index]
        if torun:
            nextitem = items[torun[0]]
        else:
            nextitem = None

        worker_title("[pytest-xdist running] %s" % item.nodeid)

        start = time.time()
        self.config.hook.pytest_runtest_protocol(item=item, nextitem=nextitem)
        duration = time.time() - start

        worker_title("[pytest-xdist idle]")

        self.sendevent(
            "runtest_protocol_complete", item_index=self.item_index, duration=duration
        )

    @pytest.hookimpl
    def pytest_collection_finish(self, session):
        try:
            topdir = str(self.config.rootpath)
        except AttributeError:  # pytest <= 6.1.0
            topdir = str(self.config.rootdir)

        self.sendevent(
            "collectionfinish",
            topdir=topdir,
            ids=[item.nodeid for item in session.items],
        )

    @pytest.hookimpl
    def pytest_runtest_logstart(self, nodeid, location):
        self.sendevent("logstart", nodeid=nodeid, location=location)

    @pytest.hookimpl
    def pytest_runtest_logfinish(self, nodeid, location):
        self.sendevent("logfinish", nodeid=nodeid, location=location)

    @pytest.hookimpl
    def pytest_runtest_logreport(self, report):
        data = self.config.hook.pytest_report_to_serializable(
            config=self.config, report=report
        )
        data["item_index"] = self.item_index
        data["worker_id"] = self.workerid
        data["testrun_uid"] = self.testrunuid
        assert self.session.items[self.item_index].nodeid == report.nodeid
        self.sendevent("testreport", data=data)

    @pytest.hookimpl
    def pytest_collectreport(self, report):
        # send only reports that have not passed to controller as optimization (#330)
        if not report.passed:
            data = self.config.hook.pytest_report_to_serializable(
                config=self.config, report=report
            )
            self.sendevent("collectreport", data=data)

    @pytest.hookimpl
    def pytest_warning_recorded(self, warning_message, when, nodeid, location):
        self.sendevent(
            "warning_recorded",
            warning_message_data=serialize_warning_message(warning_message),
            when=when,
            nodeid=nodeid,
            location=location,
        )


def serialize_warning_message(warning_message):
    if isinstance(warning_message.message, Warning):
        message_module = type(warning_message.message).__module__
        message_class_name = type(warning_message.message).__name__
        message_str = str(warning_message.message)
        # check now if we can serialize the warning arguments (#349)
        # if not, we will just use the exception message on the controller node
        try:
            dumps(warning_message.message.args)
        except DumpError:
            message_args = None
        else:
            message_args = warning_message.message.args
    else:
        message_str = warning_message.message
        message_module = None
        message_class_name = None
        message_args = None
    if warning_message.category:
        category_module = warning_message.category.__module__
        category_class_name = warning_message.category.__name__
    else:
        category_module = None
        category_class_name = None

    result = {
        "message_str": message_str,
        "message_module": message_module,
        "message_class_name": message_class_name,
        "message_args": message_args,
        "category_module": category_module,
        "category_class_name": category_class_name,
    }
    # access private _WARNING_DETAILS because the attributes vary between Python versions
    for attr_name in warning_message._WARNING_DETAILS:
        if attr_name in ("message", "category"):
            continue
        attr = getattr(warning_message, attr_name)
        # Check if we can serialize the warning detail, marking `None` otherwise
        # Note that we need to define the attr (even as `None`) to allow deserializing
        try:
            dumps(attr)
        except DumpError:
            result[attr_name] = repr(attr)
        else:
            result[attr_name] = attr
    return result


def getinfodict():
    import platform

    return dict(
        version=sys.version,
        version_info=tuple(sys.version_info),
        sysplatform=sys.platform,
        platform=platform.platform(),
        executable=sys.executable,
        cwd=os.getcwd(),
    )


def remote_initconfig(option_dict, args):
    option_dict["plugins"].append("no:terminal")
    return Config.fromdictargs(option_dict, args)


def setup_config(config, basetemp):
    config.option.looponfail = False
    config.option.usepdb = False
    config.option.dist = "no"
    config.option.distload = False
    config.option.numprocesses = None
    config.option.maxprocesses = None
    config.option.basetemp = basetemp


if __name__ == "__channelexec__":
    channel = channel  # type: ignore[name-defined] # noqa: F821
    workerinput, args, option_dict, change_sys_path = channel.receive()  # type: ignore[name-defined]

    if change_sys_path is None:
        importpath = os.getcwd()
        sys.path.insert(0, importpath)
        os.environ["PYTHONPATH"] = (
            importpath + os.pathsep + os.environ.get("PYTHONPATH", "")
        )
    else:
        sys.path = change_sys_path

    os.environ["PYTEST_XDIST_TESTRUNUID"] = workerinput["testrunuid"]
    os.environ["PYTEST_XDIST_WORKER"] = workerinput["workerid"]
    os.environ["PYTEST_XDIST_WORKER_COUNT"] = str(workerinput["workercount"])

    if hasattr(Config, "InvocationParams"):
        config = _prepareconfig(args, None)
    else:
        config = remote_initconfig(option_dict, args)
        config.args = args

    setup_config(config, option_dict.get("basetemp"))
    config._parser.prog = os.path.basename(workerinput["mainargv"][0])
    config.workerinput = workerinput  # type: ignore[attr-defined]
    config.workeroutput = {}  # type: ignore[attr-defined]
    interactor = WorkerInteractor(config, channel)  # type: ignore[name-defined]
    config.hook.pytest_cmdline_main(config=config)
