from __future__ import print_function

import pytest
import time
import datetime
import requests
import os
import sys
import logging
import shutil
from contextlib import contextmanager
from tests import utils
from six.moves import queue
from wandb import wandb_sdk

# from multiprocessing import Process
import subprocess
import click
from click.testing import CliRunner
import webbrowser
import git
import psutil
import atexit
import wandb
import shutil
from wandb.util import mkdir_exists_ok
from six.moves import urllib

# TODO: consolidate dynamic imports
PY3 = sys.version_info.major == 3 and sys.version_info.minor >= 6
if PY3:
    from wandb.sdk.lib.module import unset_globals
    from wandb.sdk.lib.git import GitRepo
    from wandb.sdk.interface.interface import BackendSender
else:
    from wandb.sdk_py27.lib.module import unset_globals
    from wandb.sdk_py27.lib.git import GitRepo
    from wandb.sdk_py27.interface.interface import BackendSender

from wandb.proto import wandb_internal_pb2

try:
    import nbformat
except ImportError:  # TODO: no fancy notebook fun in python2
    pass

try:
    from unittest.mock import MagicMock
except ImportError:  # TODO: this is only for python2
    from mock import MagicMock

DUMMY_API_KEY = "1824812581259009ca9981580f8f8a9012409eee"
server = None


def test_cleanup(*args, **kwargs):
    global server
    print("Shutting down mock server")
    server.terminate()
    print("Open files during tests: ")
    proc = psutil.Process()
    print(proc.open_files())


def start_mock_server():
    """We start a server on boot for use by tests"""
    global server
    port = utils.free_port()
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    path = os.path.join(root, "tests", "utils", "mock_server.py")
    command = [sys.executable, "-u", path]
    env = os.environ
    env["PORT"] = str(port)
    env["PYTHONPATH"] = root
    logfile = open("tests/logs/live_mock_server.log", "w")
    server = subprocess.Popen(
        command,
        stdout=logfile,
        env=env,
        stderr=subprocess.STDOUT,
        bufsize=1,
        close_fds=True,
    )
    server._port = port
    server.base_url = "http://localhost:%i" % server._port

    def get_ctx():
        return requests.get(server.base_url + "/ctx").json()

    def set_ctx(payload):
        return requests.put(server.base_url + "/ctx", json=payload).json()

    def reset_ctx():
        return requests.delete(server.base_url + "/ctx").json()

    server.get_ctx = get_ctx
    server.set_ctx = set_ctx
    server.reset_ctx = reset_ctx

    started = False
    for i in range(10):
        try:
            res = requests.get("%s/ctx" % server.base_url, timeout=5)
            if res.status_code == 200:
                started = True
                break
            print("Attempting to connect but got: %s" % res)
        except requests.exceptions.RequestException:
            print(
                "Timed out waiting for server to start...", server.base_url, time.time()
            )
            if server.poll() is None:
                time.sleep(1)
            else:
                raise ValueError("Server failed to start.")
    if started:
        print(
            "Mock server listing on %s see tests/logs/live_mock_server.log"
            % server._port
        )
    else:
        server.terminate()
        print("Server failed to launch, see tests/logs/live_mock_server.log")
        try:
            print("=" * 40)
            with open("tests/logs/live_mock_server.log") as f:
                for l in f.readlines():
                    print(l.strip())
            print("=" * 40)
        except Exception as e:
            print("EXCEPTION:", e)
        raise ValueError("Failed to start server!  Exit code %s" % server.returncode)
    return server


start_mock_server()
atexit.register(test_cleanup)


@pytest.fixture
def test_name(request):
    # change "test[1]" to "test__1__"
    name = urllib.parse.quote(request.node.name.replace("[", "__").replace("]", "__"))
    return name


