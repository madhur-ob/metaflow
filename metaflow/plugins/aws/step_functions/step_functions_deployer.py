import sys
import tempfile
from typing import Optional, ClassVar

from metaflow.plugins.aws.step_functions.step_functions import StepFunctions
from metaflow.runner.deployer import (
    DeployerImpl,
    DeployedFlow,
    TriggeredRun,
    get_lower_level_group,
    handle_timeout,
)


def terminate(instance: TriggeredRun, **kwargs):
    """
    Terminate a running workflow.

    Parameters
    ----------
    instance : TriggeredRun
        The triggered run instance to terminate.
    **kwargs : Any
        Additional arguments to pass to the terminate command.

    Returns
    -------
    bool
        True if the command was successful, False otherwise.
    """
    _, run_id = instance.pathspec.split("/")

    # every subclass needs to have `self.deployer_kwargs`
    command = get_lower_level_group(
        instance.deployer.api,
        instance.deployer.top_level_kwargs,
        instance.deployer.TYPE,
        instance.deployer.deployer_kwargs,
    ).terminate(run_id=run_id, **kwargs)

    pid = instance.deployer.spm.run_command(
        [sys.executable, *command],
        env=instance.deployer.env_vars,
        cwd=instance.deployer.cwd,
        show_output=instance.deployer.show_output,
    )

    command_obj = instance.deployer.spm.get(pid)
    return command_obj.process.returncode == 0


def production_token(instance: DeployedFlow):
    """
    Get the production token for a deployed flow.

    Parameters
    ----------
    instance : DeployedFlow
        The deployed flow instance to get the production token for.

    Returns
    -------
    str
        The production token.
    """
    _, production_token = StepFunctions.get_existing_deployment(instance.deployer.name)
    return production_token


def list_runs(instance: DeployedFlow, **kwargs):
    """
    List runs of a deployed flow.

    Parameters
    ----------
    instance : DeployedFlow
        The deployed flow instance to list runs for.
    **kwargs : Any
        Additional arguments to pass to the list_runs command.

    Returns
    -------
    bool
        True if the command was successful, False otherwise.
    """
    command = getattr(
        get_lower_level_group(
            instance.deployer.api,
            instance.deployer.top_level_kwargs,
            instance.deployer.TYPE,
            instance.deployer.deployer_kwargs,
        ),
        "list-runs",
    )(**kwargs)

    pid = instance.deployer.spm.run_command(
        [sys.executable, *command],
        env=instance.deployer.env_vars,
        cwd=instance.deployer.cwd,
        show_output=instance.deployer.show_output,
    )

    command_obj = instance.deployer.spm.get(pid)
    return command_obj.process.returncode == 0


def delete(instance: DeployedFlow, **kwargs):
    """
    Delete a deployed flow.

    Parameters
    ----------
    instance : DeployedFlow
        The deployed flow instance to delete.
    **kwargs : Any
        Additional arguments to pass to the delete command.

    Returns
    -------
    bool
        True if the command was successful, False otherwise.
    """
    command = get_lower_level_group(
        instance.deployer.api,
        instance.deployer.top_level_kwargs,
        instance.deployer.TYPE,
        instance.deployer.deployer_kwargs,
    ).delete(**kwargs)

    pid = instance.deployer.spm.run_command(
        [sys.executable, *command],
        env=instance.deployer.env_vars,
        cwd=instance.deployer.cwd,
        show_output=instance.deployer.show_output,
    )

    command_obj = instance.deployer.spm.get(pid)
    return command_obj.process.returncode == 0


def trigger(instance: DeployedFlow, **kwargs):
    """
    Trigger a new run for a deployed flow.

    Parameters
    ----------
    instance : DeployedFlow
        The deployed flow instance to trigger a new run for.
    **kwargs : Any
        Additional arguments to pass to the trigger command.

    Returns
    -------
    TriggeredRun
        The triggered run instance.

    Raises
    ------
    Exception
        If there is an error during the trigger process.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        tfp_runner_attribute = tempfile.NamedTemporaryFile(dir=temp_dir, delete=False)

        # every subclass needs to have `self.deployer_kwargs`
        command = get_lower_level_group(
            instance.deployer.api,
            instance.deployer.top_level_kwargs,
            instance.deployer.TYPE,
            instance.deployer.deployer_kwargs,
        ).trigger(runner_attribute_file=tfp_runner_attribute.name, **kwargs)

        pid = instance.deployer.spm.run_command(
            [sys.executable, *command],
            env=instance.deployer.env_vars,
            cwd=instance.deployer.cwd,
            show_output=instance.deployer.show_output,
        )

        command_obj = instance.deployer.spm.get(pid)
        content = handle_timeout(tfp_runner_attribute, command_obj)

        if command_obj.process.returncode == 0:
            triggered_run = TriggeredRun(deployer=instance.deployer, content=content)
            triggered_run._enrich_object({"terminate": terminate})
            return triggered_run

    raise Exception(
        "Error triggering %s on %s for %s"
        % (instance.deployer.name, instance.deployer.TYPE, instance.deployer.flow_file)
    )


class StepFunctionsDeployer(DeployerImpl):
    """
    Deployer implementation for AWS Step Functions.

    Attributes
    ----------
    TYPE : ClassVar[Optional[str]]
        The type of the deployer, which is "step-functions".
    """

    TYPE: ClassVar[Optional[str]] = "step-functions"

    def __init__(self, deployer_kwargs, **kwargs):
        """
        Initialize the StepFunctionsDeployer.

        Parameters
        ----------
        deployer_kwargs : dict
            The deployer-specific keyword arguments.
        **kwargs : Any
            Additional arguments to pass to the superclass constructor.
        """
        self.deployer_kwargs = deployer_kwargs
        super().__init__(**kwargs)

    def _enrich_deployed_flow(self, deployed_flow: DeployedFlow):
        """
        Enrich the DeployedFlow object with additional properties and methods.

        Parameters
        ----------
        deployed_flow : DeployedFlow
            The deployed flow object to enrich.
        """
        deployed_flow._enrich_object(
            {
                "production_token": property(production_token),
                "trigger": trigger,
                "delete": delete,
                "list_runs": list_runs,
            }
        )