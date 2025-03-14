# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------
import json
import logging
import time
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Type, Union

from olive.cache import OliveCache
from olive.common.config_utils import validate_config
from olive.common.constants import DEFAULT_CACHE_DIR, DEFAULT_WORKFLOW_ID
from olive.common.utils import hash_dict
from olive.engine.cloud_cache_helper import check_model_cache, is_valid_cloud_cache_model, update_input_model_config
from olive.engine.config import FAILED_CONFIG, INVALID_CONFIG, PRUNED_CONFIGS
from olive.engine.footprint import Footprint, FootprintNodeMetric
from olive.engine.packaging.packaging_generator import generate_output_artifacts
from olive.evaluator.metric import Metric
from olive.evaluator.metric_result import MetricResult, joint_metric_key
from olive.evaluator.olive_evaluator import OliveEvaluatorConfig
from olive.exception import EXCEPTIONS_TO_RAISE, OlivePassError
from olive.hardware import AcceleratorSpec
from olive.logging import enable_filelog
from olive.model import ModelConfig
from olive.strategy.search_strategy import SearchStrategy, SearchStrategyConfig
from olive.systems.common import SystemType
from olive.systems.system_config import SystemConfig
from olive.systems.utils import create_managed_system_with_cache

if TYPE_CHECKING:
    from olive.engine.cloud_cache_helper import CloudCacheConfig
    from olive.engine.packaging.packaging_config import PackagingConfig
    from olive.passes.olive_pass import Pass
    from olive.systems.olive_system import OliveSystem

logger = logging.getLogger(__name__)


