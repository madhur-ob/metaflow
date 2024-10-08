import os
import sys
import json
import time
import importlib
import tempfile

from typing import Optional, Dict, ClassVar, TYPE_CHECKING

from metaflow.exception import MetaflowNotFound
from metaflow.metaflow_config import DEFAULT_FROM_DEPLOYMENT_IMPL
from metaflow.runner.subprocess_manager import SubprocessManager
from metaflow.runner.utils import handle_timeout

if TYPE_CHECKING:
    import metaflow


def get_lower_level_group(
    api, top_level_kwargs: Dict, _type: Optional[str], deployer_kwargs: Dict
):
    """
    Retrieve a lower-level group from the API based on the type and provided arguments.

    Parameters
    ----------
    api : MetaflowAPI
        Metaflow API instance.
    top_level_kwargs : Dict
        Top-level keyword arguments to pass to the API.
    _type : str
        Type of the deployer implementation to target.
    deployer_kwargs : Dict
        Keyword arguments specific to the deployer.

    Returns
    -------
    Any
        The lower-level group object retrieved from the API.

    Raises
    ------
    ValueError
        If the `_type` is None.
    """
    if _type is None:
        raise ValueError(
            "DeployerImpl doesn't have a 'TYPE' to target. Please use a sub-class of DeployerImpl."
        )
    return getattr(api(**top_level_kwargs), _type)(**deployer_kwargs)


class DeployerMeta(type):
    def __new__(mcs, name, bases, dct):
        cls = super().__new__(mcs, name, bases, dct)

        from metaflow.plugins import DEPLOYER_IMPL_PROVIDERS

        def _injected_method(deployer_class):
            def f(self, **deployer_kwargs):
                return deployer_class(
                    deployer_kwargs=deployer_kwargs,
                    flow_file=self.flow_file,
                    show_output=self.show_output,
                    profile=self.profile,
                    env=self.env,
                    cwd=self.cwd,
                    file_read_timeout=self.file_read_timeout,
                    **self.top_level_kwargs
                )

            return f

        for provider_class in DEPLOYER_IMPL_PROVIDERS:
            # TYPE is the name of the CLI groups i.e.
            # `argo-workflows` instead of `argo_workflows`
            # The injected method names replace '-' by '_' though.
            method_name = provider_class.TYPE.replace("-", "_")
            setattr(cls, method_name, _injected_method(provider_class))

        return cls


class Deployer(metaclass=DeployerMeta):
    """
    Use the `Deployer` class to configure and access one of the production
    orchestrators supported by Metaflow.

    Parameters
    ----------
    flow_file : str
        Path to the flow file to deploy.
    show_output : bool, default True
        Show the 'stdout' and 'stderr' to the console by default.
    profile : Optional[str], default None
        Metaflow profile to use for the deployment. If not specified, the default
        profile is used.
    env : Optional[Dict[str, str]], default None
        Additional environment variables to set for the deployment.
    cwd : Optional[str], default None
        The directory to run the subprocess in; if not specified, the current
        directory is used.
    file_read_timeout : int, default 3600
        The timeout until which we try to read the deployer attribute file.
    **kwargs : Any
        Additional arguments that you would pass to `python myflow.py` before
        the deployment command.
    """

    def __init__(
        self,
        flow_file: str,
        show_output: bool = True,
        profile: Optional[str] = None,
        env: Optional[Dict] = None,
        cwd: Optional[str] = None,
        file_read_timeout: int = 3600,
        **kwargs
    ):
        self.flow_file = flow_file
        self.show_output = show_output
        self.profile = profile
        self.env = env
        self.cwd = cwd
        self.file_read_timeout = file_read_timeout
        self.top_level_kwargs = kwargs


class TriggeredRun(object):
    """
    TriggeredRun class represents a run that has been triggered on a
    production orchestrator.
    """

    def __init__(
        self,
        deployer: "DeployerImpl",
        content: str,
    ):
        self.deployer = deployer
        content_json = json.loads(content)
        self.metadata_for_flow = content_json.get("metadata")
        self.pathspec = content_json.get("pathspec")
        self.name = content_json.get("name")

    def wait_for_run(self, timeout: Optional[int] = None):
        """
        Wait for the `run` property to become available.

        The `run` property becomes available only after the `start` task of the triggered
        flow starts running.

        Parameters
        ----------
        timeout : int, optional, default None
            Maximum time to wait for the `run` to become available, in seconds. If
            None, wait indefinitely.

        Raises
        ------
        TimeoutError
            If the `run` is not available within the specified timeout.
        """
        start_time = time.time()
        check_interval = 5
        while True:
            if self.run is not None:
                return self.run

            if timeout is not None and (time.time() - start_time) > timeout:
                raise TimeoutError(
                    "Timed out waiting for the run object to become available."
                )

            time.sleep(check_interval)

    @property
    def run(self) -> Optional["metaflow.Run"]:
        """
        Retrieve the `Run` object for the triggered run.

        Note that Metaflow `Run` becomes available only when the `start` task
        has started executing.

        Returns
        -------
        Run, optional
            Metaflow Run object if the `start` step has started executing, otherwise None.
        """
        from metaflow import Run

        try:
            return Run(self.pathspec, _namespace_check=False)
        except MetaflowNotFound:
            return None