@pytest.fixture
def test_dir(test_name):
    orig_dir = os.getcwd()
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    test_dir = os.path.join(root, "tests", "logs", test_name)
    if os.path.exists(test_dir):
        shutil.rmtree(test_dir)
    mkdir_exists_ok(test_dir)
    os.chdir(test_dir)
    yield runner
    os.chdir(orig_dir)


@pytest.fixture
def git_repo(runner):
    with runner.isolated_filesystem():
        r = git.Repo.init(".")
        mkdir_exists_ok("wandb")
        # Because the forked process doesn't use my monkey patch above
        with open("wandb/settings", "w") as f:
            f.write("[default]\nproject: test")
        open("README", "wb").close()
        r.index.add(["README"])
        r.index.commit("Initial commit")
        yield GitRepo(lazy=False)


@pytest.fixture
def git_repo_with_remote(runner):
    with runner.isolated_filesystem():
        r = git.Repo.init(".")
        r.create_remote("origin", "https://foo:bar@github.com/FooTest/Foo.git")
        yield GitRepo(lazy=False)


@pytest.fixture
def git_repo_with_remote_and_empty_pass(runner):
    with runner.isolated_filesystem():
        r = git.Repo.init(".")
        r.create_remote("origin", "https://foo:@github.com/FooTest/Foo.git")
        yield GitRepo(lazy=False)


@pytest.fixture
def dummy_api_key():
    return DUMMY_API_KEY


@pytest.fixture
def test_settings(test_dir, mocker, live_mock_server):
    """ Settings object for tests"""
    #  TODO: likely not the right thing to do, we shouldn't be setting this
    wandb._IS_INTERNAL_PROCESS = False
    wandb.wandb_sdk.wandb_run.EXIT_TIMEOUT = 15
    wandb.wandb_sdk.wandb_setup._WandbSetup.instance = None
    wandb_dir = os.path.join(os.getcwd(), "wandb")
    mkdir_exists_ok(wandb_dir)
    # root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    settings = wandb.Settings(
        _start_time=time.time(),
        base_url=live_mock_server.base_url,
        root_dir=os.getcwd(),
        save_code=True,
        project="test",
        console="off",
        host="test",
        api_key=DUMMY_API_KEY,
        run_id=wandb.util.generate_id(),
        _start_datetime=datetime.datetime.now(),
    )
    settings.setdefaults()
    yield settings
    # Just incase someone forgets to join in tests
    if wandb.run is not None:
        wandb.run.join()


@pytest.fixture
def mocked_run(runner, test_settings):
    """ A managed run object for tests with a mock backend """
    run = wandb.wandb_sdk.wandb_run.Run(settings=test_settings)
    run._set_backend(MagicMock())
    yield run


@pytest.fixture
def runner(monkeypatch, mocker):
    whaaaaat = wandb.util.vendor_import("whaaaaat")
    # monkeypatch.setattr('wandb.cli.api', InternalApi(
    #    default_settings={'project': 'test', 'git_tag': True}, load_settings=False))
    monkeypatch.setattr(click, "launch", lambda x: 1)
    monkeypatch.setattr(
        whaaaaat,
        "prompt",
        lambda x: {
            "project_name": "test_model",
            "files": ["weights.h5"],
            "attach": False,
            "team_name": "Manual Entry",
        },
    )
    monkeypatch.setattr(webbrowser, "open_new_tab", lambda x: True)
    mocker.patch("wandb.wandb_lib.apikey.isatty", lambda stream: True)
    mocker.patch("wandb.wandb_lib.apikey.input", lambda x: 1)
    mocker.patch("wandb.wandb_lib.apikey.getpass.getpass", lambda x: DUMMY_API_KEY)
    return CliRunner()


@pytest.fixture(autouse=True)
def reset_setup():
    wandb.wandb_sdk.wandb_setup._WandbSetup._instance = None


