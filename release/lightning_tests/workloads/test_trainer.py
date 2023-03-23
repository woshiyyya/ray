import os
import time
import json


from pytorch_lightning.loggers.csv_logs import CSVLogger
from ray.air.config import ScalingConfig
from ray.train.lightning import LightningTrainer, LightningConfigBuilder

from lightning_utils import MNISTClassifier, MNISTDataModule


if __name__ == "__main__":
    start = time.time()

    lightning_config = (
        LightningConfigBuilder()
        .module(MNISTClassifier, feature_dim=128, lr=0.001)
        .trainer(
            max_epochs=3,
            accelerator="gpu",
            logger=CSVLogger("logs", name="my_exp_name"),
        )
        .fit_params(datamodule=MNISTDataModule(batch_size=128))
        .checkpointing(monitor="ptl/val_accuracy", mode="max", save_last=True)
        .build()
    )

    scaling_config = ScalingConfig(
        num_workers=3, use_gpu=True, resources_per_worker={"CPU": 1, "GPU": 1}
    )

    trainer = LightningTrainer(
        lightning_config=lightning_config,
        scaling_config=scaling_config,
    )

    result = trainer.fit()

    taken = time.time() - start
    result = {
        "time_taken": taken,
        "ptl/val_accuracy": result.metrics["ptl/val_accuracy"],
    }
    test_output_json = os.environ.get(
        "TEST_OUTPUT_JSON", "/tmp/lightning_trainer_test.json"
    )
    with open(test_output_json, "wt") as f:
        json.dump(result, f)

    print("Test Successful!")
