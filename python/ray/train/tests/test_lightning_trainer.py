import numpy as np
from ray.train.lightning import LightningConfigBuilder, LightningTrainer
import ray
from ray.air.util.data_batch_conversion import convert_batch_type_to_pandas
import pytest

from ray.train.tests._lightning_utils import (
    LinearModule,
    DoubleLinearModule,
    DummyDataModule,
)


@pytest.mark.parametrize("accelerator", ["cpu", "gpu"])
@pytest.mark.parametrize("datasource", ["dataloader", "datamodule"])
def test_trainer_with_native_dataloader(accelerator, datasource):
    num_epochs = 4
    batch_size = 8
    num_workers = 2
    dataset_size = 256

    config_builder = (
        LightningConfigBuilder()
        .module(LinearModule, input_dim=32, output_dim=4)
        .trainer(max_epochs=num_epochs, accelerator=accelerator)
    )

    datamodule = DummyDataModule(batch_size, dataset_size)
    train_loader = datamodule.train_dataloader()
    val_loader = datamodule.val_dataloader()

    if datasource == "dataloader":
        config_builder.fit(train_dataloaders=train_loader, val_dataloaders=val_loader)
    if datasource == "datamodule":
        config_builder.fit(datamodule=datamodule)

    scaling_config = ray.air.ScalingConfig(
        num_workers=num_workers, use_gpu=(accelerator == "gpu")
    )

    trainer = LightningTrainer(
        lightning_config=config_builder.build(), scaling_config=scaling_config
    )

    trainer.fit()


@pytest.mark.parametrize("accelerator", ["cpu", "gpu"])
def test_trainer_with_ray_data(accelerator):
    num_epochs = 4
    batch_size = 8
    num_workers = 2
    dataset_size = 256

    dataset = np.random.rand(dataset_size, 32).astype(np.float32)
    train_dataset = ray.data.from_numpy(dataset)
    val_dataset = ray.data.from_numpy(dataset)

    lightning_config = (
        LightningConfigBuilder()
        .module(LinearModule, input_dim=32, output_dim=4)
        .trainer(max_epochs=num_epochs, accelerator=accelerator)
        .build()
    )

    scaling_config = ray.air.ScalingConfig(
        num_workers=num_workers, use_gpu=(accelerator == "gpu")
    )

    trainer = LightningTrainer(
        lightning_config=lightning_config,
        scaling_config=scaling_config,
        datasets={"train": train_dataset, "val": val_dataset},
        datasets_iter_config={"batch_size": batch_size},
    )

    trainer.fit()


@pytest.mark.parametrize("accelerator", ["gpu"])
def test_trainer_with_categorical_ray_data(accelerator):
    num_epochs = 4
    batch_size = 8
    num_workers = 2
    dataset_size = 256

    input_1 = np.random.rand(dataset_size, 32).astype(np.float32)
    input_2 = np.random.rand(dataset_size, 32).astype(np.float32)
    pd = convert_batch_type_to_pandas({"input_1": input_1, "input_2": input_2})
    train_dataset = ray.data.from_pandas(pd)
    val_dataset = ray.data.from_pandas(pd)

    lightning_config = (
        LightningConfigBuilder()
        .module(
            DoubleLinearModule,
            input_dim_1=32,
            input_dim_2=32,
            output_dim=4,
        )
        .trainer(max_epochs=num_epochs, accelerator=accelerator)
        .build()
    )

    scaling_config = ray.air.ScalingConfig(
        num_workers=num_workers, use_gpu=(accelerator == "gpu")
    )

    trainer = LightningTrainer(
        lightning_config=lightning_config,
        scaling_config=scaling_config,
        datasets={"train": train_dataset, "val": val_dataset},
        datasets_iter_config={"batch_size": batch_size},
    )

    trainer.fit()


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main(["-v", "-x", __file__]))