@pytest.fixture(autouse=True)
def local_netrc(monkeypatch):
    """Never use our real credentials, put them in their own isolated dir"""
    with CliRunner().isolated_filesystem():
        # TODO: this seems overkill...
        origexpand = os.path.expanduser
        # Touch that netrc
        open(".netrc", "wb").close()

        def expand(path):
            return os.path.realpath("netrc") if "netrc" in path else origexpand(path)

        monkeypatch.setattr(os.path, "expanduser", expand)
        yield


@pytest.fixture(autouse=True)
def local_settings(mocker):
    """Place global settings in an isolated dir"""
    with CliRunner().isolated_filesystem():
        cfg_path = os.path.join(os.getcwd(), ".config", "wandb", "settings")
        mkdir_exists_ok(os.path.join(".config", "wandb"))
        mocker.patch("wandb.old.settings.Settings._global_path", return_value=cfg_path)
        yield


@pytest.fixture
def mock_server(mocker):
    return utils.mock_server(mocker)


@pytest.fixture
def live_mock_server(request):
    global server
    name = urllib.parse.quote(request.node.name)
    # We set the username so the mock backend can namespace state
    os.environ["WANDB_USERNAME"] = name
    os.environ["WANDB_BASE_URL"] = server.base_url
    os.environ["WANDB_ERROR_REPORTING"] = "false"
    os.environ["WANDB_API_KEY"] = DUMMY_API_KEY
    # clear mock server ctx
    server.reset_ctx()
    yield server
    del os.environ["WANDB_USERNAME"]
    del os.environ["WANDB_BASE_URL"]
    del os.environ["WANDB_ERROR_REPORTING"]
    del os.environ["WANDB_API_KEY"]


@pytest.fixture
def notebook(live_mock_server, test_dir):
    """This launches a live server, configures a notebook to use it, and enables
    devs to execute arbitrary cells.  See tests/test_notebooks.py
    """

    @contextmanager
    def notebook_loader(nb_path, kernel_name="wandb_python", save_code=True, **kwargs):
        with open(utils.notebook_path("setup.ipynb")) as f:
            setupnb = nbformat.read(f, as_version=4)
            setupcell = setupnb["cells"][0]
            # Ensure the notebooks talks to our mock server
            new_source = setupcell["source"].replace(
                "__WANDB_BASE_URL__", live_mock_server.base_url,
            )
            if save_code:
                new_source = new_source.replace("__WANDB_NOTEBOOK_NAME__", nb_path)
            else:
                new_source = new_source.replace("__WANDB_NOTEBOOK_NAME__", "")
            setupcell["source"] = new_source

        nb_path = utils.notebook_path(nb_path)
        shutil.copy(nb_path, os.path.join(os.getcwd(), os.path.basename(nb_path)))
        with open(nb_path) as f:
            nb = nbformat.read(f, as_version=4)
        nb["cells"].insert(0, setupcell)

        try:
            client = utils.WandbNotebookClient(nb, kernel_name=kernel_name)
            with client.setup_kernel(**kwargs):
                # Run setup commands for mocks
                client.execute_cells(-1, store_history=False)
                yield client
        finally:
            with open(os.path.join(os.getcwd(), "notebook.log"), "w") as f:
                f.write(client.all_output_text())
            wandb.termlog("Find debug logs at: %s" % os.getcwd())
            wandb.termlog(client.all_output_text())

    notebook_loader.base_url = live_mock_server.base_url

    return notebook_loader


@pytest.fixture
def mocked_module(monkeypatch):
    """This allows us to mock modules loaded via wandb.util.get_module"""

    def mock_get_module(module):
        orig_get_module = wandb.util.get_module
        mocked_module = MagicMock()

        def get_module(mod):
            if mod == module:
                return mocked_module
            else:
                return orig_get_module(mod)

        monkeypatch.setattr(wandb.util, "get_module", get_module)
        return mocked_module

    return mock_get_module


