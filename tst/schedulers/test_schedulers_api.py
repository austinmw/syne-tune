import tempfile
from pathlib import Path

import dill
import pytest
from ray.tune.schedulers import AsyncHyperBandScheduler
from ray.tune.suggest.skopt import SkOptSearch

from examples.launch_height_standalone_scheduler import SimpleScheduler
from syne_tune.backend.trial_status import Trial
from syne_tune.optimizer.scheduler import SchedulerDecision
from syne_tune.optimizer.schedulers.fifo import FIFOScheduler
from syne_tune.optimizer.schedulers.hyperband import HyperbandScheduler
from syne_tune.optimizer.schedulers.multiobjective.moasha import MOASHA
from syne_tune.optimizer.schedulers.pbt import PopulationBasedTraining
from syne_tune.optimizer.schedulers.ray_scheduler import RayTuneScheduler
from syne_tune.optimizer.schedulers.synchronous.hyperband_impl import \
    SynchronousGeometricHyperbandScheduler
import syne_tune.search_space as sp


config_space = {
    "steps": 100,
    "x": sp.randint(0, 20),
    "y": sp.uniform(0, 1),
    "z": sp.choice(["a", "b", "c"]),
}
metric1 = "objective1"
metric2 = "objective2"
resource_attr = 'step'
max_t = 10
sync_batch_size = 4


def make_ray_skopt():
    ray_searcher = SkOptSearch()
    ray_searcher.set_search_properties(
        mode='min', metric=metric1,
        config=RayTuneScheduler.convert_config_space(config_space)
    )
    return ray_searcher


@pytest.mark.parametrize("scheduler", [
    FIFOScheduler(config_space, searcher='random', metric=metric1),
    FIFOScheduler(config_space, searcher='bayesopt', metric=metric1),
    HyperbandScheduler(config_space, searcher='random', resource_attr=resource_attr, max_t=max_t, metric=metric1),
    HyperbandScheduler(config_space, searcher='bayesopt', resource_attr=resource_attr, max_t=max_t, metric=metric1),
    HyperbandScheduler(
        config_space, searcher='random', type='pasha', max_t=max_t, resource_attr=resource_attr, metric=metric1
    ),
    MOASHA(config_space=config_space, time_attr=resource_attr, metrics=[metric1, metric2]),
    PopulationBasedTraining(config_space=config_space, metric=metric1, resource_attr=resource_attr, max_t=max_t),
    RayTuneScheduler(
        config_space=config_space,
        ray_scheduler=AsyncHyperBandScheduler(max_t=max_t, time_attr=resource_attr, mode='min', metric=metric1)
    ),
    RayTuneScheduler(
        config_space=config_space,
        ray_scheduler=AsyncHyperBandScheduler(max_t=max_t, time_attr=resource_attr, mode='min', metric=metric1),
        ray_searcher=make_ray_skopt(),
    ),
    SimpleScheduler(config_space=config_space, metric=metric1),
])
def test_async_schedulers_api(scheduler):
    trial_ids = range(4)

    if isinstance(scheduler, MOASHA):
        assert scheduler.metric_names() == [metric1, metric2]
    else:
        assert scheduler.metric_names() == [metric1]
    assert scheduler.metric_mode() == "min"

    # checks suggestions are properly formatted
    trials = []
    for i in trial_ids:
        suggestion = scheduler.suggest(i)
        assert all(x in suggestion.config.keys() for x in config_space.keys()), \
            "suggestion configuration should contains all keys of configspace."
        trials.append(Trial(trial_id=i, config=suggestion.config, creation_time=None))

    for trial in trials:
        scheduler.on_trial_add(trial=trial)

    # checks results can be transmitted with appropriate scheduling decisions
    make_metric = lambda t, x: {resource_attr: t, metric1: x, metric2: -x}
    for i, trial in enumerate(trials):
        for t in range(1, max_t + 1):
            decision = scheduler.on_trial_result(trial, make_metric(t, i))
            assert decision in [SchedulerDecision.CONTINUE, SchedulerDecision.PAUSE, SchedulerDecision.STOP]

    scheduler.on_trial_error(trials[0])
    for i, trial in enumerate(trials):
        scheduler.on_trial_complete(trial, make_metric(max_t, i))

    # checks serialization
    with tempfile.TemporaryDirectory() as local_path:
        with open(Path(local_path) / "scheduler.dill", "wb") as f:
            dill.dump(scheduler, f)
        with open(Path(local_path) / "scheduler.dill", "rb") as f:
            dill.load(f)

@pytest.mark.parametrize("scheduler", [
    SynchronousGeometricHyperbandScheduler(
        config_space=config_space,
        max_resource_level=max_t,
        brackets=3,
        resource_attr=resource_attr,
        batch_size=sync_batch_size,
        metric=metric1,
        max_resource_attr='steps'),
])
def test_sync_schedulers_api(scheduler):
    assert scheduler.metric_names() == [metric1]
    assert scheduler.metric_mode() == "min"

    # Synchronous schedulers expect switching between suggest and collect
    # phase. In the suggest phase, `scheduler.suggest` is called `sync_batch_size`
    # times, in the collect phase, `scheduler.on_trial_result` is expected for these
    # trials (or `scheduler.on_trial_error`).
    all_trials = dict()
    num_iterations = 3
    next_trial_id = 0
    for iter in range(num_iterations):
        # suggest phase
        trials_this_batch = []
        for trial_id in range(next_trial_id, next_trial_id + sync_batch_size):
            suggestion = scheduler.suggest(trial_id)
            assert all(x in suggestion.config.keys() for x in config_space.keys()), \
                "suggestion configuration should contains all keys of configspace."
            if suggestion.spawn_new_trial_id:
                trial = Trial(
                    trial_id=trial_id, config=suggestion.config,
                    creation_time=None)
                scheduler.on_trial_add(trial=trial)
                all_trials[str(trial_id)] = trial
            else:
                # Trial is resumed
                resume_trial_id = str(suggestion.checkpoint_trial_id)
                trial = all_trials[resume_trial_id]
            trials_this_batch.append(trial)
        next_trial_id += sync_batch_size
        # collect phase
        # checks results can be transmitted with appropriate scheduling decisions
        make_metric = lambda t, x: {resource_attr: t, metric1: x, metric2: -x}
        is_running = {
            str(trial.trial_id): True for trial in trials_this_batch}
        for t in range(1, max_t + 1):
            for i, trial in enumerate(trials_this_batch):
                trial_id = str(trial.trial_id)
                if is_running[trial_id]:
                    if t == 1 and i == sync_batch_size - 1:
                        # This trial fails
                        scheduler.on_trial_error(trial)
                        is_running[trial_id] = False
                    else:
                        decision = scheduler.on_trial_result(
                            trial, make_metric(t, i))
                        assert decision in [SchedulerDecision.CONTINUE,
                                            SchedulerDecision.PAUSE,
                                            SchedulerDecision.STOP]
                        if decision != SchedulerDecision.CONTINUE:
                            is_running[trial_id] = False

    # checks serialization
    with tempfile.TemporaryDirectory() as local_path:
        with open(Path(local_path) / "scheduler.dill", "wb") as f:
            dill.dump(scheduler, f)
        with open(Path(local_path) / "scheduler.dill", "rb") as f:
            dill.load(f)