class DeployedFlowMeta(type):
    def __new__(mcs, name, bases, dct):
        cls = super().__new__(mcs, name, bases, dct)

        from metaflow.plugins import DEPLOYER_IMPL_PROVIDERS

        allowed_providers = dict(
            {
                provider.TYPE.replace("-", "_"): provider
                for provider in DEPLOYER_IMPL_PROVIDERS
            }
        )

        def _default_injected_method():
            def f(
                identifier: str,
                metadata: Optional[str] = None,
                impl: str = DEFAULT_FROM_DEPLOYMENT_IMPL.replace("-", "_"),
            ):
                if impl in allowed_providers:
                    return allowed_providers[impl].DEPLOYED_FLOW_TYPE.from_deployment(
                        identifier, metadata, impl
                    )

            return f

        setattr(cls, "from_deployment", _default_injected_method())

        return cls


class DeployedFlow(metaclass=DeployedFlowMeta):
    """
    DeployedFlow class represents a flow that has been deployed.

    This class is not meant to be instantiated directly. Instead, it is returned from
    methods of `Deployer`.
    """

    def __init__(self, deployer: "DeployerImpl"):
        self.deployer = deployer
        self.name = self.deployer.name
        self.flow_name = self.deployer.flow_name
        self.metadata = self.deployer.metadata

    @staticmethod
    def from_deployment(
        identifier: str,
        metadata: str = None,
        impl: str = "argo-workflows",
    ):
        if impl == "argo-workflows":  # TODO: use a metaflow config variable for `impl`
            from metaflow.plugins.argo.argo_workflows_deployer import from_deployment

            return from_deployment(identifier, metadata)
        raise NotImplementedError("This method is not available for: %s" % impl)


class DeployerImpl(object):
    """
    Base class for deployer implementations. Each implementation should define a TYPE
    class variable that matches the name of the CLI group.

    Parameters
    ----------
    flow_file : str
        Path to the flow file to deploy.
    show_output : bool, default True
        Show the 'stdout' and 'stderr' to the console by default.
    profile : Optional[str], default None
        Metaflow profile to use for the deployment. If not specified, the default
        profile is used.
    env : Optional[Dict], default None
        Additional environment variables to set for the deployment.
    cwd : Optional[str], default None
        The directory to run the subprocess in; if not specified, the current
        directory is used.
    file_read_timeout : int, default 3600
        The timeout until which we try to read the deployer attribute file.
    **kwargs : Any
        Additional arguments that you would pass to `python myflow.py` before
        the deployment command.
    """

    TYPE: ClassVar[Optional[str]] = None
    DEPLOYED_FLOW_TYPE: ClassVar[Optional[DeployedFlow]] = None

    def __init__(
        self,
        flow_file: str,
        show_output: bool = True,
        profile: Optional[str] = None,
        env: Optional[Dict] = None,
        cwd: Optional[str] = None,
        file_read_timeout: int = 3600,
        **kwargs
    ):
        if self.TYPE is None:
            raise ValueError(
                "DeployerImpl doesn't have a 'TYPE' to target. Please use a sub-class "
                "of DeployerImpl."
            )

        if "metaflow.cli" in sys.modules:
            importlib.reload(sys.modules["metaflow.cli"])
        from metaflow.cli import start
        from metaflow.runner.click_api import MetaflowAPI

        self.flow_file = flow_file
        self.show_output = show_output
        self.profile = profile
        self.env = env
        self.cwd = cwd
        self.file_read_timeout = file_read_timeout

        self.env_vars = os.environ.copy()
        self.env_vars.update(self.env or {})
        if self.profile:
            self.env_vars["METAFLOW_PROFILE"] = profile

        self.spm = SubprocessManager()
        self.top_level_kwargs = kwargs
        self.api = MetaflowAPI.from_cli(self.flow_file, start)

    def __enter__(self) -> "DeployerImpl":
        return self

    def create(self, **kwargs) -> DeployedFlow:
        """
        Create a sub-class of a `DeployedFlow` depending on the deployer implementation.

        Parameters
        ----------
        **kwargs : Any
            Additional arguments to pass to `create` corresponding to the
            command line arguments of `create`

        Returns
        -------
        DeployedFlow
            DeployedFlow object representing the deployed flow.

        Raises
        ------
        Exception
            If there is an error during deployment.
        """
        # Sub-classes should implement this by simply calling _create and pass the
        # proper class as the DeployedFlow to return.
        raise NotImplementedError

    def _create(self, create_class: type(DeployedFlow), **kwargs) -> DeployedFlow:
        with tempfile.TemporaryDirectory() as temp_dir:
            tfp_runner_attribute = tempfile.NamedTemporaryFile(
                dir=temp_dir, delete=False
            )
            # every subclass needs to have `self.deployer_kwargs`
            command = get_lower_level_group(
                self.api, self.top_level_kwargs, self.TYPE, self.deployer_kwargs
            ).create(deployer_attribute_file=tfp_runner_attribute.name, **kwargs)

            pid = self.spm.run_command(
                [sys.executable, *command],
                env=self.env_vars,
                cwd=self.cwd,
                show_output=self.show_output,
            )

            command_obj = self.spm.get(pid)
            content = handle_timeout(
                tfp_runner_attribute, command_obj, self.file_read_timeout
            )
            content = json.loads(content)
            self.name = content.get("name")
            self.flow_name = content.get("flow_name")
            self.metadata = content.get("metadata")
            # Additional info is used to pass additional deployer specific information.
            # It is used in non-OSS deployers (extensions).
            self.additional_info = content.get("additional_info", {})

            if command_obj.process.returncode == 0:
                return create_class(deployer=self)

        raise Exception("Error deploying %s to %s" % (self.flow_file, self.TYPE))

    def __exit__(self, exc_type, exc_value, traceback):
        """
        Cleanup resources on exit.
        """
        self.cleanup()

    def cleanup(self):
        """
        Cleanup resources.
        """
        self.spm.cleanup()