@pytest.fixture
def mocked_ipython(monkeypatch):
    monkeypatch.setattr(
        wandb.wandb_sdk.wandb_settings, "_get_python_type", lambda: "jupyter"
    )
    ipython = MagicMock()
    # TODO: this is really unfortunate, for reasons not clear to me, monkeypatch doesn't work
    orig_get_ipython = wandb.jupyter.get_ipython
    wandb.jupyter.get_ipython = lambda: ipython
    yield ipython
    wandb.jupyter.get_ipython = orig_get_ipython


def default_wandb_args():
    """This allows us to parameterize the wandb_init_run fixture
    The most general arg is "env", you can call:

    @pytest.mark.wandb_args(env={"WANDB_API_KEY": "XXX"})

    To set env vars and have them unset when the test completes.
    """
    return {
        "error": None,
        "k8s": None,
        "sagemaker": False,
        "tensorboard": False,
        "resume": False,
        "env": {},
        "wandb_init": {},
    }


def mocks_from_args(mocker, args, mock_server):
    if args["k8s"] is not None:
        mock_server.ctx["k8s"] = args["k8s"]
        args["env"].update(utils.mock_k8s(mocker))
    if args["sagemaker"]:
        args["env"].update(utils.mock_sagemaker(mocker))


@pytest.fixture
def wandb_init_run(request, runner, mocker, mock_server):
    marker = request.node.get_closest_marker("wandb_args")
    args = default_wandb_args()
    if marker:
        args.update(marker.kwargs)
    try:
        mocks_from_args(mocker, args, mock_server)
        for k, v in args["env"].items():
            os.environ[k] = v
        #  TODO: likely not the right thing to do, we shouldn't be setting this
        wandb._IS_INTERNAL_PROCESS = False
        #  We want to run setup every time in tests
        wandb.wandb_sdk.wandb_setup._WandbSetup._instance = None
        mocker.patch("wandb.wandb_sdk.wandb_init.Backend", utils.BackendMock)
        run = wandb.init(
            settings=wandb.Settings(console="off", mode="offline", _except_exit=False),
            **args["wandb_init"]
        )
        yield run
        wandb.join()
    finally:
        unset_globals()
        for k, v in args["env"].items():
            del os.environ[k]


@pytest.fixture()
def restore_version():
    save_current_version = wandb.__version__
    yield
    wandb.__version__ = save_current_version
    try:
        del wandb.__hack_pypi_latest_version__
    except AttributeError:
        pass


@pytest.fixture()
def disable_console():
    os.environ["WANDB_CONSOLE"] = "off"
    yield
    del os.environ["WANDB_CONSOLE"]


@pytest.fixture()
def parse_ctx():
    """Fixture providing class to parse context data."""

    def parse_ctx_fn(ctx):
        return utils.ParseCTX(ctx)

    yield parse_ctx_fn


@pytest.fixture()
def record_q():
    return queue.Queue()


@pytest.fixture()
def fake_interface(record_q):
    return BackendSender(record_q=record_q)


@pytest.fixture
def fake_backend(fake_interface):
    class FakeBackend:
        def __init__(self):
            self.interface = fake_interface

    yield FakeBackend()


@pytest.fixture
def fake_run(fake_backend):
    def run_fn():
        s = wandb.Settings()
        run = wandb_sdk.wandb_run.Run(settings=s)
        run._set_backend(fake_backend)
        return run

    yield run_fn


@pytest.fixture
def records_util():
    def records_fn(q):
        ru = utils.RecordsUtil(q)
        return ru

    yield records_fn


# @pytest.hookimpl(tryfirst=True, hookwrapper=True)
# def pytest_runtest_makereport(item, call):
#     outcome = yield
#     rep = outcome.get_result()
#     if rep.when == "call" and rep.failed:
#         print("DEBUG PYTEST", rep, item, call, outcome)


@pytest.fixture
def log_debug(caplog):
    caplog.set_level(logging.DEBUG)
    yield
    # for rec in caplog.records:
    #     print("LOGGER", rec.message, file=sys.stderr)