class Engine:
    """The engine executes the registered Olive Steps.

    It facilitate evaluation of the output models using provided evaluation criteria and produces output model(s).
    """

    def __init__(
        self,
        workflow_id: str = DEFAULT_WORKFLOW_ID,
        search_strategy: Optional[Union[Dict[str, Any], SearchStrategyConfig]] = None,
        host: Optional[Union[Dict[str, Any], "SystemConfig"]] = None,
        target: Optional[Union[Dict[str, Any], "SystemConfig"]] = None,
        evaluator: Optional[Union[Dict[str, Any], "OliveEvaluatorConfig"]] = None,
        cache_dir: str = DEFAULT_CACHE_DIR,
        clean_cache: bool = False,
        clean_evaluation_cache: bool = False,
        plot_pareto_frontier: bool = False,
        *,
        azureml_client_config=None,
    ):
        self.workflow_id = workflow_id
        self.no_search = False
        if not search_strategy:
            # if search strategy is None or False, disable search
            self.no_search = True
            self.search_strategy = None
        else:
            # if search strategy is provided in config, use it
            self.search_strategy = SearchStrategy(search_strategy)

        # default host
        host = host or {"type": SystemType.Local}
        self.host_config = validate_config(host, SystemConfig)
        self.host = None

        # engine target
        target = target or {"type": SystemType.Local}
        self.target_config = validate_config(target, SystemConfig)
        self.target = None

        # default evaluator
        self.evaluator_config = validate_config(evaluator, OliveEvaluatorConfig) if evaluator else None

        self.cache = OliveCache(
            str(Path(cache_dir) / workflow_id), clean_cache=clean_cache, clean_evaluation_cache=clean_evaluation_cache
        )

        self.plot_pareto_frontier = plot_pareto_frontier
        self.azureml_client_config = azureml_client_config

        # dictionary of passes
        self.pass_config = OrderedDict()

        # {"pass_name": {"pass": pass, "host": host, "evaluator": evaluator, "clean_run_cache": clean_run_cache}}
        self.passes = OrderedDict()
        self.pass_flows = None
        self.pass_flows_search_spaces = None

        self.footprints = defaultdict(Footprint)

        self.cloud_cache_helper = None

        self._initialized = False

    def initialize(self, log_to_file: bool = False, log_severity_level: int = 1):
        """Initialize engine state. This should be done before running the registered passes."""
        if log_to_file:
            enable_filelog(log_severity_level, self.cache.cache_dir, self.workflow_id)

        # set cache dir environment variables
        # might be used by other parts of olive to cache data
        self.cache.set_cache_env()

        # clean pass run cache if requested
        # removes all run cache for pass type and all children elements
        for pass_config in self.pass_config.values():
            clean_run_cache = pass_config["clean_run_cache"]
            if clean_run_cache:
                self.cache.clean_pass_run_cache(pass_config["type"].__name__)

        # prepare non-local resources if host/target is not AzureML
        # TODO(anyone): Should the cloud cache care about this? If so, the cloud cache helper can
        # check for cached non-local resource paths and replace them with the original config
        # during hash calculation.
        if self.target_config.type != SystemType.AzureML:
            if self.evaluator_config:
                self.evaluator_config = self.cache.prepare_resources_for_local(self.evaluator_config)
            for pass_config in self.pass_config.values():
                if pass_config["evaluator"]:
                    pass_config["evaluator"] = self.cache.prepare_resources_for_local(pass_config["evaluator"])

        for pass_config in self.pass_config.values():
            host_type = pass_config["host"].system_type if pass_config["host"] else self.host_config.type
            if host_type == SystemType.AzureML:
                continue
            pass_config["config"] = self.cache.prepare_resources_for_local(pass_config["config"])

        self.set_pass_flows(self.pass_flows)
        self._initialized = True

    def register(
        self,
        pass_type: Type["Pass"],
        config: Dict[str, Any] = None,
        disable_search: bool = False,
        name: str = None,
        host: "OliveSystem" = None,
        evaluator_config: "OliveEvaluatorConfig" = None,
        clean_run_cache: bool = False,
        output_name: str = None,
    ):
        """Register a pass configuration so that it could be instantiated and executed later."""
        if name is not None:
            assert name not in self.passes, f"Pass with name {name} already registered"
        else:
            idx = 0
            while True:
                name = pass_type.__name__
                if idx > 0:
                    name = f"{name}_{idx}"
                idx += 1
                if name not in self.pass_config:
                    break

        self.pass_config[name] = {
            "type": pass_type,
            "config": config or {},
            "disable_search": disable_search,
            "host": host,
            "evaluator": evaluator_config,
            "clean_run_cache": clean_run_cache,
            "output_name": output_name,
        }

    def register_pass(
        self,
        p: "Pass",
        name: str = None,
        host: "OliveSystem" = None,
        evaluator_config: "OliveEvaluatorConfig" = None,
        output_name: str = None,
    ):
        """Register a pass instance."""
        if name is not None:
            assert name not in self.passes, f"Pass with name {name} already registered"
        else:
            idx = 0
            while True:
                name = p.__class__.__name__
                if idx > 0:
                    name = f"{name}_{idx}"
                idx += 1
                if name not in self.passes:
                    break

        if self.no_search and len(p.search_space) > 0:
            raise ValueError(f"Search strategy is None but pass {name} has search space")
        if output_name and not self.no_search:
            # In no-search mode, if output_name is provided, the output model of the pass will be saved to
            # engine's output_dir with the prefix of output_name.
            logger.debug("output_name %s for pass %s will be ignored if search strategy is None", output_name, name)

        self.passes[name] = {
            "pass": p,
            "host": host,
            "evaluator": evaluator_config,
            "output_name": output_name,
        }

    def set_pass_flows(self, pass_flows: List[List[str]] = None):
        """Construct pass flows from a list of pass names.

        Args:
            pass_flows: a list of pass names, each pass name is a string.

        """
        if not pass_flows:
            self.pass_flows = [list(self.pass_config.keys())] if self.pass_config else []
        else:
            self.pass_flows = pass_flows

    def run(
        self,
        input_model_config: ModelConfig,
        accelerator_specs: List["AcceleratorSpec"],
        packaging_config: Optional[Union["PackagingConfig", List["PackagingConfig"]]] = None,
        output_dir: str = None,
        output_name: str = None,
        evaluate_input_model: bool = True,
        log_to_file: bool = False,
        log_severity_level: int = 1,
        cloud_cache_config: "CloudCacheConfig" = None,
    ):
        """Run all the registered Olive passes on the input model and produce one or more candidate models.

        Args:
            input_model_config: input Olive model configuration
            accelerator_specs: list of accelerator specs
            packaging_config: packaging configuration, if packaging_config is provided, the output
                model will be packaged into a zip file.
            output_dir: output directory for the output model
            output_name: output name for the output model, if output_name is provided, the output
                model will be saved to engine's output_dir with the prefix of output_name.
            evaluate_input_model: if evaluate_input_model is True, run the evaluation on the input model.
            log_to_file: if save logs to a file.
            log_severity_level: severity level of the logger.
            cloud_cache_config: Cloud model cache configuration.

        Return:
            if search strategy is None, all passes are run in the order they were registered.
                1. Final model -> {output_dir}/{output_name}_{AcceleratorSpec}_model.onnx
                2. JSON file -> {output_dir}/{output_name}_{AcceleratorSpec}_model.json
                3. Evaluation results of the final model -> {output_dir}/{output_name}_{AcceleratorSpec}_metrics.json

            Return footprint/zip(packaging_config) of the final model and evaluation results of the final model.

            if search strategy is not None, run the search strategy to find candidate models.
            Return footprint/zip(packaging_config) of candidate models and evaluation results.

        """
        if not accelerator_specs:
            raise ValueError("No accelerator specified")

        if not self._initialized:
            self.initialize(log_to_file, log_severity_level)

        output_dir: Path = Path(output_dir) if output_dir else Path.cwd()
        output_dir.mkdir(parents=True, exist_ok=True)

        outputs = {}

        for accelerator_spec in accelerator_specs:
            logger.info("Running Olive on accelerator: %s", accelerator_spec)
            with self._create_system(accelerator_spec):
                run_result = self.run_accelerator(
                    input_model_config,
                    output_dir,
                    output_name,
                    evaluate_input_model,
                    accelerator_spec,
                    cloud_cache_config,
                )

                if run_result is None:
                    continue

                outputs[accelerator_spec] = run_result

        for accelerator_spec in self.footprints:
            logger.info("Run history for %s:", accelerator_spec)
            run_history = self.footprints[accelerator_spec].summarize_run_history()
            self.dump_run_history(run_history, output_dir / f"run_history_{accelerator_spec}.txt")

        if packaging_config and self.passes:
            # TODO(trajep): should we support packaging pytorch model?
            logger.info("Package top ranked %d models as artifacts", sum(len(f.nodes) for f in outputs.values()))
            generate_output_artifacts(
                packaging_config,
                self.footprints,
                outputs,
                output_dir,
                self.azureml_client_config,
            )
        else:
            logger.info("No packaging config provided, skip packaging artifacts")

        return outputs

    def run_accelerator(
        self,
        input_model_config: ModelConfig,
        output_dir: Path,
        output_name: str,
        evaluate_input_model: bool,
        accelerator_spec: "AcceleratorSpec",
        cloud_cache_config: "CloudCacheConfig",
    ):
        # generate search space and initialize the passes for each hardware accelerator
        self.setup_passes(accelerator_spec)
        # hash the input model
        input_model_id = self._init_input_model(input_model_config)
        self.footprints[accelerator_spec].record(model_id=input_model_id)
        prefix_output_name = Engine._get_prefix_output_name(output_name, accelerator_spec)
        if cloud_cache_config.enable_cloud_cache:
            cloud_cache_config.input_model_config = deepcopy(input_model_config)

        try:
            if evaluate_input_model and not self.evaluator_config:
                logger.debug(
                    "evaluate_input_model is True but no evaluator provided in no-search mode. Skipping input model"
                    " evaluation."
                )
            elif evaluate_input_model:
                results = self._evaluate_model(
                    input_model_config, input_model_id, self.evaluator_config, accelerator_spec
                )
                logger.info("Input model evaluation results: %s", results)
                result_name = f"{prefix_output_name}_input_model_metrics"
                results_path = output_dir / f"{result_name}.json"
                with results_path.open("w") as f:
                    json.dump(results.to_json(), f, indent=4)
                logger.info("Saved evaluation results of input model to %s", results_path)
                if not self.passes:
                    logger.debug("No passes registered, return input model evaluation results.")
                    return results

            if self.no_search:
                logger.debug("Running Olive in no-search mode ...")
                output_footprint = self.run_no_search(
                    input_model_config,
                    input_model_id,
                    accelerator_spec,
                    output_dir,
                    output_name,
                    cloud_cache_config,
                )
            else:
                logger.debug("Running Olive in search mode ...")
                output_footprint = self.run_search(
                    input_model_config,
                    input_model_id,
                    accelerator_spec,
                    output_dir,
                    output_name,
                    cloud_cache_config,
                )
        except EXCEPTIONS_TO_RAISE:
            raise
        except Exception:
            logger.warning("Failed to run Olive on %s.", accelerator_spec, exc_info=True)
            return None

        output_fp_path = output_dir / f"{prefix_output_name}_footprints.json"
        logger.info("Save footprint to %s.", output_fp_path)
        self.footprints[accelerator_spec].to_file(output_fp_path)
        logger.debug("run_accelerator done")
        return output_footprint

    def get_host_device(self):
        if self.host_config.config.accelerators:
            # for host device, we will always use the first accelerator device
            return self.host_config.config.accelerators[0].device
        else:
            return None

    def setup_passes(self, accelerator_spec: "AcceleratorSpec"):
        host_device = self.get_host_device()
        # clean the passes
        self.passes.clear()
        for name, config in self.pass_config.items():
            pass_cls: Type[Pass] = config["type"]
            pass_cfg = config["config"]
            pass_cfg = pass_cls.generate_search_space(accelerator_spec, pass_cfg, config["disable_search"])
            p = pass_cls(accelerator_spec, pass_cfg, config["disable_search"], host_device)
            self.register_pass(
                p,
                name=name,
                host=config["host"],
                evaluator_config=config["evaluator"],
                output_name=config["output_name"],
            )

        # list of passes starting from the first pass with non-empty search space
        # These passes will be added to the search space
        self.pass_flows_search_spaces = []
        for pass_flow in self.pass_flows:
            pass_search_spaces = []
            for pass_name in pass_flow:
                p: Pass = self.passes[pass_name]["pass"]
                pass_search_spaces.append((pass_name, p.search_space))
            self.pass_flows_search_spaces.append(pass_search_spaces)

    def reset_passes(self):
        """Cleanup the passes."""
        self.passes.clear()
        self.pass_config.clear()
        self.pass_flows = []

    def run_no_search(
        self,
        input_model_config: ModelConfig,
        input_model_id: str,
        accelerator_spec: "AcceleratorSpec",
        output_dir: str = None,
        output_name: str = None,
        cloud_cache_config: "CloudCacheConfig" = None,
    ):
        """Run all the registered Olive pass flows in no-search mode."""
        for pass_item in self.passes.values():
            if len(pass_item["pass"].search_space) > 0:
                pass_name = pass_item["name"]
                raise ValueError(f"Pass {pass_name} has search space but search strategy is None")

        output_models = {}
        for pass_flow in self.pass_flows:
            # search point is empty since there is no search
            passes_to_run = [(pass_id, {}) for pass_id in pass_flow]

            # run all the passes in the pass flow
            logger.debug("Running %s with no search ...", pass_flow)
            should_prune, signal, model_ids = self._run_passes(
                passes_to_run,
                input_model_config,
                input_model_id,
                accelerator_spec,
                cloud_cache_config,
            )

            if should_prune:
                failed_pass = pass_flow[len(model_ids)]
                logger.warning(
                    "Flow %s is pruned due to failed or invalid config for pass '%s'", pass_flow, failed_pass
                )
                continue

            # names of the output models of the passes
            pass_output_names = [self.passes[pass_id]["output_name"] for pass_id in pass_flow]
            pass_output_names = [f"{name}_{accelerator_spec}" if name else None for name in pass_output_names]

            # output dir with pass flow
            output_dir_with_pf = Path(output_dir) / "-".join(pass_flow)

            if not pass_output_names[-1] or output_name:
                # if the last pass does not have output name, use the prefix output name
                pass_output_names[-1] = Engine._get_prefix_output_name(output_name, accelerator_spec)
            final_output_name = pass_output_names[-1]

            output_model_json = None
            for pass_output_name, pass_output_model_id in zip(pass_output_names, model_ids):
                if not pass_output_name:
                    continue
                output_model_json = self.cache.save_model(
                    model_number=pass_output_model_id,
                    output_dir=output_dir_with_pf,
                    output_name=f"{pass_output_name}_model",
                    overwrite=True,
                )
                # it is not supported to save compositemodel again
                # so the output_model_json could be None
                output_models[pass_output_model_id] = output_model_json

            # save the evaluation results to output_dir
            if signal is not None:
                results_path = output_dir_with_pf / f"{final_output_name}_metrics.json"
                with results_path.open("w") as f:
                    json.dump(signal.to_json(), f, indent=4)

        output_model_ids = list(output_models.keys())
        fp_outputs = self.footprints[accelerator_spec].create_footprints_by_model_ids(output_model_ids)
        # update the output model config
        for model_id, model_config in output_models.items():
            if model_config:
                fp_outputs.nodes[model_id].model_config = model_config

        return fp_outputs

    def run_search(
        self,
        input_model_config: ModelConfig,
        input_model_id: str,
        accelerator_spec: "AcceleratorSpec",
        output_dir: str = None,
        output_name: str = None,
        cloud_cache_config: "CloudCacheConfig" = None,
    ):
        """Run all the registered Olive passes in search model where search strategy is not None."""
        prefix_output_name = Engine._get_prefix_output_name(output_name, accelerator_spec)

        # get objective_dict
        evaluator_config = self.evaluator_for_pass(list(self.passes.keys())[-1])

        if evaluator_config is None:
            raise ValueError("No evaluator provided for the last pass")
        else:
            objective_dict = self.resolve_objectives(
                input_model_config, input_model_id, evaluator_config.metrics, accelerator_spec
            )
            self.footprints[accelerator_spec].record_objective_dict(objective_dict)

        # initialize the search strategy
        self.search_strategy.initialize(self.pass_flows_search_spaces, input_model_id, objective_dict)
        output_model_num = self.search_strategy.get_output_model_num()

        # record start time
        start_time = time.time()
        iter_num = 0
        while True:
            iter_num += 1

            # get the next step
            next_step = self.search_strategy.next_step()

            # if no more steps, break
            if next_step is None:
                break

            # get the model id of the first input model
            model_id = next_step["model_id"]
            if model_id == input_model_id:
                model_config = input_model_config
            else:
                model_config = self._load_model(model_id)

            logger.debug("Step %d with search point %s ...", iter_num, next_step["search_point"])

            # run all the passes in the step
            should_prune, signal, model_ids = self._run_passes(
                next_step["passes"],
                model_config,
                model_id,
                accelerator_spec,
                cloud_cache_config,
            )

            # record feedback signal
            self.search_strategy.record_feedback_signal(next_step["search_point"], signal, model_ids, should_prune)

            time_diff = time.time() - start_time
            self.search_strategy.check_exit_criteria(iter_num, time_diff, signal)

        return self.create_pareto_frontier_footprints(
            accelerator_spec, output_model_num, output_dir, prefix_output_name
        )

    def create_pareto_frontier_footprints(self, accelerator_spec, output_model_num, output_dir, prefix_output_name):
        pf_footprints = self.footprints[accelerator_spec].create_pareto_frontier(output_model_num)
        if not pf_footprints:
            return None
        pf_footprints.to_file(output_dir / f"{prefix_output_name}_pareto_frontier_footprints.json")

        if self.plot_pareto_frontier:
            pf_footprints.plot_pareto_frontier_to_html(
                save_path=output_dir / f"{prefix_output_name}_pareto_frontier_footprints_chart.html"
            )

        return pf_footprints

    def dump_run_history(self, run_history, output_path: str = None):
        if not run_history:
            logger.info("No run history to dump!")
            return
        headers = run_history[0]._fields
        try:
            from tabulate import tabulate

            formatted_rls = tabulate([tuple(rh) for rh in run_history], headers=headers, tablefmt="grid")
            logger.info("run history:\n%s", formatted_rls)
        except ImportError:
            logger.info("Please install tabulate for better run history output")
            formatted_rls = run_history
        with Path(output_path).open("w") as f:
            f.write(f"{formatted_rls}")

    def resolve_objectives(
        self,
        input_model_config: ModelConfig,
        input_model_id: str,
        metrics: List[Metric],
        accelerator_spec: "AcceleratorSpec",
    ) -> Dict[str, Dict[str, Any]]:
        """Return a dictionary of objectives and their higher_is_better and goal values.

        {objective_name: {"higher_is_better": bool, "goal": float}}
        """
        goals = self.resolve_goals(input_model_config, input_model_id, metrics, accelerator_spec)
        objective_dict = {}
        for metric in metrics:
            for sub_type in metric.sub_types:
                if sub_type.priority <= 0:
                    continue
                metric_key = joint_metric_key(metric.name, sub_type.name)
                objective_dict[metric_key] = {
                    "higher_is_better": sub_type.higher_is_better,
                    "goal": goals.get(metric_key),
                    "priority": sub_type.priority,
                }
        return dict(sorted(objective_dict.items(), key=lambda x: x[1]["priority"]))

    def resolve_goals(
        self,
        input_model_config: ModelConfig,
        input_model_id: str,
        metrics: List[Metric],
        accelerator_spec: "AcceleratorSpec",
    ) -> Dict[str, float]:
        """Resolve the goals of the given metrics into thresholds for the given model."""
        goals = {}
        multipliers = {}
        for metric in metrics:
            # only resolve sub metrics whose priority > 0
            goals[metric.name] = metric.get_sub_type_info("goal")
            multipliers[metric.name] = metric.get_sub_type_info(
                info_name="higher_is_better",
                callback=lambda x: 1 if x else -1,
            )

        if goals:
            logger.debug("Resolving goals: %s", goals)

        baseline = None
        for goal in goals.values():
            _evaluated = False
            for sub_goal in goal.values():
                if not sub_goal:
                    break
                if sub_goal.type != "threshold":
                    assert self.evaluator_config is not None, "Default evaluator must be provided to resolve goals"
                    logger.debug("Computing baseline for metrics ...")
                    baseline = self._evaluate_model(
                        input_model_config, input_model_id, self.evaluator_config, accelerator_spec
                    )
                    _evaluated = True
                    break
            if _evaluated:
                break
        if not baseline:
            logger.debug("No baseline got as no goal is provided the the goal is threshold")
            return {}

        if baseline:
            logger.debug("Baseline: %s", baseline)

        # resolve goals to thresholds
        resolved_goals = {}
        for metric_name, sub_type_goals in goals.items():
            for sub_type_name, goal in sub_type_goals.items():
                # TODO(trajep): make the logic cleaner
                resolved_goal_value = None
                if goal is not None:
                    baseline_sub_type = baseline.get_value(metric_name, sub_type_name)
                    multiplier = multipliers[metric_name][sub_type_name]
                    if goal.type == "threshold":
                        resolved_goal_value = goal.value
                    elif goal.type == "max-degradation":
                        resolved_goal_value = baseline_sub_type - multiplier * goal.value
                    elif goal.type == "min-improvement":
                        resolved_goal_value = baseline_sub_type + multiplier * goal.value
                    elif goal.type == "percent-max-degradation":
                        resolved_goal_value = baseline_sub_type * (1 - multiplier * goal.value / 100)
                    elif goal.type == "percent-min-improvement":
                        resolved_goal_value = baseline_sub_type * (1 + multiplier * goal.value / 100)

                resolved_goals[joint_metric_key(metric_name, sub_type_name)] = resolved_goal_value
        if len(resolved_goals) > 0:
            logger.debug("Resolved goals: %s", resolved_goals)

        return resolved_goals

    def host_for_pass(self, pass_id: str):
        host = self.passes[pass_id]["host"]
        if host is None:
            return self.host
        return host

    def evaluator_for_pass(self, pass_id: str):
        """Return evaluator for the given pass."""
        e = self.passes[pass_id]["evaluator"]
        if e is None:
            return self.evaluator_config
        return e

    def _cache_model(self, model: Union[ModelConfig, str], model_id: str, check_object: bool = True):
        """Cache the model in the cache directory."""
        # TODO(trajep): move model/pass run/evaluation cache into footprints
        if model == FAILED_CONFIG:
            model_json = {}
        else:
            model_json = model.to_json(check_object=check_object)
        model_json_path = self.cache.get_model_json_path(model_id)
        try:
            with model_json_path.open("w") as f:
                json.dump(model_json, f, indent=4)
            logger.debug("Cached model %s to %s", model_id, model_json_path)
        except Exception:
            logger.exception("Failed to cache model")

    def _load_model(self, model_id: str) -> Union[ModelConfig, str]:
        """Load the model from the cache directory."""
        model_json_path = self.cache.get_model_json_path(model_id)
        try:
            with model_json_path.open() as f:
                model_json = json.load(f)
        except Exception:
            logger.exception("Failed to load model.")
            return None

        if model_json == {}:
            return FAILED_CONFIG

        return ModelConfig.from_json(model_json)

    def _init_input_model(self, input_model_config: ModelConfig):
        """Initialize the input model."""
        model_hash = hash_dict(input_model_config.to_json())[:8]

        # cache the model
        self._cache_model(input_model_config, model_hash, check_object=False)

        return model_hash

    def _cache_run(
        self,
        pass_name: int,
        pass_config: dict,
        input_model_id: str,
        output_model_id: str,
        accelerator_spec: "AcceleratorSpec",
        run_start_time: float = 0,
        run_end_time: float = 0,
    ):
        """Cache the run in the cache directory."""
        run_json = {
            "pass_name": pass_name,
            "pass_config": pass_config,
            "input_model_id": input_model_id,
            "output_model_id": output_model_id,
            "run_start_time": run_start_time,
            "run_end_time": run_end_time,
        }
        input_model_number = input_model_id.split("_")[0]
        run_json_path = self.cache.get_run_json_path(pass_name, input_model_number, pass_config, accelerator_spec)
        try:
            with run_json_path.open("w") as f:
                json.dump(run_json, f, indent=4)
            logger.debug("Cached run for %s->%s into %s", input_model_id, output_model_id, run_json_path)
        except Exception:
            logger.exception("Failed to cache run")

    def _load_run(self, input_model_id: str, pass_name: int, pass_config: dict, accelerator_spec: "AcceleratorSpec"):
        """Load the run from the cache directory."""
        input_model_number = input_model_id.split("_")[0]
        run_json_path = self.cache.get_run_json_path(pass_name, input_model_number, pass_config, accelerator_spec)
        run_json = {}
        if run_json_path.exists():
            try:
                with run_json_path.open() as f:
                    run_json = json.load(f)
            except Exception:
                logger.exception("Failed to load run")
                run_json = {}
        return run_json

    def _run_passes(
        self,
        passes: List[Tuple[str, Dict[str, Any]]],
        model_config: ModelConfig,
        model_id: str,
        accelerator_spec: "AcceleratorSpec",
        cloud_cache_config: "CloudCacheConfig",
    ):
        """Run all the passes in the order they were registered.

        the passes is the list of (pass_name, pass_search_point) tuples
        """
        should_prune = False
        # run all the passes in the step
        model_ids = []
        pass_id = None
        output_model_hash = None

        if cloud_cache_config.enable_cloud_cache:
            if not is_valid_cloud_cache_model(model_config):
                logger.warning(
                    "Only HfModel with huggingface id as model_path "
                    "or HfModel from Azure ML registry model and Azure ML curated model "
                    "is supported by cloud cache. "
                    "Setting enable_cloud_cache=False."
                )
                cloud_cache_config.enable_cloud_cache = False
            else:
                self.cloud_cache_helper = cloud_cache_config.create_cloud_cache_helper()

        for pass_id, pass_search_point in passes:
            model_config, model_id, output_model_hash = self._run_pass(
                pass_id,
                pass_search_point,
                model_config,
                model_id,
                accelerator_spec,
                cloud_cache_config.enable_cloud_cache,
                cloud_cache_config.upload_to_cloud,
                output_model_hash,
            )
            if model_config in PRUNED_CONFIGS:
                should_prune = True
                logger.debug("Pruned for pass %s", pass_id)
                break
            model_ids.append(model_id)

        if (
            cloud_cache_config.enable_cloud_cache
            and model_config not in PRUNED_CONFIGS
            and model_config.config.get("model_path") is None
        ):
            # download model files
            logger.info("Cloud model cache is enabled. Download final model files ...")
            cloud_model_path, cloud_adapter_path = self.cloud_cache_helper.get_path_from_cloud(output_model_hash)
            self.cloud_cache_helper.update_model_config(
                cloud_model_path,
                cloud_adapter_path,
                model_config,
                output_model_hash,
                self.cache.get_model_output_path(model_id),
            )

        if not should_prune:
            # evaluate the model
            evaluator_config = self.evaluator_for_pass(pass_id)
            if self.no_search and evaluator_config is None:
                # skip evaluation if no search and no evaluator
                signal = None
            else:
                logger.info("Run model evaluation for the final model...")
                signal = self._evaluate_model(model_config, model_id, evaluator_config, accelerator_spec)
            logger.debug("Signal: %s", signal)
        else:
            signal = None
            logger.warning("Skipping evaluation as model was pruned")

        return should_prune, signal, model_ids

    def _run_pass(
        self,
        pass_id: str,
        pass_search_point: Dict[str, Any],
        input_model_config: ModelConfig,
        input_model_id: str,
        accelerator_spec: "AcceleratorSpec",
        enable_cloud_cache: bool,
        upload_to_cloud: bool,
        input_model_hash: str = None,
    ):
        """Run a pass on the input model."""
        # pass
        p: Pass = self.passes[pass_id]["pass"]
        pass_name = p.__class__.__name__
        logger.info("Running pass %s:%s", pass_id, pass_name)
        pass_config = p.config_at_search_point(pass_search_point)
        pass_config = p.serialize_config(pass_config)
        output_model_config = None
        output_model_hash = None
        pass_run_locally = True

        # check whether the config is valid
        if not p.validate_search_point(pass_search_point, accelerator_spec, with_fixed_value=True):
            logger.warning("Invalid search point, prune")
            output_model_config = INVALID_CONFIG
            # no need to record in footprint since there was no run and thus no valid/failed model
            # invalid configs are also not cached since the same config can be valid for other accelerator specs
            # a pass can be accelerator agnostic but still have accelerator specific invalid configs
            # this helps reusing cached models for different accelerator specs
            return output_model_config, None, None

        # load run from cache if it exists
        run_accel = None if p.is_accelerator_agnostic(accelerator_spec) else accelerator_spec
        run_cache = self._load_run(input_model_id, pass_name, pass_config, run_accel)
        output_model_id = run_cache.get("output_model_id", None)
        if output_model_id is not None:
            logger.debug("Loading model from cache ...")
            output_model_config = self._load_model(output_model_id)
            if output_model_config is not None:
                # footprint model and run
                self.footprints[accelerator_spec].record(
                    model_id=output_model_id,
                    model_config=(
                        output_model_config.to_json() if output_model_config != FAILED_CONFIG else {"is_pruned": True}
                    ),
                    parent_model_id=input_model_id,
                    from_pass=pass_name,
                    pass_run_config=pass_config,
                    start_time=run_cache.get("run_start_time", 0),
                    end_time=run_cache.get("run_end_time", 0),
                )
                logger.info("Loaded model from cache: %s", output_model_id)
                return output_model_config, output_model_id, None

        # new model id
        input_model_number = input_model_id.split("_")[0]
        # Note: the final output model id need contains the accelerator information
        # if the output model is accelerator dependent.
        output_model_id_parts = [
            f"{self.cache.get_new_model_number()}_{pass_name}",
            input_model_number,
            hash_dict(pass_config)[:8],
        ]

        if not p.is_accelerator_agnostic(accelerator_spec):
            output_model_id_parts.append(f"{accelerator_spec}")

        output_model_id = "-".join(map(str, output_model_id_parts))
        output_model_path = str(self.cache.get_model_output_path(output_model_id))

        run_start_time = datetime.now().timestamp()

        if enable_cloud_cache:
            output_model_hash = self.cloud_cache_helper.get_hash_key(
                input_model_config, pass_search_point, input_model_hash
            )
            output_model_config = check_model_cache(self.cloud_cache_helper, output_model_hash, Path(output_model_path))
            pass_run_locally = output_model_config is None

        # run pass
        if pass_run_locally:
            if enable_cloud_cache and input_model_config.config.get("model_path") is None:
                update_input_model_config(
                    self.cloud_cache_helper, input_model_config, input_model_hash, Path(output_model_path)
                )

            host = self.host_for_pass(pass_id)
            if host.system_type != SystemType.AzureML:
                input_model_config = self.cache.prepare_resources_for_local(input_model_config)

            try:
                if p.run_on_target:
                    if self.target.system_type == SystemType.IsolatedORT:
                        logger.warning(
                            "Cannot run pass %s on IsolatedORT target, will use the host to run the pass.", pass_id
                        )
                    else:
                        host = self.target

                output_model_config = host.run_pass(p, input_model_config, output_model_path, pass_search_point)
            except OlivePassError:
                logger.exception("Pass run_pass failed")
                output_model_config = FAILED_CONFIG
            except EXCEPTIONS_TO_RAISE:
                # Don't catch these errors since most of time, it is caused by the user errors and need not retry.
                raise
            except Exception:
                output_model_config = FAILED_CONFIG
                # TODO(jambayk): from the time being, we need to catch all exceptions to make the
                #      search process robust. We need rethrow the exception only when
                #      it is not pass specific. For example, for olive bugs and user errors
                logger.exception("Pass run failed.")
                if self.no_search:
                    raise  # rethrow the exception if no search is performed

        run_end_time = datetime.now().timestamp()
        logger.info("Pass %s:%s finished in %f seconds", pass_id, pass_name, run_end_time - run_start_time)

        if not enable_cloud_cache:
            # cache model
            self._cache_model(output_model_config, output_model_id)

            # cache run
            self._cache_run(
                pass_name, pass_config, input_model_id, output_model_id, run_accel, run_start_time, run_end_time
            )

        # footprint model and run
        self.footprints[accelerator_spec].record(
            model_id=output_model_id,
            model_config=output_model_config.to_json() if output_model_config != FAILED_CONFIG else {"is_pruned": True},
            parent_model_id=input_model_id,
            from_pass=pass_name,
            pass_run_config=pass_config,
            start_time=run_start_time,
            end_time=run_end_time,
        )

        if enable_cloud_cache and upload_to_cloud and pass_run_locally:
            self.cloud_cache_helper.upload_model_to_cloud_cache(output_model_hash, output_model_config)

        return output_model_config, output_model_id, output_model_hash

    def save_olive_config(self, olive_config: dict):
        """Save the olive config to the output directory."""
        olive_config_path = self.cache.get_cache_dir() / "olive_config.json"
        olive_config_path.parent.mkdir(parents=True, exist_ok=True)
        with olive_config_path.open("w") as f:
            json.dump(olive_config, f, indent=4)
        logger.info("Saved Olive config to %s", olive_config_path)

    def _cache_evaluation(self, model_id: str, signal: MetricResult):
        """Cache the evaluation in the cache directory."""
        evaluation_json = {
            "model_id": model_id,
            "signal": signal.dict(),
        }
        evaluation_json_path = self.cache.get_evaluation_json_path(model_id)
        try:
            with evaluation_json_path.open("w") as f:
                json.dump(evaluation_json, f, indent=4)
        except Exception:
            logger.exception("Failed to cache evaluation")

    def _load_evaluation(self, model_id: str):
        """Load the evaluation from the cache directory."""
        evaluation_json_path = self.cache.get_evaluation_json_path(model_id)
        if evaluation_json_path.exists():
            try:
                with evaluation_json_path.open() as f:
                    evaluation_json = json.load(f)
                signal = evaluation_json["signal"]
                signal = MetricResult(**signal)
            except Exception:
                logger.exception("Failed to load evaluation")
                signal = None
            return signal
        else:
            return None

    def _evaluate_model(
        self,
        model_config: ModelConfig,
        model_id: str,
        evaluator_config: "OliveEvaluatorConfig",
        accelerator_spec: "AcceleratorSpec",
    ):
        """Evaluate a model."""
        logger.debug("Evaluating model ...")
        accelerator_suffix = f"-{accelerator_spec}" if accelerator_spec else ""
        if not model_id.endswith(accelerator_suffix):
            # append the suffix if the model is accelerator independent
            model_id_with_accelerator = f"{model_id}{accelerator_suffix}"
        else:
            model_id_with_accelerator = model_id

        # load evaluation from cache if it exists
        signal = self._load_evaluation(model_id_with_accelerator)
        if signal is not None:
            logger.debug("Loading evaluation from cache ...")
            # footprint evaluation
            self.footprints[accelerator_spec].record(
                model_id=model_id,
                metrics=FootprintNodeMetric(
                    value=signal,
                    if_goals_met=False,
                ),
            )
            return signal

        # evaluate model
        if self.target.system_type != SystemType.AzureML:
            model_config = self.cache.prepare_resources_for_local(model_config)
        signal = self.target.evaluate_model(model_config, evaluator_config, accelerator_spec)

        # cache evaluation
        self._cache_evaluation(model_id_with_accelerator, signal)

        # footprint evaluation
        self.footprints[accelerator_spec].record(
            model_id=model_id,
            metrics=FootprintNodeMetric(
                value=signal,
                if_goals_met=False,
            ),
        )
        return signal

    @staticmethod
    def _get_prefix_output_name(output_name: str, accelerator_spec: "AcceleratorSpec"):
        return f"{output_name}_{accelerator_spec}" if output_name else str(accelerator_spec)

    @contextmanager
    def _create_system(self, accelerator_spec):
        def create_system(config: "SystemConfig", accelerator_spec):
            assert config, "System config is not provided"
            if config.olive_managed_env:
                logger.debug(
                    "Creating olive_managed_env %s with EP %s", config.type, accelerator_spec.execution_provider
                )
                return create_managed_system_with_cache(config, accelerator_spec)
            else:
                logger.debug("create native OliveSystem %s", config.type)
                return config.create_system()

        if not self.target:
            logger.info("Creating target system ...")
            target_start_time = time.time()
            self.target = create_system(self.target_config, accelerator_spec)
            logger.info("Target system created in %f seconds", time.time() - target_start_time)
        if not self.host:
            host_accelerators = self.host_config.config.accelerators
            if host_accelerators and host_accelerators[0].execution_providers:
                host_accelerator_spec = AcceleratorSpec(
                    host_accelerators[0].device, host_accelerators[0].execution_providers[0]
                )
            else:
                host_accelerator_spec = None
            logger.info("Creating host system ...")
            host_start_time = time.time()
            self.host = create_system(self.host_config, host_accelerator_spec)
            logger.info("Host system created in %f seconds", time.time() - host_start_time)

        yield

        if self.target_config.olive_managed_env:
            # could we put it under cache system for reusing?
            logger.info("Removing target system ...")
            self.target.remove()
            self.target = None
        if self.host_config.olive_managed_env:
            logger.info("Removing host system ...")
            self.host.remove()
            self.host = None

        create_managed_system_with_cache.cache_clear()
