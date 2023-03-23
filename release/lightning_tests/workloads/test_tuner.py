import os
import time
import json
from pytorch_lightning.loggers.csv_logs import CSVLogger

import ray
import ray.tune as tune
from ray.air.config import CheckpointConfig, ScalingConfig
from ray.train.lightning import LightningTrainer, LightningConfigBuilder
from ray.tune.schedulers import PopulationBasedTraining

from lightning_test_utils import MNISTClassifier, MNISTDataModule

if __name__ == "__main__":
    ray.init(address="auto", runtime_env={"working_dir": os.path.dirname(__file__)})

    start = time.time()

    lightning_config = (
        LightningConfigBuilder()
        .module(
            MNISTClassifier,
            feature_dim=tune.choice([64, 128]),
            lr=tune.grid_search([0.01, 0.001]),
        )
        .trainer(
            max_epochs=5,
            accelerator="gpu",
            logger=CSVLogger("logs", name="my_exp_name"),
        )
        .fit_params(datamodule=MNISTDataModule(batch_size=200))
        .checkpointing(monitor="ptl/val_accuracy", mode="max")
        .build()
    )

    scaling_config = ScalingConfig(
        num_workers=3, use_gpu=True, resources_per_worker={"CPU": 1, "GPU": 1}
    )

    lightning_trainer = LightningTrainer(
        scaling_config=scaling_config,
    )

    mutation_config = (
        LightningConfigBuilder()
        .module(
            feature_dim=[64, 128],
            lr=tune.choice([0.01, 0.001]),
        )
        .build()
    )

    tuner = tune.Tuner(
        lightning_trainer,
        param_space={"lightning_config": lightning_config},
        run_config=ray.air.RunConfig(
            name="release-tuner-test",
            verbose=2,
            checkpoint_config=CheckpointConfig(
                num_to_keep=2,
                checkpoint_score_attribute="ptl/val_accuracy",
                checkpoint_score_order="max",
            ),
        ),
        tune_config=tune.TuneConfig(
            metric="ptl/val_accuracy",
            mode="max",
            num_samples=2,
            scheduler=PopulationBasedTraining(
                time_attr="training_iteration",
                hyperparam_mutations={"lightning_config": mutation_config},
                perturbation_interval=1,
            ),
        ),
    )
    results = tuner.fit()
    best_result = results.get_best_result(metric="ptl/val_accuracy", mode="max")
    best_result

    taken = time.time() - start

    # Report experiment results
    result = {
        "time_taken": taken,
        "ptl/val_accuracy": best_result.metrics["ptl/val_accuracy"],
    }

    test_output_json = os.environ.get(
        "TEST_OUTPUT_JSON",
        "/tmp/lightning_gpu_tuner_test.json",
    )
    with open(test_output_json, "wt") as f:
        json.dump(result, f)

    print("Test Successful!")
